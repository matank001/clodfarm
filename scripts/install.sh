#!/bin/sh
# claude-farm installer: one container, one login.
#   curl -fsSL https://raw.githubusercontent.com/matank001/claude-farm/main/scripts/install.sh | sh
# Options (environment): FARM_MISSION="..."  CLAUDE_FARM_IMAGE=...  CLAUDE_FARM_NAME=claude-farm
set -eu
IMAGE="${CLAUDE_FARM_IMAGE:-ghcr.io/matank001/claude-farm:latest}"
NAME="${CLAUDE_FARM_NAME:-claude-farm}"

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
  docker run -d --name "$NAME" --restart unless-stopped \
    -e FARM_CONTAINER_NAME="$NAME" ${FARM_MISSION:+-e FARM_MISSION="$FARM_MISSION"} \
    -v claude-farm_claude-home:/home/farm/.claude -v claude-farm_workspace:/workspace \
    "$IMAGE" >/dev/null
fi

if docker exec "$NAME" claude-farm whoami >/dev/null 2>&1; then
  say "Already logged in"
else
  say "Log in to your Claude subscription: open the URL on any device, approve, paste the code here"
  docker exec -it "$NAME" claude-farm login </dev/tty
fi

cat <<MSG

  claude-farm is running.

  Give it a mission:  docker exec $NAME claude-farm mission "Build a CSV to Markdown CLI with tests"
  Or a single task:   docker exec $NAME claude-farm task add "Add a --align flag" --prompt "..."
  Watch it:           docker exec $NAME claude-farm status      (or: docker logs -f $NAME)
  From your phone:    Claude app -> Code -> "claude-farm"

MSG
