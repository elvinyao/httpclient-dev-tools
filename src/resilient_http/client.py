"""Requests backend: native ``requests.Session`` objects with urllib3 retries.

This module is re-exported by the package root, so ``from resilient_http import
create_session`` keeps working. It deliberately returns plain Requests objects
and never wraps or translates Requests exceptions.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from ._timeout import validate_timeout_value

_DEFAULT_ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_STATUS_FORCELIST = frozenset({429, 500, 502, 503, 504})
_TIMEOUT_UNSET = object()
_Timeout = Union[float, tuple[Optional[float], Optional[float]]]


class _TimeoutSession(requests.Session):
    """Requests Session that supplies a timeout only when one is omitted."""

    # requests.Session pickles only the attributes listed in __attrs__. Without
    # this entry an unpickled session would lose its default timeout and fail
    # with AttributeError on the first request.
    __attrs__ = [*requests.Session.__attrs__, "_default_timeout"]

    def __init__(self, timeout: _Timeout) -> None:
        super().__init__()
        self._default_timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        params: Any = None,
        data: Any = None,
        headers: Any = None,
        cookies: Any = None,
        files: Any = None,
        auth: Any = None,
        timeout: Any = _TIMEOUT_UNSET,
        allow_redirects: bool = True,
        proxies: Any = None,
        hooks: Any = None,
        stream: Any = None,
        verify: Any = None,
        cert: Any = None,
        json: Any = None,
    ) -> requests.Response:
        """Send a high-level request using the configured default timeout.

        The signature mirrors ``requests.Session.request`` exactly, including
        positional order, so positional callers keep working. Omitting
        ``timeout`` selects the session default; an explicit ``timeout=None``
        disables the timeout for this request only.
        """

        if timeout is _TIMEOUT_UNSET:
            timeout = self._default_timeout
        return super().request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            cookies=cookies,
            files=files,
            auth=auth,
            timeout=timeout,
            allow_redirects=allow_redirects,
            proxies=proxies,
            hooks=hooks,
            stream=stream,
            verify=verify,
            cert=cert,
            json=json,
        )

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        """Send a prepared request using the configured default timeout.

        Requests calls ``send`` for every redirect hop with the effective
        timeout, so ``setdefault`` only fills it in for direct ``send`` calls.
        """

        kwargs.setdefault("timeout", self._default_timeout)
        return super().send(request, **kwargs)


def _normalize_allowed_methods(allowed_methods: Optional[Collection[str]]) -> Optional[frozenset[str]]:
    """Validate and upper-case an urllib3 ``allowed_methods`` collection.

    urllib3 compares ``method.upper()`` against the collection, so lowercase
    entries would silently never match. It also treats *any* falsy value as
    "retry every method", which turns an empty collection meant as "disable"
    into the most dangerous setting. ``None`` stays available for callers who
    really want every method.
    """

    if allowed_methods is None:
        return None
    if isinstance(allowed_methods, str):
        raise TypeError("allowed_methods must be a collection of method names, not a single string")
    if not allowed_methods:
        raise ValueError(
            "allowed_methods must not be empty; pass None to allow every method or use total=0 to disable retries"
        )
    return frozenset(method.upper() for method in allowed_methods)


def create_retry(
    *,
    total: int = 3,
    connect: Optional[int] = None,
    read: Optional[int] = None,
    status: Optional[int] = None,
    other: Optional[int] = 0,
    allowed_methods: Optional[Collection[str]] = _DEFAULT_ALLOWED_METHODS,
    status_forcelist: Optional[Collection[int]] = _DEFAULT_STATUS_FORCELIST,
    backoff_factor: float = 0.5,
    raise_on_status: bool = False,
) -> Retry:
    """Return a conservative ``urllib3.util.Retry`` policy for common API calls.

    The same policy object is used by the Requests and aiohttp backends.
    ``redirect=0`` and ``respect_retry_after_header=True`` are always set:
    redirects stay the client's responsibility, and server back-pressure is
    honored. Build ``Retry`` directly for anything not covered here, such as
    ``backoff_max``, ``backoff_jitter`` or ``retry_after_max``.

    Args:
        total: Retries allowed after the first attempt; ``total=3`` sends at
            most four requests. Shared ceiling for every category.
        connect: Optional separate limit for connection-phase errors (DNS,
            refused connection, connect timeout). ``None`` adds no separate
            limit; ``total`` still applies.
        read: Optional separate limit for errors after the request was sent
            but before response headers arrived.
        status: Optional separate limit for retries triggered by
            ``status_forcelist`` responses.
        other: Limit for errors outside the categories above, such as TLS
            failures. Defaults to ``0`` so unexpected errors are not retried.
        allowed_methods: Methods allowed to retry read errors and statuses.
            Names are upper-cased. ``None`` allows every method. Connect
            errors are retried regardless of method because nothing was sent.
        status_forcelist: Statuses that trigger a retry for allowed methods.
            ``None`` or an empty collection disables status retries.
        backoff_factor: Exponential backoff scale. urllib3 waits roughly
            ``backoff_factor * 2 ** (consecutive_errors - 1)`` seconds and does
            not sleep before the first retry.
        raise_on_status: When status retries are exhausted, raise instead of
            returning the final response. Defaults to ``False`` so callers can
            use ``response.raise_for_status()``.

    Returns:
        A new, independent ``urllib3.util.Retry`` instance.

    Raises:
        TypeError: If ``allowed_methods`` is a single string.
        ValueError: If ``allowed_methods`` is an empty collection.
    """

    return Retry(
        total=total,
        connect=connect,
        read=read,
        redirect=0,
        status=status,
        other=other,
        allowed_methods=_normalize_allowed_methods(allowed_methods),
        status_forcelist=status_forcelist,
        backoff_factor=backoff_factor,
        raise_on_status=raise_on_status,
        respect_retry_after_header=True,
    )


def _validate_session_timeout(timeout: object) -> None:
    """Fail at factory time for timeouts Requests would reject per request."""

    if isinstance(timeout, tuple):
        if len(timeout) != 2:
            raise TypeError("timeout tuple must contain (connect, read)")
        validate_timeout_value(timeout[0], name="connect timeout")
        validate_timeout_value(timeout[1], name="read timeout")
    else:
        validate_timeout_value(timeout, name="timeout")


def create_session(
    retry: Retry,
    *,
    timeout: Optional[_Timeout] = None,
) -> requests.Session:
    """Return a new ``requests.Session`` with ``retry`` mounted for HTTP(S).

    Every call creates an independent session, two ``HTTPAdapter`` objects and
    their connection pools. The ``retry`` object is shared as an immutable
    template; urllib3 derives fresh per-request state from it.

    Args:
        retry: The ``urllib3.util.Retry`` policy, typically from
            :func:`create_retry`. Required: there is no hidden default policy.
        timeout: Optional default for requests that omit ``timeout``. Accepts a
            number, or a ``(connect, read)`` tuple whose items may be ``None``.
            It applies to each physical attempt and is not a total deadline.
            ``None`` (the default) sets no timeout, exactly like Requests; a
            per-request ``timeout=None`` disables the default for that call.

    Returns:
        A plain ``requests.Session`` when ``timeout`` is ``None``; otherwise a
        private ``requests.Session`` subclass that only fills in omitted
        timeouts.

    Raises:
        TypeError: If ``retry`` is not a ``urllib3.util.Retry``, or ``timeout``
            has an unsupported type or tuple length.
        ValueError: If a timeout number is zero, negative, NaN or infinite.
    """

    if not isinstance(retry, Retry):
        raise TypeError("retry must be an urllib3.util.Retry")

    if timeout is None:
        session = requests.Session()
    else:
        _validate_session_timeout(timeout)
        # Keep the caller's original value so Requests sees exactly what was
        # configured (for example an int stays an int).
        session = _TimeoutSession(timeout)

    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session
