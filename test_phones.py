import pytest

from phones import normalize_indian_mobile


@pytest.mark.parametrize("raw", [
    "9876543210", "98765 43210", "+91 98765 43210", "+91-98765-43210",
    "919876543210", "09876543210", "(+91) 98765-43210",
])
def test_all_formats_become_one(raw):
    assert normalize_indian_mobile(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["12345", "1234567890", "5876543210", "98765432101234", "abc", "+1 415 555 0100"])
def test_invalid_numbers_rejected(raw):
    with pytest.raises(ValueError):
        normalize_indian_mobile(raw)
