"""指数退避重试装饰器。移植自 professional-research/scripts/utils.py:118-148。"""
import functools
import logging
import time

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 5.0,
    max_delay: float = 60.0,
    retryable_exceptions: tuple = (Exception,),
):
    """装饰器：调用失败时按指数退避重试。"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.warning(
                            "[重试 %d/%d] %s: %s — %ss 后重试",
                            attempt + 1, max_retries, type(e).__name__,
                            str(e)[:100], int(delay),
                        )
                        time.sleep(delay)
            raise last
        return wrapper
    return decorator
