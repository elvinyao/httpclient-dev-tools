"""Create native HTTPX clients configured with httpx-retries."""

from __future__ import annotations

from collections.abc import Collection
from typing import Optional, Union

import httpx as _httpx
from httpx_retries import Retry, RetryTransport

_DEFAULT_ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_STATUS_FORCELIST = frozenset({429, 500, 502, 503, 504})
_DEFAULT_RETRY_EXCEPTIONS = (
    _httpx.ConnectError,
    _httpx.ConnectTimeout,
    _httpx.ReadError,
    _httpx.ReadTimeout,
    _httpx.RemoteProtocolError,
    _httpx.ProxyError,
)

# httpx-retries 0.4.6 replaces an empty status_forcelist with its own defaults.
# A status outside the HTTP range preserves an explicit "no status retries"
# policy across Retry.increment() without maintaining a custom Retry subclass.
_NO_RETRY_STATUS = -1

_Timeout = Union[
    float,
    _httpx.Timeout,
    tuple[Optional[float], Optional[float]],
    tuple[Optional[float], Optional[float], Optional[float]],
    tuple[Optional[float], Optional[float], Optional[float], Optional[float]],
]


def create_retry(
    *,
    total: int = 3,
    allowed_methods: Collection[str] = _DEFAULT_ALLOWED_METHODS,
    status_forcelist: Optional[Collection[int]] = _DEFAULT_STATUS_FORCELIST,
    retry_on_exceptions: Optional[Collection[type[Exception]]] = _DEFAULT_RETRY_EXCEPTIONS,
    backoff_factor: float = 0.5,
) -> Retry:
    """Return a conservative HTTPX retry policy for common API calls."""

    if not allowed_methods:
        raise ValueError("allowed_methods must not be empty; use total=0 to disable retries")

    statuses: Collection[int] = status_forcelist or (_NO_RETRY_STATUS,)
    exceptions: Collection[type[Exception]] = retry_on_exceptions or ()

    return Retry(
        total=total,
        allowed_methods=allowed_methods,
        status_forcelist=statuses,
        retry_on_exceptions=exceptions,
        backoff_factor=backoff_factor,
        respect_retry_after_header=True,
        backoff_jitter=0.0,
    )


def _require_retry(retry: Retry) -> None:
    if not isinstance(retry, Retry):
        raise TypeError("retry must be an httpx_retries.Retry")


def _normalize_timeout(timeout: Optional[_Timeout]) -> Optional[Union[float, _httpx.Timeout]]:
    """Convert supported tuples without relying on HTTPX's private tuple parsing."""

    if not isinstance(timeout, tuple):
        return timeout

    if len(timeout) == 2:
        connect, read = timeout
        write = pool = None
    elif len(timeout) == 3:
        connect, read, write = timeout
        pool = None
    elif len(timeout) == 4:
        connect, read, write, pool = timeout
    else:
        raise TypeError("timeout tuple must contain 2, 3, or 4 values")

    return _httpx.Timeout(
        connect=connect,
        read=read,
        write=write,
        pool=pool,
    )


def _sync_transport(transport: Optional[_httpx.BaseTransport]) -> _httpx.BaseTransport:
    """Return one unwrapped synchronous transport for RetryTransport."""

    if isinstance(transport, RetryTransport):
        raise TypeError("transport must be an unwrapped httpx.BaseTransport, not RetryTransport")
    if transport is None:
        return _httpx.HTTPTransport()
    if not isinstance(transport, _httpx.BaseTransport):
        raise TypeError("transport must be an httpx.BaseTransport for create_client")
    return transport


def _async_transport(transport: Optional[_httpx.AsyncBaseTransport]) -> _httpx.AsyncBaseTransport:
    """Return one unwrapped asynchronous transport for RetryTransport."""

    if isinstance(transport, RetryTransport):
        raise TypeError("transport must be an unwrapped httpx.AsyncBaseTransport, not RetryTransport")
    if transport is None:
        return _httpx.AsyncHTTPTransport()
    if not isinstance(transport, _httpx.AsyncBaseTransport):
        raise TypeError("transport must be an httpx.AsyncBaseTransport for create_async_client")
    return transport


def create_client(
    retry: Retry,
    *,
    timeout: Optional[_Timeout] = None,
    transport: Optional[_httpx.BaseTransport] = None,
) -> _httpx.Client:
    """Return a retrying Client that owns its underlying sync transport."""

    _require_retry(retry)
    normalized_timeout = _normalize_timeout(timeout)
    retry_transport = RetryTransport(
        transport=_sync_transport(transport),
        retry=retry,
    )
    return _httpx.Client(transport=retry_transport, timeout=normalized_timeout)


def create_async_client(
    retry: Retry,
    *,
    timeout: Optional[_Timeout] = None,
    transport: Optional[_httpx.AsyncBaseTransport] = None,
) -> _httpx.AsyncClient:
    """Return a retrying AsyncClient that owns its underlying async transport."""

    _require_retry(retry)
    normalized_timeout = _normalize_timeout(timeout)
    retry_transport = RetryTransport(
        transport=_async_transport(transport),
        retry=retry,
    )
    return _httpx.AsyncClient(transport=retry_transport, timeout=normalized_timeout)


__all__ = [
    "Retry",
    "create_async_client",
    "create_client",
    "create_retry",
]
