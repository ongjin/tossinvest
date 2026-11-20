# examples

라이브 키 없이 **그냥 실행되는** 데모. `git clone` 후 바로 돌아갑니다(네트워크·키 0).

| 파일 | 무엇을 보여주나 | 실행 |
|---|---|---|
| [`sdk_quickstart.py`](sdk_quickstart.py) | SDK의 금액 검증과 계좌·시세 조회 | `uv run --package pytossinvest-sdk python examples/sdk_quickstart.py` |
| [`mcp_paper_demo.py`](mcp_paper_demo.py) | MCP 안전모델 전 과정 — 가드레일 차단 → preview→place paper 체결 → 멱등 → 감사로그 | `uv run --package pytossinvest-mcp python examples/mcp_paper_demo.py` |

- **SDK 데모**는 키 없이 금액 변환을 보여줍니다. 키를 설정하면 계좌 목록과 삼성전자 시세까지 조회합니다.
- **MCP 데모**는 시세만 작은 가짜 피드로 대체해 `paper` 모드로 전 과정을 돌립니다 — **실주문 0 건**. MCP 클라이언트(Claude Desktop 등) 없이 툴 함수를 직접 호출하는 방식이라 안전모델이 한눈에 보입니다.

> 실제 MCP 클라이언트 연결(Claude Desktop·HTTP 원격)은 [`pytossinvest-mcp/README.md`](../pytossinvest-mcp/README.md) 참고.
