import ipaddress
import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger('ipv6_proxy_pool.network')

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
            if re.match(r'^\d+:', line):
                match = re.search(r'^\d+:\s+(\w+):', line)
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
