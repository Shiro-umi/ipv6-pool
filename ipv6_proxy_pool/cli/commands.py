import ipaddress
import random
import subprocess
from typing import Optional

from ipv6_proxy_pool.utils.network import _get_global_ipv6_prefix, _get_default_ipv6_interface

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
            base = str(prefix).rstrip(':').rstrip('/')
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
