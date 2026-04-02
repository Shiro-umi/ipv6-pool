import asyncio
import ipaddress
import logging
import random
import subprocess
import time
from typing import Dict, List, Optional, Set, Tuple

from ipv6_proxy_pool.utils.network import _get_global_ipv6_prefix, _get_default_ipv6_interface

logger = logging.getLogger('ipv6_proxy_pool.pool')

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

    async def setup(self):
        """异步执行启动前准备"""
        loop = asyncio.get_running_loop()
        # 3. 启动前强制清理（防止残留）
        await loop.run_in_executor(None, self._pre_startup_cleanup)

        # 4. 生成并安装初始池
        self._generate_pool()
        await loop.run_in_executor(None, self._install_pool_to_interface)

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
        """释放并汰换地址（即用即弃，带延迟清理以确保TCP挥手完成）"""
        if not ip or ip in ('::', '0.0.0.0'): return

        loop = asyncio.get_running_loop()
        async with self._lock:
            if ip in self._in_use:
                self._in_use.discard(ip)
                
                # 汰换：立即生成并安装新IP，保持可用池大小
                new_ip = self._generate_ip()
                if await loop.run_in_executor(None, self._add_ip_to_interface_sync, new_ip):
                    self._available.append(new_ip)
                    logger.debug(f"IP汰换: {ip} -> {new_ip}")
                
                # 延迟清理：10秒后再从网卡删除旧IP，给TCP挥手留出时间
                async def delayed_remove():
                    await asyncio.sleep(10)
                    await loop.run_in_executor(None, self._remove_ip_from_interface_sync, ip)
                
                asyncio.create_task(delayed_remove())

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
        # 移除 asyncio.Lock 的同步使用，因为 it is not thread-safe to use sync with on an async lock
        # 而且 get_stats 是在主事件循环线程运行，且只是读操作
        utilization = 0
        if self.pool_size > 0:
            utilization = (len(self._in_use) / self.pool_size) * 100
            
        return {
            'total': self.pool_size,
            'available': len(self._available),
            'in_use': len(self._in_use),
            'utilization': f"{utilization:.2f}%"
        }

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
