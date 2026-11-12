from decimal import Decimal, InvalidOperation

import pytest

from pytossinvest.money import to_decimal, decimal_to_str


def test_to_decimal_from_string():
    assert to_decimal("70000") == Decimal("70000")
    assert to_decimal("0.1516") == Decimal("0.1516")


def test_to_decimal_from_int_and_decimal():
    assert to_decimal(10) == Decimal("10")
    assert to_decimal(Decimal("5")) == Decimal("5")


def test_to_decimal_rejects_float():
    with pytest.raises(TypeError):
        to_decimal(0.1)


def test_to_decimal_rejects_bool():
    with pytest.raises(TypeError):
        to_decimal(True)


def test_decimal_to_str_roundtrip():
    assert decimal_to_str(to_decimal("70000")) == "70000"
    assert decimal_to_str(to_decimal("0.10")) == "0.10"


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity", Decimal("NaN"),
                                   Decimal("Infinity")])
def test_to_decimal_rejects_non_finite(value):
    # same failure as an unparseable string: neither is a usable amount
    with pytest.raises(InvalidOperation):
        to_decimal(value)


def test_to_decimal_keeps_negative_values():
    assert to_decimal("-12.5") == Decimal("-12.5")  # e.g. a realized loss
