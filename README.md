# IPv6 出口代理池

高性能 HTTP/HTTPS 代理服务器，支持**即用即弃**的 IPv6 地址池模式，用于绕过目标站点的频率限制。

## 功能特性

- **即用即弃模式**: 每个 IPv6 地址只用一次，用完立即丢弃并生成新地址，永不重复
- **动态网卡配置**: 自动将新 IP 添加到网卡，用完自动删除，无需手动预配置
- **HTTP/HTTPS 代理**: 完整支持 CONNECT 方法和普通 HTTP 代理
- **双栈支持**: 自动适配 IPv4/IPv6 目标站点
- **连接复用**: 高效的异步 I/O，支持高并发连接
- **速率限制**: 内置令牌桶限流器，防止过载
- **统计监控**: 实时统计请求数、成功率、IP 使用情况
- **管理接口**: HTTP API 查询运行状态和统计信息
- **访问控制**: 支持 IP 白名单、局域网访问控制

## 项目结构

```
ipv6_proxy_pool/
├── ipv6_proxy_pool.py      # 主程序（推荐）
├── ipv6_proxy.py           # 简化版（单文件，嵌入式使用）
├── start.sh                # 快速启动脚本（uv）
├── test_proxy.py           # 测试脚本
├── pyproject.toml          # uv 项目配置（推荐）
├── README.md               # 本文档
└── examples/
    ├── basic_usage.py      # 基础使用示例
    └── lan_usage.py        # 局域网使用示例
```

## 快速开始

### 1. 环境要求

```bash
# Python 3.8+
python3 --version

# Linux系统（需要ip命令配置IPv6地址）
which ip

# root权限（用于动态配置IPv6地址）
sudo -v
```

### 2. 安装 uv（推荐）

本项目使用 [uv](https://docs.astral.sh/uv/) 进行依赖管理和运行。

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 验证安装
uv --version
```

### 3. 克隆项目并安装依赖

```bash
# 进入项目目录
cd ipv6_proxy_pool

# 使用 uv 同步依赖（自动创建虚拟环境）
uv sync

# 或只安装核心依赖（无示例依赖）
uv sync --no-dev
```

### 4. 启动代理服务器（需要 root）

**注意**: 由于需要动态配置 IPv6 地址到网卡，必须使用 root 权限运行。
默认配置已允许局域网访问（绑定到 0.0.0.0）。

```bash
# 方式1: 使用启动脚本（推荐）
sudo ./start.sh

# 方式2: 仅允许本地访问（禁止局域网）
sudo ./start.sh --local-only

# 方式3: 使用 uv 直接运行
sudo uv run python ipv6_proxy_pool.py --port 8899 --pool-size 1000
```

### 5. 使用代理

```bash
# 设置环境变量
export HTTP_PROXY=http://127.0.0.1:8899
export HTTPS_PROXY=http://127.0.0.1:8899

# 使用 uv 运行测试
uv run python examples/basic_usage.py
```

## 命令行参数

```
python ipv6_proxy_pool.py [选项]

服务器选项:
  --host HOST           绑定地址 (默认: 0.0.0.0，允许局域网访问)
  --port, -p PORT       代理端口 (默认: 8899)
  --pool-size SIZE      IPv6地址池大小 (默认: 1000)
  --ipv6-prefix PREFIX  IPv6地址前缀 (默认: fd00::)
  --interface, -i IF    网卡接口 (默认: lo)
  --rate-limit RPS      每秒请求限速，0表示不限速 (默认: 0)
  --timeout SECONDS     连接超时时间 (默认: 30s)
  --debug               启用调试日志

访问控制选项:
  --allow-lan           允许局域网设备访问 (默认: 开启)
  --deny-lan            禁止局域网访问，仅本地127.0.0.1
  --allowed-ips IPS     IP白名单，逗号分隔 (如: 192.168.1.0/24,10.0.0.5)

IP配置选项（旧版预配置模式）:
  --setup-ip            预配置IPv6地址池到系统（需要root，新版不需要）
  --clear-ip            清除配置的IPv6地址（需要root）
  --ip-count COUNT      配置/清除的IP数量 (默认: 100)
```

## uv 使用指南

本项目使用 [uv](https://docs.astral.sh/uv/) 进行 Python 环境管理。

### 常用命令

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 进入项目目录
cd ipv6_proxy_pool

# 同步依赖（根据 pyproject.toml）
uv sync

# 运行程序
uv run python ipv6_proxy_pool.py --port 8899

# 运行示例
uv run python examples/basic_usage.py

# 运行测试
uv run pytest

# 安装额外依赖（示例用）
uv sync --extra examples

# 安装开发依赖
uv sync --extra dev

# 更新依赖
uv lock
uv sync

# 添加新依赖
uv add requests
uv add --dev pytest

# 查看虚拟环境信息
uv pip list
```

## 使用示例

### 基础 HTTP 代理

```python
import requests

proxies = {
    "http": "http://127.0.0.1:8899",
    "https": "http://127.0.0.1:8899",
}

response = requests.get("https://httpbin.org/ip", proxies=proxies)
print(response.text)
```

### 并发请求

```python
from concurrent.futures import ThreadPoolExecutor
import requests

proxies = {"http": "http://127.0.0.1:8899", "https": "http://127.0.0.1:8899"}

def fetch(url):
    return requests.get(url, proxies=proxies, timeout=30)

urls = ["http://httpbin.org/get"] * 10
with ThreadPoolExecutor(max_workers=10) as executor:
    results = list(executor.map(fetch, urls))
```

## 管理接口

代理启动后，可通过管理接口查询状态（端口 = 代理端口 + 1）：

```bash
# 查看统计信息
curl --noproxy "127.0.0.1" http://127.0.0.1:8900/stats

# 健康检查
curl --noproxy "127.0.0.1" http://127.0.0.1:8900/health
```

### 统计信息示例

```json
{
  "proxy_stats": {
    "uptime_seconds": 3600,
    "total_requests": 10000,
    "successful_requests": 9995,
    "failed_requests": 5,
    "success_rate": "99.95%",
    "bytes_transferred_mb": "50.23MB",
    "active_connections": 15,
    "peak_connections": 100,
    "ip_pool_usage": 50,
    "top_targets": {
      "httpbin.org:80": {"requests": 5000, "success": 4995},
      "api.example.com:443": {"requests": 3000, "success": 3000}
    }
  },
  "ip_pool": {
    "total": 1000,
    "available": 950,
    "in_use": 50,
    "utilization": "5.00%"
  },
  "timestamp": "2026-04-01T17:35:56.744988"
}
```

## 工作原理

### 即用即弃模式

本代理采用创新的**即用即弃**模式管理 IPv6 地址：

1. **IP 池初始化**: 生成 1000 个随机 IPv6 地址（fd00::/64 范围）
2. **动态网卡配置**: 启动时自动将 IP 添加到网卡（lo 接口）
3. **用完即弃**: 每个请求使用一个 IP，完成后立即从网卡删除该 IP
4. **即时补充**: 删除旧 IP 的同时，生成新 IP 并添加到网卡，保持 1000 个可用
5. **永不重复**: 每个 IP 只用一次，极大降低被封概率

```
┌─────────────┐      HTTP/HTTPS      ┌─────────────────┐      即用即弃IPv6       ┌─────────────┐
│   客户端    │  ───────────────────> │  IPv6代理池      │  ───────────────────> │  目标站点   │
│             │   (http://127.0.0.1)  │  (本机:8899)     │   (每次请求新IP)      │             │
└─────────────┘                       └─────────────────┘                      └─────────────┘
                                             │
                                             ▼
                              ┌─────────────────────────┐
                              │    动态网卡配置管理      │
                              │  ip addr add/del 自动    │
                              └─────────────────────────┘
```

### 地址轮换流程

```
请求 #1: 使用 fd00::1234 -> 发送请求 -> 删除 fd00::1234 -> 添加 fd00::5678
请求 #2: 使用 fd00::5678 -> 发送请求 -> 删除 fd00::5678 -> 添加 fd00::9abc
请求 #3: 使用 fd00::9abc -> 发送请求 -> 删除 fd00::9abc -> 添加 fd00::def0
...
```

### 与传统模式的区别

| 特性 | 传统模式 | 即用即弃模式（本代理） |
|------|----------|------------------------|
| IP 复用 | 1000 个 IP 循环使用 | 每个 IP 只用一次 |
| 被封风险 | 高（IP 会被记住） | 极低（永不重复） |
| 网卡配置 | 预配置 1000 个 IP | 动态增删，保持 1000 个 |
| 权限要求 | 需要 root 预配置 | 需要 root 运行动态配置 |
| 清理工作 | 需要手动清理旧 IP | 自动清理，网卡干净 |

## 局域网访问

代理默认只监听本地接口 (127.0.0.1)。要允许局域网其他设备使用：

### 快速启动（局域网模式）

```bash
# 使用启动脚本
./start.sh --lan

# 或直接启动
uv run python ipv6_proxy_pool.py --bind-all --allow-lan --port 8899
```

### 访问控制

```bash
# 允许所有局域网设备
uv run python ipv6_proxy_pool.py --bind-all --allow-lan

# 只允许特定网段
uv run python ipv6_proxy_pool.py --bind-all --allowed-ips "192.168.1.0/24"

# 只允许特定IP
uv run python ipv6_proxy_pool.py --bind-all --allowed-ips "192.168.1.100,192.168.1.101"

# 混合使用
uv run python ipv6_proxy_pool.py --bind-all --allow-lan --allowed-ips "10.0.0.0/8"
```

### 局域网客户端配置

假设代理服务器IP是 `192.168.1.10`：

**Linux/macOS:**
```bash
export HTTP_PROXY=http://192.168.1.10:8899
export HTTPS_PROXY=http://192.168.1.10:8899
```

**Windows CMD:**
```cmd
set HTTP_PROXY=http://192.168.1.10:8899
set HTTPS_PROXY=http://192.168.1.10:8899
```

**Windows PowerShell:**
```powershell
$env:HTTP_PROXY="http://192.168.1.10:8899"
$env:HTTPS_PROXY="http://192.168.1.10:8899"
```

**Python:**
```python
import requests
proxies = {
    "http": "http://192.168.1.10:8899",
    "https": "http://192.168.1.10:8899"
}
response = requests.get("https://httpbin.org/get", proxies=proxies)
```

### 安全注意事项

- **不要直接暴露到公网**：仅在受信任的局域网内使用
- **使用防火墙**：配合 iptables/ufw 限制访问
- **启用IP白名单**：限制可访问的客户端IP

```bash
# ufw 示例：只允许特定网段访问
sudo ufw allow from 192.168.1.0/24 to any port 8899

# iptables 示例
sudo iptables -A INPUT -p tcp --dport 8899 -s 192.168.1.0/24 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8899 -j DROP
```

## 性能优化

### 即用即弃模式的性能考虑

由于每个请求都涉及 `ip addr add/del` 系统调用，在高并发场景下可能成为瓶颈：

```bash
# 大池子 + 适当限速，减少系统调用频率
sudo uv run python ipv6_proxy_pool.py \
    --port 8899 \
    --pool-size 5000 \
    --rate-limit 500 \
    --timeout 60
```

### 系统调优

```bash
# 增加文件描述符限制
ulimit -n 65535

# 写入 /etc/security/limits.conf
* soft nofile 65535
* hard nofile 65535

# TCP 优化（临时）
sysctl -w net.ipv4.tcp_tw_reuse=1
sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sysctl -w net.core.somaxconn=65535

# TCP 优化（永久，写入 /etc/sysctl.conf）
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 1024 65535
net.core.somaxconn = 65535
```

### 高并发场景

```bash
# 超大并发（需要root调整系统限制）
sudo ulimit -n 100000
sudo uv run python ipv6_proxy_pool.py \
    --port 8899 \
    --pool-size 10000 \
    --rate-limit 2000
```

## 故障排查

### 1. 权限被拒绝

**症状:** "Permission denied" 或 "Operation not permitted"

**原因:** 动态配置 IPv6 地址需要 root 权限

**解决:**
```bash
# 必须使用 sudo 运行
sudo ./start.sh
# 或
sudo uv run python ipv6_proxy_pool.py --port 8899
```

### 2. IPv6 地址绑定失败

**症状:** 日志显示 "绑定 fd00::xxx 失败" 或 "Cannot assign requested address"

**原因:** 网卡上没有对应的 IPv6 地址

**解决:**
```bash
# 检查网卡上的 IPv6 地址
ip -6 addr show dev lo | grep fd00

# 应该看到 1000 个地址在动态变化
# 如果为空，检查代理是否有权限添加地址
```

### 3. 连接目标超时

**症状:** 大量 502 Bad Gateway 错误

**原因:** 网络超时或目标不可达

**解决:**
```bash
# 增加超时时间
sudo uv run python ipv6_proxy_pool.py --timeout 60

# 测试网络连通性
curl -v https://目标网站
```

### 4. 速率限制太严格

**症状:** 请求响应慢或大量等待

**解决:**
```bash
# 取消限速
sudo uv run python ipv6_proxy_pool.py --rate-limit 0

# 或提高限速
sudo uv run python ipv6_proxy_pool.py --rate-limit 1000
```

### 5. 端口被占用

**症状:** "Address already in use" 错误

**解决:**
```bash
# 查找占用端口的进程
sudo lsof -i :8899

# 或更换端口
sudo uv run python ipv6_proxy_pool.py --port 8080
```

### 6. 局域网无法访问

**症状:** 其他设备无法连接代理

**检查步骤:**
```bash
# 1. 确认代理监听地址
sudo netstat -tlnp | grep 8899
# 应该显示 0.0.0.0:8899 或具体IP

# 2. 检查防火墙
sudo iptables -L -n | grep 8899

# 3. 测试连通性
# 从其他设备
telnet 代理服务器IP 8899
```

## 许可证

MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
