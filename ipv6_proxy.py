#!/usr/bin/env python3
"""
IPv6出口代理 - 基础测试版本
"""

import asyncio
import argparse
import logging
import random
import socket
import struct
import sys
from typing import Optional, Tuple, List
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('ipv6_proxy')


class IPv6Pool:
    """IPv6地址池管理"""

    def __init__(self, prefix: str = "fd00::", pool_size: int = 100):
        self.prefix = prefix
        self.pool_size = pool_size
        self.available_ips: List[str] = []
        self.in_use_ips: set = set()
        self._generate_pool()

    def _generate_pool(self):
        """生成IPv6地址池"""
        for _ in range(self.pool_size):
            # 生成64位随机后缀
            suffix = ''.join(f'{random.randint(0, 65535):04x}:' for _ in range(3)) + f'{random.randint(0, 65535):04x}'
            ip = f"{self.prefix}{suffix}"
            self.available_ips.append(ip)
        logger.info(f"已生成 {self.pool_size} 个IPv6地址")

    def acquire(self) -> Optional[str]:
        """获取一个可用的IPv6地址"""
        if not self.available_ips:
            return None
        ip = self.available_ips.pop(0)
        self.in_use_ips.add(ip)
        return ip

    def release(self, ip: str):
        """释放IPv6地址回池"""
        if ip in self.in_use_ips:
            self.in_use_ips.discard(ip)
            self.available_ips.append(ip)


class IPv6Connector:
    """使用IPv6地址创建出站连接（支持IPv4/IPv6双栈）"""

    def __init__(self, ip_pool: IPv6Pool, prefer_ipv6: bool = False):
        self.ip_pool = ip_pool
        self.prefer_ipv6 = prefer_ipv6
        self.local_addr: Optional[str] = None

    async def connect(self, host: str, port: int, timeout: float = 30.0) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
        """建立出站连接，返回(reader, writer, 使用的本地IP)"""
        # 获取IPv6地址（如果可用）
        local_v6_addr = self.ip_pool.acquire()
        if not local_v6_addr:
            local_v6_addr = "::"
        self.local_addr = local_v6_addr

        try:
            # 解析目标地址（支持IPv4和IPv6）
            addr_info = await asyncio.wait_for(
                asyncio.get_event_loop().getaddrinfo(
                    host, port,
                    family=socket.AF_UNSPEC,  # 支持IPv4和IPv6
                    type=socket.SOCK_STREAM
                ),
                timeout=5.0
            )

            if not addr_info:
                self.ip_pool.release(local_v6_addr)
                raise ConnectionError(f"无法解析: {host}:{port}")

            # 选择目标地址（优先IPv6如果可用且偏好设置）
            target_addr = addr_info[0][4]
            target_family = addr_info[0][0]

            if self.prefer_ipv6:
                for family, _, _, _, sockaddr in addr_info:
                    if family == socket.AF_INET6:
                        target_family = family
                        target_addr = sockaddr
                        break

            # 根据目标地址族创建对应socket
            if target_family == socket.AF_INET6:
                # IPv6目标：尝试使用IPv6源地址
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.setblocking(False)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                # 尝试绑定到IPv6地址
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except:
                    pass

                try:
                    sock.bind((local_v6_addr, 0, 0, 0))
                    used_addr = local_v6_addr
                except OSError as e:
                    logger.debug(f"绑定 {local_v6_addr} 失败: {e}")
                    try:
                        sock.bind(("::", 0, 0, 0))
                        used_addr = "::"
                    except OSError:
                        sock.close()
                        raise
            else:
                # IPv4目标：创建IPv4 socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(False)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                # 尝试从IPv6地址提取或映射到IPv4（简化：使用默认）
                # 实际场景需要系统支持IPv6-to-IPv4映射或独立IPv4池
                try:
                    # 尝试绑定到任意IPv4地址
                    sock.bind(("0.0.0.0", 0))
                    used_addr = "0.0.0.0"
                    # 释放IPv6地址（未使用）
                    self.ip_pool.release(local_v6_addr)
                except OSError:
                    sock.close()
                    self.ip_pool.release(local_v6_addr)
                    raise

            # 连接目标
            await asyncio.wait_for(
                asyncio.get_event_loop().sock_connect(sock, target_addr),
                timeout=timeout
            )

            # 包装为asyncio流
            reader = asyncio.StreamReader(limit=65536)
            protocol = asyncio.StreamReaderProtocol(reader)
            transport, _ = await asyncio.get_event_loop().connect_accepted_socket(
                lambda: protocol, sock
            )
            writer = asyncio.StreamWriter(transport, protocol, reader, asyncio.get_event_loop())

            return reader, writer, used_addr

        except Exception as e:
            self.ip_pool.release(local_v6_addr)
            raise ConnectionError(f"连接失败 {host}:{port}: {e}")


class HTTPProxyProtocol(asyncio.Protocol):
    """HTTP代理协议处理器"""

    def __init__(self, ip_pool: IPv6Pool):
        self.ip_pool = ip_pool
        self.transport: Optional[asyncio.Transport] = None
        self.peername: Optional[Tuple] = None
        self.buffer = b''
        self.state = 'initial'
        self.target_host: Optional[str] = None
        self.target_port: int = 0
        self.connector: Optional[IPv6Connector] = None
        self.outbound_writer: Optional[asyncio.StreamWriter] = None
        self.used_ip: Optional[str] = None

    def connection_made(self, transport: asyncio.Transport):
        self.transport = transport
        self.peername = transport.get_extra_info('peername')
        logger.debug(f"客户端连接: {self.peername}")

    def data_received(self, data: bytes):
        if self.state == 'relaying':
            if self.outbound_writer:
                self.outbound_writer.write(data)
            return

        self.buffer += data

        if b'\r\n\r\n' not in self.buffer:
            return

        try:
            headers_end = self.buffer.index(b'\r\n\r\n') + 4
            header_data = self.buffer[:headers_end]
            body_data = self.buffer[headers_end:]
            self._handle_http_request(header_data, body_data)
        except Exception as e:
            logger.error(f"处理请求失败: {e}")
            self._send_error(400, f"Bad Request: {e}")

    def _handle_http_request(self, header_data: bytes, body_data: bytes):
        try:
            lines = header_data.split(b'\r\n')
            request_line = lines[0].decode('utf-8', errors='ignore')
            method, url, version = request_line.split(' ', 2)

            logger.debug(f"收到请求: {method} {url}")

            if method == 'CONNECT':
                host, port = url.rsplit(':', 1)
                self.target_host = host
                self.target_port = int(port)
                asyncio.create_task(self._do_connect())
            else:
                parsed = urlparse(url)
                self.target_host = parsed.hostname or url.split('/')[0].split(':')[0]
                self.target_port = parsed.port or 80
                asyncio.create_task(self._do_http_request(method, parsed, header_data, body_data))

        except Exception as e:
            logger.error(f"解析请求失败: {e}")
            self._send_error(400, "Bad Request")

    async def _do_connect(self):
        try:
            self.connector = IPv6Connector(self.ip_pool, prefer_ipv6=False)
            reader, writer, used_ip = await self.connector.connect(self.target_host, self.target_port)
            self.outbound_writer = writer
            self.used_ip = used_ip

            self.transport.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.state = 'relaying'

            if self.buffer:
                remainder = self.buffer.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in self.buffer else b''
                if remainder:
                    self.outbound_writer.write(remainder)
                self.buffer = b''

            asyncio.create_task(self._relay_from_target(reader))

            logger.info(f"CONNECT [{used_ip}] -> {self.target_host}:{self.target_port}")

        except Exception as e:
            logger.error(f"CONNECT失败: {e}")
            self._send_error(502, f"Bad Gateway: {e}")

    async def _do_http_request(self, method: str, parsed, header_data: bytes, body_data: bytes):
        try:
            self.connector = IPv6Connector(self.ip_pool, prefer_ipv6=False)
            reader, writer, used_ip = await self.connector.connect(self.target_host, self.target_port)
            self.outbound_writer = writer
            self.used_ip = used_ip

            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query

            lines = header_data.split(b'\r\n')
            new_request = f"{method} {path} HTTP/1.1\r\n".encode()

            for line in lines[1:]:
                if line and not line.lower().startswith(b'proxy-connection:'):
                    new_request += line + b'\r\n'

            new_request += b'\r\n'
            writer.write(new_request + body_data)
            await writer.drain()

            self.state = 'relaying'
            self.buffer = b''

            asyncio.create_task(self._relay_from_target(reader))

            logger.info(f"HTTP [{used_ip}] -> {self.target_host}:{self.target_port}{path}")

        except Exception as e:
            logger.error(f"HTTP请求失败: {e}")
            self._send_error(502, f"Bad Gateway: {e}")

    async def _relay_from_target(self, reader: asyncio.StreamReader):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                self.transport.write(data)
        except Exception as e:
            logger.debug(f"目标连接关闭: {e}")
        finally:
            self._cleanup()

    def _send_error(self, code: int, message: str):
        response = f"HTTP/1.1 {code} {message}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        self.transport.write(response.encode())
        self._cleanup()

    def connection_lost(self, exc):
        self._cleanup()

    def _cleanup(self):
        self.state = 'closed'
        if self.used_ip and self.ip_pool:
            self.ip_pool.release(self.used_ip)
            self.used_ip = None
        if self.outbound_writer:
            self.outbound_writer.close()
        if self.transport:
            self.transport.close()


class IPv6ProxyServer:
    """IPv6出口代理服务器"""

    def __init__(self, host: str = '127.0.0.1', port: int = 8899, pool_size: int = 100):
        self.host = host
        self.port = port
        self.ip_pool = IPv6Pool(pool_size=pool_size)
        self.server: Optional[asyncio.Server] = None

    async def start(self):
        loop = asyncio.get_event_loop()

        self.server = await loop.create_server(
            lambda: HTTPProxyProtocol(self.ip_pool),
            self.host,
            self.port,
            reuse_address=True,
            reuse_port=True
        )

        logger.info(f"=" * 50)
        logger.info(f"IPv6出口代理已启动: {self.host}:{self.port}")
        logger.info(f"IPv6地址池大小: {self.ip_pool.pool_size}")
        logger.info(f"=" * 50)
        logger.info(f"使用方法:")
        logger.info(f"  export HTTP_PROXY=http://{self.host}:{self.port}")
        logger.info(f"  export HTTPS_PROXY=http://{self.host}:{self.port}")
        logger.info(f"=" * 50)

        async with self.server:
            await self.server.serve_forever()

    def stop(self):
        if self.server:
            self.server.close()


def main():
    parser = argparse.ArgumentParser(description='IPv6出口代理测试')
    parser.add_argument('--host', default='127.0.0.1', help='绑定地址')
    parser.add_argument('--port', '-p', type=int, default=8899, help='绑定端口')
    parser.add_argument('--pool-size', type=int, default=100, help='IPv6地址池大小')
    parser.add_argument('--debug', action='store_true', help='调试模式')

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    server = IPv6ProxyServer(args.host, args.port, args.pool_size)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("代理服务器已停止")


if __name__ == '__main__':
    main()
