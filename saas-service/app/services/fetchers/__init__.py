from .base import FetchResult
from .hn import fetch_hn
from .stackexchange import fetch_stackexchange
from .ddg import fetch_ddg
from .reddit import fetch_reddit

__all__ = [
    "FetchResult",
    "fetch_hn",
    "fetch_stackexchange",
    "fetch_ddg",
    "fetch_reddit",
]