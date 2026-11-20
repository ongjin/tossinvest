"""pytossinvest (SDK) 빠른 시작 — 라이브 키 없이 그냥 실행됩니다.

이 파일은 두 부분으로 나뉩니다:
  1) 돈/Decimal 안전계약 — 네트워크·키 0 으로 항상 실행 (SDK 의 핵심 차별점).
  2) 계좌·시세 조회 예시 — 환경변수에 키가 있을 때만 실행 (없으면 안전하게 스킵).

실행:
    uv run --package pytossinvest-sdk python examples/sdk_quickstart.py

실 API 까지 보려면 키를 주고 실행:
    TOSSINVEST_CLIENT_ID=... TOSSINVEST_CLIENT_SECRET=... \
      uv run --package pytossinvest-sdk python examples/sdk_quickstart.py
"""

from __future__ import annotations

import os
from decimal import Decimal

from pytossinvest import (
    TossInvestClient,
    to_decimal,
    decimal_to_str,
)


def demo_money_is_never_float() -> None:
    """돈/수량은 전구간 문자열/Decimal — float 은 들어오는 순간 거부됩니다."""
    print("── 1) 돈은 절대 float 이 아니다 ──")

    print("  to_decimal('70000')   ->", repr(to_decimal("70000")))   # Decimal('70000')
    print("  to_decimal(70000)     ->", repr(to_decimal(70000)))     # int 는 허용
    print("  decimal_to_str(...)   ->", decimal_to_str(Decimal("70000.50")))  # '70000.50' (지수표기 X)

    for bad in (70000.0, True):  # float·bool 은 금지
        try:
            to_decimal(bad)
        except TypeError as e:
            print(f"  to_decimal({bad!r}) -> TypeError ({e}) ✓ 막힘")
    print()


def demo_real_usage() -> None:
    """키가 있을 때만 — 계좌와 시세를 조회합니다."""
    print("── 2) 실 API 사용 (키 있을 때만) ──")
    client_id = os.environ.get("TOSSINVEST_CLIENT_ID")
    client_secret = os.environ.get("TOSSINVEST_CLIENT_SECRET")
    if not (client_id and client_secret):
        print("  TOSSINVEST_CLIENT_ID/SECRET 없음 → 실 API 호출 스킵.")
        print("  (위 안전계약 데모만으로도 SDK 의 핵심은 다 보입니다.)\n")
        return

    with TossInvestClient(client_id=client_id, client_secret=client_secret) as c:
        # 계좌 — 첫 호출 시 accountSeq 자동 캐싱 (계좌 헤더용)
        accounts = c.get_accounts()
        print("  accounts:", accounts)

        # 시세 — 계좌 컨텍스트 불필요. last_price 는 Decimal (절대 float 아님)
        prices = c.get_prices(["005930"])
        if prices:
            print("  005930 last_price:", repr(prices[0].last_price))
        else:
            print("  005930 시세가 없습니다.")

    print()


if __name__ == "__main__":
    demo_money_is_never_float()
    demo_real_usage()
    print("done.")
