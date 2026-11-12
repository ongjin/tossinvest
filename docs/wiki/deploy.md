> **언제 읽나**: MCP 서버를 서버에 올리거나 새 버전으로 바꿀 때, 배포가 실패해 되돌려야 할 때, 새 서버를 처음 세팅할 때.

# 배포

MCP 서버를 띄우는 방법은 두 가지다. 소스에서 빌드하는 로컬 스택(`deploy/docker-compose.yml`, Redis 포함)과, PyPI 배포본으로 이미지를 만들어 원격 서버에 올리는 방식이다. 이 문서는 뒤쪽을 다룬다. 원격 서버에는 저장소 소스를 두지 않고, 해시까지 고정한 PyPI 패키지로만 이미지를 만든다. 배포는 `deploy/remote.sh` 한 줄이고, 그 안에서 백업·빌드·사전 검사·교체·검증을 하며, 새 컨테이너가 검증을 통과하지 못하면 스스로 이전 것으로 되돌린다.

로컬 스택은 [pytossinvest-mcp/README.md](../../pytossinvest-mcp/README.md) 의 'HTTP 원격 모드'를 본다. PyPI 에 새 버전을 내는 절차는 [release.md](release.md).

## 구성

| 파일 | 역할 |
|---|---|
| `deploy/pypi/requirements.in` | 서버에 올릴 버전. `pytossinvest-sdk`·`pytossinvest-mcp[http]` 와 `mcp`·`uvicorn` 을 `==` 로 고정한다 |
| `deploy/pypi/requirements.txt` | 위 파일에서 만든 잠금 파일. 하위 의존성까지 버전과 해시가 들어 있다 |
| `deploy/pypi/Dockerfile` | `pip install --require-hashes` 와 `pip check` 로 설치한다. 빌드 context 가 이 폴더라 이미지에는 잠금 파일만 들어간다 |
| `deploy/preflight.py` | 컨테이너 안에서 도는 검사. 인증 없는 요청 거부, MCP 초기화, 도구 목록(`read_only` 면 주문 도구가 없는지)을 본다. 토스 API 는 부르지 않는다 |
| `deploy/remote.sh` | 작업 머신에서 실행하는 드라이버 |
| `deploy/server-update.sh` | 서버에서 도는 부분. `remote.sh` 가 넘겨 실행하며 직접 부르지 않는다 |
| `deploy/remote.env` | 배포 대상. gitignore 돼 있다. 형식은 `deploy/remote.env.example`. `DEPLOY_*` 설정만 들어간다 |

이미지 태그는 고정한 버전에서 나온다. `pytossinvest-mcp` 0.0.3 과 `pytossinvest-sdk` 0.0.4 면 `pypi-mcp0.0.3-sdk0.0.4` 다.

대상은 환경변수가 먼저고, 환경변수에 없는 것만 `remote.env` 에서 채운다. 어디로 무엇이 나갈지는 아무것도 건드리지 않는 명령으로 미리 볼 수 있다.

```bash
deploy/remote.sh target
```

## 새 버전 배포

### 전제

- 올릴 버전이 PyPI 에 있다([release.md](release.md)).
- `deploy/remote.env` 에 `DEPLOY_HOST`(ssh 호스트)와 `DEPLOY_DIR` 이 적혀 있다.
- 서버의 `DEPLOY_DIR` 에 `docker-compose.yml` 이 있고 서비스가 떠 있다(없으면 아래 '서버 첫 세팅').
- 작업 머신에 `uv` 가 있다(잠금 파일을 만들 때만 쓴다).

### 단계

1. `deploy/pypi/requirements.in` 의 두 패키지 버전을 올린다.
2. 잠금 파일을 다시 만든다.

   ```bash
   deploy/remote.sh lock
   ```

3. 바뀐 줄이 의도한 패키지뿐인지 본다.

   ```bash
   git diff deploy/pypi/requirements.txt
   ```

4. 대상을 확인하고 배포한다.

   ```bash
   deploy/remote.sh target
   ```

   ```bash
   deploy/remote.sh
   ```

`remote.sh` 는 잠금 파일이 `requirements.in` 과 맞는지 확인하고 빌드 파일을 서버의 `DEPLOY_DIR/.incoming` 에 올린 뒤, 서버에서 다음을 순서대로 한다.

1. **백업**: `backup-<UTC 시각>/` 에 compose·env 파일·빌드 파일을 복사한다(소유자만 읽을 수 있다). 지금 실행 중인 이미지에 `rollback-<시각>` 태그를 붙이고 `rollback.sh` 를 쓴다.
2. **빌드**: 새 이미지를 버전 태그로 만든다. 실행 중인 것은 건드리지 않는다.
3. **사전 검사**: 새 이미지를 `--network none` 과 임시 bearer 로 띄워 `preflight.py` 를 돌린다. 실제 자격증명은 쓰지 않는다.
4. **교체**: compose 의 `image:` 줄에서 태그만 바꾸고 `docker compose up -d --no-deps --no-build --pull never <서비스>` 로 그 서비스만 다시 만든다.
5. **검증**: 실제 컨테이너에서 `preflight.py` 를 다시 돌리고, `DEPLOY_POST_CHECK` 가 있으면 실행한다.

4나 5가 실패하면 그 자리에서 `rollback.sh` 를 실행해 이전 이미지로 되돌리고 0이 아닌 코드로 끝난다. 3까지의 실패는 실행 중인 서비스에 영향이 없다.

### 검증

- 출력이 `[5/5] verified` 로 끝난다.
- `deploy/remote.sh status` 의 compose image 와 container 가 새 태그다.
- MCP 를 쓰는 클라이언트에서 도구 목록과 조회 하나를 불러 본다. 같은 서버의 클라이언트라면 그 명령을 `DEPLOY_POST_CHECK` 에 넣어 두면 배포가 대신 확인한다.

### 실패 시

- **빌드에서 `pip install` 이 실패한다.** 방금 올린 패키지가 PyPI 색인에 퍼지기 전이면 생긴다. 잠시 뒤 다시 실행한다. 실행 중인 서비스는 그대로다.
- **`does not lock <패키지>==<버전>` 으로 시작도 못 한다.** `requirements.in` 을 고치고 잠금 파일을 안 만들었다. `deploy/remote.sh lock`.
- **`expected exactly one 'image: ...' line`.** 서버 compose 의 이미지 이름이 `DEPLOY_IMAGE` 와 다르거나 같은 이름이 두 번 나온다. `remote.env` 를 맞춘다.
- **`rolled back to ...` 로 끝난다.** 새 컨테이너가 검증을 통과하지 못해 이전 것으로 돌아갔다. 위에 찍힌 검사 출력이 원인이다. 실패한 시도의 파일은 서버의 `DEPLOY_DIR/.incoming` 에 남는다.
- **`ROLLBACK FAILED`.** 자동 복구가 안 됐다. 서버에서 메시지에 나온 `backup-<시각>/rollback.sh` 를 직접 실행한다.
- **배포는 성공했는데 나중에 문제가 보인다.**

  ```bash
  deploy/remote.sh rollback
  ```

  가장 최근 백업의 `rollback.sh` 를 실행해 compose·빌드 파일·이미지를 그 배포 직전으로 돌린다.

## 서버 첫 세팅

1. 서버에 `DEPLOY_DIR` 을 만들고 `deploy/pypi-compose.example.yml` 을 `docker-compose.yml` 로, `deploy/pypi/` 를 `pypi/` 로 복사한다.
2. 같은 폴더에 `toss.env` 를 만들어 `TOSSINVEST_AUTH_TOKEN`·`TOSSINVEST_CLIENT_ID`·`TOSSINVEST_CLIENT_SECRET` 를 넣고 권한을 600 으로 한다.
3. compose 의 포트·네트워크를 환경에 맞춘다. 예시는 localhost 에만 공개한다.
4. 서버에서 한 번 빌드하고 띄운다.

   ```bash
   docker compose build
   ```

   ```bash
   docker compose up -d
   ```

이후로는 `deploy/remote.sh` 만 쓴다.

## 리허설

`DEPLOY_HOST=local` 이면 ssh 대신 이 머신에서 같은 절차를 돈다. 스크립트를 고쳤을 때 임시 폴더에 예시 compose 로 스택을 하나 띄우고 `DEPLOY_DIR` 을 그 폴더로, `DEPLOY_IMAGE` 를 다른 이름으로 주어 배포·롤백을 돌려 본다. 사후 검사 실패는 `DEPLOY_POST_CHECK=false` 로 만든다.

리허설 명령을 실행하기 전에 같은 환경변수로 `deploy/remote.sh target` 을 먼저 실행해 host 가 `local` 인지 본다. `remote.env` 가 실제 서버를 가리키고 있으므로, 환경변수가 하나라도 빠지면 그 값은 파일에서 온다.

## 한계

- `http` 트랜스포트 전용이다. 검사가 HTTP 엔드포인트로 들어간다.
- `TOSSINVEST_HTTP_ALLOWED_HOSTS` 로 Host 를 고정한 서버는 컨테이너 안의 `127.0.0.1` 요청이 421 로 거절돼 검증이 실패한다.
- 서비스 하나를 교체한다. Redis 를 같이 쓰는 여러 인스턴스 구성은 인스턴스마다 따로 실행해야 한다.
- 교체하는 몇 초 동안은 서비스가 없다.

## 이력

- 2026-11-12 배포 도구 추가(`deploy/remote.sh`·`server-update.sh`·`preflight.py`, `deploy/pypi/`). 그전엔 서버에만 있던 빌드 파일을 손으로 고치고 명령을 하나씩 실행했다.
