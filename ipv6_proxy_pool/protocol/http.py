import asyncio
import ipaddress
import logging
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

from ipv6_proxy_pool.core.config import ProxyConfig
from ipv6_proxy_pool.core.stats import ConnectionStats
from ipv6_proxy_pool.core.connector import OutboundConnector

logger = logging.getLogger('ipv6_proxy_pool.protocol')

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
