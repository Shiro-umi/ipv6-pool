# IPv6 出口代理池 (Anti-Detection Optimized)

高性能 HTTP/HTTPS 代理服务器，支持**即用即弃**的 IPv6 地址池模式，并针对**抗指纹识别 (TLS/HTTP2 Fingerprinting)** 进行了优化。

## 功能特性

- **即用即弃模式**: 每个 IPv6 地址只用一次，用完立即丢弃并生成新地址，永不重复
- **动态网卡配置**: 自动将新 IP 添加到网卡，用完自动删除，无需手动预配置
- **抗特征指纹**: 推荐配合 `curl_cffi` 使用，模拟真实浏览器 (Chrome/Safari) 的 TLS 握手 (JA3/JA4) 和 HTTP/2 指纹
- **头部顺序保护**: 代理逻辑严格保持原始请求头部顺序，确保符合浏览器特征
- **HTTP/HTTPS 代理**: 完整支持 CONNECT 方法和普通 HTTP 代理
- **双栈支持**: 自动适配 IPv4/IPv6 目标站点
- **速率限制**: 内置令牌桶限流器，防止过载
- **统计监控**: 实时统计请求数、成功率、IP 使用情况

## 核心理念：对抗高置信度特征

在现代爬虫对抗中，仅仅更换 IP 已不足够。目标站点（如 Cloudflare, Akamai）会通过以下特征识别代理：

1.  **TLS 指纹 (JA3/JA4)**: 标准 Python `requests` 的 TLS 握手特征非常明显。
2.  **HTTP/2 指纹**: HTTP/2 的窗口大小、优先级等设置也是重要特征。
3.  **HTTP 头部顺序**: 真实浏览器有固定的头部排列顺序。

**本项目的优化方案：**
- **客户端层**: 强制推荐使用 `curl_cffi` 替代 `requests`。
- **代理层**: 采用透明转发模式，不插入任何 `Proxy-` 或 `X-Forwarded-` 头部，且严格保持 Header 原始顺序。

## 快速开始

### 1. 安装依赖

```bash
# 使用 uv 安装 (推荐)
uv sync

# 或者使用 pip
pip install curl_cffi
```

### 2. 启动代理服务器 (需要 root)

```bash
sudo uv run python ipv6_proxy_pool.py --port 8899 --pool-size 1000
```

### 3. 编写抗探测爬虫 (Python 示例)

```python
import curl_cffi.requests as requests

proxies = {
    "http": "http://127.0.0.1:8899",
    "https": "http://127.0.0.1:8899",
}

# 使用 impersonate="chrome120" 完美模拟 Chrome 浏览器指纹
response = requests.get(
    "https://ja3er.com/json", 
    proxies=proxies,
    impersonate="chrome120",
    timeout=30
)

print(f"状态码: {response.status_code}")
print(f"JA3 Hash: {response.json().get('ja3_hash')}")
```

## 命令行参数

```
python ipv6_proxy_pool.py [选项]

服务器选项:
  --host HOST           绑定地址 (默认: 0.0.0.0)
  --port, -p PORT       代理端口 (默认: 8899)
  --pool-size SIZE      IPv6地址池大小 (默认: 1000)
  --ipv6-prefix PREFIX  IPv6地址前缀 (默认: fd00::)
  --interface, -i IF    网卡接口 (默认: lo)
  --rate-limit RPS      每秒请求限速 (默认: 0)
```

## 使用示例

### 基础使用

详见 `examples/basic_usage.py`。该示例展示了如何使用 `curl_cffi` 的 `impersonate` 功能来绕过高级检测。

### 局域网共享

详见 `examples/lan_usage.py`。

## 管理接口

代理启动后，可通过管理接口查询状态（端口 = 代理端口 + 1）：

```bash
# 查看统计信息
curl http://127.0.0.1:8900/stats
```

## 性能调优

### 系统内核参数优化 (Linux)

为支持超大规模并发，建议调整以下参数：

```bash
# 增加文件描述符
ulimit -n 65535

# 优化 TCP 栈
sudo sysctl -w net.ipv4.tcp_tw_reuse=1
sudo sysctl -w net.core.somaxconn=65535
```

## 许可证

MIT License
