#!/usr/bin/env python3
"""
测试IPv6代理服务器的可用性
"""

import curl_cffi.requests as requests
import sys
import time
import socket
from concurrent.futures import ThreadPoolExecutor

# 测试配置
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8899
PROXY_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"

# 测试目标URL（使用httpbin.org进行测试）
TEST_URLS = [
    "https://httpbin.org/get",
    "https://httpbin.org/ip",
    "https://ja3er.com/json",  # 测试JA3指纹
]


def test_proxy_with_curl_cffi():
    """使用curl_cffi库测试代理，模拟Chrome浏览器指纹"""
    print("\n" + "=" * 50)
    print("测试1: 使用curl_cffi库测试代理 (模拟Chrome 120)")
    print("=" * 50)

    proxies = {
        "http": PROXY_URL,
        "https": PROXY_URL,
    }

    for url in TEST_URLS:
        try:
            print(f"\n测试请求: {url}")
            start = time.time()
            response = requests.get(
                url,
                proxies=proxies,
                timeout=30,
                impersonate="chrome120",
                verify=False
            )
            elapsed = time.time() - start
            print(f"  状态码: {response.status_code}")
            print(f"  响应时间: {elapsed:.2f}s")
            if response.status_code == 200:
                content = response.text[:200]
                print(f"  响应内容: {content}...")
                if "ja3" in url:
                    print(f"  JA3指纹: {response.json().get('ja3_hash')}")
        except Exception as e:
            print(f"  失败: {e}")


def test_proxy_with_session():
    """使用curl_cffi Session测试代理保持"""
    print("\n" + "=" * 50)
    print("测试2: 使用curl_cffi Session测试连接保持")
    print("=" * 50)

    proxies = {
        "http": PROXY_URL,
        "https": PROXY_URL,
    }

    session = requests.Session(impersonate="chrome120")

    for i in range(3):
        try:
            print(f"\n请求 #{i+1}:")
            response = session.get(
                "https://httpbin.org/get",
                proxies=proxies,
                timeout=30
            )
            print(f"  状态码: {response.status_code}")
        except Exception as e:
            print(f"  失败: {e}")


def test_concurrent_requests():
    """测试并发请求"""
    print("\n" + "=" * 50)
    print("测试3: 并发请求测试 (10个并发)")
    print("=" * 50)

    proxies = {
        "http": PROXY_URL,
        "https": PROXY_URL,
    }

    def fetch(i):
        try:
            response = requests.get(
                "https://httpbin.org/get",
                proxies=proxies,
                timeout=30,
                impersonate="chrome120"
            )
            return i, response.status_code, None
        except Exception as e:
            return i, None, str(e)

    start = time.time()
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(fetch, range(10)))
    elapsed = time.time() - start

    success = sum(1 for _, status, _ in results if status == 200)
    print(f"\n总请求数: 10")
    print(f"成功数: {success}")
    print(f"失败数: {10 - success}")
    print(f"总耗时: {elapsed:.2f}s")
    print(f"平均耗时: {elapsed/10:.2f}s")


def test_direct_vs_proxy():
    """对比直接请求和代理请求"""
    print("\n" + "=" * 50)
    print("测试4: 直接请求 vs 代理请求 对比 (带JA3指纹验证)")
    print("=" * 50)

    url = "https://httpbin.org/get"

    print("\n直接请求:")
    try:
        start = time.time()
        response = requests.get(url, timeout=30, impersonate="chrome120")
        elapsed = time.time() - start
        print(f"  状态码: {response.status_code}")
        print(f"  响应时间: {elapsed:.2f}s")
    except Exception as e:
        print(f"  失败: {e}")

    print("\n代理请求:")
    try:
        start = time.time()
        response = requests.get(
            url,
            proxies={"http": PROXY_URL, "https": PROXY_URL},
            timeout=30,
            impersonate="chrome120"
        )
        elapsed = time.time() - start
        print(f"  状态码: {response.status_code}")
        print(f"  响应时间: {elapsed:.2f}s")
    except Exception as e:
        print(f"  失败: {e}")


def check_proxy_process():
    """检查代理进程是否在运行"""
    print("\n" + "=" * 50)
    print("检查代理服务器状态")
    print("=" * 50)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((PROXY_HOST, PROXY_PORT))
        sock.close()

        if result == 0:
            print(f"✓ 代理服务器正在运行: {PROXY_HOST}:{PROXY_PORT}")
            return True
        else:
            print(f"✗ 代理服务器未运行: {PROXY_HOST}:{PROXY_PORT}")
            print("\n请先启动代理服务器:")
            print(f"  ipv6-proxy-pool --port {PROXY_PORT}")
            return False
    except Exception as e:
        print(f"✗ 检查失败: {e}")
        return False


def main():
    print("=" * 50)
    print("IPv6代理服务器测试程序 (支持指纹伪造)")
    print("=" * 50)

    if not check_proxy_process():
        sys.exit(1)

    try:
        test_proxy_with_curl_cffi()
        test_proxy_with_session()
        test_concurrent_requests()
        test_direct_vs_proxy()

        print("\n" + "=" * 50)
        print("所有测试完成!")
        print("=" * 50)

    except KeyboardInterrupt:
        print("\n测试被中断")
    except Exception as e:
        print(f"\n测试出错: {e}")


if __name__ == '__main__':
    main()

