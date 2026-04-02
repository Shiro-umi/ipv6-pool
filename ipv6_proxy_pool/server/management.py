import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from ipv6_proxy_pool.core.stats import ConnectionStats
from ipv6_proxy_pool.core.pool import IPv6AddressPool

logger = logging.getLogger('ipv6_proxy_pool.management')

class ManagementServer:
    """管理HTTP接口，用于查询统计信息"""

    def __init__(self, stats: ConnectionStats, ip_pool: IPv6AddressPool, host: str = '0.0.0.0', port: int = 8900):
        self.stats = stats
        self.ip_pool = ip_pool
        self.host = host
        self.port = port
        self.server: Optional[asyncio.Server] = None
        self._is_running = False

    async def start(self):
        """启动管理服务器"""
        if self._is_running:
            return

        try:
            self.server = await asyncio.get_running_loop().create_server(
                lambda: ManagementProtocol(self.stats, self.ip_pool),
                self.host, self.port,
                reuse_address=True,
                reuse_port=True
            )
            self._is_running = True
            logger.info(f"管理接口已启动: http://{self.host}:{self.port}/stats")
        except Exception as e:
            logger.error(f"管理接口启动失败: {e}")
            raise

    def stop(self):
        if self.server:
            self.server.close()
            self._is_running = False
            self.server = None


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
