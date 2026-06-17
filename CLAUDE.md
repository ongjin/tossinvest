# CRITICAL RULES

- **AI 작성 표시 금지**: 커밋 메시지·코드 주석·문서 어디에도 AI 가 작성했다는 내용을 넣지 않는다 (`Co-Authored-By: Claude`, "Generated with AI" 등 전부 금지). **공개 OSS 레포**라 더 엄격히.
- **커밋/푸시/머지는 요청 시에만**. 브랜치 전략은 `main` 단일. feature 작업은 `feat/<name>` 브랜치 → 리뷰 후 머지.
- **돈/수량 float 금지** — 전구간 **문자열/Decimal**. SDK 의 `pytossinvest.money.to_decimal` 이 float 를 `TypeError` 로 거부(강제). MCP 레이어도 입출력 모두 문자열 유지(JSON/Decimal 안전).
- **SDK 공개 API 깨지 말 것** — `tossinvest-mcp` 가 `pytossinvest` 에 의존. SDK 시그니처/반환 타입 변경 시 **MCP 테스트도 그린 확인** 후 진행.
- **`place_order` 안전 불변식 (프로젝트 핵심)** — 체결 경로(`paper.place` / `client.place_order`)는 **반드시 `safety.check_guardrails` 를 거친다**. confirmation 토큰은 `preview_order` 에서만, 가드레일 통과 후 발급. `place_order` 는 `consume(token)` → 실행 → **성공 시에만 `finalize`**(실패하면 토큰 살아남아 같은 `clientOrderId` 로 멱등 재시도). 이 불변식을 우회하는 변경 금지.

# 토스증권 Open API 오픈소스 (pytossinvest + tossinvest-mcp)

토스증권 Open API 를 AI/퀀트에 연결하는 **오픈소스**. 1순위 목표는 **평판/포트폴리오**(수익은 스폰서·콘텐츠 부차). 차별점 = "어떻게 LLM 에게 진짜 증권계좌 키를 **안전하게** 쥐여주나"(안전모델). API 는 2026-06 기준 **사전신청 단계**라 라이브 키 없이 전부 만들고·테스트·데모 가능하게 설계됨(paper 모드 + mock fixture).

## Tech Stack

- **Runtime**: Python 3.12, **uv 워크스페이스** 모노레포 (hatchling build)
- **`pytossinvest`** (SDK, **MIT**): `httpx`(sync) + `pydantic` v2. 토큰매니저·그룹별 레이트리미터·decimal-safe money·code 기반 에러·17 엔드포인트.
- **`tossinvest-mcp`** (MCP 서버, **Apache-2.0**, SDK 의존): `mcp`(FastMCP, stdio) + `pydantic-settings`. 안전모델(모드·가드레일·preview/confirm·멱등성·감사로그) + 13 툴.
- **테스트**: `pytest`. SDK 는 `respx` 로 httpx mock, MCP 는 `FakeClient` + paper 엔진 — **라이브 키 불필요, 네트워크 0**.

## Project Structure

```
toss/
├── pyproject.toml                 # uv workspace: members = ["pytossinvest", "tossinvest-mcp"]
├── pytossinvest/                  # SDK (MIT)
│   └── src/pytossinvest/          # money · errors · ratelimit · auth · models · client
├── tossinvest-mcp/                # MCP 서버 (Apache-2.0)
│   └── src/tossinvest_mcp/        # config · audit · paper · market_hours · safety · tools · server
└── docs/
    ├── claude/tossinvest-open-api.md          # ★ 토스 API 레퍼런스 (코어 — 코드 손대기 전 읽기)
    └── superpowers/specs|plans/               # 설계 스펙 + 구현 플랜 (Plan 1 SDK / Plan 2 MCP)
```

## Commands

```bash
# 의존성 동기화 (패키지별)
uv sync --package tossinvest-mcp --extra dev

# 테스트
uv run --package pytossinvest --extra dev pytest pytossinvest/tests   # SDK (42) — respx mock
uv run --package tossinvest-mcp pytest tossinvest-mcp/tests           # MCP (64) — FakeClient

# MCP 서버 실행 (stdio — Claude Desktop/Cursor 등 MCP 클라이언트용)
TOSSINVEST_MODE=paper TOSSINVEST_CLIENT_ID=... TOSSINVEST_CLIENT_SECRET=... \
  uv run --package tossinvest-mcp tossinvest-mcp
```

## Conventions

- **money/quantity**: 전부 문자열/Decimal. float 진입 경로 자체를 안 만든다. (위 CRITICAL RULES)
- **SDK 규약** (`pytossinvest`): 응답 `result` 자동 언래핑(토큰 엔드포인트 제외)·**`code` 기반 에러 분기**(unknown code 관용)·**`X-RateLimit-*` 헤더가 진실**(표 숫자 하드코딩 금지)·`accountSeq` 1회 캐싱(ACCOUNT 1/s)·**`clientOrderId` 멱등성 수동**(10분). v0.0.1 한계: 헤더 동적 동기화·자동 retry/backoff 미구현(`RateLimitError.retry_after` 던짐 → 호출자/ MCP 책임).
- **MCP 안전모델** (`tossinvest-mcp`): 3모드 `TOSSINVEST_MODE` = `read_only`(주문툴 미등록) / **`paper`**(기본, 로컬 `PaperBroker` 체결) / `live`(`TOSSINVEST_ALLOW_LIVE=1` 까지 있어야 켜짐 — config validator 가 이중게이트). 가드레일(주문당·일일 상한·allow/deny·1억↑ confirm 필수·30억↑ 거부·장시간 게이트는 live 전용). preview→place 2단계 + consume-on-success 멱등성 + 감사로그(JSONL).
- **설정**: `tossinvest-mcp` 는 `pydantic-settings`, env prefix `TOSSINVEST_` (`MODE`/`ALLOW_LIVE`/`CLIENT_ID`/`CLIENT_SECRET`/`MAX_ORDER_AMOUNT`/`DAILY_ORDER_LIMIT`/`ALLOW_SYMBOLS`/`DENY_SYMBOLS`/`ENFORCE_MARKET_HOURS`). 상세는 `tossinvest-mcp/README.md`.
- **라이선스**: SDK=**MIT**, MCP=**Apache-2.0**. 각 패키지에 `LICENSE`(+MCP `NOTICE`) + `pyproject.toml` `license` 필드. README 에 명시. "비공식(unofficial) 클라이언트" 표기로 토스 상표/엔도르스먼트 오해 방지.

## 주의할 함정 (이미 겪었거나 설계로 막은 것)

- **토스 API 사전신청 단계** — 한도·정책·엔드포인트·필드가 정식 오픈 전까지 바뀔 수 있다. `docs/claude/tossinvest-open-api.md` 는 **스냅샷**이니 막히면 canonical `openapi.json` 재확인(자가갱신).
- **paper modify/cancel 은 live 전용** — paper 는 즉시체결 모델이라 정정/취소할 미체결 주문이 없음(`PaperError` 로 명확히 거부, 실제 `409 already-filled` 미러링).
- **paper MARKET 체결가** — 체결 시점에 시세가 비면 가격 0 으로 조용히 체결되던 버그를 막음 → ref price 없으면 `PaperError`(토큰 살려둠, 재시도 가능).
- **notional 통화** — 가드레일은 주문통화 기준 비교(FX 환산 X). 1억/30억 임계는 KRW 가정.
- **market_hours US 자정넘김** — 미국장을 KST 로 표기하면 23:30→06:00 처럼 자정을 넘긴다. `start > end` 면 wrap 윈도우(`now >= start or now < end`)로 처리. 깨진 시간 문자열은 "닫힘"으로 안전 처리.
- **MCP 테스트 import** — 테스트에서 `conftest` 의 `FakeClient` 는 `from conftest import FakeClient`(pytest 가 tests/ 를 sys.path 에 넣음). `from tests.conftest` 는 `tests` 패키지가 없어 깨진다.

## 추가 문서 (docs/)

특정 작업 들어갈 때 아래를 직접 읽어와서 참고.

- [docs/claude/tossinvest-open-api.md](docs/claude/tossinvest-open-api.md) — **토스증권 Open API 레퍼런스 (코어)**. 인증 2단(`X-Tossinvest-Account`)·엔드포인트 전체·요청/응답 스키마·enum·rate limit 10그룹·에러코드 전체표·주문 함정(멱등성 10분·고액확인·US 금액주문·OrderStatus 10종). SDK 엔드포인트 추가·MCP 툴·주문 로직 손대기 **전에** 읽는다.
- [docs/superpowers/specs/2026-06-17-tossinvest-mcp-design.md](docs/superpowers/specs/2026-06-17-tossinvest-mcp-design.md) — **설계 확정본**. 모드 3단계·안전모델(§3)·툴 매핑(§4)·크로스커팅 인프라(§5)·테스트 전략(§6)·규제 메모(§7)·라이선스(§9). 무엇을 왜 이렇게 만들었나 궁금할 때.
- [docs/superpowers/plans/2026-06-17-pytossinvest-sdk.md](docs/superpowers/plans/2026-06-17-pytossinvest-sdk.md) — **SDK 구현 플랜 (Plan 1)**. TDD 태스크별 코드. SDK 구조/패턴 참고.
- [docs/superpowers/plans/2026-06-17-tossinvest-mcp-server.md](docs/superpowers/plans/2026-06-17-tossinvest-mcp-server.md) — **MCP 구현 플랜 (Plan 2)**. 모듈별 TDD 태스크·안전 불변식·테스트. MCP 구조 참고.

## 🔄 문서 자가갱신 (lean 유지 — 꼭 지킬 것)

- **작업 끝나면 동기화 (별도 요청 없이)**: 의미 있는 변경(새 모듈·엔드포인트·MCP 툴·모드·가드레일·새 함정·새 컨벤션/환경변수)은 **같은 세션에서** 이 `CLAUDE.md` 관련 섹션 + `docs/` 문서를 갱신한다. **커밋/푸시는 수동** (문서 갱신 ≠ 커밋). 자잘한 버그픽스·일회성 작업은 제외.
- **토스 API 레퍼런스(`docs/claude/tossinvest-open-api.md`)는 스냅샷**: 코드 작업 중 canonical `openapi.json` 과 불일치(새 필드·엔드포인트·enum·함정)를 발견하면 **그 자리에서 그 문서를 갱신**한다. 권위 순서 ① openapi.json → ② developers.tossinvest.com/docs → ③ 본 문서.
- **CLAUDE.md 는 high-signal 만** (CRITICAL RULES·Tech Stack·Structure·Commands·Conventions·함정·docs 인덱스). 한 주제가 길어지고 *특정 작업 시에만* 필요하면 `docs/claude/<topic>.md` 로 분할하고 본문엔 인덱스 한 줄만. 인덱스 hook(언제 읽나)이 부정확해지면 같이 고친다.
