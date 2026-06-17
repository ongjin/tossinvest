# SDK 레이트리밋 복원력 설계 — B3 헤더 동기화 + B4 자동 retry/backoff

**날짜:** 2026-06-18
**대상 패키지:** `pytossinvest` (`client.py`, `ratelimit.py`)
**상태:** 설계 확정 (구현 플랜 대기)
**선행:** v0.0.1 은 정적 `_GROUP_RATES` + 09:00–09:10 피크 반토막만. 429 면 `RateLimitError` throw, 재시도 정책은 호출자/MCP 책임. 본 스펙은 그 v0.0.2 후속(B3·B4).

---

## 1. 배경 / 문제

`pytossinvest` 의 레이트리밋은 두 가지가 미구현으로 남아 있었다.

- **B3 — `X-RateLimit-*` 헤더 동적 동기화 미구현.** 버킷은 문서 표의 정적 기본값(`_GROUP_RATES`)으로만 페이싱하고, 응답 헤더(`X-RateLimit-Limit`/`Remaining`/`Reset`)를 반영하지 않는다. 토스 API 는 한도를 사전 공지 없이 조정할 수 있어 "**헤더가 source of truth**"인데 따라가지 못한다.
- **B4 — 자동 retry/backoff 미구현.** 429 면 `RateLimitError.retry_after` 를 던지고 끝. 공식 권장(① `Retry-After` 대기 후 재시도 ② 지수 백오프+jitter ③ Remaining 낮으면 선제 감속)을 호출자에게 떠넘긴다. MCP 도 자체 재시도가 없어 결국 사용자에게 에러가 전파된다.

두 항목은 같은 `_request` 경로에서 맞물린다: 429 응답의 헤더로 버킷을 동기화하고(B3), 그 응답을 `Retry-After` 만큼 대기 후 재시도한다(B4). 한 스펙으로 묶는다.

## 2. 외부 API 사실 (source of truth)

`docs/claude/tossinvest-open-api.md` §3 기준:
- 응답 헤더(정상·429 공통): `X-RateLimit-Limit`(현재 burst capacity) · `X-RateLimit-Remaining`(남은 토큰, 429 시 0) · `X-RateLimit-Reset`(토큰 1개 재충전 예상 초) · `Retry-After`(429 에만, 초).
- 429 대응 공식 3원칙: (1) `Retry-After` 대기 후 재시도, (2) 지수 백오프(1→2→4…)+jitter, (3) `Remaining` 낮으면 429 전에 선제 감속. 한도 수치는 공지 없이 바뀌므로 **표 숫자 하드코딩 금지**.

이 헤더는 기존 `TokenBucket(capacity, refill_per_sec)` 모델에 직접 매핑된다:
- `X-RateLimit-Limit` → `capacity`
- `X-RateLimit-Reset`(초/토큰) → `refill_per_sec = 1 / Reset`
- `X-RateLimit-Remaining` → 현재 토큰 수

## 3. 목표 / 비목표

**목표**
- 응답 헤더로 그룹별 버킷을 동기화(헤더가 진실)하고, 그 부수효과로 선제 감속을 얻는다.
- 429 를 bounded 자동 재시도(기본 켬)하되 소진 시 종전대로 throw.
- 공개 API 비파괴(생성자 인자 추가만), 기존 46 SDK 테스트 무회귀, MCP 무변경.

**비목표**
- 5xx·타임아웃 자동 재시도(쓰기 중복 위험 — 호출자 책임 유지).
- 비동기/async 클라이언트(여전히 sync httpx).
- 영속적 한도 캐시(프로세스 메모리 내 버킷만).

## 4. B3 — `X-RateLimit-*` 헤더 → 토큰버킷 동기화

### 4.1 동기화 동작
- `_request` 가 응답을 받으면(200·429 공통) 그 요청의 `group` 에 대해 `_sync_bucket_from_headers(group, resp.headers)` 호출.
- 세 헤더가 모두 존재·파싱 가능하면: `bucket.capacity = Limit`; `Reset > 0` 이면 `bucket.refill_per_sec = 1 / Reset`; `bucket._tokens = min(bucket._tokens, Remaining)`(서버가 더 적게 남았다고 하면 신뢰해 깎음, 절대 부풀리지 않음).
- 헤더 누락 / `Reset <= 0` / 숫자 파싱 실패 → 해당 항목만 skip(무동작). 헤더가 전혀 없으면 완전 무동작 → 헤더 없는 기존 respx mock 테스트 무영향.

### 4.2 source of truth 일원화 (피크 반토막 reconciliation)
- 그룹별 "헤더 수신 이력" 플래그(`_rate_from_header: dict[str, bool]` 또는 동등) 유지.
- `_gate(group)`: 해당 그룹이 **헤더를 한 번이라도 받았으면** 버킷의 현재 capacity/refill(=헤더 유래)을 그대로 쓰고 **정적 `effective_rate` 피크 반토막을 적용하지 않는다**(서버 헤더가 09:00–09:10 반토막을 이미 반영하므로 이중 적용 방지). 아직 못 받았으면 종전대로 `effective_rate(group, _GROUP_RATES[group], now_kst)` 로 capacity/refill 설정.

### 4.3 선제 감속 (공식 #3) — 부수효과
`_tokens = min(_tokens, Remaining)` 로 맞추면 Remaining 이 낮을 때 버킷 토큰이 적어 `_gate` 의 `while not try_acquire: sleep(time_until_available())` 가 자연히 대기한다. 별도 메커니즘 불필요.

## 5. B4 — 429 자동 retry (bounded, 기본 켬)

### 5.1 재시도 루프
- `_request` 에 429 한정 재시도. 기존 401 expired-token one-shot 재시도와 독립 공존(별도 카운터 `_attempt: int = 0`).
- 429 수신 & `_attempt < max_retries` 이면: 대기 시간 계산 → `self._sleep(wait)` → `_attempt+1` 로 재귀(또는 루프) 재시도(재시도 시 `_gate` 재통과). `_attempt >= max_retries` 이면 종전대로 `error_from_response` → `RateLimitError` throw.
- 429 응답에도 §4 헤더 동기화는 적용(버킷이 Remaining=0/Reset 반영).

### 5.2 대기 시간 계산 (`ratelimit.py` 순수 함수)
`backoff_wait(attempt, retry_after, *, base=1.0, cap, rng) -> float`:
- `retry_after` 가 있으면(>0) 그 값을 사용, 없으면 지수 백오프 `base * 2**attempt`(1→2→4…)에 **full jitter**(`rng()` 곱) 적용.
- 결과를 `cap`(= `retry_max_wait`)으로 클램프(병적인 `Retry-After`/백오프로 무한 대기 방지).
- 순수 함수라 주입된 `rng` 로 결정적 테스트 가능.

### 5.3 안전 / 멱등
- **429 한정**: 429 는 레이트리밋으로 요청이 **미실행** → POST(place/modify/cancel)도 재시도 안전(중복 주문 없음). 5xx·타임아웃은 재시도 안 함(처리 여부 불확실 → 호출자 책임).
- 재시도해도 호출자가 넘긴 `clientOrderId` 그대로 → 주문 멱등성 불변.
- 최종 실패는 항상 기존 `RateLimitError` 타입으로 throw → 호출자 계약 동일.

## 6. 공개 API 변경 (비파괴적)

`TossInvestClient.__init__` 에 인자 추가(기존 `sleep`/`monotonic`/`now_kst` 주입 패턴과 동일):
- `max_retries: int = 3` — 429 자동 재시도 횟수. `0` 이면 즉시 throw(= v0.0.1 동작).
- `retry_max_wait: float = 60.0` — per-wait 상한(초).
- `rng: Callable[[], float] = random.random` — jitter 소스(테스트 주입용).

기본값만으로 켜지므로 기존 호출자는 코드 변경 없이 복원력 향상. MCP 무변경.

## 7. 테스트 전략 (TDD, respx)

**B3**
- 200 응답에 `X-RateLimit-Limit/Remaining/Reset` 주면 해당 group 버킷의 capacity/refill/tokens 갱신(`refill = 1/Reset`).
- `Remaining` 이 현재 토큰보다 작으면 tokens 클램프(절대 부풀리지 않음).
- 헤더 없으면 정적값 유지(무회귀).
- 헤더 수신 후엔 피크 시간대라도 반토막 미적용(헤더값 사용).

**B4**
- 429→200 시퀀스: 1회 재시도 후 성공(주입 sleep 으로 즉시).
- `Retry-After` 존중(그 초만큼 sleep 호출 인자 검증).
- `Retry-After` 없으면 지수 백오프+jitter(주입 rng 로 결정화), `cap` 클램프.
- `max_retries` 소진 → `RateLimitError`.
- `max_retries=0` → 즉시 throw(재시도/슬립 없음).
- **5xx 는 재시도 안 함**(1회 호출 후 즉시 ServerError).
- POST 재시도 시 동일 `clientOrderId` 전송.

**무회귀**: SDK 전체(`uv run --package pytossinvest --extra dev pytest pytossinvest/tests -q`) + MCP(`uv run --package tossinvest-mcp pytest tossinvest-mcp/tests -q`). 기존 429 테스트는 새 동작(주입 sleep 또는 `max_retries=0`)에 맞게 갱신.

## 8. 영향 파일 요약

| 파일 | 변경 |
|---|---|
| `pytossinvest/src/pytossinvest/client.py` | 생성자 `max_retries`/`retry_max_wait`/`rng`; `_sync_bucket_from_headers`; group별 헤더-수신 플래그 + `_gate` 헤더-우선 분기; `_request` 429 재시도 루프 + 응답 후 헤더 동기화 |
| `pytossinvest/src/pytossinvest/ratelimit.py` | `backoff_wait(attempt, retry_after, *, base, cap, rng)` 순수 함수 |
| `pytossinvest/tests/test_client_core.py` (+ ratelimit 테스트 파일) | B3/B4 테스트, 기존 429 테스트 갱신 |
| `docs/claude/pytossinvest-sdk.md`, `CLAUDE.md`(SDK 규약), `pytossinvest/README.md` | 레이트리밋 동적동기화·자동 retry 구현됨으로 갱신(v0.0.1 한계 문구 제거) |
