from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from .money import to_decimal

__all__ = ["Account", "Price", "BuyingPower", "OrderResponse", "HoldingsItem", "Money"]

# A Decimal that accepts the API's string (and int) values but rejects float,
# preserving the SDK-wide "money is never a float" guarantee via money.to_decimal.
Money = Annotated[Decimal, BeforeValidator(to_decimal)]


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Account(_Base):
    account_no: str = Field(alias="accountNo")
    account_seq: int = Field(alias="accountSeq")
    account_type: str = Field(alias="accountType")


class Price(_Base):
    symbol: str
    last_price: Money = Field(alias="lastPrice")
    currency: str
    timestamp: str | None = None


class BuyingPower(_Base):
    currency: str
    cash_buying_power: Money = Field(alias="cashBuyingPower")


class OrderResponse(_Base):
    order_id: str = Field(alias="orderId")
    client_order_id: str | None = Field(default=None, alias="clientOrderId")


class HoldingsItem(_Base):
    symbol: str
    name: str
    market_country: str = Field(alias="marketCountry")
    currency: str
    quantity: Money
    last_price: Money = Field(alias="lastPrice")
    average_purchase_price: Money = Field(alias="averagePurchasePrice")
