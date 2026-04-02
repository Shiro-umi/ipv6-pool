#!/usr/bin/env python3
"""
IPv6代理池基础使用示例 (抗特征指纹版)
"""

import curl_cffi.requests as requests
import os

# 配置代理
PROXY_URL = "http://127.0.0.1:8899"
os.environ['HTTP_PROXY'] = PROXY_URL
os.environ['HTTPS_PROXY'] = PROXY_URL


def test_http_request():
    """测试HTTP请求"""
    print("测试HTTP请求 (模拟Chrome指纹)...")
    # 使用 impersonate 参数伪装浏览器 TLS/HTTP2 指纹
    response = requests.get(
        "https://httpbin.org/get", 
        timeout=30, 
        impersonate="chrome120"
    )
    print(f"状态码: {response.status_code}")
    print(f"响应: {response.text[:200]}...")


def test_https_request():
    """测试HTTPS请求"""
    print("\n测试HTTPS请求 (模拟Safari指纹)...")
    response = requests.get(
        "https://httpbin.org/get", 
        timeout=30, 
        impersonate="safari15_5"
    )
    print(f"状态码: {response.status_code}")
    print(f"响应: {response.text[:200]}...")


def test_session():
    """测试Session保持"""
    print("\n测试Session (模拟Chrome指纹)...")
    # 在 Session 层级设置指纹
    session = requests.Session(impersonate="chrome120")

    for i in range(3):
        response = session.get("https://httpbin.org/get", timeout=30)
        print(f"请求 #{i+1}: 状态码 {response.status_code}")


if __name__ == '__main__':
    print("IPv6代理池基础示例 (支持 TLS/HTTP2 指纹伪造)")
    print(f"代理地址: {PROXY_URL}")
    print()

    test_http_request()
    test_https_request()
    test_session()

    print("\n完成!")
