"""pytossinvest-mcp 안전모델 데모 — 라이브 키 없이 그냥 실행됩니다.

MCP 툴은 `AppContext` 를 받는 평범한 함수라, MCP 클라이언트 없이도 직접 호출해
안전모델 전 과정을 보여줄 수 있습니다(테스트 스위트와 같은 방식). paper 모드라
실주문 0 건 — 시세만 작은 가짜 피드로 대체하면 키도 네트워크도 필요 없습니다.

보여주는 것:
  1) 가드레일이 과대 주문을 막는다           (preview 단계에서 거부)
  2) preview → place 2단계로 paper 체결      (현금이 실제로 줄어듦)
  3) 토큰은 성공 시 1회만 소비 (멱등)         (같은 토큰 2차 발사 거부)
  4) 모든 결정이 감사 로그(JSONL)에 남는다

실행:
    uv run --package pytossinvest-mcp python examples/mcp_paper_demo.py
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from pytossinvest.models import Price
from pytossinvest_mcp.audit import AuditLog
from pytossinvest_mcp.config import Settings
from pytossinvest_mcp.paper import MemoryPaperStore, PaperBroker
from pytossinvest_mcp.safety import GuardrailError, SafetyManager
from pytossinvest_mcp.stores import MemorySpendStore, MemoryTokenStore
from pytossinvest_mcp.tools import (
    AppContext,
    get_order_readiness,
    place_order,
    preview_order,
)

KST = ZoneInfo("Asia/Seoul")
STARTING_CASH = "10000000"  # 1,000만원


class PriceFeed:
    """paper preview/place 가 권위 통화·참조가를 얻으려 get_prices 만 호출한다.

    실 서버에선 진짜 TossInvestClient(시세는 계좌와 무관해 키만 있으면 됨)지만,
    데모에선 005930 을 70,000원으로 고정한 가짜 피드로 충분하다.
    """

    def get_prices(self, symbols):
        return [Price.model_validate(
            {"symbol": s, "lastPrice": "70000", "currency": "KRW"}) for s in symbols]


def build_app() -> AppContext:
    """conftest.make_app 과 같은 와이어링을, pytest 없이 실시계로."""
    settings = Settings(_env_file=None, mode="paper", paper_starting_cash=STARTING_CASH)
    safety = SafetyManager(
        settings,
        now=time.monotonic,                       # 토큰 TTL 용 (실시계)
        today=date.today,                         # 일일 누적 리셋 기준
        gen_id=lambda: "demo-" + uuid.uuid4().hex[:8],
        token_store=MemoryTokenStore(),
        spend_store=MemorySpendStore(),
    )
    counter = itertools.count(1)
    paper = PaperBroker(
        MemoryPaperStore(starting_cash=STARTING_CASH),
        next_id=lambda: f"paper-{next(counter)}",
    )
    audit_path = os.path.join(tempfile.mkdtemp(), "audit.log")
    return AppContext(
        config=settings, client=PriceFeed(), paper=paper, safety=safety,
        audit=AuditLog(audit_path), now_kst=lambda: datetime.now(KST),
    )


def main() -> None:
    app = build_app()

    print("── 0) 주문 전 점검 (paper 포트폴리오) ──")
    readiness = get_order_readiness(app, "005930")
    print("  매수여력:", readiness["buyingPower"], "원\n")

    print("── 1) 가드레일: 과대 주문은 preview 에서 막힌다 ──")
    # 100주 × 70,000 = 7,000,000원 > 주문당 상한(기본 1,000,000원)
    try:
        preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                      quantity="100", price="70000")
    except GuardrailError as e:
        print(f"  100주 미리보기 -> 거부 code={e.code!r} ✓ (한도 초과는 체결 근처도 못 감)\n")

    print("── 2) 정상 주문: preview → place 2단계 체결 ──")
    pv = preview_order(app, symbol="005930", side="BUY", order_type="LIMIT",
                       quantity="10", price="70000")  # 700,000원 (한도 내)
    print("  preview: 예상비용", pv["estimatedNotional"], "원, token=", pv["confirmationToken"][:16], "…")
    out = place_order(app, confirmation_token=pv["confirmationToken"])
    print("  place:", out["status"], "| 체결 후 매수여력:", app.paper.buying_power(), "원\n")

    print("── 3) 멱등: 토큰은 성공 시 1회만 소비된다 ──")
    try:
        place_order(app, confirmation_token=pv["confirmationToken"])  # 같은 토큰 재발사
    except GuardrailError as e:
        print(f"  같은 토큰 재place -> 거부 code={e.code!r} ✓ (두 번 체결 불가)\n")

    print("── 4) 감사 로그 (JSONL, append-only) ──")
    for ev in app.audit.read_events():
        print("  ", json.dumps(ev, ensure_ascii=False))
    print("\ndone. (실주문 0 건 — 전부 paper 시뮬)")


if __name__ == "__main__":
    main()
