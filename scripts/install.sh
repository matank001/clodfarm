#!/bin/sh
# clodfarm installer: one container, one login.
#   curl -fsSL https://raw.githubusercontent.com/matank001/clodfarm/main/scripts/install.sh | sh
# Options (environment): any FARM_* setting, e.g. FARM_MISSION="..." FARM_VERIFY_CMD="pytest -q"; CLAUDE_FARM_IMAGE, CLAUDE_FARM_NAME
set -eu
IMAGE="${CLAUDE_FARM_IMAGE:-ghcr.io/matank001/clodfarm:latest}"
NAME="${CLAUDE_FARM_NAME:-clodfarm}"

say() { printf '\033[1;38;5;209m==>\033[0m %s\n' "$*"; }
command -v docker >/dev/null 2>&1 || { echo "Docker is required: https://docs.docker.com/get-docker/"; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start it and run this again."; exit 1; }

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  say "Container $NAME already exists; starting it"
  docker start "$NAME" >/dev/null
else
  say "Pulling $IMAGE"
  docker pull -q "$IMAGE" >/dev/null
  say "Starting $NAME (restarts on reboot; your login and work live in Docker volumes)"
  # pass every FARM_* setting from your shell through (FARM_VERIFY_CMD, FARM_MAX_WORKERS, FARM_NOTIFY_URL, ...)
  for v in $(env | sed -n 's/^\(FARM_[A-Z0-9_]*\)=.*/\1/p'); do set -- "$@" -e "$v"; done
  docker run -d --name "$NAME" --restart unless-stopped "$@" \
    -e FARM_CONTAINER_NAME="$NAME" \
    -v clodfarm_claude-home:/home/farm/.claude -v clodfarm_workspace:/workspace \
    "$IMAGE" >/dev/null
fi

if docker exec "$NAME" clodfarm whoami >/dev/null 2>&1; then
  say "Already logged in"
else
  say "Log in to your Claude subscription: open the URL on any device, approve, paste the code here"
  docker exec -it "$NAME" clodfarm login </dev/tty
fi

cat <<MSG

  clodfarm is running.

  Give it a mission:  docker exec $NAME clodfarm mission "Build a CSV to Markdown CLI with tests"
  Or a single task:   docker exec $NAME clodfarm task add "Add a --align flag" --prompt "..."
  Watch it:           docker exec $NAME clodfarm status      (or: docker logs -f $NAME)
  From your phone:    Claude app -> Code -> "clodfarm"

MSG
