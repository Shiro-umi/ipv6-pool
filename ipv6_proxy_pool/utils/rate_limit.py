import asyncio
import time

class RateLimiter:
    """令牌桶速率限制器"""

    def __init__(self, rate: int = 0):
        self.rate = rate  # 每秒请求数，0表示不限速
        self.tokens = rate if rate > 0 else float('inf')
        self.last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """获取一个令牌"""
        if self.rate <= 0:
            return True

        async with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False

    async def wait(self):
        """等待获取令牌"""
        if self.rate <= 0:
            return

        while not await self.acquire():
            await asyncio.sleep(0.001)
