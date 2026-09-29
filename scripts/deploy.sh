#!/usr/bin/env bash
# deploy.sh — recreate this node's execution-engine container, and refuse any image whose
# architecture is not this node's.
#
# Why the architecture is asserted, and not just the image ID: a matching ID proves identity,
# not architecture. HOME builds the AWS (arm64) images and keeps a standing arm64 emulation
# handler for it (TK-0599, 2026-09-11), so an arm64 image recreated on HOME does not fail at
# start — it runs under QEMU. Operator ruling 2026-09-29: every engine deploy, on either node,
# asserts the image's architecture equals the node's before it runs, and refuses otherwise.
#
# Usage (from anywhere; the script works on the checkout it lives in):
#   scripts/deploy.sh                       recreate from the image the compose files resolve
#   scripts/deploy.sh --candidate <image>   point the compose image at <image>, then recreate
#   scripts/deploy.sh ... --dry-run         run every check, change nothing
#
# The compose file set is read from the running container's own compose labels, so a deploy
# recreates the container with exactly the files it was created with (HOME: base + override;
# AWS: base + private + aws). A first bring-up is not a deploy and is refused.
#
# Exit codes:
#   0  deployed (or, with --dry-run, every check passed)
#   2  usage or precondition failure (no container, image absent, compose file missing)
#   3  REFUSED: an image's architecture is not this node's. The first check runs before any
#      tag moves, so a refusal there changes nothing
#   4  the recreate or the post-deploy check failed; the rollback command is printed
set -euo pipefail

SERVICE=execution-engine
WAIT_TIMEOUT=${DEPLOY_WAIT_TIMEOUT:-180}

say() { printf 'deploy: %s\n' "$*"; }
die() {
  local code=$1
  shift
  printf 'deploy: %s\n' "$*" >&2
  exit "$code"
}
usage() { sed -n '11,14p' "$0" | sed 's/^# \{0,1\}//'; }

CANDIDATE=""
DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --candidate)
      [ $# -ge 2 ] || die 2 "--candidate needs an image reference"
      CANDIDATE=$2
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die 2 "unknown argument: $1 (see --help)" ;;
  esac
done

cd "$(dirname "$0")/.."
REPO_DIR=$(pwd -P)

norm_arch() {
  case "$1" in
    x86_64 | amd64) echo amd64 ;;
    aarch64 | arm64) echo arm64 ;;
    *) echo "$1" ;;
  esac
}

NODE_ARCH=$(norm_arch "$(docker info --format '{{.Architecture}}')")
[ -n "$NODE_ARCH" ] || die 2 "could not read this node's architecture from 'docker info'"

# An absent image prints nothing but an empty line, which command substitution drops, so it
# reads as "". `|| true` makes that an answer rather than a failure: under `set -e` the failing
# inspect would otherwise end the script with no message.
image_platform() { docker image inspect "$1" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true; }
image_id() { docker image inspect "$1" --format '{{.Id}}' 2>/dev/null || true; }

# THE CHECK. Everything that changes state comes after the first call to it.
CHANGED=0
assert_arch() {
  local ref=$1 role=$2 plat os arch
  plat=$(image_platform "$ref")
  [ -n "$plat" ] || die 2 "$role image '$ref' is not present on this node"
  os=${plat%%/*}
  arch=$(norm_arch "${plat#*/}")
  if [ "$os" != linux ] || [ "$arch" != "$NODE_ARCH" ]; then
    printf 'deploy: REFUSED — %s image %s is %s/%s; this node is linux/%s.\n' \
      "$role" "$ref" "$os" "$arch" "$NODE_ARCH" >&2
    if [ "$CHANGED" = 0 ]; then
      printf 'deploy: nothing was changed.\n' >&2
    else
      printf 'deploy: the container was NOT recreated, but tags moved. Restore: docker tag %s %s\n' \
        "$CUR_ID" "$REF" >&2
    fi
    exit 3
  fi
  say "architecture OK — $role image $ref is linux/$arch, this node is linux/$NODE_ARCH"
}

# The one container this checkout's compose project created for the service.
find_container() {
  docker ps -aq \
    --filter "label=com.docker.compose.service=$SERVICE" \
    --filter "label=com.docker.compose.project.working_dir=$REPO_DIR"
}
CIDS=$(find_container)
N=$(printf '%s' "$CIDS" | grep -c . || true)
[ "$N" = 1 ] || die 2 "expected exactly one $SERVICE container created from $REPO_DIR, found $N. A first bring-up is not a deploy."
CID=$CIDS
label() { docker inspect "$CID" --format "{{index .Config.Labels \"$1\"}}"; }
NAME=$(docker inspect "$CID" --format '{{.Name}}')
NAME=${NAME#/}
PROJECT=$(label com.docker.compose.project)
CUR_ID=$(docker inspect "$CID" --format '{{.Image}}')

COMPOSE=(docker compose -p "$PROJECT")
IFS=',' read -r -a FILES <<<"$(label com.docker.compose.project.config_files)"
[ "${#FILES[@]}" -gt 0 ] || die 2 "container $NAME carries no compose config_files label"
for f in "${FILES[@]}"; do
  [ -f "$f" ] || die 2 "compose file $f (from $NAME's labels) is missing"
  COMPOSE+=(-f "$f")
done

# The image reference compose will run: the service's `image:`, or the compose default name
# `<project>-<service>` when the service only has `build:`.
REF=$("${COMPOSE[@]}" config --format json |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["services"][sys.argv[1]].get("image") or "")' "$SERVICE")
[ -n "$REF" ] || REF="$PROJECT-$SERVICE"

say "node linux/$NODE_ARCH · container $NAME · running image ${CUR_ID:7:12}"
say "compose image $REF · files: ${FILES[*]##*/}"

if [ -n "$CANDIDATE" ]; then
  assert_arch "$CANDIDATE" candidate
  TARGET_ID=$(image_id "$CANDIDATE")
else
  assert_arch "$REF" compose
  TARGET_ID=$(image_id "$REF")
fi

ROLLBACK_TAG=""
if [ "$TARGET_ID" = "$CUR_ID" ]; then
  say "NOTE: the target is the image already running — this recreates for configuration, not code"
else
  ROLLBACK_TAG="${REF%:*}:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
fi

if [ "$DRY_RUN" = 1 ]; then
  [ -z "$ROLLBACK_TAG" ] || say "would tag the running image ${CUR_ID:7:12} as $ROLLBACK_TAG"
  [ -z "$CANDIDATE" ] || say "would tag $CANDIDATE as $REF"
  say "would run: ${COMPOSE[*]} up -d --no-build --pull never --force-recreate --no-deps --wait $SERVICE"
  say "DRY RUN — every check passed, nothing was changed"
  exit 0
fi

# ---- changes start here ---------------------------------------------------------------------
CHANGED=1
if [ -n "$ROLLBACK_TAG" ]; then
  docker tag "$CUR_ID" "$ROLLBACK_TAG"
  say "rollback tag: $ROLLBACK_TAG -> ${CUR_ID:7:12}"
fi
ROLLBACK_CMD="scripts/deploy.sh --candidate ${ROLLBACK_TAG:-<the previous image>}"
if [ -n "$CANDIDATE" ] && [ "$(image_id "$REF")" != "$TARGET_ID" ]; then
  docker tag "$CANDIDATE" "$REF"
  say "tagged $CANDIDATE as $REF"
fi
# The tag above moved; check what compose will actually run, once more, before it runs.
assert_arch "$REF" compose

if ! "${COMPOSE[@]}" up -d --no-build --pull never --force-recreate --no-deps \
  --wait --wait-timeout "$WAIT_TIMEOUT" "$SERVICE"; then
  die 4 "recreate failed or $SERVICE did not become healthy in ${WAIT_TIMEOUT}s. Roll back: $ROLLBACK_CMD"
fi

# Post-check: identity AND architecture of what is now running.
CID=$(find_container)
RUN_ID=$(docker inspect "$CID" --format '{{.Image}}')
[ "$RUN_ID" = "$TARGET_ID" ] ||
  die 4 "running image ${RUN_ID:7:12} is not the target ${TARGET_ID:7:12}. Roll back: $ROLLBACK_CMD"
RUN_PLAT=$(image_platform "$RUN_ID")
RUN_ARCH=$(norm_arch "${RUN_PLAT#*/}")
[ "${RUN_PLAT%%/*}" = linux ] && [ "$RUN_ARCH" = "$NODE_ARCH" ] ||
  die 4 "running image ${RUN_ID:7:12} is $RUN_PLAT, node is linux/$NODE_ARCH. Roll back: $ROLLBACK_CMD"
say "deployed — $NAME runs ${RUN_ID:7:12} (linux/$NODE_ARCH), healthy"
say "rollback: $ROLLBACK_CMD"
