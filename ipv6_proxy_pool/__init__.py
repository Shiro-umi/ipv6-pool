# IPv6 Proxy Pool
__version__ = "1.0.0"

def main():
    """CLI entry point helper to avoid early imports"""
    from ipv6_proxy_pool.cli.main import main as _main
    _main()
