> **언제 읽나**: `pytossinvest` 나 `pytossinvest-mcp` 의 새 버전을 PyPI 에 낼 때.

# PyPI 릴리스

두 패키지는 한 저장소에 있지만 버전도 배포도 따로다. PyPI 의 배포 이름은 `pytossinvest-sdk`(디렉터리·import 이름은 `pytossinvest`)와 `pytossinvest-mcp` 다. `pytossinvest` 라는 이름은 PyPI 에 이미 있는 `py-toss-invest` 와 구분 문자를 지우면 같아져서 등록이 거부된다. 배포는 사람이 올리지 않고, `<배포 이름>-v<버전>` 태그를 푸시하면 `.github/workflows/release.yml` 이 그 패키지의 테스트·빌드·업로드를 한다. 업로드는 PyPI Trusted Publishing(OIDC)을 쓴다. GitHub Actions 실행 자체가 자격증명이라 API 토큰을 저장소에도 개인 머신에도 두지 않는다. 증권계좌 키를 다루는 패키지라, 배포 권한이 장기 토큰 하나에 걸려 있지 않게 하려는 선택이다.

## 전제

- pypi.org 에 각 프로젝트의 Trusted Publisher 가 등록돼 있다. 값은 Owner `ongjin`, Repository `tossinvest`, Workflow `release.yml`, 그리고 Environment 는 `pytossinvest-sdk` 가 `pypi`, `pytossinvest-mcp` 가 `pypi-mcp`. 환경이 패키지마다 다른 이유: PyPI 는 저장소·워크플로·환경이 모두 같은 pending publisher 를 두 프로젝트에 등록해 주지 않는다. 워크플로가 태그를 보고 환경을 고른다. 프로젝트가 아직 PyPI 에 없으면 계정의 Publishing 메뉴에서 pending publisher 로 등록한다. 첫 업로드 때 프로젝트가 만들어진다.
- 낼 변경이 `main` 에 머지돼 있고 CI 가 통과했다.

## 단계

1. 버전을 올린다. 패키지마다 세 곳이다: `pyproject.toml` 의 `version`, `src/<모듈>/__init__.py` 의 `__version__`, 스모크 테스트의 기대값(SDK 는 `tests/test_smoke.py`, MCP 는 `tests/test_mcp_smoke.py`).
2. MCP 가 SDK 의 새 기능을 쓰면 `pytossinvest-mcp/pyproject.toml` 의 `pytossinvest>=` 하한을 그 SDK 버전으로 올린다.
3. 락파일을 맞춘다.

   ```bash
   uv lock
   ```

4. 올리기 전에 산출물을 확인한다.

   ```bash
   uv build --package <배포 이름> --out-dir /tmp/dist
   ```

   ```bash
   uvx twine check /tmp/dist/*
   ```

5. 커밋하고 `main` 에 머지·푸시한다.
6. 태그를 만들어 푸시한다. 두 패키지를 같이 낼 때는 SDK 태그를 먼저 푸시한다. MCP 가 그 SDK 버전을 요구할 수 있다.

   ```bash
   git tag <배포 이름>-v<버전>
   ```

   ```bash
   git push origin <배포 이름>-v<버전>
   ```

## 검증

- GitHub Actions 의 Release 실행이 성공으로 끝난다.
- `https://pypi.org/project/<배포 이름>/` 에 새 버전이 보인다.
- 빈 가상환경에서 `pip install <배포 이름>==<버전>` 이 된다.

## 실패 시

- 업로드 단계가 `invalid-publisher` 로 실패한다. 원인은 pypi.org 에 등록한 값이 워크플로와 다른 것이다(워크플로 파일명과 Environment 를 먼저 본다). 등록을 고친 뒤 실패한 실행을 Re-run 한다. 태그를 다시 만들 필요는 없다.
- "Tag must match the built version" 단계가 실패한다. 원인은 태그의 버전과 `pyproject.toml` 의 버전이 다른 것이다. 태그를 지우고 맞는 이름으로 다시 푸시한다.
- 이미 올라간 버전은 같은 번호로 다시 올릴 수 없다. 버전을 올려 새로 낸다. 잘못 나간 버전은 PyPI 에서 yank 한다.

## 이력

- 2026-11-12 릴리스 워크플로 추가. SDK 배포 이름을 `pytossinvest-sdk` 로 정함(PyPI 가 `pytossinvest` 를 `py-toss-invest` 와 유사하다며 거부). pending publisher 두 개 등록. 첫 배포 대상은 pytossinvest-sdk 0.0.3, pytossinvest-mcp 0.0.2.
