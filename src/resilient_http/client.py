"""Create native Requests sessions configured with urllib3 retries."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

_DEFAULT_ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_STATUS_FORCELIST = frozenset({429, 500, 502, 503, 504})
_TIMEOUT_UNSET = object()
_Timeout = Union[float, tuple[float, float]]


class _TimeoutSession(requests.Session):
    """Requests Session that supplies a timeout only when one is omitted."""

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
        """Send a high-level request using the configured default timeout."""

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
        """Send a prepared request using the configured default timeout."""

        kwargs.setdefault("timeout", self._default_timeout)
        return super().send(request, **kwargs)


def create_retry(
    *,
    total: int = 3,
    connect: Optional[int] = None,
    read: Optional[int] = None,
    status: Optional[int] = None,
    other: Optional[int] = 0,
    allowed_methods: Optional[Collection[str]] = _DEFAULT_ALLOWED_METHODS,
    status_forcelist: Collection[int] = _DEFAULT_STATUS_FORCELIST,
    backoff_factor: float = 0.5,
    raise_on_status: bool = False,
) -> Retry:
    """Return a conservative urllib3 retry policy for common API calls."""

    return Retry(
        total=total,
        connect=connect,
        read=read,
        redirect=0,
        status=status,
        other=other,
        allowed_methods=allowed_methods,
        status_forcelist=status_forcelist,
        backoff_factor=backoff_factor,
        raise_on_status=raise_on_status,
        respect_retry_after_header=True,
    )


def create_session(
    retry: Retry,
    *,
    timeout: Optional[Union[float, tuple[float, float]]] = None,
) -> requests.Session:
    """Return a new Session with retries and an optional default timeout."""

    if not isinstance(retry, Retry):
        raise TypeError("retry must be an urllib3.util.Retry")

    session = requests.Session() if timeout is None else _TimeoutSession(timeout)
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session
