#!/usr/bin/env bash
# Deploys the PyPI build of pytossinvest-mcp to a server and can take it back.
#
#   deploy/remote.sh            deploy the versions pinned in deploy/pypi/requirements.in
#   deploy/remote.sh target     where a deploy would go and what it would run (touches nothing)
#   deploy/remote.sh status     what runs on the server now, and which backups exist
#   deploy/remote.sh rollback   put back what ran before the last deploy
#   deploy/remote.sh lock       re-create deploy/pypi/requirements.txt from requirements.in
#
# The target comes from the environment, and for whatever is not set there from
# deploy/remote.env (gitignored):
#   DEPLOY_HOST        an ssh host, or "local" to use this machine          (required)
#   DEPLOY_DIR         directory on the server holding docker-compose.yml   (infra/tossinvest-mcp)
#   DEPLOY_SERVICE     compose service to replace                           (tossinvest-mcp)
#   DEPLOY_IMAGE       image repository; the tag is derived from the pins   (tossinvest-mcp)
#   DEPLOY_POST_CHECK  optional command run on the server after the swap; a failure rolls back
#
# How it works and how to set a server up the first time: docs/wiki/deploy.md
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYPI_DIR="$HERE/pypi"
LOCAL_HOST=local
INCOMING=.incoming

die() { echo "deploy: $*" >&2; exit 1; }

load_target() {
    # The environment wins: DEPLOY_HOST=local deploy/remote.sh must never reach the server
    # that remote.env names. So the file only fills in what is not set yet.
    local file="$HERE/remote.env" line name
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        name="${line%%=*}"
        case "$name" in
            DEPLOY_HOST|DEPLOY_DIR|DEPLOY_SERVICE|DEPLOY_IMAGE|DEPLOY_POST_CHECK) ;;
            *) die "deploy/remote.env: only DEPLOY_* settings belong here, got: $name" ;;
        esac
        if [ -z "${!name+set}" ]; then eval "$line"; fi
    done < "$file"
}

load_target
DEPLOY_DIR="${DEPLOY_DIR:-infra/tossinvest-mcp}"
DEPLOY_SERVICE="${DEPLOY_SERVICE:-tossinvest-mcp}"
DEPLOY_IMAGE="${DEPLOY_IMAGE:-tossinvest-mcp}"
DEPLOY_POST_CHECK="${DEPLOY_POST_CHECK:-}"

need_host() {
    [ -n "${DEPLOY_HOST:-}" ] || die "set DEPLOY_HOST (an ssh host, or '$LOCAL_HOST') in deploy/remote.env"
}

# Run bash on the server, reading the script from stdin.
server_bash() {
    if [ "$DEPLOY_HOST" = "$LOCAL_HOST" ]; then
        bash -s
    else
        ssh -o BatchMode=yes "$DEPLOY_HOST" bash -s
    fi
}

# Run one of our scripts on the server with the DEPLOY_* settings in front of it.
on_server() {
    local script="$1"
    {
        printf 'DEPLOY_DIR=%q\nDEPLOY_SERVICE=%q\nDEPLOY_IMAGE=%q\nDEPLOY_TAG=%q\nDEPLOY_POST_CHECK=%q\n' \
            "$DEPLOY_DIR" "$DEPLOY_SERVICE" "$DEPLOY_IMAGE" "${DEPLOY_TAG:-}" "$DEPLOY_POST_CHECK"
        cat "$script"
    } | server_bash
}

pinned() {
    # version of one package in requirements.in, e.g. pinned pytossinvest-mcp -> 0.0.3
    sed -n -E "s/^$1(\[[^]]*\])?==([^[:space:]]+)[[:space:]]*\$/\2/p" "$PYPI_DIR/requirements.in"
}

image_tag() {
    local mcp sdk
    mcp="$(pinned pytossinvest-mcp)"
    sdk="$(pinned pytossinvest-sdk)"
    [ -n "$mcp" ] && [ -n "$sdk" ] || die "deploy/pypi/requirements.in must pin pytossinvest-mcp and pytossinvest-sdk with =="
    echo "pypi-mcp${mcp}-sdk${sdk}"
}

check_lock() {
    # every pin of requirements.in must be what requirements.txt locks, hashes included
    local line name version
    while IFS= read -r line; do
        case "$line" in ''|\#*) continue ;; esac
        name="$(printf '%s' "$line" | sed -E 's/(\[[^]]*\])?==.*$//')"
        version="${line##*==}"
        grep -q -E "^${name}==${version}([[:space:]]|\\\\|\$)" "$PYPI_DIR/requirements.txt" \
            || die "deploy/pypi/requirements.txt does not lock ${name}==${version}: run deploy/remote.sh lock"
    done < "$PYPI_DIR/requirements.in"
    grep -q -e '--hash=sha256:' "$PYPI_DIR/requirements.txt" || die "deploy/pypi/requirements.txt has no hashes"
}

cmd_lock() {
    command -v uv >/dev/null || die "uv is needed to write the lock file"
    ( cd "$PYPI_DIR" && uv pip compile --python-version 3.12 --universal --generate-hashes \
        --no-header --no-annotate --no-emit-index-url --output-file requirements.txt requirements.in >/dev/null )
    echo "wrote deploy/pypi/requirements.txt for $(image_tag)"
}

send_files() {
    # the new build files wait in $INCOMING until the server-side script has taken its backup
    local dir unpack
    dir="$(printf '%q' "$DEPLOY_DIR")"
    unpack="cd && cd $dir && rm -rf $INCOMING && mkdir $INCOMING && tar -C $INCOMING -xf -"
    # --no-xattrs: macOS tags files with extended attributes that the server's tar warns about
    tar --no-xattrs -C "$HERE" -cf - pypi/Dockerfile pypi/requirements.in pypi/requirements.txt preflight.py \
        | if [ "$DEPLOY_HOST" = "$LOCAL_HOST" ]; then bash -c "$unpack"; else ssh -o BatchMode=yes "$DEPLOY_HOST" "$unpack"; fi
}

cmd_target() {
    need_host
    echo "host:       $DEPLOY_HOST"
    echo "directory:  $DEPLOY_DIR"
    echo "service:    $DEPLOY_SERVICE"
    echo "image:      $DEPLOY_IMAGE:$(image_tag)"
    echo "post check: ${DEPLOY_POST_CHECK:-none}"
}

cmd_deploy() {
    need_host
    check_lock
    DEPLOY_TAG="$(image_tag)"
    echo "deploying $DEPLOY_IMAGE:$DEPLOY_TAG to $DEPLOY_HOST:$DEPLOY_DIR (service $DEPLOY_SERVICE)"
    send_files
    on_server "$HERE/server-update.sh"
}

cmd_status() {
    need_host
    on_server /dev/stdin <<'EOF'
set -euo pipefail
cd; cd "$DEPLOY_DIR"
echo "compose image: $(sed -n -E "s/^[[:space:]]*image:[[:space:]]*(${DEPLOY_IMAGE}:[^[:space:]]+).*/\1/p" docker-compose.yml)"
docker compose ps "$DEPLOY_SERVICE" --format 'container: {{.Name}}  {{.Image}}  {{.Status}}' 2>/dev/null || echo "container: not running"
echo "images:"; docker images "$DEPLOY_IMAGE" --format '{{.Repository}}:{{.Tag}}  {{.ID}}  {{.CreatedSince}}' | sed 's/^/  /'
echo "backups:"; ls -d backup-* 2>/dev/null | sed 's/^/  /' || echo "  none"
EOF
}

cmd_rollback() {
    need_host
    on_server /dev/stdin <<'EOF'
set -euo pipefail
cd; cd "$DEPLOY_DIR"
latest="$(ls -d backup-* 2>/dev/null | sort | tail -n 1)"
[ -n "$latest" ] && [ -x "$latest/rollback.sh" ] || { echo "deploy: no backup with a rollback.sh in $DEPLOY_DIR" >&2; exit 1; }
echo "rolling back with $DEPLOY_DIR/$latest/rollback.sh"
bash "$latest/rollback.sh" >/dev/null
docker compose ps "$DEPLOY_SERVICE" --format 'now running: {{.Image}}  {{.Status}}'
EOF
}

case "${1:-deploy}" in
    deploy)   cmd_deploy ;;
    target)   cmd_target ;;
    status)   cmd_status ;;
    rollback) cmd_rollback ;;
    lock)     cmd_lock ;;
    *)        die "unknown command '$1' (deploy | target | status | rollback | lock)" ;;
esac
