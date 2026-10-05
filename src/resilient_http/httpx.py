"""HTTPX backend: native ``httpx.Client``/``AsyncClient`` with httpx-retries.

Requires the ``httpx`` extra. The ``Retry`` exported here is
``httpx_retries.Retry``, which is *not* interchangeable with the
``urllib3.util.Retry`` exported by the package root. Responses and exceptions
are always native HTTPX objects.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Optional, Union

import httpx as _httpx
from httpx_retries import Retry, RetryTransport

from ._timeout import validate_timeout_value

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

_TIMEOUT_FIELDS = ("connect", "read", "write", "pool")

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
    """Return a conservative ``httpx_retries.Retry`` policy for common API calls.

    The helper always sets ``respect_retry_after_header=True`` and
    ``backoff_jitter=0.0`` (httpx-retries defaults to full jitter). It also
    works around two httpx-retries 0.4.6 behaviors where an empty collection
    silently restores the library's broader defaults.

    Args:
        total: Retries allowed after the first attempt; ``total=3`` sends at
            most four requests. httpx-retries has no per-category budgets, so
            statuses and exceptions share this single limit.
        allowed_methods: Methods that may enter the retry loop at all. Unlike
            urllib3, this gate also applies to connection errors, so a ``POST``
            outside this set is never retried.
        status_forcelist: Statuses that trigger a retry. ``None`` or an empty
            collection disables status retries.
        retry_on_exceptions: Transport exception types that trigger a retry.
            ``None`` or an empty collection disables exception retries.
        backoff_factor: Exponential backoff scale. httpx-retries waits
            ``backoff_factor * 2 ** attempts_made`` seconds, i.e. about 1 s,
            2 s and 4 s for the default ``0.5``, capped at 120 s.

    Returns:
        A new, independent ``httpx_retries.Retry`` instance.

    Raises:
        TypeError: If ``allowed_methods`` is a single string.
        ValueError: If ``allowed_methods`` is empty (use ``total=0`` to disable
            retries), or httpx-retries rejects a value such as a negative
            ``total``/``backoff_factor`` or an unknown method name.
    """

    if isinstance(allowed_methods, str):
        raise TypeError("allowed_methods must be a collection of method names, not a single string")
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
    """Validate a factory timeout and convert tuples to ``httpx.Timeout``.

    ``None`` and native ``httpx.Timeout`` objects pass through unchanged.
    Numbers and tuple items are validated so that zero, negative, NaN or
    non-numeric values fail at factory time instead of on the first request.
    Tuples are converted explicitly rather than relying on HTTPX's private
    tuple parsing.
    """

    if timeout is None or isinstance(timeout, _httpx.Timeout):
        return timeout

    if not isinstance(timeout, tuple):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("timeout must be a number, a 2/3/4-item tuple, httpx.Timeout, or None")
        validate_timeout_value(timeout, name="timeout")
        return timeout

    if len(timeout) not in (2, 3, 4):
        raise TypeError("timeout tuple must contain 2, 3, or 4 values")

    values = [validate_timeout_value(value, name=f"{field} timeout") for field, value in zip(_TIMEOUT_FIELDS, timeout)]
    # Missing trailing fields (write/pool) mean "no limit".
    values.extend([None] * (len(_TIMEOUT_FIELDS) - len(values)))
    connect, read, write, pool = values
    return _httpx.Timeout(connect=connect, read=read, write=write, pool=pool)


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
    """Return a new ``httpx.Client`` whose transport retries with ``retry``.

    All arguments are validated before any transport or connection pool is
    created, so a bad argument never leaks resources.

    Args:
        retry: The ``httpx_retries.Retry`` policy, typically from
            :func:`create_retry`.
        timeout: Client default timeout. Accepts a number, a 2/3/4-item tuple
            mapped to ``(connect, read, write, pool)`` with missing fields set
            to ``None``, or a native ``httpx.Timeout``. ``None`` (the default)
            disables HTTPX's built-in 5 second timeout so that no hidden
            timeout applies. Limits apply to each attempt, not the whole call.
        transport: Optional unwrapped native ``httpx.BaseTransport`` (for
            proxies, TLS, HTTP/2 or pool limits). Ownership moves to the
            returned client, which closes it. Never share one transport between
            factory calls.

    Returns:
        A native ``httpx.Client``.

    Raises:
        TypeError: If ``retry`` is not an ``httpx_retries.Retry``, ``timeout``
            has an unsupported type or tuple length, or ``transport`` is not a
            synchronous transport or is already a ``RetryTransport``.
        ValueError: If a timeout number is zero, negative, NaN or infinite.
    """

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
    """Return a new ``httpx.AsyncClient`` whose transport retries with ``retry``.

    Backoff sleeps use ``asyncio.sleep`` and never block the event loop. The
    arguments behave exactly like :func:`create_client`, except that
    ``transport`` must be an ``httpx.AsyncBaseTransport``.

    Args:
        retry: The ``httpx_retries.Retry`` policy.
        timeout: Client default timeout; see :func:`create_client`.
        transport: Optional unwrapped native ``httpx.AsyncBaseTransport``.

    Returns:
        A native ``httpx.AsyncClient``.

    Raises:
        TypeError: See :func:`create_client`.
        ValueError: See :func:`create_client`.
    """

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
