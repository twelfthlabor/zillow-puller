from __future__ import annotations

import pytest

from field_utils import (
    apply_validation,
    as_bool,
    backoff_seconds,
    normalize,
    normalize_record_key,
)


def test_money_normalization_handles_symbols_and_commas() -> None:
    assert normalize("$1,234,567", "money") == "1234567"
    assert normalize("CAD 850,000.50", "money") == "850000.50"
    assert normalize("Price: 499,999", "money") == "499999"


def test_detail_transforms() -> None:
    assert normalize("3 bds 2 ba 1,234 sqft", "detail_beds") == "3"
    assert normalize("3 bds 2 ba 1,234 sqft", "detail_baths") == "2"
    assert normalize("1,234 sqft", "detail_sqft") == "1234"
    assert normalize("Studio 1 ba", "detail_beds") == "0"
    assert normalize("2 Bedroom 1 Bath", "detail_beds") == "2"


def test_detail_transforms_live_concatenated_format() -> None:
    """Live Zillow renders the detail spans with no separator: '5 bds7 ba'."""
    assert normalize("5 bds7 ba", "detail_beds") == "5"
    assert normalize("5 bds7 ba", "detail_baths") == "7"
    assert normalize("5 BDS7 BA", "detail_beds") == "5"
    assert normalize("5 BDS7 BA", "detail_baths") == "7"
    # A following digit must not stop the unit from matching either.
    assert normalize("7 ba1,500 sqft", "detail_baths") == "7"
    assert normalize("7 ba1,500 sqft", "detail_sqft") == "1500"
    assert normalize("3 bds2 ba900 sqft", "detail_beds") == "3"
    assert normalize("3 bds2 ba900 sqft", "detail_baths") == "2"
    assert normalize("3 bds2 ba900 sqft", "detail_sqft") == "900"
    assert normalize("900 sqft500", "detail_sqft") == "900"


def test_brokerage_strips_mls_prefix() -> None:
    assert (
        normalize("MLS® ID #W123456, ROYAL LEPAGE RCR REALTY", "brokerage")
        == "ROYAL LEPAGE RCR REALTY"
    )


def test_unknown_transform_raises() -> None:
    with pytest.raises(ValueError, match="Unknown transform"):
        normalize("anything", "not_a_transform")


def test_apply_validation_bounds() -> None:
    assert apply_validation("2000000000", "money", {"max": 50000000}) == ""
    assert apply_validation("1234567", "money", {"max": 50000000}) == "1234567"
    assert apply_validation("2000000", "money", {"min": 100000}) == "2000000"
    assert apply_validation("50000", "money", {"min": 100000}) == ""
    assert apply_validation("not-a-number", "money", {"max": 10}) == ""
    assert apply_validation("3", "detail_beds", {"max": 10}) == "3"


def test_backoff_grows_and_is_capped() -> None:
    first = backoff_seconds(1, 5, jitter=0)
    second = backoff_seconds(2, 5, jitter=0)
    third = backoff_seconds(3, 5, jitter=0)
    assert first == 5.0
    assert second == 10.0
    assert third == 20.0
    assert backoff_seconds(10, 5, cap=120, jitter=0) <= 120


def test_record_key_prefers_id_and_normalizes_url() -> None:
    assert normalize_record_key("123", "https://x.example/") == "id:123"
    assert normalize_record_key("", "HTTPS://X.EXAMPLE.COM/A/") == "url:https://x.example.com/a"
    assert normalize_record_key("", "") == ""


def test_as_bool_string_safety() -> None:
    assert as_bool(None, True) is True
    assert as_bool(True, False) is True
    assert as_bool("false", True) is False
    assert as_bool("TRUE", False) is True
