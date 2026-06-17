from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Account", "Price", "BuyingPower", "OrderResponse", "HoldingsItem"]


class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Account(_Base):
    account_no: str = Field(alias="accountNo")
    account_seq: int = Field(alias="accountSeq")
    account_type: str = Field(alias="accountType")


class Price(_Base):
    symbol: str
    last_price: Decimal = Field(alias="lastPrice")
    currency: str
    timestamp: str | None = None


class BuyingPower(_Base):
    currency: str
    cash_buying_power: Decimal = Field(alias="cashBuyingPower")


class OrderResponse(_Base):
    order_id: str = Field(alias="orderId")
    client_order_id: str | None = Field(default=None, alias="clientOrderId")


class HoldingsItem(_Base):
    symbol: str
    name: str
    market_country: str = Field(alias="marketCountry")
    currency: str
    quantity: Decimal
    last_price: Decimal = Field(alias="lastPrice")
    average_purchase_price: Decimal = Field(alias="averagePurchasePrice")
