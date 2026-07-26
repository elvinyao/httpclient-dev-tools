"""Public API for the resilient HTTP client."""

from ._vendor.httpx_retries import Retry
from .client import AsyncHttpClient, HttpClient
from .config import (
    ErrorMappingPolicy,
    ErrorMappingRule,
    HttpClientConfig,
    retry_from_dict,
)
from .exceptions import (
    BaseHttpError,
    BusinessHttpError,
    NonReplayableRequestError,
    SystemHttpError,
)

__all__ = [
    "AsyncHttpClient",
    "BaseHttpError",
    "BusinessHttpError",
    "ErrorMappingPolicy",
    "ErrorMappingRule",
    "HttpClient",
    "HttpClientConfig",
    "NonReplayableRequestError",
    "Retry",
    "SystemHttpError",
    "retry_from_dict",
]
