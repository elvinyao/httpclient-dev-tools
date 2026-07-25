"""Exceptions exposed to applications using the HTTP client."""

from __future__ import annotations

import httpx


class BaseHttpError(Exception):
    """Base class for every expected failure exposed by this package.

    Applications can catch this class when they do not need to distinguish
    caller/business failures from infrastructure/system failures.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str,
        url: str,
        attempts: int,
        rule_name: str | None = None,
        status_code: int | None = None,
        retry_exhausted: bool = False,
        response: httpx.Response | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.attempts = attempts
        self.rule_name = rule_name
        self.status_code = status_code
        self.retry_exhausted = retry_exhausted
        self.response = response
        self.cause = cause


class BusinessHttpError(BaseHttpError):
    """The remote service rejected the request for a business/caller reason."""


class SystemHttpError(BaseHttpError):
    """The request failed because of a network or upstream system condition."""


class NonReplayableRequestError(SystemHttpError):
    """A retryable method was given a body that cannot be sent more than once."""
