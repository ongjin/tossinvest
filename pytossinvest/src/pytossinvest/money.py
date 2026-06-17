from decimal import Decimal

__all__ = ["to_decimal", "decimal_to_str"]


def to_decimal(value: "str | int | Decimal") -> Decimal:
    """Convert an API money/quantity value to Decimal. Floats are forbidden."""
    if isinstance(value, bool):
        raise TypeError("bool is not a valid money/quantity value")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(
        f"refusing to convert {type(value).__name__} to Decimal (float forbidden)"
    )


def decimal_to_str(value: Decimal) -> str:
    """Serialize a Decimal to the plain string form the API expects."""
    return format(value, "f")
