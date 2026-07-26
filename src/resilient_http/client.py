"""Create native Requests sessions configured with urllib3 retries."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry


def create_session(retry: Retry) -> requests.Session:
    """Return a new Session with ``retry`` mounted for HTTP and HTTPS."""

    if not isinstance(retry, Retry):
        raise TypeError("retry must be an urllib3.util.Retry")

    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session
