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
import signal
import socket
import struct
import sys
import time
import threading
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
    host: str = '0.0.0.0'  # 默认绑定到所有接口，允许局域网访问
    port: int = 8899
    pool_size: int = 1000
    ipv6_prefix: str = 'fd00::'
    interface: str = 'lo'  # 网卡接口
    max_connections_per_ip: int = 10
    connection_timeout: float = 30.0
    read_timeout: float = 60.0
    rate_limit: int = 0  # 0表示不限速，每秒请求数
    enable_stats: bool = True
    prefer_ipv6_target: bool = False
    buffer_size: int = 65536
    # 访问控制
    allow_lan: bool = True  # 默认允许局域网访问
    allowed_ips: Optional[List[str]] = None  # IP白名单
    auth_token: Optional[str] = None  # 简单认证令牌


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

def _get_global_ipv6_prefix(interface: str) -> Optional[str]:
    """自动探测网卡上可路由的全球单播IPv6前缀（/64）"""
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
                            if iface.ip.is_global:
                                net = iface.network
                                if net.prefixlen <= 64:
                                    return f"{net.network_address}/64"
                        except ValueError:
                            continue
    except Exception as e:
        logger.warning(f"探测IPv6前缀失败: {e}")
    return None


class IPv6AddressPool:
    """
    IPv6地址池管理器 - 基于真实可路由前缀

    自动探测网卡上的全球单播IPv6前缀(GUA)，在该前缀下生成地址池。
    每个地址绑定到指定网卡，确保能作为合法源地址路由到公网。
    """

    def __init__(self, prefix: str = 'fd00::', pool_size: int = 1000, interface: str = 'lo'):
        self.interface = interface
        self.pool_size = pool_size
        self._available: List[str] = []
        self._in_use: Set[str] = set()
        self._lock = threading.Lock()
        self._setup_mode = False

        # 自动探测真实前缀
        if prefix == 'fd00::' or not prefix:
            detected = _get_global_ipv6_prefix(interface)
            if detected:
                logger.info(f"自动探测到IPv6前缀: {detected}，将基于此生成地址池")
                self.prefix = detected
            else:
                logger.warning(
                    f"未在 {interface} 上探测到公网IPv6前缀(GUA)，回退到 {prefix}。"
                    f"注意：fd00:: 是ULA私有地址，无法直接路由到公网，"
                    f"外部目标会表现为连接超时或黑洞。如需使用公网IPv6，"
                    f"请为 {interface} 配置全球单播地址，或通过 --ipv6-prefix 显式指定。"
                )
                self.prefix = prefix
        else:
            self.prefix = prefix

        self._generate_pool()
        self._install_pool_to_interface()

    def _generate_pool(self):
        """生成IPv6地址池"""
        for _ in range(self.pool_size):
            ip = self._generate_ip()
            self._available.append(ip)
        logger.info(f"已生成 {self.pool_size} 个IPv6地址，前缀: {self.prefix}")

    def _generate_ip(self) -> str:
        """基于前缀生成一个新的IPv6地址"""
        try:
            network = ipaddress.ip_network(self.prefix, strict=False)
            prefix_len = network.prefixlen
            if prefix_len >= 127:
                host_bits = 128 - prefix_len
                max_val = (1 << host_bits) - 2
                offset = random.randint(1, max(1, max_val))
                ip_int = int(network.network_address) + offset
                return str(ipaddress.IPv6Address(ip_int))
            else:
                host = random.getrandbits(128 - prefix_len)
                if host == 0:
                    host = 1
                mask = (1 << (128 - prefix_len)) - 1
                ip_int = (int(network.network_address) & (~mask)) | (host & mask)
                return str(ipaddress.IPv6Address(ip_int))
        except Exception as e:
            logger.warning(f"基于前缀生成地址失败: {e}，回退到简单拼接")
            suffix_parts = [f'{random.randint(0, 65535):04x}' for _ in range(4)]
            suffix = ':'.join(suffix_parts)
            base = self.prefix.rstrip(':').rstrip('/')
            return f"{base}::{suffix}"

    def _install_pool_to_interface(self):
        """将可用池中的地址批量添加到网卡"""
        failed = 0
        for ip in list(self._available):
            if not self._add_ip_to_interface(ip):
                self._available.remove(ip)
                failed += 1
        total = len(self._available)
        if failed:
            logger.warning(f"地址池初始化：{failed} 个地址添加到网卡失败，可用池: {total}")
        logger.info(f"地址池初始化完成：{total}/{self.pool_size} 个地址已安装到 {self.interface}")

    def _add_ip_to_interface(self, ip: str) -> bool:
        """将IP添加到网卡"""
        try:
            # 优先尝试 nodad 以跳过重复地址检测，加快可用
            result = subprocess.run(
                ['ip', '-6', 'addr', 'add', f'{ip}/128', 'dev', self.interface, 'nodad'],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0 or 'File exists' in result.stderr:
                return True
            # 回退到标准模式（部分旧版 iproute 不支持 nodad）
            result = subprocess.run(
                ['ip', '-6', 'addr', 'add', f'{ip}/128', 'dev', self.interface],
                capture_output=True,
                text=True,
                check=False
            )
            return result.returncode == 0 or 'File exists' in result.stderr
        except Exception as e:
            logger.warning(f"添加IP {ip} 到网卡失败: {e}")
            return False

    def _remove_ip_from_interface(self, ip: str) -> bool:
        """从网卡删除IP"""
        try:
            result = subprocess.run(
                ['ip', '-6', 'addr', 'del', f'{ip}/128', 'dev', self.interface],
                capture_output=True,
                text=True,
                check=False
            )
            return result.returncode == 0
        except Exception as e:
            logger.warning(f"从网卡删除IP {ip} 失败: {e}")
            return False

    def acquire(self) -> Optional[str]:
        """获取一个可用的IPv6地址"""
        with self._lock:
            while self._available:
                ip = self._available.pop(0)
                if self._add_ip_to_interface(ip):
                    self._in_use.add(ip)
                    return ip
                else:
                    logger.warning(f"acquire 时添加IP {ip} 失败，尝试下一个")
            return None

    def release(self, ip: str):
        """释放IPv6地址 - 即弃模式：从网卡删除旧IP，生成新IP并添加"""
        with self._lock:
            if ip in self._in_use:
                self._in_use.discard(ip)

                # 从网卡删除旧IP
                if ip != '::' and not ip.startswith('0.0.0.0') and not ip.startswith('0.0'):
                    self._remove_ip_from_interface(ip)

                # 生成新IP
                new_ip = self._generate_ip()

                # 将新IP添加到网卡
                if self._add_ip_to_interface(new_ip):
                    self._available.append(new_ip)
                    logger.debug(f"IP汰换: {ip} -> {new_ip}")
                else:
                    # 如果添加失败，尝试生成另一个
                    logger.warning(f"添加新IP失败，尝试另一个...")
                    for _ in range(5):  # 最多尝试5次
                        new_ip = self._generate_ip()
                        if self._add_ip_to_interface(new_ip):
                            self._available.append(new_ip)
                            logger.debug(f"IP汰换: {ip} -> {new_ip} (重试)")
                            break

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

from functools import lru_cache
import time

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
        """
        建立出站连接，优先尝试IPv6

        Returns:
            (reader, writer, source_ip)
        """
        await self.rate_limiter.wait()

        # 检查缓存
        cached_result = self._ipv6_cache.get(host)

        if cached_result is True:
            # 缓存显示支持IPv6，直接尝试
            return await self._try_ipv6_first(host, port)
        elif cached_result is False:
            # 缓存显示不支持IPv6，直接用IPv4
            return await self._connect_ipv4(host, port)
        else:
            # 无缓存，优先尝试IPv6
            try:
                return await self._try_ipv6_first(host, port)
            except Exception as e:
                # IPv6失败，回退到IPv4
                logger.debug(f"IPv6连接失败，回退到IPv4: {host}:{port} - {e}")
                return await self._connect_ipv4(host, port)

    async def _try_ipv6_first(self, host: str, port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """优先尝试IPv6连接，快速失败"""
        local_v6_addr = self.ip_pool.acquire()
        if not local_v6_addr:
            local_v6_addr = "::"

        try:
            # 获取IPv6地址信息
            addr_info = await asyncio.wait_for(
                asyncio.get_event_loop().getaddrinfo(
                    host, port,
                    family=socket.AF_INET6,  # 只获取IPv6
                    type=socket.SOCK_STREAM
                ),
                timeout=3.0
            )

            if not addr_info:
                raise ConnectionError(f"无IPv6地址: {host}")

            # 尝试IPv6连接（短暂超时，避免黑洞等待）
            target_family, _, _, _, target_addr = addr_info[0]
            sock = self._create_ipv6_socket(local_v6_addr)

            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().sock_connect(sock, target_addr),
                    timeout=3.0
                )

                # IPv6连接成功，更新缓存
                self._ipv6_cache.set(host, True)

                # 包装为asyncio流
                reader, writer = await asyncio.open_connection(sock=sock)

                logger.debug(f"IPv6连接成功: [{local_v6_addr}] -> {host}:{port}")
                return reader, writer, local_v6_addr

            except Exception as e:
                sock.close()
                raise

        except Exception as e:
            self.ip_pool.release(local_v6_addr)
            # IPv6失败，记录缓存并抛出让上层回退
            self._ipv6_cache.set(host, False)
            raise

    async def _connect_ipv4(self, host: str, port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """使用IPv4连接（回退方案）"""
        v4_display = "0.0.0.0"

        try:
            # 获取IPv4地址信息
            addr_info = await asyncio.wait_for(
                asyncio.get_event_loop().getaddrinfo(
                    host, port,
                    family=socket.AF_INET,  # 只获取IPv4
                    type=socket.SOCK_STREAM
                ),
                timeout=5.0
            )

            if not addr_info:
                raise ConnectionError(f"DNS解析失败: {host}")

            target_family, _, _, _, target_addr = addr_info[0]

            # 创建IPv4 socket
            sock = self._create_ipv4_socket()

            # 连接目标
            await asyncio.wait_for(
                asyncio.get_event_loop().sock_connect(sock, target_addr),
                timeout=self.config.connection_timeout
            )

            # 包装为asyncio流
            reader, writer = await asyncio.open_connection(sock=sock)

            logger.debug(f"IPv4连接成功(回退): [{v4_display}] -> {host}:{port}")
            return reader, writer, v4_display

        except Exception as e:
            sock.close()
            raise ConnectionError(f"连接失败 {host}:{port} - {e}")

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

        # 访问控制检查
        if not self._check_access():
            client_ip = self.peername[0] if self.peername else 'unknown'
            logger.warning(f"拒绝连接: {client_ip}")
            self._send_error(403, "Forbidden")
            return

        self.stats.connection_started()
        logger.debug(f"客户端连接: {self.peername}")

    def _check_access(self) -> bool:
        """检查客户端是否有权限访问"""
        if not self.peername:
            return False

        client_ip = self.peername[0]

        # 本地连接总是允许
        if client_ip in ('127.0.0.1', '::1', 'localhost'):
            return True

        # 如果允许局域网
        if self.config.allow_lan:
            # 检查是否是局域网IP
            if self._is_lan_ip(client_ip):
                return True

        # 检查白名单
        if self.config.allowed_ips:
            if client_ip in self.config.allowed_ips:
                return True
            # 支持CIDR格式
            for allowed in self.config.allowed_ips:
                if '/' in allowed and self._ip_in_cidr(client_ip, allowed):
                    return True

        return False

    def _is_lan_ip(self, ip: str) -> bool:
        """检查IP是否是局域网地址"""
        try:
            import ipaddress
            addr = ipaddress.ip_address(ip)
            # 私有地址范围
            return addr.is_private
        except:
            return False

    def _ip_in_cidr(self, ip: str, cidr: str) -> bool:
        """检查IP是否在CIDR网段内"""
        try:
            import ipaddress
            addr = ipaddress.ip_address(ip)
            network = ipaddress.ip_network(cidr, strict=False)
            return addr in network
        except:
            return False

    def data_received(self, data: bytes):
        """接收客户端数据"""
        if self.state == 'relaying':
            # 隧道模式，直接转发
            if self.outbound_writer:
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
            self._handle_request(header_data, body_data)
        except Exception as e:
            logger.error(f"处理请求失败: {e}")
            self._send_error(400, f"Bad Request: {e}")

    def _handle_request(self, header_data: bytes, body_data: bytes):
        """处理HTTP请求"""
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

        except Exception as e:
            logger.error(f"CONNECT失败 {self.target_host}:{self.target_port}: {e}")
            self.stats.record_request(False, self.used_ip or 'unknown', f"{self.target_host}:{self.target_port}")
            self._send_error(502, f"Bad Gateway: {e}")

    async def _handle_http_request(self, method: str, parsed, header_data: bytes, body_data: bytes):
        """处理普通HTTP请求"""
        try:
            reader, writer, used_ip = await self.connector.connect(
                self.target_host, self.target_port
            )
            self.outbound_writer = writer
            self.used_ip = used_ip

            # 重构请求
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query

            lines = header_data.split(b'\r\n')
            new_request = f"{method} {path} HTTP/1.1\r\n".encode()

            # 转发头（去除代理相关头）
            for line in lines[1:]:
                if line:
                    lower_line = line.lower()
                    if not lower_line.startswith(b'proxy-'):
                        new_request += line + b'\r\n'

            new_request += b'\r\n'

            # 发送请求
            writer.write(new_request + body_data)
            await writer.drain()
            self.bytes_sent += len(new_request) + len(body_data)

            self.state = 'relaying'
            self.buffer = b''

            # 启动响应转发
            asyncio.create_task(self._relay_responses(reader))

            elapsed = (time.time() - self.start_time) * 1000
            logger.info(f"HTTP [{used_ip}] -> {self.target_host}:{self.target_port}{path} ({elapsed:.1f}ms)")
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
        if self.used_ip and self.used_ip not in ('0.0.0.0', '::'):
            self.connector.ip_pool.release(self.used_ip)

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
        self.server = await asyncio.get_event_loop().create_server(
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
            prefix=config.ipv6_prefix,
            pool_size=config.pool_size,
            interface=getattr(config, 'interface', 'lo')
        )
        self.stats = ConnectionStats()
        self.connector = OutboundConnector(self.ip_pool, config, self.stats)

        self.proxy_server: Optional[asyncio.Server] = None
        self.mgmt_server: Optional[ManagementServer] = None

        self._shutdown_event = asyncio.Event()

    async def start(self):
        """启动服务器"""
        loop = asyncio.get_event_loop()

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
        logger.info("服务器已停止")


# ============== 系统配置辅助 ==============

def setup_ipv6_addresses(count: int, interface: str = 'lo'):
    """
    配置IPv6地址池到系统

    需要root权限运行。自动探测公网GUA前缀，探测不到则回退到 fd00::。
    """
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


def clear_ipv6_addresses(interface: str = 'lo'):
    """清除自动配置的IPv6地址（包括 fd00:: 和探测到的公网前缀）"""
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
    parser.add_argument('--host', default='0.0.0.0', help='代理绑定地址 (默认: 0.0.0.0，允许局域网访问)')
    parser.add_argument('--port', '-p', type=int, default=8899, help='代理端口 (默认: 8899)')
    parser.add_argument('--pool-size', type=int, default=1000, help='IPv6地址池大小 (默认: 1000)')
    parser.add_argument('--ipv6-prefix', default='fd00::', help='IPv6地址前缀 (默认: fd00::)')
    parser.add_argument('--rate-limit', type=int, default=0, help='每秒请求限速，0表示不限速 (默认: 0)')
    parser.add_argument('--timeout', type=float, default=30.0, help='连接超时时间 (默认: 30s)')
    parser.add_argument('--debug', action='store_true', help='启用调试日志')

    # 访问控制
    parser.add_argument('--allow-lan', action='store_true', default=True, help='允许局域网设备访问 (默认: 开启)')
    parser.add_argument('--deny-lan', action='store_true', help='禁止局域网设备访问 (仅本地127.0.0.1)')
    parser.add_argument('--allowed-ips', type=str, help='允许的IP白名单，逗号分隔 (如: 192.168.1.0/24,10.0.0.5)')
    parser.add_argument('--bind-all', action='store_true', help='绑定到所有接口 (等同于 --host 0.0.0.0)')

    # IP配置
    parser.add_argument('--setup-ip', action='store_true', help='配置IPv6地址池到系统（需要root）')
    parser.add_argument('--clear-ip', action='store_true', help='清除配置的IPv6地址（需要root）')
    parser.add_argument('--ip-count', type=int, default=100, help='配置/清除的IP数量 (默认: 100)')
    parser.add_argument('--interface', '-i', default='lo', help='网络接口 (默认: lo)')

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

    # 处理bind-all
    if args.bind_all:
        args.host = '0.0.0.0'

    # 处理deny-lan
    if args.deny_lan:
        args.allow_lan = False
        args.host = '127.0.0.1'

    # 解析IP白名单
    allowed_ips = None
    if args.allowed_ips:
        allowed_ips = [ip.strip() for ip in args.allowed_ips.split(',')]

    # 服务器模式
    config = ProxyConfig(
        host=args.host,
        port=args.port,
        pool_size=args.pool_size,
        ipv6_prefix=args.ipv6_prefix,
        interface=args.interface,
        rate_limit=args.rate_limit,
        connection_timeout=args.timeout,
        allow_lan=args.allow_lan,
        allowed_ips=allowed_ips
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
