import argparse
import asyncio
import logging

from ipv6_proxy_pool.core.config import ProxyConfig
from ipv6_proxy_pool.server.proxy import IPv6ProxyPoolServer
from ipv6_proxy_pool.cli.commands import setup_ipv6_addresses, clear_ipv6_addresses

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def main():
    parser = argparse.ArgumentParser(
        description='IPv6出口代理池 - 高性能HTTP/HTTPS代理',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 启动代理服务器
  ipv6-proxy-pool --port 8899 --pool-size 1000

  # 配置IPv6地址池（需要root）
  sudo ipv6-proxy-pool --setup-ip --ip-count 1000

  # 使用代理
  export HTTP_PROXY=http://127.0.0.1:8899
  export HTTPS_PROXY=http://127.0.0.1:8899
  python your_script.py
        """
    )

    # 服务器配置
    parser.add_argument('--host', default='0.0.0.0', help='代理绑定地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', '-p', type=int, default=8899, help='代理端口 (默认: 8899)')
    parser.add_argument('--pool-size', type=int, default=1000, help='IPv6地址池大小 (默认: 1000)')
    parser.add_argument('--rate-limit', type=int, default=0, help='每秒请求限速，0表示不限速 (默认: 0)')
    parser.add_argument('--timeout', type=float, default=30.0, help='连接超时时间 (默认: 30s)')
    parser.add_argument('--debug', action='store_true', help='启用调试日志')

    # 指纹混淆配置 (默认开启)
    parser.add_argument('--disable-fp', action='store_true', help='禁用OS/TCP指纹混淆 (TTL, Window Size, Flow Label)')
    parser.add_argument('--ttl-range', default='64,128', help='TTL/Hop Limit 随机范围 (默认: 64,128)')
    parser.add_argument('--win-range', default='65536,131072', help='TCP 窗口大小/缓冲区随机范围 (默认: 65536,131072)')
    parser.add_argument('--no-flow-label', action='store_true', help='禁用 IPv6 Flow Label 随机化')

    # IP配置
    parser.add_argument('--setup-ip', action='store_true', help='配置IPv6地址池到系统（需要root）')
    parser.add_argument('--clear-ip', action='store_true', help='清除配置的IPv6地址（需要root）')
    parser.add_argument('--ip-count', type=int, default=1000, help='配置/清除的IP数量 (默认: 1000)')
    parser.add_argument('--interface', '-i', default=None, help='网络接口 (默认: 自动检测)')

    # 访问控制
    parser.add_argument('--allow-lan', action='store_true', default=True, help='允许局域网访问 (默认: 开启)')
    parser.add_argument('--deny-lan', action='store_false', dest='allow_lan', help='禁止局域网访问')

    args = parser.parse_args()

    # 配置日志
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # IP配置模式
    if args.setup_ip:
        setup_ipv6_addresses(args.ip_count, args.interface)
        return

    if args.clear_ip:
        clear_ipv6_addresses(args.interface)
        return

    # 解析指纹范围
    min_ttl, max_ttl = 64, 128
    try:
        min_ttl, max_ttl = map(int, args.ttl_range.split(','))
    except:
        pass

    win_min, win_max = 65536, 131072
    try:
        win_min, win_max = map(int, args.win_range.split(','))
    except:
        pass

    # 服务器模式
    config = ProxyConfig(
        host=args.host,
        port=args.port,
        pool_size=args.pool_size,
        interface=args.interface,
        rate_limit=args.rate_limit,
        connection_timeout=args.timeout,
        allow_lan=args.allow_lan,
        enable_fingerprint=not args.disable_fp,
        min_ttl=min_ttl,
        max_ttl=max_ttl,
        randomize_flow_label=not args.no_flow_label,
        window_size_min=win_min,
        window_size_max=win_max
    )

    async def run_server():
        server = IPv6ProxyPoolServer(config)
        try:
            await server.start()
        finally:
            await server.stop()

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
