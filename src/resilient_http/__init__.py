"""Public API for the resilient HTTP client."""

from urllib3.util import Retry

from .client import HttpClient, create_session
from .config import (
    ErrorMappingPolicy,
    ErrorMappingRule,
    HttpClientConfig,
    PoolConfig,
    RetryConfig,
    TimeoutConfig,
    retry_from_dict,
)
from .exceptions import (
    BaseHttpError,
    BusinessHttpError,
    NonReplayableRequestError,
    SystemHttpError,
)

__all__ = [
    "BaseHttpError",
    "BusinessHttpError",
    "ErrorMappingPolicy",
    "ErrorMappingRule",
    "HttpClient",
    "HttpClientConfig",
    "NonReplayableRequestError",
    "PoolConfig",
    "Retry",
    "RetryConfig",
    "SystemHttpError",
    "TimeoutConfig",
    "create_session",
    "retry_from_dict",
]
