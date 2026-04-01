#!/usr/bin/env python3
"""
IPv6代理池基础使用示例
"""

import requests
import os

# 配置代理
PROXY_URL = "http://127.0.0.1:8899"
os.environ['HTTP_PROXY'] = PROXY_URL
os.environ['HTTPS_PROXY'] = PROXY_URL


def test_http_request():
    """测试HTTP请求"""
    print("测试HTTP请求...")
    response = requests.get("http://httpbin.org/get", timeout=30)
    print(f"状态码: {response.status_code}")
    print(f"响应: {response.text[:200]}...")


def test_https_request():
    """测试HTTPS请求"""
    print("\n测试HTTPS请求...")
    response = requests.get("https://httpbin.org/get", timeout=30)
    print(f"状态码: {response.status_code}")
    print(f"响应: {response.text[:200]}...")


def test_session():
    """测试Session保持"""
    print("\n测试Session...")
    session = requests.Session()

    for i in range(3):
        response = session.get("http://httpbin.org/get", timeout=30)
        print(f"请求 #{i+1}: 状态码 {response.status_code}")


if __name__ == '__main__':
    print("IPv6代理池基础示例")
    print(f"代理地址: {PROXY_URL}")
    print()

    test_http_request()
    test_https_request()
    test_session()

    print("\n完成!")
