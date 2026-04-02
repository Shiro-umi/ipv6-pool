# IPv6 出口代理池 (Anti-Detection Optimized)

高性能 HTTP/HTTPS 代理服务器，支持**即用即弃**的 IPv6 地址池模式，针对**抗指纹识别 (TLS/HTTP2/HTTP Header Fingerprinting)** 进行了底层深度优化。

## 功能特性

- **即用即弃模式 (Use-and-Discard)**: 每个 IPv6 地址只用一次，用完立即从网卡删除并生成新地址，永不重复。
- **零延迟 IP 池管理**: 
    - **预装机制**: 启动时自动将数千个随机 IPv6 地址预安装到网卡，`acquire` 操作为 $O(1)$ 内存操作，无 subprocess 开销，支持高并发。
    - **动态前缀探测**: 自动检测 ISP 分发的 IPv6 前缀长度（如 /48, /56, /64），自适应生成合法路由地址。
- **字节级 Header 保护 (Byte-level Preservation)**:
    - **指纹级转发**: 采用字节切片技术，完美保留原始 HTTP Header 的**顺序、大小写、空格排版及换行符**，确保在 Akamai/Cloudflare 等高级检测系统面前表现如真实浏览器。
- **抗特征指纹**: 
    - **应用层**: 推荐配合 `curl_cffi` 使用，模拟真实浏览器 (Chrome/Safari) 的 TLS 握手 (JA3/JA4) 和 HTTP/2 指纹。
    - **传输层 (TCP)**: 随机化 TCP 窗口大小 (Window Size)，模拟不同操作系统的初始握手特征。
    - **网络层 (IP)**: 随机化 TTL / Hop Limit 以及 IPv6 Flow Label，防止通过协议栈识别代理服务器。
- **访问控制 (ACL)**: 内置 IP 过滤机制，默认支持局域网白名单（192.168.x.x 等），防止代理被滥用。
- **异常安全回收**: 完善的信号处理机制，确保在程序退出（Ctrl+C）或崩溃时，自动清理网卡上的所有临时 IPv6 地址。
- **双栈智能路由**: 自动获取 A/AAAA 记录，优先尝试 IPv6 连接并支持透明回退到 IPv4。

## 核心理念：对抗高置信度特征

在现代爬虫对抗中，仅仅更换 IP 已不足够。目标站点（如 Cloudflare, Akamai）会通过以下特征识别代理：

1.  **TLS 指纹 (JA3/JA4)**: 标准 Python `requests` 的 TLS 握手特征非常明显。
2.  **HTTP/2 指纹**: HTTP/2 的窗口大小、优先级等设置也是重要特征。
3.  **HTTP 头部细节**: 真实浏览器有固定的头部排列顺序，甚至特定的空格和大小写（如 `User-Agent` 与 `user-agent`）。
4.  **OS TCP 指纹**: 通过 TCP Window Size, TTL, IPv6 Flow Label 等底层字段识别服务器操作系统特征。

**本项目的优化方案：**
- **客户端层**: 强制推荐使用 `curl_cffi` 替代 `requests`。
- **代理层**: 采用**字节级透明转发**模式，不插入任何 `Proxy-` 头部，且严格保持 Header 原始字节细节。
- **协议栈层**: 动态随机化每一个外发连接的底层协议字段（TTL, WinSize, FlowLabel），使流量在网关层看起来像来自成千上万个不同的移动或桌面端设备。

## 快速开始

### 1. 安装依赖

```bash
# 使用 uv 安装 (推荐)
uv sync

# 或者使用 pip
pip install curl_cffi
```

### 2. 启动代理服务器 (需要 root 权限以管理网卡)

```bash
# 启动代理，绑定端口 8899，建立 1000 个地址的 IP 池
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
  --pool-size SIZE      IPv6 地址池大小 (默认: 1000)
  --interface, -i IF    网卡接口 (默认: 自动检测)
  --rate-limit RPS      每秒请求限速 (默认: 0)
  --disable-fp          禁用 OS/TCP 指纹随机化混淆 (默认: 开启)
  --ttl-range MIN,MAX   TTL/Hop Limit 随机范围 (默认: 64,128)
  --win-range MIN,MAX   TCP 窗口大小随机范围 (默认: 65536,131072)
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
