from decimal import Decimal, InvalidOperation

__all__ = ["to_decimal", "decimal_to_str"]


def to_decimal(value: "str | int | Decimal") -> Decimal:
    """Convert an API money/quantity value to Decimal. Floats are forbidden, and NaN or an
    infinity raises decimal.InvalidOperation exactly like an unparseable string."""
    if isinstance(value, bool):
        raise TypeError("bool is not a valid money/quantity value")
    if not isinstance(value, (Decimal, int, str)):
        raise TypeError(
            f"refusing to convert {type(value).__name__} to Decimal (float forbidden)"
        )

    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        raise InvalidOperation(f"non-finite money/quantity value: {value!r}")
    return result


def decimal_to_str(value: Decimal) -> str:
    """Serialize a Decimal to the plain string form the API expects."""
    return format(value, "f")
