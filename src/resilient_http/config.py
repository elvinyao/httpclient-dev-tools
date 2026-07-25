"""Configuration objects for retry matching, backoff, and error mapping."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from typing import (
    Any,
    Union,
)

import httpx

from .exceptions import BaseHttpError, BusinessHttpError, SystemHttpError

DEFAULT_RETRY_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

HttpxErrorType = type[httpx.RequestError]
RaisedErrorType = type[BaseHttpError]


_HTTPX_ERROR_TYPES: dict[str, HttpxErrorType] = {
    name: getattr(httpx, name)
    for name in (
        "RequestError",
        "TransportError",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "NetworkError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "CloseError",
        "ProtocolError",
        "LocalProtocolError",
        "RemoteProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
        "DecodingError",
        "TooManyRedirects",
    )
    if hasattr(httpx, name)
}

_RAISED_ERROR_TYPES: dict[str, RaisedErrorType] = {
    "business": BusinessHttpError,
    "businesshttperror": BusinessHttpError,
    "system": SystemHttpError,
    "systemhttperror": SystemHttpError,
}


def _unknown_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown {where} configuration key(s): {names}")


def _resolve_httpx_error_type(
    value: str | HttpxErrorType,
) -> HttpxErrorType:
    if isinstance(value, str):
        try:
            return _HTTPX_ERROR_TYPES[value]
        except KeyError as error:
            supported = ", ".join(sorted(_HTTPX_ERROR_TYPES))
            raise ValueError(f"Unknown HTTPX exception {value!r}; supported names: {supported}") from error

    if not isinstance(value, type) or not issubclass(value, httpx.RequestError):
        raise TypeError("Retry exception types must be names or subclasses of httpx.RequestError")
    return value


def _resolve_raised_error_type(
    value: str | RaisedErrorType,
) -> RaisedErrorType:
    if isinstance(value, str):
        try:
            return _RAISED_ERROR_TYPES[value.replace("_", "").lower()]
        except KeyError as error:
            raise ValueError("raise_as must be 'business', 'system', or a BaseHttpError subclass") from error

    if not isinstance(value, type) or not issubclass(value, BaseHttpError):
        raise TypeError("raise_as must be a subclass of BaseHttpError")
    return value


@dataclass(frozen=True)
class BackoffConfig:
    """Exponential backoff settings.

    ``retry_number`` starts at one for the wait between attempt 1 and attempt 2.
    The calculated delay is:

        initial_delay * multiplier ** (retry_number - 1)

    Jitter is an additive random value between zero and ``jitter``. The final
    delay is capped by ``max_delay``.
    """

    initial_delay: float = 0.5
    multiplier: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.0
    respect_retry_after: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("initial_delay", self.initial_delay),
            ("multiplier", self.multiplier),
            ("max_delay", self.max_delay),
            ("jitter", self.jitter),
        ):
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.initial_delay < 0:
            raise ValueError("initial_delay must be >= 0")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if self.max_delay < 0:
            raise ValueError("max_delay must be >= 0")
        if self.jitter < 0:
            raise ValueError("jitter must be >= 0")

    def delay_for_retry(
        self,
        retry_number: int,
        *,
        retry_after: float | None = None,
        random_value: float = 0.0,
    ) -> float:
        if retry_number < 1:
            raise ValueError("retry_number must be >= 1")
        if not 0.0 <= random_value <= 1.0:
            raise ValueError("random_value must be between 0 and 1")

        try:
            exponential_delay = self.initial_delay * (self.multiplier ** (retry_number - 1))
        except OverflowError:
            exponential_delay = self.max_delay
        calculated_delay = exponential_delay + self.jitter * random_value
        if self.respect_retry_after and retry_after is not None:
            calculated_delay = max(calculated_delay, max(0.0, retry_after))

        return min(self.max_delay, calculated_delay)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BackoffConfig:
        _unknown_keys(
            data,
            {
                "initial",
                "initial_delay",
                "multiplier",
                "max",
                "max_delay",
                "jitter",
                "respect_retry_after",
            },
            "backoff",
        )
        if "initial" in data and "initial_delay" in data:
            raise ValueError("Use only one of backoff.initial or initial_delay")
        if "max" in data and "max_delay" in data:
            raise ValueError("Use only one of backoff.max or max_delay")

        return cls(
            initial_delay=float(data.get("initial_delay", data.get("initial", 0.5))),
            multiplier=float(data.get("multiplier", 2.0)),
            max_delay=float(data.get("max_delay", data.get("max", 30.0))),
            jitter=float(data.get("jitter", 0.0)),
            respect_retry_after=bool(data.get("respect_retry_after", True)),
        )


@dataclass(frozen=True)
class RetryRule:
    """One ordered retry and exception-mapping rule."""

    name: str
    max_attempts: int = 1
    status_codes: frozenset[int] = field(default_factory=frozenset)
    exception_types: tuple[HttpxErrorType, ...] = field(default_factory=tuple)
    retry_methods: frozenset[str] = DEFAULT_RETRY_METHODS
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    raise_as: RaisedErrorType = SystemHttpError

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Retry rule name must not be empty")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")

        status_codes = frozenset(int(code) for code in self.status_codes)
        if any(code < 400 or code > 599 for code in status_codes):
            raise ValueError("status_codes must contain HTTP error codes from 400-599")
        object.__setattr__(self, "status_codes", status_codes)

        exception_types = tuple(_resolve_httpx_error_type(exception_type) for exception_type in self.exception_types)
        object.__setattr__(self, "exception_types", exception_types)

        if not status_codes and not exception_types:
            raise ValueError("A retry rule must define status_codes and/or exception_types")

        retry_methods = frozenset(method.upper() for method in self.retry_methods)
        object.__setattr__(self, "retry_methods", retry_methods)
        object.__setattr__(self, "raise_as", _resolve_raised_error_type(self.raise_as))

    def matches_status(self, status_code: int) -> bool:
        return status_code in self.status_codes

    def matches_exception(self, error: httpx.RequestError) -> bool:
        return bool(self.exception_types) and isinstance(error, self.exception_types)

    def can_retry(self, method: str, attempt: int) -> bool:
        return attempt < self.max_attempts and method.upper() in self.retry_methods

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RetryRule:
        _unknown_keys(
            data,
            {
                "name",
                "max_attempts",
                "status_codes",
                "exceptions",
                "exception_types",
                "retry_methods",
                "backoff",
                "raise_as",
            },
            "retry rule",
        )
        if "exceptions" in data and "exception_types" in data:
            raise ValueError("Use only one of exceptions or exception_types")

        raw_errors: Sequence[str | HttpxErrorType] = data.get("exception_types", data.get("exceptions", ()))
        if isinstance(raw_errors, str):
            raise TypeError("exceptions must be a sequence, not a single string")
        exception_types = tuple(_resolve_httpx_error_type(value) for value in raw_errors)

        raw_backoff = data.get("backoff", {})
        backoff = raw_backoff if isinstance(raw_backoff, BackoffConfig) else BackoffConfig.from_dict(raw_backoff)

        raw_methods = data.get("retry_methods", DEFAULT_RETRY_METHODS)
        if isinstance(raw_methods, str):
            raise TypeError("retry_methods must be a sequence, not a single string")

        return cls(
            name=str(data["name"]),
            max_attempts=data.get("max_attempts", 1),
            status_codes=frozenset(data.get("status_codes", ())),
            exception_types=exception_types,
            retry_methods=frozenset(raw_methods),
            backoff=backoff,
            raise_as=_resolve_raised_error_type(data.get("raise_as", "system")),
        )


@dataclass(frozen=True)
class RetryPolicy:
    """An ordered list of rules. The first matching rule wins."""

    rules: tuple[RetryRule, ...] = field(default_factory=tuple)
    default_business_error: RaisedErrorType = BusinessHttpError
    default_system_error: RaisedErrorType = SystemHttpError

    def __post_init__(self) -> None:
        rules = tuple(rule if isinstance(rule, RetryRule) else RetryRule.from_dict(rule) for rule in self.rules)
        object.__setattr__(self, "rules", rules)
        object.__setattr__(
            self,
            "default_business_error",
            _resolve_raised_error_type(self.default_business_error),
        )
        object.__setattr__(
            self,
            "default_system_error",
            _resolve_raised_error_type(self.default_system_error),
        )

    def for_status(self, status_code: int) -> RetryRule | None:
        return next(
            (rule for rule in self.rules if rule.matches_status(status_code)),
            None,
        )

    def for_exception(self, error: httpx.RequestError) -> RetryRule | None:
        return next(
            (rule for rule in self.rules if rule.matches_exception(error)),
            None,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RetryPolicy:
        _unknown_keys(
            data,
            {"rules", "default_business_error", "default_system_error"},
            "retry policy",
        )
        return cls(
            rules=tuple(RetryRule.from_dict(rule) for rule in data.get("rules", ())),
            default_business_error=_resolve_raised_error_type(data.get("default_business_error", "business")),
            default_system_error=_resolve_raised_error_type(data.get("default_system_error", "system")),
        )


TimeoutValue = Union[float, httpx.Timeout]


def _timeout_from_value(value: Any) -> TimeoutValue:
    if isinstance(value, httpx.Timeout):
        return value
    if isinstance(value, Mapping):
        _unknown_keys(
            value,
            {"default", "connect", "read", "write", "pool"},
            "timeout",
        )
        default = float(value.get("default", 10.0))
        return httpx.Timeout(
            default,
            connect=float(value.get("connect", default)),
            read=float(value.get("read", default)),
            write=float(value.get("write", default)),
            pool=float(value.get("pool", default)),
        )
    return float(value)


@dataclass(frozen=True)
class HttpClientConfig:
    """Top-level client configuration."""

    base_url: str = ""
    timeout: TimeoutValue = 10.0
    headers: Mapping[str, str] = field(default_factory=dict)
    follow_redirects: bool = False
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeout", _timeout_from_value(self.timeout))
        object.__setattr__(self, "headers", dict(self.headers))
        if not isinstance(self.retry_policy, RetryPolicy):
            object.__setattr__(
                self,
                "retry_policy",
                RetryPolicy.from_dict(self.retry_policy),
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HttpClientConfig:
        _unknown_keys(
            data,
            {
                "base_url",
                "timeout",
                "headers",
                "follow_redirects",
                "retry",
                "retry_policy",
            },
            "HTTP client",
        )
        if "retry" in data and "retry_policy" in data:
            raise ValueError("Use only one of retry or retry_policy")

        raw_policy = data.get("retry_policy", data.get("retry", {}))
        policy = raw_policy if isinstance(raw_policy, RetryPolicy) else RetryPolicy.from_dict(raw_policy)

        return cls(
            base_url=str(data.get("base_url", "")),
            timeout=_timeout_from_value(data.get("timeout", 10.0)),
            headers=dict(data.get("headers", {})),
            follow_redirects=bool(data.get("follow_redirects", False)),
            retry_policy=policy,
        )
