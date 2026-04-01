#!/usr/bin/env python3
"""
IPv6代理池 局域网使用示例

本机（代理服务器）运行:
    python ipv6_proxy_pool.py --bind-all --allow-lan --port 8899

局域网其他设备使用:
    export HTTP_PROXY=http://<代理服务器IP>:8899
"""

import requests
import sys


def get_proxy_server_ip():
    """获取代理服务器IP（假设是本机）"""
    import socket
    try:
        # 获取本机局域网IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "192.168.1.x"


def test_lan_proxy():
    """测试通过局域网代理访问"""
    proxy_ip = get_proxy_server_ip()
    proxy_url = f"http://{proxy_ip}:8899"

    print("=" * 50)
    print("IPv6代理池 局域网访问测试")
    print("=" * 50)
    print(f"代理服务器: {proxy_url}")
    print()

    proxies = {
        "http": proxy_url,
        "https": proxy_url,
    }

    try:
        print("测试HTTP请求...")
        response = requests.get(
            "http://httpbin.org/get",
            proxies=proxies,
            timeout=10
        )
        print(f"✓ 成功! 状态码: {response.status_code}")
        print(f"  响应大小: {len(response.text)} bytes")
    except Exception as e:
        print(f"✗ 失败: {e}")
        return False

    try:
        print("\n测试HTTPS请求...")
        response = requests.get(
            "https://httpbin.org/get",
            proxies=proxies,
            timeout=10,
            verify=False
        )
        print(f"✓ 成功! 状态码: {response.status_code}")
    except Exception as e:
        print(f"✗ 失败: {e}")
        return False

    print("\n" + "=" * 50)
    print(f"局域网代理可用!")
    print(f"其他设备可设置: export HTTP_PROXY={proxy_url}")
    print("=" * 50)
    return True


def print_setup_guide():
    """打印设置指南"""
    proxy_ip = get_proxy_server_ip()

    guide = f"""
{'='*60}
IPv6代理池 局域网使用指南
{'='*60}

【代理服务器端（本机）】
1. 确保IPv6地址池已配置:
   sudo python ipv6_proxy_pool.py --setup-ip --ip-count 1000

2. 启动代理（允许局域网访问）:
   python ipv6_proxy_pool.py --bind-all --allow-lan --port 8899

3. 查看代理状态:
   curl http://127.0.0.1:8900/stats

【局域网客户端】

Linux/macOS:
   export HTTP_PROXY=http://{proxy_ip}:8899
   export HTTPS_PROXY=http://{proxy_ip}:8899
   python your_script.py

Windows (CMD):
   set HTTP_PROXY=http://{proxy_ip}:8899
   set HTTPS_PROXY=http://{proxy_ip}:8899

Windows (PowerShell):
   $env:HTTP_PROXY="http://{proxy_ip}:8899"
   $env:HTTPS_PROXY="http://{proxy_ip}:8899"

Python代码:
   import requests
   proxies = {{
       "http": "http://{proxy_ip}:8899",
       "https": "http://{proxy_ip}:8899"
   }}
   response = requests.get("https://httpbin.org/get", proxies=proxies)

{'='*60}
"""
    print(guide)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--guide':
        print_setup_guide()
    else:
        print_setup_guide()
        test_lan_proxy()
