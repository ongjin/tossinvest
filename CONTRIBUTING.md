# 기여 가이드 (Contributing)

기여 환영합니다. 작은 수정도 좋고, 이슈로 먼저 논의해도 좋습니다. 이 프로젝트는 **라이브 키 없이 전부 빌드·테스트** 되도록 설계돼 있어 진입장벽이 0 입니다.

## 개발 환경

[uv](https://docs.astral.sh/uv/) 워크스페이스 모노레포입니다. Python **3.12+**.

```bash
git clone https://github.com/ongjin/tossinvest && cd tossinvest

# 패키지별 동기화 (dev extra 포함)
uv sync --package pytossinvest --extra dev
uv sync --package pytossinvest-mcp --extra dev
```

## 테스트 (라이브 키 불필요, 네트워크 0)

```bash
uv run --package pytossinvest --extra dev pytest pytossinvest/tests        # SDK — respx mock
uv run --package pytossinvest-mcp --extra dev pytest pytossinvest-mcp/tests # MCP — FakeClient + paper
```

PR 은 [CI](.github/workflows/ci.yml)가 위 테스트를 자동으로 돌립니다 — **그린이어야 머지**됩니다. 동작을 바꾸면 테스트도 같이 추가/수정해 주세요(TDD 권장).

## 꼭 지킬 규칙

- **돈/수량은 절대 `float` 가 아니다.** 금액·수량은 전구간 문자열/`Decimal` 입니다. `float` 은 들어오는 순간 `TypeError`(`pytossinvest.money.to_decimal` 이 강제) — 이 불변식을 우회하지 마세요.
- **`place_order` 안전 불변식.** 체결·정정 경로는 반드시 가드레일을 통과하고, preview→place(또는 preview_modify→modify) 2단계 토큰을 거칩니다. 우회 경로를 만들지 마세요. 상세는 [`pytossinvest-mcp/README.md`](pytossinvest-mcp/README.md).
- **SDK 공개 API 를 깨지 마세요.** `pytossinvest-mcp` 가 `pytossinvest` 에 의존합니다. SDK 시그니처/반환 타입을 바꾸면 MCP 테스트도 그린인지 확인하세요.
- **외과적 변경.** 요청·이슈에 직접 연결되지 않는 인접 코드 리팩터·포맷 변경은 피하고, 기존 스타일을 따르세요.

## 브랜치 & 커밋

- 브랜치 전략은 **`main` 단일**. 기능 작업은 `feat/<name>` 브랜치 → 리뷰 후 머지.
- 커밋 메시지는 **Conventional Commits** 스타일(`feat:`, `fix:`, `docs:`, `ci:`, `refactor:` …)을 따릅니다.

## 라이선스

기여물은 각 패키지의 라이선스로 배포됩니다 — `pytossinvest` 는 **MIT**, `pytossinvest-mcp` 는 **Apache-2.0**. PR 을 열면 해당 라이선스에 동의하는 것으로 간주합니다.
