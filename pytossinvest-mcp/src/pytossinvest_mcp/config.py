from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOSSINVEST_", env_file=".env", extra="ignore"
    )

    # credentials / endpoint
    client_id: str = ""
    client_secret: str = ""
    base_url: str = "https://openapi.tossinvest.com"

    # mode (default safe). live requires allow_live too.
    mode: Literal["read_only", "paper", "live"] = "paper"
    allow_live: bool = False

    # guardrails (amounts are KRW-equivalent, conservative defaults)
    max_order_amount: Decimal = Decimal("1000000")
    daily_order_limit: Decimal = Decimal("5000000")
    allow_symbols: list[str] = []  # empty = allow all
    deny_symbols: list[str] = []
    enforce_market_hours: bool = True

    max_order_amount_usd: Decimal = Decimal("1000")
    daily_order_limit_usd: Decimal = Decimal("5000")

    # paper engine
    paper_starting_cash: Decimal = Decimal("10000000")

    # preview -> confirm window
    confirmation_ttl_sec: int = 120
    # live-only: minimum seconds between preview and place (0 = off). 권장 live+수동 5.
    live_confirm_min_delay_sec: int = 0

    # audit
    audit_log_path: str = "pytossinvest-mcp-audit.log"

    # state backend (HA). redis requires redis_url too.
    state_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = ""

    # remote transport (default stdio = unchanged). http requires auth_token too.
    transport: Literal["stdio", "http"] = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    auth_token: str = ""
    # optional host pinning (defense-in-depth). empty = DNS-rebinding protection off
    # (bearer is the auth surface). non-empty = only these Host headers accepted.
    http_allowed_hosts: list[str] = []

    @field_validator(
        "max_order_amount", "daily_order_limit", "paper_starting_cash",
        "max_order_amount_usd", "daily_order_limit_usd", mode="before",
    )
    @classmethod
    def _no_float(cls, v):
        if isinstance(v, float):
            raise TypeError("money config must be a string or int, never float")
        return v

    @model_validator(mode="after")
    def _live_requires_allow(self):
        if self.mode == "live" and not self.allow_live:
            raise ValueError(
                "mode='live' requires TOSSINVEST_ALLOW_LIVE=1 (double safety gate)"
            )
        return self

    @model_validator(mode="after")
    def _redis_requires_url(self):
        if self.state_backend == "redis" and not self.redis_url:
            raise ValueError(
                "state_backend='redis' requires TOSSINVEST_REDIS_URL"
            )
        return self

    @model_validator(mode="after")
    def _http_requires_auth_token(self):
        if self.transport == "http" and not self.auth_token:
            raise ValueError(
                "transport='http' requires TOSSINVEST_AUTH_TOKEN (an exposed "
                "endpoint must be authenticated)"
            )
        return self

    @property
    def use_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"
