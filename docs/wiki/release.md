> **언제 읽나**: `pytossinvest` 나 `pytossinvest-mcp` 의 새 버전을 PyPI 에 낼 때.

# PyPI 릴리스

두 패키지는 한 저장소에 있지만 버전도 배포도 따로다. 배포는 사람이 올리지 않고, `<패키지>-v<버전>` 태그를 푸시하면 `.github/workflows/release.yml` 이 그 패키지의 테스트·빌드·업로드를 한다. 업로드는 PyPI Trusted Publishing(OIDC)을 쓴다. GitHub Actions 실행 자체가 자격증명이라 API 토큰을 저장소에도 개인 머신에도 두지 않는다. 증권계좌 키를 다루는 패키지라, 배포 권한이 장기 토큰 하나에 걸려 있지 않게 하려는 선택이다.

## 전제

- pypi.org 에 각 프로젝트의 Trusted Publisher 가 등록돼 있다. 값은 Owner `ongjin`, Repository `tossinvest`, Workflow `release.yml`, Environment `pypi`. 프로젝트가 아직 PyPI 에 없으면 계정의 Publishing 메뉴에서 같은 값으로 pending publisher 를 등록한다. 첫 업로드 때 프로젝트가 만들어진다.
- 낼 변경이 `main` 에 머지돼 있고 CI 가 통과했다.

## 단계

1. 버전을 올린다. 패키지마다 세 곳이다: `pyproject.toml` 의 `version`, `src/<모듈>/__init__.py` 의 `__version__`, `tests/test_smoke.py` 의 기대값.
2. MCP 가 SDK 의 새 기능을 쓰면 `pytossinvest-mcp/pyproject.toml` 의 `pytossinvest>=` 하한을 그 SDK 버전으로 올린다.
3. 락파일을 맞춘다.

   ```bash
   uv lock
   ```

4. 올리기 전에 산출물을 확인한다.

   ```bash
   uv build --package <패키지> --out-dir /tmp/dist
   ```

   ```bash
   uvx twine check /tmp/dist/*
   ```

5. 커밋하고 `main` 에 머지·푸시한다.
6. 태그를 만들어 푸시한다. 두 패키지를 같이 낼 때는 SDK 태그를 먼저 푸시한다. MCP 가 그 SDK 버전을 요구할 수 있다.

   ```bash
   git tag <패키지>-v<버전>
   ```

   ```bash
   git push origin <패키지>-v<버전>
   ```

## 검증

- GitHub Actions 의 Release 실행이 성공으로 끝난다.
- `https://pypi.org/project/<패키지>/` 에 새 버전이 보인다.
- 빈 가상환경에서 `pip install <패키지>==<버전>` 이 된다.

## 실패 시

- 업로드 단계가 `invalid-publisher` 로 실패한다. 원인은 pypi.org 에 등록한 값이 워크플로와 다른 것이다(워크플로 파일명과 Environment 를 먼저 본다). 등록을 고친 뒤 실패한 실행을 Re-run 한다. 태그를 다시 만들 필요는 없다.
- "Tag must match the built version" 단계가 실패한다. 원인은 태그의 버전과 `pyproject.toml` 의 버전이 다른 것이다. 태그를 지우고 맞는 이름으로 다시 푸시한다.
- 이미 올라간 버전은 같은 번호로 다시 올릴 수 없다. 버전을 올려 새로 낸다. 잘못 나간 버전은 PyPI 에서 yank 한다.

## 이력

- 2026-11-12 릴리스 워크플로 추가. 첫 배포 대상은 pytossinvest 0.0.3, pytossinvest-mcp 0.0.2.
