"""Minimal retry client factories for Requests, HTTPX, and aiohttp.

The package root exposes the Requests backend, which only needs the base
install::

    from resilient_http import Retry, create_retry, create_session

The optional backends live in their own submodules so that their ``Retry``
types and factories never mix with the Requests ones:

- ``resilient_http.httpx`` (``pip install 'resilient-http-client[httpx]'``)
- ``resilient_http.aiohttp`` (``pip install 'resilient-http-client[aiohttp]'``)

Importing the package root never imports HTTPX or aiohttp.
"""

from urllib3.util import Retry

from .client import create_retry, create_session

__all__ = [
    "Retry",
    "create_retry",
    "create_session",
]
