"""Minimal Requests Session factory with urllib3 retries."""

from urllib3.util import Retry

from .client import create_session

__all__ = [
    "Retry",
    "create_session",
]
