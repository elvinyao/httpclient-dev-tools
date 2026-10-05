"""Shared validation for the Requests-style timeouts accepted by the factories.

Every backend accepts the same "simple" timeout forms at factory time: ``None``,
a single number, or a tuple of numbers/``None``. Validating them here lets a
misconfiguration fail when the client is created instead of on the first
request, and keeps the error types identical across backends:

- ``TypeError`` for a value of the wrong type or a tuple of the wrong length.
- ``ValueError`` for a number that is not finite or not greater than zero.

Native timeout objects (``httpx.Timeout``, ``aiohttp.ClientTimeout``) are not
handled here; each backend passes them through unchanged.
"""

from __future__ import annotations

import math
from typing import Optional

__all__ = ["validate_timeout_value"]


def validate_timeout_value(value: object, *, name: str) -> Optional[float]:
    """Validate one timeout component and return it as a float.

    Args:
        value: The candidate timeout in seconds, or ``None`` for "no limit".
        name: Human-readable name used in error messages, such as
            ``"connect timeout"``.

    Returns:
        ``None`` when ``value`` is ``None``; otherwise ``value`` converted to
        ``float``.

    Raises:
        TypeError: If ``value`` is a ``bool`` or not an ``int``/``float``.
        ValueError: If ``value`` is NaN, infinite, zero, or negative.
    """

    if value is None:
        return None
    # bool is a subclass of int, but timeout=True is always a mistake.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number or None")

    try:
        normalized = float(value)
    except OverflowError as error:  # an int too large for a float
        raise ValueError(f"{name} must be finite and greater than 0") from error

    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be finite and greater than 0")
    return normalized
