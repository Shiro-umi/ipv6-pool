import asyncio
import logging
import random
import socket
from typing import Tuple

from ipv6_proxy_pool.core.config import ProxyConfig
from ipv6_proxy_pool.core.stats import ConnectionStats
from ipv6_proxy_pool.core.pool import IPv6AddressPool, IPv6ConnectivityCache
from ipv6_proxy_pool.utils.rate_limit import RateLimiter

logger = logging.getLogger('ipv6_proxy_pool.connector')

class OutboundConnector:
    """
    出站连接管理器

    优先尝试IPv6连接，失败则回退到IPv4
    使用LRU缓存避免重复测试连通性
    """

    def __init__(self, ip_pool: IPv6AddressPool, config: ProxyConfig, stats: ConnectionStats):
        self.ip_pool = ip_pool
        self.config = config
        self.stats = stats
        self.rate_limiter = RateLimiter(config.rate_limit)
        self._ipv6_cache = IPv6ConnectivityCache(maxsize=1024, ttl=300)  # 5分钟缓存

    async def connect(self, host: str, port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """建立出站连接，支持智能双栈 A/AAAA 选择"""
        await self.rate_limiter.wait()

        # 解析目标地址（支持IPv4和IPv6）
        try:
            addr_info = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    host, port,
                    family=socket.AF_UNSPEC, # 获取所有可用记录
                    type=socket.SOCK_STREAM
                ),
                timeout=5.0
            )
        except Exception as e:
            raise ConnectionError(f"DNS解析失败 {host}: {e}")

        if not addr_info:
            raise ConnectionError(f"无法解析地址: {host}")

        # 根据配置偏好排序 addr_info
        # 如果 prefer_ipv6_target 为 True，将 IPv6 放在前面
        if self.config.prefer_ipv6_target:
            v6_targets = [a for a in addr_info if a[0] == socket.AF_INET6]
            v4_targets = [a for a in addr_info if a[0] == socket.AF_INET]
            addr_info = v6_targets + v4_targets
        else:
            # 默认：如果 IPv6 连接缓存成功过，则优先 IPv6
            cached = self._ipv6_cache.get(host)
            if cached:
                v6_targets = [a for a in addr_info if a[0] == socket.AF_INET6]
                v4_targets = [a for a in addr_info if a[0] == socket.AF_INET]
                addr_info = v6_targets + v4_targets

        # 尝试连接列表中的目标
        last_err = None
        for family, _, _, _, target_addr in addr_info:
            try:
                if family == socket.AF_INET6:
                    return await self._connect_ipv6_single(host, port, target_addr)
                else:
                    return await self._connect_ipv4_single(host, port, target_addr)
            except Exception as e:
                last_err = e
                continue
        
        raise ConnectionError(f"所有地址连接均失败: {last_err}")

    async def _connect_ipv6_single(self, host: str, port: int, target_addr) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """单次IPv6连接尝试"""
        local_v6_addr = await self.ip_pool.acquire()
        if not local_v6_addr:
            raise ConnectionError("IPv6池已耗尽")

        sock = None
        try:
            sock = self._create_ipv6_socket(local_v6_addr)
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(sock, target_addr),
                timeout=self.config.connection_timeout
            )
            
            # 连接成功，标记缓存为 True（有效期5分钟）
            self._ipv6_cache.set(host, True)
            reader, writer = await asyncio.open_connection(sock=sock)
            logger.debug(f"IPv6连接成功: [{local_v6_addr}] -> {host}:{port}")
            return reader, writer, local_v6_addr
        except (socket.gaierror, socket.herror, socket.timeout, asyncio.TimeoutError) as e:
            if sock: sock.close()
            await self.ip_pool.release(local_v6_addr)
            # 针对超时或解析错误，标记缓存为 False（仅缓存30秒，避免长期封杀）
            self._ipv6_cache.ttl = 30
            self._ipv6_cache.set(host, False)
            self._ipv6_cache.ttl = 300 # 恢复默认
            raise ConnectionError(f"IPv6连接超时或DNS错误: {e}")
        except Exception as e:
            if sock: sock.close()
            await self.ip_pool.release(local_v6_addr)
            # 其它错误（如 Connect Refused）不记录缓存，允许下次重试
            logger.debug(f"IPv6连接尝试失败 [{local_v6_addr}] -> {host}:{port}: {e}")
            raise

    async def _connect_ipv4_single(self, host: str, port: int, target_addr) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """单次IPv4连接尝试"""
        sock = None
        try:
            sock = self._create_ipv4_socket()
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(sock, target_addr),
                timeout=self.config.connection_timeout
            )
            reader, writer = await asyncio.open_connection(sock=sock)
            logger.debug(f"IPv4连接成功: [0.0.0.0] -> {host}:{port}")
            return reader, writer, "0.0.0.0"
        except Exception as e:
            if sock: sock.close()
            raise

    def _apply_fingerprint(self, sock: socket.socket, family: int):
        """应用TCP/IP指纹混淆"""
        if not self.config.enable_fingerprint:
            return

        try:
            # 1. 随机化 TTL / Hop Limit
            ttl = random.randint(self.config.min_ttl, self.config.max_ttl)
            if family == socket.AF_INET:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            else:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_UNICAST_HOPS, ttl)

            # 2. 精准控制 TCP 窗口大小
            # 目标窗口大小
            target_win = random.randint(self.config.window_size_min, self.config.window_size_max)
            
            # 设置接收缓冲区。注意：Linux会翻倍SO_RCVBUF，且受tcp_adv_win_scale影响
            # 我们设置一个足够大的值，确保缓冲区不会成为瓶颈
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, target_win * 2)
            
            # 使用 TCP_WINDOW_CLAMP 强制锁定通告窗口大小
            # 这能直接控制外发 SYN 报文中的 Window 字段
            if hasattr(socket, 'TCP_WINDOW_CLAMP'):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_WINDOW_CLAMP, target_win)

            # 3. 随机化 MSS (Maximum Segment Size)
            # 不同操作系统的初始 MSS 不同，这对于指纹识别非常重要
            if hasattr(socket, 'TCP_MAXSEG'):
                # 基础 MSS：IPv6 通常为 1440, IPv4 为 1460
                base_mss = 1440 if family == socket.AF_INET6 else 1460
                # 模拟略小的 MSS (如某些隧道或特定系统行为)
                if random.random() > 0.7:
                    custom_mss = base_mss - random.choice([0, 12, 20, 40])
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, custom_mss)
                    except OSError:
                        pass

            # 4. IPv6 特有：随机化 Flow Label
            if family == socket.AF_INET6 and self.config.randomize_flow_label:
                # Flow Label 是 20 位 (0 to 0xFFFFF)
                flow_label = random.randint(1, 0xFFFFF)
                # IPv6 Flow Info 结构通常包含: flow_label (20 bits), traffic_class (8 bits)
                # 在 Linux setsockopt 中，可以直接设置
                try:
                    # 注意：某些系统可能需要特定格式或权限
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_FLOWINFO, flow_label)
                except OSError:
                    pass # 某些内核版本可能不支持直接修改

        except Exception as e:
            logger.debug(f"应用指纹混淆失败: {e}")

    def _create_ipv6_socket(self, bind_addr: str) -> socket.socket:
        """创建IPv6 socket并绑定到指定地址"""
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # 启用双栈支持（如果可用）
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except:
            pass

        # 应用指纹
        self._apply_fingerprint(sock, socket.AF_INET6)

        try:
            sock.bind((bind_addr, 0, 0, 0))
        except OSError as e:
            logger.debug(f"绑定 {bind_addr} 失败: {e}，回退到默认地址")
            try:
                sock.bind(("::", 0, 0, 0))
            except OSError:
                sock.close()
                raise

        return sock

    def _create_ipv4_socket(self) -> socket.socket:
        """创建IPv4 socket"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # 应用指纹
        self._apply_fingerprint(sock, socket.AF_INET)

        try:
            sock.bind(("0.0.0.0", 0))
        except OSError:
            sock.close()
            raise

        return sock
