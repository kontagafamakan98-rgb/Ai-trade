# utils/retry.py
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import httpx
import asyncio
from functools import wraps

def retry_async(max_attempts=4, multiplier=1):
    """Décorateur retry pour fonctions async"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retryer = retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=multiplier, min=2, max=30),
                retry=retry_if_exception_type((httpx.RequestError, Exception)),
                reraise=True,
            )
            return await retryer(func)(*args, **kwargs)
        return wrapper
    return decorator