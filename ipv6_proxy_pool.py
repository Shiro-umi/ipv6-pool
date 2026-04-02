#!/usr/bin/env python3
"""
IPv6出口代理池 - 高性能版

功能特性：
- IPv6地址池动态管理
- HTTP/HTTPS代理支持
- 连接池复用
- 速率限制
- 统计监控
- 多进程支持

作者: Claude Code
版本: 1.0.0
"""

import asyncio
import argparse
import ipaddress
import json
import logging
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple, Any, Callable
from urllib.parse import urlparse


# ============== 配置和日志 ==============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ipv6_proxy_pool')


@dataclass
class ProxyConfig:
    """代理配置"""
    host: str = '0.0.0.0'
    port: int = 8899
    pool_size: int = 1000
    interface: Optional[str] = None
    max_connections_per_ip: int = 10
    connection_timeout: float = 30.0
    read_timeout: float = 60.0
    rate_limit: int = 0
    enable_stats: bool = True
    prefer_ipv6_target: bool = False
    buffer_size: int = 65536
    # 访问控制
    allow_lan: bool = True
    allowed_ips: List[str] = field(default_factory=lambda: ['127.0.0.1', '192.168.0.0/16', '10.0.0.0/8', '172.16.0.0/12'])
    # 指纹混淆配置
    enable_fingerprint: bool = True
    min_ttl: int = 64
    max_ttl: int = 128
    randomize_flow_label: bool = True
    window_size_min: int = 65536
    window_size_max: int = 131072


# ============== 统计监控 ==============

@dataclass
class ConnectionStats:
    """连接统计"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    bytes_transferred: int = 0
    active_connections: int = 0
    peak_connections: int = 0
    ip_usage: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    target_stats: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    start_time: datetime = field(default_factory=datetime.now)

    def record_request(self, success: bool, ip: str, target: str, bytes_count: int = 0):
        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
        self.bytes_transferred += bytes_count
        self.ip_usage[ip] += 1
        self.target_stats[target]['requests'] += 1
        if success:
            self.target_stats[target]['success'] += 1
        else:
            self.target_stats[target]['failed'] += 1

    def connection_started(self):
        self.active_connections += 1
        self.peak_connections = max(self.peak_connections, self.active_connections)

    def connection_ended(self):
        self.active_connections = max(0, self.active_connections - 1)

    def to_dict(self) -> dict:
        uptime = datetime.now() - self.start_time
        return {
            'uptime_seconds': uptime.total_seconds(),
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'failed_requests': self.failed_requests,
            'success_rate': f"{(self.successful_requests / max(self.total_requests, 1) * 100):.2f}%",
            'bytes_transferred': self.bytes_transferred,
            'bytes_transferred_mb': f"{self.bytes_transferred / (1024*1024):.2f}MB",
            'active_connections': self.active_connections,
            'peak_connections': self.peak_connections,
            'ip_pool_usage': len(self.ip_usage),
            'top_targets': dict(sorted(
                self.target_stats.items(),
                key=lambda x: x[1]['requests'],
                reverse=True
            )[:5])
        }


# ============== IPv6地址池 ==============

def _get_global_ipv6_prefix(interface: str) -> Optional[ipaddress.IPv6Network]:
    """自动探测网卡上可路由的全球单播IPv6前缀及其掩码长度"""
    try:
        result = subprocess.run(
            ['ip', '-6', 'addr', 'show', 'dev', interface],
            capture_output=True, text=True, check=False
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if 'inet6' in line and 'scope global' in line:
                parts = line.split()
                for part in parts:
                    if '/' in part:
                        try:
                            iface = ipaddress.ip_interface(part)
                            # 严格检查：必须是GUA，排除ULA和链路本地
                            if iface.ip.is_global and not iface.ip.is_private:
                                logger.info(f"检测到GUA前缀: {iface.network} 来自 {part}")
                                return iface.network
                        except ValueError:
                            continue
    except Exception as e:
        logger.warning(f"探测IPv6前缀失败: {e}")
    return None


def _get_default_ipv6_interface() -> Optional[str]:
    """自动检测具有GUA地址的默认网络接口

    优先顺序：
    1. 检查默认路由的出接口
    2. 遍历常见网卡名称，查找有GUA地址的接口
    """
    try:
        # 方法1：从默认路由获取出接口
        result = subprocess.run(
            ['ip', '-6', 'route', 'show', 'default'],
            capture_output=True, text=True, check=False
        )
        for line in result.stdout.splitlines():
            # 格式: default via fe80:: dev eth0 metric 1024
            if 'dev' in line:
                parts = line.split()
                for i, part in enumerate(parts):
                    if part == 'dev' and i + 1 < len(parts):
                        iface = parts[i + 1]
                        # 验证该接口是否有GUA地址
                        if _get_global_ipv6_prefix(iface):
                            logger.info(f"从默认路由检测到接口: {iface}")
                            return iface

        # 方法2：遍历常见网卡名称
        common_interfaces = ['eth0', 'ens3', 'ens160', 'enp0s3', 'enp1s0',
                            'en0', 'eno1', 'enp3s0', 'wlan0', 'wlp2s0']

        for iface in common_interfaces:
            if _get_global_ipv6_prefix(iface):
                logger.info(f"从常见接口列表检测到: {iface}")
                return iface

        # 方法3：遍历所有接口查找有GUA的
        result = subprocess.run(
            ['ip', '-6', 'addr', 'show'],
            capture_output=True, text=True, check=False
        )
        current_iface = None
        for line in result.stdout.splitlines():
            # 接口行格式: 2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> ...
            if re.match(r'^\\d+:', line):
                match = re.search(r'^\\d+:\\s+(\\w+):', line)
                if match:
                    current_iface = match.group(1)
            # 检查是否有GUA地址
            elif 'inet6' in line and 'scope global' in line:
                parts = line.split()
                for part in parts:
                    if '/' in part:
                        try:
                            ip_iface = ipaddress.ip_interface(part)
                            if ip_iface.ip.is_global and not ip_iface.ip.is_private:
                                if current_iface and current_iface != 'lo':
                                    logger.info(f"遍历检测到接口: {current_iface}")
                                    return current_iface
                        except ValueError:
                            continue

    except Exception as e:
        logger.warning(f"自动检测默认接口失败: {e}")

    return None


class IPv6AddressPool:
    """
    IPv6地址池管理器 - 基于真实可路由前缀
    """

    def __init__(self, pool_size: int = 1000, interface: Optional[str] = None):
        self.pool_size = pool_size
        self._available: List[str] = []
        self._in_use: Set[str] = set()
        self._all_generated: Set[str] = set()
        self._lock = asyncio.Lock()

        # 1. 探测接口
        if interface is None or interface == 'lo':
            self.interface = _get_default_ipv6_interface() or 'lo'
        else:
            self.interface = interface

        # 2. 探测前缀和掩码
        network = _get_global_ipv6_prefix(self.interface)
        if network:
            self.network = network
            logger.info(f"使用探测到的网络: {self.network}")
        else:
            self.network = ipaddress.IPv6Network('fd00::/64')
            logger.warning(f"未在 {self.interface} 探测到公网前缀，回退到私有网络: {self.network}")

        # 3. 启动前强制清理（防止残留）
        self._pre_startup_cleanup()

        # 4. 生成并安装初始池
        self._generate_pool()
        self._install_pool_to_interface()

    def _pre_startup_cleanup(self):
        """启动前清理所有符合前缀的旧IP"""
        try:
            logger.info(f"正在清理接口 {self.interface} 上的旧IPv6地址...")
            result = subprocess.run(
                ['ip', '-6', 'addr', 'show', 'dev', self.interface],
                capture_output=True, text=True, check=False
            )
            # 提取前缀部分进行匹配
            base_prefix = str(self.network.network_address).split('::')[0]
            count = 0
            for line in result.stdout.splitlines():
                if 'inet6' in line and base_prefix in line:
                    parts = line.strip().split()
                    for part in parts:
                        if '/' in part and part.startswith(base_prefix):
                            subprocess.run(['ip', '-6', 'addr', 'del', part, 'dev', self.interface], check=False)
                            count += 1
            if count > 0:
                logger.info(f"已清理 {count} 个残留地址")
        except Exception as e:
            logger.debug(f"清理旧地址失败: {e}")

    def _generate_pool(self):
        """生成IPv6地址池"""
        for _ in range(self.pool_size):
            ip = self._generate_ip()
            self._available.append(ip)

    def _generate_ip(self) -> str:
        """基于探测到的掩码长度生成一个新的IPv6地址"""
        prefix_len = self.network.prefixlen
        host_bits = 128 - prefix_len
        host = random.getrandbits(host_bits)
        if host == 0: host = 1
        
        ip_int = (int(self.network.network_address) & ~((1 << host_bits) - 1)) | (host & ((1 << host_bits) - 1))
        ip_str = str(ipaddress.IPv6Address(ip_int))
        self._all_generated.add(ip_str)
        return ip_str

    def _install_pool_to_interface(self):
        """将可用池中的地址批量添加到网卡"""
        for ip in list(self._available):
            if not self._add_ip_to_interface_sync(ip):
                self._available.remove(ip)
        logger.info(f"地址池就绪：{len(self._available)}/{self.pool_size} 个地址已安装")

    def _add_ip_to_interface_sync(self, ip: str) -> bool:
        try:
            addr = f"{ip}/{self.network.prefixlen}"
            result = subprocess.run(
                ['ip', '-6', 'addr', 'add', addr, 'dev', self.interface, 'nodad'],
                capture_output=True, check=False
            )
            return result.returncode == 0 or b'File exists' in result.stderr
        except Exception:
            return False

    def _remove_ip_from_interface_sync(self, ip: str) -> bool:
        try:
            addr = f"{ip}/{self.network.prefixlen}"
            subprocess.run(['ip', '-6', 'addr', 'del', addr, 'dev', self.interface], capture_output=True, check=False)
            return True
        except Exception:
            return False

    async def acquire(self) -> Optional[str]:
        """获取一个可用的IPv6地址（无需再次调用 subprocess，已预装）"""
        async with self._lock:
            if self._available:
                ip = self._available.pop(0)
                self._in_use.add(ip)
                return ip
            return None

    async def release(self, ip: Optional[str]):
        """释放并汰换地址（即用即弃）"""
        if not ip or ip in ('::', '0.0.0.0'): return

        loop = asyncio.get_running_loop()
        async with self._lock:
            if ip in self._in_use:
                self._in_use.discard(ip)
                # 异步执行耗时的网卡操作
                await loop.run_in_executor(None, self._remove_ip_from_interface_sync, ip)
                
                # 汰换：生成并安装新IP
                new_ip = self._generate_ip()
                if await loop.run_in_executor(None, self._add_ip_to_interface_sync, new_ip):
                    self._available.append(new_ip)
                    logger.debug(f"IP汰换: {ip} -> {new_ip}")

    async def cleanup(self):
        """清理所有已安装到网卡的IP (包括曾生成的所有IP)"""
        loop = asyncio.get_running_loop()
        async with self._lock:
            # 使用 _all_generated 确保即使地址不在可用/在用池中也能被清理
            total_to_remove = list(self._all_generated)
            if not total_to_remove:
                return

            logger.info(f"正在从接口 {self.interface} 清理 {len(total_to_remove)} 个IPv6地址...")
            removed = 0
            for ip in total_to_remove:
                if await loop.run_in_executor(None, self._remove_ip_from_interface_sync, ip):
                    removed += 1

            self._available.clear()
            self._in_use.clear()
            self._all_generated.clear()
            logger.info(f"清理完成: 成功删除 {removed}/{len(total_to_remove)} 个地址")

    def get_stats(self) -> dict:
        """获取池统计"""
        with self._lock:
            return {
                'total': self.pool_size,
                'available': len(self._available),
                'in_use': len(self._in_use),
                'utilization': f"{(len(self._in_use) / self.pool_size * 100):.2f}%"
            }


# ============== 速率限制器 ==============

class RateLimiter:
    """令牌桶速率限制器"""

    def __init__(self, rate: int = 0):
        self.rate = rate  # 每秒请求数，0表示不限速
        self.tokens = rate if rate > 0 else float('inf')
        self.last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """获取一个令牌"""
        if self.rate <= 0:
            return True

        async with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

    async def wait(self):
        """等待获取令牌"""
        if self.rate <= 0:
            return

        while not await self.acquire():
            await asyncio.sleep(0.001)


# ============== 连接管理器 ==============

class IPv6ConnectivityCache:
    """IPv6连通性缓存 - 带过期时间的LRU缓存"""

    def __init__(self, maxsize: int = 1024, ttl: int = 300):
        self.maxsize = maxsize
        self.ttl = ttl  # 缓存过期时间（秒）
        self._cache: Dict[str, Tuple[bool, float]] = {}

    def get(self, host: str) -> Optional[bool]:
        """获取缓存的IPv6支持状态，返回 None 表示无缓存"""
        if host in self._cache:
            result, timestamp = self._cache[host]
            if time.time() - timestamp < self.ttl:
                return result
            else:
                # 过期，删除
                del self._cache[host]
        return None

    def set(self, host: str, supports_ipv6: bool):
        """设置缓存"""
        # 简单LRU：如果满了，删除最旧的
        if len(self._cache) >= self.maxsize:
            oldest = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[host] = (supports_ipv6, time.time())


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
            
            self._ipv6_cache.set(host, True)
            reader, writer = await asyncio.open_connection(sock=sock)
            logger.debug(f"IPv6连接成功: [{local_v6_addr}] -> {host}:{port}")
            return reader, writer, local_v6_addr
        except Exception as e:
            if sock: sock.close()
            await self.ip_pool.release(local_v6_addr)
            self._ipv6_cache.set(host, False)
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


# ============== HTTP代理协议 ==============

class HTTPProxyProtocol(asyncio.Protocol):
    """HTTP/HTTPS代理协议处理器"""

    def __init__(self, connector: OutboundConnector, stats: ConnectionStats, config: ProxyConfig):
        self.connector = connector
        self.stats = stats
        self.config = config

        # 连接相关
        self.transport: Optional[asyncio.Transport] = None
        self.peername: Optional[Tuple] = None
        self.buffer = b''
        self.state = 'initial'  # initial, handshaking, relaying, closed

        # 目标连接
        self.target_host: Optional[str] = None
        self.target_port: int = 0
        self.outbound_writer: Optional[asyncio.StreamWriter] = None
        self.used_ip: Optional[str] = None

        # 统计
        self.bytes_sent = 0
        self.bytes_received = 0
        self.start_time = time.time()

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.peername = transport.get_extra_info('peername')
        
        # 访问控制校验
        if not self._is_client_allowed():
            logger.warning(f"拒绝未授权访问: {self.peername}")
            self.transport.close()
            return

        self.stats.connection_started()
        logger.debug(f"客户端连接: {self.peername}")

    def _is_client_allowed(self) -> bool:
        """检查客户端IP是否在允许范围内"""
        if not self.peername: return False
        client_ip = ipaddress.ip_address(self.peername[0])
        
        # 本地回环始终允许
        if client_ip.is_loopback: return True
        
        # 检查允许列表
        for allowed in self.config.allowed_ips:
            try:
                if client_ip in ipaddress.ip_network(allowed):
                    return True
            except ValueError:
                continue
        
        # 检查局域网
        if self.config.allow_lan and client_ip.is_private:
            return True
            
        return False

    def data_received(self, data: bytes):
        """接收客户端数据"""
        if self.state in ('relaying', 'handshaking'):
            # 隧道模式或正在建立连接，直接转发（如relay）或忽略（如handshaking）
            if self.state == 'relaying' and self.outbound_writer:
                self.outbound_writer.write(data)
                self.bytes_sent += len(data)
            return

        # 收集HTTP请求头
        self.buffer += data

        # 检查是否收到完整请求头
        if b'\r\n\r\n' not in self.buffer:
            if len(self.buffer) > 65536:  # 最大头大小限制
                self._send_error(413, "Request Entity Too Large")
            return

        # 解析并处理请求
        try:
            headers_end = self.buffer.index(b'\r\n\r\n') + 4
            header_data = self.buffer[:headers_end]
            body_data = self.buffer[headers_end:]
            self.state = 'handshaking'
            self._handle_request(header_data, body_data)
        except Exception as e:
            if self.state == 'handshaking':
                self.state = 'initial'
            logger.error(f"处理请求失败: {e}")
            self._send_error(400, f"Bad Request: {e}")

    def _handle_request(self, header_data: bytes, body_data: bytes):
        """处理HTTP请求"""
        # TODO: 下一个任务详细讨论：保持原始 Header 顺序及其完整性保护
        # 针对 Akamai/Cloudflare 的指纹识别优化，应使用原始 buffer 进行切片转发
        try:
            lines = header_data.split(b'\r\n')
            request_line = lines[0].decode('utf-8', errors='ignore')
            parts = request_line.split(' ')

            if len(parts) < 3:
                raise ValueError("Invalid request line")

            method, url, version = parts[0], parts[1], parts[2]

            logger.debug(f"收到请求: {method} {url}")

            if method == 'CONNECT':
                # HTTPS代理
                host, port = self._parse_connect_url(url)
                self.target_host = host
                self.target_port = port
                asyncio.create_task(self._handle_connect())
            else:
                # HTTP代理
                parsed = urlparse(url)
                self.target_host = parsed.hostname or url.split('/')[0].split(':')[0]
                self.target_port = parsed.port or 80
                asyncio.create_task(self._handle_http_request(method, parsed, header_data, body_data))

        except Exception as e:
            logger.error(f"解析请求失败: {e}")
            self._send_error(400, "Bad Request")

    def _parse_connect_url(self, url: str) -> Tuple[str, int]:
        """解析CONNECT URL"""
        if ':' in url:
            host, port_str = url.rsplit(':', 1)
            host = host.strip('[]')
            return host, int(port_str)
        return url, 443

    async def _handle_connect(self):
        """处理CONNECT方法（HTTPS隧道）"""
        try:
            reader, writer, used_ip = await self.connector.connect(
                self.target_host, self.target_port
            )
            self.outbound_writer = writer
            self.used_ip = used_ip

            # 发送成功响应
            self.transport.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.state = 'relaying'

            # 转发剩余数据
            if self.buffer:
                remainder = self.buffer.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in self.buffer else b''
                if remainder:
                    self.outbound_writer.write(remainder)
                    self.bytes_sent += len(remainder)
                self.buffer = b''

            # 启动响应转发
            asyncio.create_task(self._relay_responses(reader))

            elapsed = (time.time() - self.start_time) * 1000
            logger.info(f"CONNECT [{used_ip}] -> {self.target_host}:{self.target_port} ({elapsed:.1f}ms)")
            self.stats.record_request(True, used_ip, f"{self.target_host}:{self.target_port}")

        except asyncio.TimeoutError as e:
            logger.error(f"CONNECT超时 {self.target_host}:{self.target_port}: {e}")
            self.stats.record_request(False, self.used_ip or 'unknown', f"{self.target_host}:{self.target_port}")
            self._send_error(504, f"Gateway Timeout: {e}")
        except ConnectionError as e:
            logger.error(f"CONNECT连接失败 {self.target_host}:{self.target_port}: {e}")
            self.stats.record_request(False, self.used_ip or 'unknown', f"{self.target_host}:{self.target_port}")
            self._send_error(502, f"Bad Gateway: {e}")
        except Exception as e:
            logger.error(f"CONNECT失败 {self.target_host}:{self.target_port}: {e}", exc_info=True)
            self.stats.record_request(False, self.used_ip or 'unknown', f"{self.target_host}:{self.target_port}")
            self._send_error(502, f"Bad Gateway: {e}")

    async def _handle_http_request(self, method: str, parsed, header_data: bytes, body_data: bytes):
        """处理普通HTTP请求 - 完美保留原始Header指纹"""
        try:
            reader, writer, used_ip = await self.connector.connect(
                self.target_host, self.target_port
            )
            self.outbound_writer = writer
            self.used_ip = used_ip

            # 1. 提取路径和查询字符串
            path_bytes = (parsed.path or '/').encode()
            if parsed.query:
                path_bytes += b'?' + parsed.query.encode()

            # 2. 处理请求行 (Request Line)
            # 找到第一行结束位置
            first_line_end = header_data.find(b'\r\n')
            if first_line_end == -1:
                first_line_end = header_data.find(b'\n')
            
            # 分割请求行：METHOD URL VERSION
            request_line = header_data[:first_line_end]
            parts = request_line.split(b' ')
            
            # 将绝对URL替换为路径（例如: GET http://host/path -> GET /path）
            # 注意保持原始 METHOD 和 HTTP 版本字段的字节，防止大小写异常
            if len(parts) >= 3:
                # 重新构建请求行: [METHOD] [PATH] [VERSION]
                new_request_line = parts[0] + b' ' + path_bytes + b' ' + parts[2]
            else:
                new_request_line = request_line

            # 3. 过滤并转发后续 Header (保持原始字节细节)
            lines = header_data.split(b'\r\n')
            if len(lines) == 1 and b'\n' in header_data: # 可能是 \n 换行
                lines = header_data.split(b'\n')
            
            new_header_buffer = new_request_line + b'\r\n'
            
            # 从第二行（Header开始）进行过滤
            for line in lines[1:]:
                if not line: continue
                # 仅过滤代理相关头，保持其它所有头（包括大小写和空格）不变
                if not line.lower().startswith(b'proxy-'):
                    new_header_buffer += line + b'\r\n'
            
            # 4. 发送完整请求
            new_header_buffer += b'\r\n'
            writer.write(new_header_buffer + body_data)
            await writer.drain()
            self.bytes_sent += len(new_header_buffer) + len(body_data)

            self.state = 'relaying'
            self.buffer = b''

            # 启动响应转发
            asyncio.create_task(self._relay_responses(reader))

            elapsed = (time.time() - self.start_time) * 1000
            logger.info(f"HTTP [{used_ip}] -> {self.target_host}:{self.target_port}{path_bytes.decode()} ({elapsed:.1f}ms)")
            self.stats.record_request(True, used_ip, f"{self.target_host}:{self.target_port}")

        except Exception as e:
            logger.error(f"HTTP请求失败 {self.target_host}:{self.target_port}: {e}")
            self.stats.record_request(False, self.used_ip or 'unknown', f"{self.target_host}:{self.target_port}")
            self._send_error(502, f"Bad Gateway: {e}")

    async def _relay_responses(self, reader: asyncio.StreamReader):
        """从目标服务器读取响应并转发给客户端"""
        try:
            while True:
                data = await asyncio.wait_for(
                    reader.read(self.config.buffer_size),
                    timeout=self.config.read_timeout
                )
                if not data:
                    break

                self.transport.write(data)
                self.bytes_received += len(data)

        except asyncio.TimeoutError:
            logger.debug(f"读取超时 {self.target_host}:{self.target_port}")
        except Exception as e:
            logger.debug(f"目标连接关闭: {e}")
        finally:
            self._cleanup()

    def _send_error(self, code: int, message: str):
        """发送HTTP错误响应"""
        response = f"HTTP/1.1 {code} {message}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        self.transport.write(response.encode())
        self._cleanup()

    def connection_lost(self, exc):
        """连接断开"""
        self._cleanup()

    def _cleanup(self):
        """清理资源"""
        if self.state == 'closed':
            return

        self.state = 'closed'

        # 释放IP
        if self.used_ip:
            asyncio.create_task(self.connector.ip_pool.release(self.used_ip))

        # 关闭连接
        if self.outbound_writer:
            self.outbound_writer.close()

        if self.transport:
            self.transport.close()

        self.stats.connection_ended()

        # 记录详细日志
        duration = time.time() - self.start_time
        logger.debug(
            f"连接关闭: {self.peername} -> {self.target_host}:{self.target_port}, "
            f"持续时间: {duration:.2f}s, "
            f"发送: {self.bytes_sent}B, 接收: {self.bytes_received}B"
        )


# ============== 管理接口 ==============

class ManagementServer:
    """管理HTTP接口，用于查询统计信息"""

    def __init__(self, stats: ConnectionStats, ip_pool: IPv6AddressPool, port: int = 8890):
        self.stats = stats
        self.ip_pool = ip_pool
        self.port = port
        self.server: Optional[asyncio.Server] = None

    async def start(self):
        """启动管理服务器"""
        self.server = await asyncio.get_running_loop().create_server(
            lambda: ManagementProtocol(self.stats, self.ip_pool),
            '127.0.0.1', self.port
        )
        logger.info(f"管理接口已启动: http://127.0.0.1:{self.port}/stats")

    def stop(self):
        if self.server:
            self.server.close()


class ManagementProtocol(asyncio.Protocol):
    """管理协议处理器"""

    def __init__(self, stats: ConnectionStats, ip_pool: IPv6AddressPool):
        self.stats = stats
        self.ip_pool = ip_pool
        self.buffer = b''

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data: bytes):
        self.buffer += data
        if b'\r\n\r\n' in self.buffer:
            self._handle_request()

    def _handle_request(self):
        try:
            lines = self.buffer.split(b'\r\n')
            request_line = lines[0].decode('utf-8', errors='ignore')
            parts = request_line.split(' ')

            if len(parts) >= 2:
                path = parts[1]

                if path == '/stats':
                    response_data = {
                        'proxy_stats': self.stats.to_dict(),
                        'ip_pool': self.ip_pool.get_stats(),
                        'timestamp': datetime.now().isoformat()
                    }
                    body = json.dumps(response_data, indent=2, ensure_ascii=False)
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                elif path == '/health':
                    body = '{"status": "ok"}'
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"
                else:
                    body = '{"error": "Not Found"}'
                    response = f"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n{body}"

                self.transport.write(response.encode())
        except Exception as e:
            logger.error(f"管理请求处理失败: {e}")
        finally:
            self.transport.close()


# ============== 主服务器 ==============

class IPv6ProxyPoolServer:
    """IPv6代理池服务器主类"""

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.ip_pool = IPv6AddressPool(
            pool_size=config.pool_size,
            interface=config.interface
        )
        self.stats = ConnectionStats()
        self.connector = OutboundConnector(self.ip_pool, config, self.stats)

        self.proxy_server: Optional[asyncio.Server] = None
        self.mgmt_server: Optional[ManagementServer] = None

        self._shutdown_event = asyncio.Event()

    async def start(self):
        """启动服务器"""
        try:
            loop = asyncio.get_running_loop()

            # 设置信号处理
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._signal_handler)

            # 启动代理服务器
            self.proxy_server = await loop.create_server(
                lambda: HTTPProxyProtocol(self.connector, self.stats, self.config),
                self.config.host,
                self.config.port,
                reuse_address=True,
                reuse_port=True
            )

            # 启动管理服务器
            self.mgmt_server = ManagementServer(self.stats, self.ip_pool, self.config.port + 1)
            await self.mgmt_server.start()

            # 打印启动信息
            self._print_startup_info()

            # 等待关闭
            async with self.proxy_server:
                await self._shutdown_event.wait()
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"服务器运行出错: {e}")
            raise
        finally:
            # 确保无论如何都尝试执行基本的清理逻辑
            # 注意：实际的主清理逻辑在 main() 的 finally 块调用的 stop() 中
            # 这里是为了双重保险
            pass

    def _signal_handler(self):
        """信号处理"""
        logger.info("收到关闭信号，正在停止...")
        self._shutdown_event.set()

    def _print_startup_info(self):
        """打印启动信息"""
        # 访问控制信息
        access_info = []
        if self.config.allow_lan:
            access_info.append("允许局域网访问")
        if self.config.allowed_ips:
            access_info.append(f"IP白名单: {', '.join(self.config.allowed_ips[:3])}{'...' if len(self.config.allowed_ips) > 3 else ''}")

        access_str = " | ".join(access_info) if access_info else "仅本地访问 (127.0.0.1)"

        info = f"""
{'='*60}
IPv6出口代理池已启动
{'='*60}
代理地址: http://{self.config.host}:{self.config.port}
管理接口: http://127.0.0.1:{self.config.port + 1}/stats
IPv6池大小: {self.config.pool_size}
限速设置: {self.config.rate_limit} req/s (0=不限速)
访问控制: {access_str}
{'='*60}
使用方法:
  export HTTP_PROXY=http://{self.config.host}:{self.config.port}
  export HTTPS_PROXY=http://{self.config.host}:{self.config.port}
{'='*60}
        """
        print(info)

    async def stop(self):
        """停止服务器"""
        if self.proxy_server:
            self.proxy_server.close()
            await self.proxy_server.wait_closed()
        if self.mgmt_server:
            self.mgmt_server.stop()

        # 清理IP池中的网卡地址
        if self.ip_pool:
            await self.ip_pool.cleanup()

        logger.info("服务器已停止")


# ============== 系统配置辅助 ==============

def setup_ipv6_addresses(count: int, interface: Optional[str] = None):
    """
    配置IPv6地址池到系统

    需要root权限运行。自动探测公网GUA前缀，探测不到则回退到 fd00::。
    """
    # 自动检测接口
    if interface is None:
        detected = _get_default_ipv6_interface()
        if detected:
            interface = detected
            print(f"自动检测到IPv6接口: {interface}")
        else:
            interface = 'lo'
            print("警告: 未自动检测到具有GUA的接口，回退到 'lo'")

    prefix = _get_global_ipv6_prefix(interface)
    if prefix:
        print(f"检测到公网IPv6前缀: {prefix}，将基于此前缀生成地址池")
    else:
        prefix = 'fd00::/64'
        print(f"警告: 未在 {interface} 上探测到公网IPv6前缀(GUA)。")
        print(f"回退到私有前缀 {prefix}，该前缀无法路由到公网，仅适合本地测试。")

    print(f"正在配置 {count} 个IPv6地址到接口 {interface}...")

    configured = 0
    for i in range(count):
        try:
            network = ipaddress.ip_network(prefix, strict=False)
            prefix_len = network.prefixlen
            if prefix_len >= 127:
                host_bits = 128 - prefix_len
                max_val = (1 << host_bits) - 2
                offset = random.randint(1, max(1, max_val))
                ip_int = int(network.network_address) + offset
                addr = str(ipaddress.IPv6Address(ip_int)) + '/128'
            else:
                host = random.getrandbits(128 - prefix_len)
                if host == 0:
                    host = 1
                mask = (1 << (128 - prefix_len)) - 1
                ip_int = (int(network.network_address) & (~mask)) | (host & mask)
                addr = str(ipaddress.IPv6Address(ip_int)) + '/128'
        except Exception as e:
            print(f"生成地址失败: {e}，回退到简单模式")
            suffix_parts = [f'{random.randint(0, 65535):04x}' for _ in range(4)]
            suffix = ':'.join(suffix_parts)
            base = prefix.rstrip(':').rstrip('/')
            addr = f"{base}::{suffix}/128"

        try:
            result = subprocess.run(
                ['ip', '-6', 'addr', 'add', addr, 'dev', interface],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0:
                configured += 1
        except Exception as e:
            print(f"错误: {e}")
            break

        if (i + 1) % 100 == 0:
            print(f"  已处理 {i + 1}/{count} 个地址")

    print(f"完成: 成功配置 {configured} 个新地址")
    print(f"\n查看已配置地址: ip -6 addr show dev {interface}")


def clear_ipv6_addresses(interface: Optional[str] = None):
    """清除自动配置的IPv6地址（包括 fd00:: 和探测到的公网前缀）"""
    # 自动检测接口
    if interface is None:
        detected = _get_default_ipv6_interface()
        if detected:
            interface = detected
            print(f"自动检测到IPv6接口: {interface}")
        else:
            interface = 'lo'
            print("警告: 未自动检测到接口，回退到 'lo'")

    prefixes_to_clear = ['fd00::']
    detected = _get_global_ipv6_prefix(interface)
    if detected:
        try:
            net = ipaddress.ip_network(detected, strict=False)
            prefixes_to_clear.append(str(net.network_address))
        except Exception:
            pass

    print(f"正在清除 {interface} 上的自动配置IPv6地址...")

    try:
        result = subprocess.run(
            ['ip', '-6', 'addr', 'show', 'dev', interface],
            capture_output=True,
            text=True,
            check=True
        )

        for line in result.stdout.splitlines():
            for prefix in prefixes_to_clear:
                if prefix in line:
                    parts = line.strip().split()
                    for part in parts:
                        if part.startswith(prefix):
                            addr = part.split('/')[0]
                            subprocess.run(
                                ['ip', '-6', 'addr', 'del', f'{addr}/128', 'dev', interface],
                                capture_output=True,
                                check=False
                            )
                            print(f"  已删除: {addr}")

    except Exception as e:
        print(f"错误: {e}")


# ============== 命令行接口 ==============

def main():
    parser = argparse.ArgumentParser(
        description='IPv6出口代理池 - 高性能HTTP/HTTPS代理',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 启动代理服务器
  python ipv6_proxy_pool.py --port 8899 --pool-size 1000

  # 配置IPv6地址池（需要root）
  sudo python ipv6_proxy_pool.py --setup-ip --ip-count 1000

  # 使用代理
  export HTTP_PROXY=http://127.0.0.1:8899
  export HTTPS_PROXY=http://127.0.0.1:8899
  python your_script.py
        """
    )

    # 服务器配置
    parser.add_argument('--host', default='0.0.0.0', help='代理绑定地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', '-p', type=int, default=8899, help='代理端口 (默认: 8899)')
    parser.add_argument('--pool-size', type=int, default=1000, help='IPv6地址池大小 (默认: 1000)')
    parser.add_argument('--rate-limit', type=int, default=0, help='每秒请求限速，0表示不限速 (默认: 0)')
    parser.add_argument('--timeout', type=float, default=30.0, help='连接超时时间 (默认: 30s)')
    parser.add_argument('--debug', action='store_true', help='启用调试日志')

    # 指纹混淆配置 (默认开启)
    parser.add_argument('--disable-fp', action='store_true', help='禁用OS/TCP指纹混淆 (TTL, Window Size, Flow Label)')
    parser.add_argument('--ttl-range', default='64,128', help='TTL/Hop Limit 随机范围 (默认: 64,128)')
    parser.add_argument('--win-range', default='65536,131072', help='TCP 窗口大小/缓冲区随机范围 (默认: 65536,131072)')
    parser.add_argument('--no-flow-label', action='store_true', help='禁用 IPv6 Flow Label 随机化')

    # IP配置
    parser.add_argument('--setup-ip', action='store_true', help='配置IPv6地址池到系统（需要root）')
    parser.add_argument('--clear-ip', action='store_true', help='清除配置的IPv6地址（需要root）')
    parser.add_argument('--ip-count', type=int, default=1000, help='配置/清除的IP数量 (默认: 1000)')
    parser.add_argument('--interface', '-i', default=None, help='网络接口 (默认: 自动检测)')

    args = parser.parse_args()

    # 配置日志
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # IP配置模式
    if args.setup_ip:
        setup_ipv6_addresses(args.ip_count, args.interface)
        return

    if args.clear_ip:
        clear_ipv6_addresses(args.interface)
        return

    # 解析指纹范围
    min_ttl, max_ttl = 64, 128
    try:
        min_ttl, max_ttl = map(int, args.ttl_range.split(','))
    except:
        pass

    win_min, win_max = 65536, 131072
    try:
        win_min, win_max = map(int, args.win_range.split(','))
    except:
        pass

    # 服务器模式
    config = ProxyConfig(
        host=args.host,
        port=args.port,
        pool_size=args.pool_size,
        interface=args.interface,
        rate_limit=args.rate_limit,
        connection_timeout=args.timeout,
        enable_fingerprint=not args.disable_fp,
        min_ttl=min_ttl,
        max_ttl=max_ttl,
        randomize_flow_label=not args.no_flow_label,
        window_size_min=win_min,
        window_size_max=win_max
    )

    server = IPv6ProxyPoolServer(config)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        pass
    finally:
        asyncio.run(server.stop())


if __name__ == '__main__':
    main()
