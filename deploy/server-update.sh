#!/usr/bin/env bash
# The server-side half of deploy/remote.sh. Not meant to be run by hand: remote.sh feeds it to
# bash on the server with DEPLOY_DIR, DEPLOY_SERVICE, DEPLOY_IMAGE, DEPLOY_TAG and
# DEPLOY_POST_CHECK set, after putting the new build files into $DEPLOY_DIR/.incoming.
#
# backup -> build -> offline preflight -> swap -> verify. Nothing that runs is touched before
# the swap, and if the new container does not pass verification the previous one is put back
# before this script fails.
set -euo pipefail

INCOMING=.incoming
COMPOSE_FILE=docker-compose.yml
BUILD_DIR=pypi
BUILD_FILES="Dockerfile requirements.in requirements.txt"

die() { echo "deploy: $*" >&2; exit 1; }
indent() { sed 's/^/      /'; }

cd
cd "$DEPLOY_DIR" || die "no such directory on the server: $DEPLOY_DIR"
[ -f "$COMPOSE_FILE" ] || die "$DEPLOY_DIR/$COMPOSE_FILE is missing (first-time setup: docs/wiki/deploy.md)"
[ -d "$INCOMING/$BUILD_DIR" ] || die "nothing to deploy: $INCOMING is missing"

new_ref="$DEPLOY_IMAGE:$DEPLOY_TAG"
image_line="^([[:space:]]*image:[[:space:]]*)(${DEPLOY_IMAGE}:[^[:space:]]+)[[:space:]]*\$"
matches="$(grep -c -E "$image_line" "$COMPOSE_FILE" || true)"
[ "$matches" = 1 ] || die "expected exactly one 'image: $DEPLOY_IMAGE:<tag>' line in $COMPOSE_FILE, found $matches"
old_ref="$(sed -n -E "s/${image_line}/\2/p" "$COMPOSE_FILE")"

if [ "$old_ref" = "$new_ref" ]; then
    rm -rf "$INCOMING"
    echo "already on $new_ref: nothing to do"
    exit 0
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
backup="backup-$stamp"

# --- 1. backup: compose, env files and build files, plus a tag on the image that runs now ---
( umask 077; mkdir -p "$backup/$BUILD_DIR" )
cp -p "$COMPOSE_FILE" "$backup/"
for env_file in .env *.env; do
    if [ -f "$env_file" ]; then cp -p "$env_file" "$backup/"; fi
done
for file in $BUILD_FILES; do
    if [ -f "$BUILD_DIR/$file" ]; then cp -p "$BUILD_DIR/$file" "$backup/$BUILD_DIR/"; fi
done
chmod -R go-rwx "$backup"

rollback_ref=""
running="$(docker compose ps -q "$DEPLOY_SERVICE" 2>/dev/null || true)"
if [ -n "$running" ]; then
    rollback_ref="$DEPLOY_IMAGE:rollback-$stamp"
    docker tag "$(docker inspect --format '{{.Image}}' "$running")" "$rollback_ref"
fi

cat > "$backup/rollback.sh" <<EOF
#!/usr/bin/env bash
# Puts back the $DEPLOY_SERVICE compose file, build files and image that ran before the
# deploy of $stamp (UTC). Env files are not restored: the deploy does not change them.
set -euo pipefail
here="\$(cd "\$(dirname "\$0")" && pwd)"
cd "\$here/.."
cp -p "\$here/$COMPOSE_FILE" "$COMPOSE_FILE"
for file in $BUILD_FILES; do
    if [ -f "\$here/$BUILD_DIR/\$file" ]; then cp -p "\$here/$BUILD_DIR/\$file" "$BUILD_DIR/"; fi
done
if [ -n "$rollback_ref" ] && ! docker image inspect "$old_ref" >/dev/null 2>&1; then
    docker tag "$rollback_ref" "$old_ref"
fi
docker compose config --quiet
docker compose up -d --no-deps --no-build --pull never "$DEPLOY_SERVICE"
EOF
chmod 700 "$backup/rollback.sh"
echo "[1/5] backup in $DEPLOY_DIR/$backup (was $old_ref)"

# --- 2. build the new image under its own tag, from the incoming files ---
if ! build_log="$(docker build --quiet --tag "$new_ref" "$INCOMING/$BUILD_DIR" 2>&1)"; then
    printf '%s\n' "$build_log" | tail -n 25 | indent >&2
    die "the image did not build. Nothing that runs was touched; a package published minutes ago may not be on the index yet, so try again"
fi
echo "[2/5] built $new_ref"

# --- 3. offline preflight: no network, dummy bearer, no brokerage credentials ---
probe="$DEPLOY_SERVICE-preflight-$stamp"
dummy_bearer="preflight-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
trap 'docker rm -f "$probe" >/dev/null 2>&1 || true' EXIT
docker run --detach --name "$probe" --network none \
    -e TOSSINVEST_TRANSPORT=http -e TOSSINVEST_HTTP_HOST=127.0.0.1 -e TOSSINVEST_HTTP_PORT=8000 \
    -e TOSSINVEST_MODE=read_only -e TOSSINVEST_STATE_BACKEND=memory \
    -e TOSSINVEST_AUTH_TOKEN="$dummy_bearer" "$new_ref" >/dev/null
docker exec -i "$probe" python - < "$INCOMING/preflight.py" | indent
docker rm -f "$probe" >/dev/null
trap - EXIT
echo "[3/5] offline preflight passed"

# --- 4 and 5. swap, then verify the real container; put the old one back if either fails ---
swap() {
    # only the image tag changes in the compose file, and only this service is recreated
    mkdir -p "$BUILD_DIR" || return 1
    for file in $BUILD_FILES; do
        cp "$INCOMING/$BUILD_DIR/$file" "$BUILD_DIR/" || return 1
    done
    sed -i.swap -E "s|${image_line}|\1${new_ref}|" "$COMPOSE_FILE" || return 1
    rm -f "$COMPOSE_FILE.swap"
    docker compose config --quiet || return 1
    docker compose up -d --no-deps --no-build --pull never "$DEPLOY_SERVICE" 2>&1 | tail -n 5 | indent
}

verify() {
    local container
    container="$(docker compose ps -q "$DEPLOY_SERVICE")"
    [ -n "$container" ] || { echo "the container is not running"; return 1; }
    [ "$(docker inspect --format '{{.Config.Image}}' "$container")" = "$new_ref" ] \
        || { echo "the container is not on $new_ref"; return 1; }
    docker exec -i "$container" python - < "$INCOMING/preflight.py" | indent || return 1
    if [ -z "${DEPLOY_POST_CHECK:-}" ]; then return 0; fi
    echo "      post check: $DEPLOY_POST_CHECK"
    bash -c "$DEPLOY_POST_CHECK" 2>&1 | tail -n 15 | indent
}

if swap; then
    echo "[4/5] $DEPLOY_SERVICE recreated on $new_ref"
    if verify; then
        rm -rf "$INCOMING"
        echo "[5/5] verified"
        echo "now running $new_ref. To undo: deploy/remote.sh rollback"
        exit 0
    fi
fi

echo "deploy: the new container did not pass: putting $old_ref back" >&2
bash "$backup/rollback.sh" >/dev/null 2>&1 || die "ROLLBACK FAILED: run $DEPLOY_DIR/$backup/rollback.sh by hand"
die "rolled back to $old_ref (the files of the failed attempt stay in $DEPLOY_DIR/$INCOMING)"
