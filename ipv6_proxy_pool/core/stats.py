from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict

@dataclass
class ConnectionStats:
    """连接统计"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    bytes_transferred: int = 0
    active_connections: int = 0
    peak_connections: int = 0
    ip_usage: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    target_stats: Dict[str, Dict[str, int]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    start_time: datetime = field(default_factory=datetime.now)

    def record_request(self, success: bool, ip: str, target: str, bytes_count: int = 0):
        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
        self.bytes_transferred += bytes_count
        self.ip_usage[ip] += 1
        self.target_stats[target]['requests'] += 1
        if success:
            self.target_stats[target]['success'] += 1
        else:
            self.target_stats[target]['failed'] += 1

    def connection_started(self):
        self.active_connections += 1
        self.peak_connections = max(self.peak_connections, self.active_connections)

    def connection_ended(self):
        self.active_connections = max(0, self.active_connections - 1)

    def to_dict(self) -> dict:
        uptime = datetime.now() - self.start_time
        return {
            'uptime_seconds': uptime.total_seconds(),
            'total_requests': self.total_requests,
            'successful_requests': self.successful_requests,
            'failed_requests': self.failed_requests,
            'success_rate': f"{(self.successful_requests / max(self.total_requests, 1) * 100):.2f}%",
            'bytes_transferred': self.bytes_transferred,
            'bytes_transferred_mb': f"{self.bytes_transferred / (1024*1024):.2f}MB",
            'active_connections': self.active_connections,
            'peak_connections': self.peak_connections,
            'ip_pool_usage': len(self.ip_usage),
            'top_targets': dict(sorted(
                self.target_stats.items(),
                key=lambda x: x[1]['requests'],
                reverse=True
            )[:5])
        }
