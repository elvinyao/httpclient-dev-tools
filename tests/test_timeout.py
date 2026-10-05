"""Unit tests for shared timeout validation."""

from __future__ import annotations

import math

import pytest

from resilient_http._timeout import validate_timeout_value


def test_validate_timeout_value_accepts_valid_numbers() -> None:
    assert validate_timeout_value(None, name="test") is None
    assert validate_timeout_value(1, name="test") == 1.0
    assert validate_timeout_value(0.5, name="test") == 0.5
    assert validate_timeout_value(100.0, name="test") == 100.0


@pytest.mark.parametrize("invalid_type", [True, False, "1.0", [1], object()])
def test_validate_timeout_value_rejects_non_numbers(invalid_type: object) -> None:
    with pytest.raises(TypeError, match="must be a number or None"):
        validate_timeout_value(invalid_type, name="connect timeout")


@pytest.mark.parametrize(
    "invalid_value",
    [0, 0.0, -1, -0.001, float("nan"), float("inf"), float("-inf")],
    ids=["zero-int", "zero-float", "negative-int", "negative-float", "nan", "inf", "-inf"],
)
def test_validate_timeout_value_rejects_non_positive_and_non_finite(invalid_value: object) -> None:
    with pytest.raises(ValueError, match="must be finite and greater than 0"):
        validate_timeout_value(invalid_value, name="read timeout")


def test_validate_timeout_value_handles_overflow_int() -> None:
    huge_int = 10**1000
    with pytest.raises(ValueError, match="must be finite and greater than 0"):
        validate_timeout_value(huge_int, name="timeout")
