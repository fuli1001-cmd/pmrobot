"""Token bucket rate limiter for API calls."""

import asyncio
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """
    Token bucket rate limiter implementation.
    
    Ensures API calls don't exceed the specified rate limit.
    """

    def __init__(self, rate: float, burst: Optional[int] = None):
        """
        Initialize the rate limiter.

        Args:
            rate: Maximum requests per second
            burst: Maximum burst size (defaults to rate)
        """
        self.rate = rate
        self.burst = burst or int(rate)
        self.tokens = float(self.burst)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """
        Acquire a token, waiting if necessary.
        
        This method blocks until a token is available.
        """
        async with self._lock:
            await self._wait_for_token()
            self.tokens -= 1

    async def _wait_for_token(self) -> None:
        """Wait until at least one token is available."""
        while True:
            self._refill()
            if self.tokens >= 1:
                return
            
            # Calculate wait time for next token
            wait_time = (1 - self.tokens) / self.rate
            logger.debug(
                "Rate limit: waiting for token",
                wait_time=f"{wait_time:.3f}s",
                tokens=self.tokens,
            )
            await asyncio.sleep(wait_time)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_update = now

    @property
    def available_tokens(self) -> float:
        """Get the current number of available tokens."""
        self._refill()
        return self.tokens


class RateLimitedSession:
    """
    Wrapper for making rate-limited HTTP requests.
    """

    def __init__(self, rate: float = 10.0):
        """
        Initialize the rate-limited session.

        Args:
            rate: Maximum requests per second
        """
        self.limiter = RateLimiter(rate)

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        pass

    async def request(self, coro):
        """
        Execute a request with rate limiting.

        Args:
            coro: Coroutine to execute after acquiring rate limit token

        Returns:
            Result of the coroutine
        """
        await self.limiter.acquire()
        return await coro
