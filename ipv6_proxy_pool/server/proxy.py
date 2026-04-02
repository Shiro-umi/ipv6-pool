import asyncio
import logging
import signal
from typing import Optional

from ipv6_proxy_pool.core.config import ProxyConfig
from ipv6_proxy_pool.core.stats import ConnectionStats
from ipv6_proxy_pool.core.pool import IPv6AddressPool
from ipv6_proxy_pool.core.connector import OutboundConnector
from ipv6_proxy_pool.protocol.http import HTTPProxyProtocol
from ipv6_proxy_pool.server.management import ManagementServer

logger = logging.getLogger('ipv6_proxy_pool.proxy')

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
        # 显式传递 host 以对齐 proxy 绑定地址
        self.mgmt_server = ManagementServer(
            self.stats, 
            self.ip_pool, 
            host=self.config.host, 
            port=self.config.port + 1
        )

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
            await self.mgmt_server.start()

            # 初始化IP池
            await self.ip_pool.setup()

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
管理接口: http://{self.config.host}:{self.config.port + 1}/stats
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
