"""
Retry + backoff exponentiel pour les appels réseau fragiles (Alpaca,
Supabase, market data). Simple et sans dépendance externe.
"""
import time
from typing import Callable, TypeVar, Any

T = TypeVar("T")


def retry_call(
    func: Callable[..., T],
    *args,
    retries: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple = (Exception,),
    label: str = "",
    **kwargs,
) -> T:
    """
    Exécute func(*args, **kwargs), retente jusqu'à `retries` fois avec un
    backoff exponentiel (1s, 2s, 4s...) en cas d'exception.
    Relance la dernière exception si toutes les tentatives échouent.
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except exceptions as e:
            last_exc = e
            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"   ⏳ retry {label or func.__name__} ({attempt}/{retries}) dans {delay:.1f}s — {type(e).__name__}: {e}")
                time.sleep(delay)
            else:
                print(f"   ❌ {label or func.__name__} : échec après {retries} tentatives — {type(e).__name__}: {e}")
    raise last_exc
