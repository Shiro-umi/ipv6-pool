import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger('ipv6_proxy_pool.config')

@dataclass
class ProxyConfig:
    """代理配置"""
    host: str = '0.0.0.0'
    port: int = 8899
    pool_size: int = 1000
    interface: Optional[str] = None
    max_connections_per_ip: int = 10
    connection_timeout: float = 30.0
    read_timeout: float = 60.0
    rate_limit: int = 0
    enable_stats: bool = True
    prefer_ipv6_target: bool = False
    buffer_size: int = 65536
    # 访问控制
    allow_lan: bool = True
    allowed_ips: List[str] = field(default_factory=lambda: ['127.0.0.1', '192.168.0.0/16', '10.0.0.0/8', '172.16.0.0/12'])
    # 指纹混淆配置
    enable_fingerprint: bool = True
    min_ttl: int = 64
    max_ttl: int = 128
    randomize_flow_label: bool = True
    window_size_min: int = 65536
    window_size_max: int = 131072
