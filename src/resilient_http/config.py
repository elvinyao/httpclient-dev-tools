"""Configuration for HTTPX retries and optional domain-error mapping."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import httpx

from ._vendor.httpx_retries import Retry
from .exceptions import BaseHttpError, BusinessHttpError, SystemHttpError

HttpxErrorType = type[httpx.RequestError]
RaisedErrorType = type[BaseHttpError]
RetryInput = Union[Retry, Mapping[str, Any]]
TimeoutValue = Union[float, httpx.Timeout]

_NO_RETRY_STATUS = -1

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

_RETRY_KEYS = {
    "total",
    "allowed_methods",
    "status_forcelist",
    "retry_on_exceptions",
    "backoff_factor",
    "respect_retry_after_header",
    "max_backoff_wait",
    "backoff_jitter",
}

_LEGACY_RETRY_KEYS = {
    "backoff",
    "exception_types",
    "exceptions",
    "max_attempts",
    "raise_as",
    "retry_methods",
    "rules",
    "status_codes",
}


def _unknown_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = set(data) - set(allowed)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown {where} configuration key(s): {names}")


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _strict_integer(
    value: Any,
    name: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _finite_number(
    value: Any,
    name: str,
    *,
    minimum: Optional[float] = None,
    exclusive_minimum: bool = False,
    maximum: Optional[float] = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")

    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None:
        if exclusive_minimum and number <= minimum:
            raise ValueError(f"{name} must be greater than {minimum}")
        if not exclusive_minimum and number < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return number


def _iterable_values(value: Any, name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Iterable):
        raise TypeError(f"{name} must be an iterable, not a string or mapping")
    return tuple(value)


def _ordered_values(value: Any, name: str) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be an ordered sequence")
    return tuple(value)


def _resolve_httpx_error_type(value: Union[str, HttpxErrorType]) -> HttpxErrorType:
    if isinstance(value, str):
        try:
            return _HTTPX_ERROR_TYPES[value]
        except KeyError as error:
            supported = ", ".join(sorted(_HTTPX_ERROR_TYPES))
            raise ValueError(
                f"Unknown HTTPX exception {value!r}; supported names: {supported}"
            ) from error

    if not isinstance(value, type) or not issubclass(value, httpx.RequestError):
        raise TypeError("Exception types must be names or subclasses of httpx.RequestError")
    return value


def _resolve_raised_error_type(
    value: Union[str, RaisedErrorType],
    name: str = "raise_as",
) -> RaisedErrorType:
    if isinstance(value, str):
        try:
            return _RAISED_ERROR_TYPES[value.replace("_", "").lower()]
        except KeyError as error:
            raise ValueError(
                f"{name} must be 'business', 'system', or a BaseHttpError subclass"
            ) from error

    if not isinstance(value, type) or not issubclass(value, BaseHttpError):
        raise TypeError(f"{name} must be a subclass of BaseHttpError")
    return value


def _validate_retry_instance(retry: Retry) -> Retry:
    if retry.attempts_made != 0:
        raise ValueError("retry.attempts_made must be 0; configure a fresh Retry instance")

    _strict_integer(retry.total, "retry.total", minimum=0)
    _finite_number(retry.backoff_factor, "retry.backoff_factor", minimum=0)
    _finite_number(
        retry.max_backoff_wait,
        "retry.max_backoff_wait",
        minimum=0,
        exclusive_minimum=True,
    )
    _finite_number(
        retry.backoff_jitter,
        "retry.backoff_jitter",
        minimum=0,
        maximum=1,
    )
    _strict_bool(
        retry.respect_retry_after_header,
        "retry.respect_retry_after_header",
    )

    for index, code in enumerate(retry.status_forcelist):
        if code == _NO_RETRY_STATUS:
            continue
        _strict_integer(
            code,
            f"retry.status_forcelist[{index}]",
            minimum=400,
            maximum=599,
        )

    for index, error_type in enumerate(retry.retryable_exceptions):
        if not isinstance(error_type, type) or not issubclass(
            error_type,
            httpx.RequestError,
        ):
            raise TypeError(
                f"retry.retry_on_exceptions[{index}] must be a subclass of httpx.RequestError"
            )
    return retry


def retry_from_dict(data: Mapping[str, Any]) -> Retry:
    """Build the Python 3.9-compatible vendored ``Retry`` configuration."""

    if not isinstance(data, Mapping):
        raise TypeError("retry must be a mapping")

    legacy = set(data) & _LEGACY_RETRY_KEYS
    if legacy:
        names = ", ".join(sorted(legacy))
        raise ValueError(
            f"Legacy retry key(s) are no longer supported: {names}. "
            "Use total=max_attempts-1, allowed_methods, status_forcelist, "
            "and retry_on_exceptions; move raise_as to error_mapping"
        )
    _unknown_keys(data, _RETRY_KEYS, "retry")

    total = _strict_integer(data.get("total", 0), "retry.total", minimum=0)

    allowed_methods: Optional[tuple[str, ...]] = None
    if "allowed_methods" in data and data["allowed_methods"] is not None:
        values = _iterable_values(
            data["allowed_methods"],
            "retry.allowed_methods",
        )
        if not values:
            raise ValueError(
                "retry.allowed_methods cannot be empty with httpx-retries 0.4.6; "
                "set total=0 and omit allowed_methods to disable retries"
            )

        normalized_methods = []
        for index, method in enumerate(values):
            if not isinstance(method, str) or not method.strip():
                raise TypeError(f"retry.allowed_methods[{index}] must be a non-empty string")
            normalized_methods.append(method.strip().upper())
        allowed_methods = tuple(normalized_methods)

    status_forcelist: Optional[tuple[int, ...]] = None
    if "status_forcelist" in data and data["status_forcelist"] is not None:
        values = _iterable_values(
            data["status_forcelist"],
            "retry.status_forcelist",
        )
        statuses = tuple(
            _strict_integer(
                code,
                f"retry.status_forcelist[{index}]",
                minimum=400,
                maximum=599,
            )
            for index, code in enumerate(values)
        )
        # Retry 0.4.6 treats an empty iterable as "use defaults". A sentinel
        # preserves the intended exception-only policy across increment().
        status_forcelist = statuses or (_NO_RETRY_STATUS,)

    retry_on_exceptions: Optional[tuple[HttpxErrorType, ...]] = None
    if "retry_on_exceptions" in data and data["retry_on_exceptions"] is not None:
        values = _iterable_values(
            data["retry_on_exceptions"],
            "retry.retry_on_exceptions",
        )
        retry_on_exceptions = tuple(_resolve_httpx_error_type(value) for value in values)

    backoff_factor = _finite_number(
        data.get("backoff_factor", 0.0),
        "retry.backoff_factor",
        minimum=0,
    )
    max_backoff_wait = _finite_number(
        data.get("max_backoff_wait", 120.0),
        "retry.max_backoff_wait",
        minimum=0,
        exclusive_minimum=True,
    )
    backoff_jitter = _finite_number(
        data.get("backoff_jitter", 1.0),
        "retry.backoff_jitter",
        minimum=0,
        maximum=1,
    )
    respect_retry_after_header = _strict_bool(
        data.get("respect_retry_after_header", True),
        "retry.respect_retry_after_header",
    )

    try:
        retry = Retry(
            total=total,
            allowed_methods=allowed_methods,
            status_forcelist=status_forcelist,
            retry_on_exceptions=retry_on_exceptions,
            backoff_factor=backoff_factor,
            respect_retry_after_header=respect_retry_after_header,
            max_backoff_wait=max_backoff_wait,
            backoff_jitter=backoff_jitter,
        )
    except ValueError as error:
        raise ValueError(f"Invalid retry configuration: {error}") from error
    return _validate_retry_instance(retry)


def _retry_from_value(value: RetryInput) -> Retry:
    if isinstance(value, Retry):
        return _validate_retry_instance(value)
    if isinstance(value, Mapping):
        return retry_from_dict(value)
    raise TypeError("retry must be a resilient_http.Retry or a mapping")


@dataclass(frozen=True)
class ErrorMappingRule:
    """Map one terminal HTTP condition to an application-facing exception."""

    name: str
    raise_as: RaisedErrorType
    status_codes: frozenset[int] = field(default_factory=frozenset)
    exception_types: tuple[HttpxErrorType, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Error-mapping rule name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())

        raw_statuses = _iterable_values(self.status_codes, "status_codes")
        status_codes = frozenset(
            _strict_integer(
                code,
                f"status_codes[{index}]",
                minimum=400,
                maximum=599,
            )
            for index, code in enumerate(raw_statuses)
        )
        object.__setattr__(self, "status_codes", status_codes)

        raw_exceptions = _iterable_values(
            self.exception_types,
            "exception_types",
        )
        exception_types = tuple(_resolve_httpx_error_type(value) for value in raw_exceptions)
        object.__setattr__(self, "exception_types", exception_types)

        if not status_codes and not exception_types:
            raise ValueError(
                "An error-mapping rule must define status_codes and/or exception_types"
            )
        object.__setattr__(
            self,
            "raise_as",
            _resolve_raised_error_type(self.raise_as),
        )

    def matches_status(self, status_code: int) -> bool:
        return status_code in self.status_codes

    def matches_exception(self, error: httpx.RequestError) -> bool:
        return bool(self.exception_types) and isinstance(
            error,
            self.exception_types,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ErrorMappingRule:
        if not isinstance(data, Mapping):
            raise TypeError("An error-mapping rule must be a mapping")
        _unknown_keys(
            data,
            {
                "name",
                "status_codes",
                "exceptions",
                "exception_types",
                "raise_as",
            },
            "error-mapping rule",
        )
        missing = [key for key in ("name", "raise_as") if key not in data]
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"An error-mapping rule must define required key(s): {names}")
        if "exceptions" in data and "exception_types" in data:
            raise ValueError("Use only one of exceptions or exception_types")

        status_values = _iterable_values(
            data.get("status_codes", ()),
            "error-mapping status_codes",
        )
        raw_errors = data.get(
            "exception_types",
            data.get("exceptions", ()),
        )
        error_values = _iterable_values(
            raw_errors,
            "error-mapping exceptions",
        )

        return cls(
            name=data["name"],
            raise_as=_resolve_raised_error_type(data["raise_as"]),
            status_codes=frozenset(status_values),
            exception_types=tuple(_resolve_httpx_error_type(value) for value in error_values),
        )


@dataclass(frozen=True)
class ErrorMappingPolicy:
    """Ordered terminal-error mappings; the first matching rule wins."""

    rules: tuple[ErrorMappingRule, ...] = field(default_factory=tuple)
    default_business_error: RaisedErrorType = BusinessHttpError
    default_system_error: RaisedErrorType = SystemHttpError

    def __post_init__(self) -> None:
        rules = _ordered_values(self.rules, "error_mapping.rules")
        for index, rule in enumerate(rules):
            if not isinstance(rule, ErrorMappingRule):
                raise TypeError(f"error_mapping.rules[{index}] must be an ErrorMappingRule")
        object.__setattr__(self, "rules", rules)

        business_error = _resolve_raised_error_type(
            self.default_business_error,
            "default_business_error",
        )
        if not issubclass(business_error, BusinessHttpError):
            raise TypeError("default_business_error must inherit BusinessHttpError")
        object.__setattr__(self, "default_business_error", business_error)

        system_error = _resolve_raised_error_type(
            self.default_system_error,
            "default_system_error",
        )
        if not issubclass(system_error, SystemHttpError):
            raise TypeError("default_system_error must inherit SystemHttpError")
        object.__setattr__(self, "default_system_error", system_error)

    def for_status(self, status_code: int) -> Optional[ErrorMappingRule]:
        return next(
            (rule for rule in self.rules if rule.matches_status(status_code)),
            None,
        )

    def for_exception(
        self,
        error: httpx.RequestError,
    ) -> Optional[ErrorMappingRule]:
        return next(
            (rule for rule in self.rules if rule.matches_exception(error)),
            None,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ErrorMappingPolicy:
        if not isinstance(data, Mapping):
            raise TypeError("error_mapping must be a mapping")
        _unknown_keys(
            data,
            {
                "rules",
                "default_business_error",
                "default_system_error",
            },
            "error mapping",
        )

        raw_rules = _ordered_values(
            data.get("rules", ()),
            "error_mapping.rules",
        )
        rules = []
        for index, rule in enumerate(raw_rules):
            if isinstance(rule, ErrorMappingRule):
                rules.append(rule)
            elif isinstance(rule, Mapping):
                rules.append(ErrorMappingRule.from_dict(rule))
            else:
                raise TypeError(
                    f"error_mapping.rules[{index}] must be a mapping or ErrorMappingRule"
                )

        return cls(
            rules=tuple(rules),
            default_business_error=_resolve_raised_error_type(
                data.get("default_business_error", "business"),
                "default_business_error",
            ),
            default_system_error=_resolve_raised_error_type(
                data.get("default_system_error", "system"),
                "default_system_error",
            ),
        )


def _timeout_from_value(value: Any) -> TimeoutValue:
    if isinstance(value, httpx.Timeout):
        for component in ("connect", "read", "write", "pool"):
            timeout = getattr(value, component)
            if timeout is not None:
                _finite_number(
                    timeout,
                    f"timeout.{component}",
                    minimum=0,
                )
        return value
    if isinstance(value, Mapping):
        _unknown_keys(
            value,
            {"default", "connect", "read", "write", "pool"},
            "timeout",
        )
        default = _finite_number(
            value.get("default", 10.0),
            "timeout.default",
            minimum=0,
        )
        return httpx.Timeout(
            default,
            connect=_finite_number(
                value.get("connect", default),
                "timeout.connect",
                minimum=0,
            ),
            read=_finite_number(
                value.get("read", default),
                "timeout.read",
                minimum=0,
            ),
            write=_finite_number(
                value.get("write", default),
                "timeout.write",
                minimum=0,
            ),
            pool=_finite_number(
                value.get("pool", default),
                "timeout.pool",
                minimum=0,
            ),
        )
    return _finite_number(value, "timeout", minimum=0)


def _headers_from_value(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("headers must be a mapping")

    headers = {}
    for key, header_value in value.items():
        if not isinstance(key, str) or not isinstance(header_value, str):
            raise TypeError("headers keys and values must be strings")
        headers[key] = header_value
    return headers


def _default_retry() -> Retry:
    # Upstream defaults to ten retries. Shared clients should opt in explicitly.
    return Retry(total=0)


@dataclass(frozen=True)
class HttpClientConfig:
    """Top-level client configuration."""

    base_url: str = ""
    timeout: TimeoutValue = 10.0
    headers: Mapping[str, str] = field(default_factory=dict)
    follow_redirects: bool = False
    retry: RetryInput = field(default_factory=_default_retry)
    enable_error_mapping: bool = True
    error_mapping: ErrorMappingPolicy = field(default_factory=ErrorMappingPolicy)

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise TypeError("base_url must be a string")

        object.__setattr__(
            self,
            "timeout",
            _timeout_from_value(self.timeout),
        )
        object.__setattr__(
            self,
            "headers",
            _headers_from_value(self.headers),
        )
        object.__setattr__(
            self,
            "follow_redirects",
            _strict_bool(self.follow_redirects, "follow_redirects"),
        )
        object.__setattr__(
            self,
            "retry",
            _retry_from_value(self.retry),
        )
        object.__setattr__(
            self,
            "enable_error_mapping",
            _strict_bool(
                self.enable_error_mapping,
                "enable_error_mapping",
            ),
        )

        if isinstance(self.error_mapping, ErrorMappingPolicy):
            mapping = self.error_mapping
        elif isinstance(self.error_mapping, Mapping):
            mapping = ErrorMappingPolicy.from_dict(self.error_mapping)
        else:
            raise TypeError("error_mapping must be an ErrorMappingPolicy or mapping")
        object.__setattr__(self, "error_mapping", mapping)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HttpClientConfig:
        if not isinstance(data, Mapping):
            raise TypeError("HTTP client configuration must be a mapping")
        if "retry_policy" in data:
            raise ValueError(
                "retry_policy was removed; configure the vendored Retry under retry "
                "and terminal exception mapping under error_mapping"
            )

        _unknown_keys(
            data,
            {
                "base_url",
                "timeout",
                "headers",
                "follow_redirects",
                "retry",
                "enable_error_mapping",
                "error_mapping",
            },
            "HTTP client",
        )

        return cls(
            base_url=data.get("base_url", ""),
            timeout=data.get("timeout", 10.0),
            headers=data.get("headers", {}),
            follow_redirects=data.get("follow_redirects", False),
            retry=data.get("retry", {"total": 0}),
            enable_error_mapping=data.get("enable_error_mapping", True),
            error_mapping=data.get("error_mapping", {}),
        )
