#!/usr/bin/env bash
# Disposable Compose transaction test. Never points at a production host.
set -euo pipefail
toolkit=$(pwd)
temp=$(mktemp -d)
cleanup() {
  docker compose -p simple-pipe-test -f "$temp/server/compose.yml" down --volumes || true
  rm -rf -- "$temp"
}
trap cleanup EXIT
mkdir -p "$temp/server/hooks" "$temp/app/.simple-pipe"
printf '%s\n' 'simple-pipe-test' > "$temp/server/project-name"
touch "$temp/server/.env"
cat > "$temp/server/compose.yml" <<'YAML'
services:
  web:
    image: alpine:3.21
    command: ["sleep", "infinity"]
    healthcheck:
      test: ["CMD", "true"]
      interval: 1s
      timeout: 1s
      retries: 3
YAML
printf '#!/bin/sh\nset -eu\ntouch "$SP_DIRECTORY/backup-created"\n' > "$temp/server/hooks/backup"
printf '#!/bin/sh\nset -eu\ntest ! -e "$SP_DIRECTORY/fail-health"\n' > "$temp/server/hooks/health"
chmod +x "$temp/server/hooks/"*
docker compose -p simple-pipe-test -f "$temp/server/compose.yml" up -d --wait
cat > "$temp/app/Dockerfile" <<'DOCKER'
FROM alpine:3.21
CMD ["sleep", "infinity"]
DOCKER
cat > "$temp/app/.simple-pipe/project.json" <<'JSON'
{"project":"simple-pipe-test","services":{"web":{"context":".","dockerfile":"Dockerfile"}}}
JSON
cd "$temp/app"
python3 "$toolkit/simple_pipe.py" build --sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
cd dist
python3 "$toolkit/deploy.py" --directory "$temp/server" --run 1
test -f "$temp/server/backup-created"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["run"] == 1' "$temp/server/.simple-pipe/current.json"
old=$(docker inspect --format '{{.Image}}' "$(docker compose -p simple-pipe-test -f "$temp/server/compose.yml" ps -q web)")
cd "$temp/app"
python3 "$toolkit/simple_pipe.py" build --sha bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --out dist2
cd dist2
# A one-time failing readiness hook lets the restored release pass its own check.
printf '#!/bin/sh\nset -eu\nif test -e "$SP_DIRECTORY/fail-health"; then rm "$SP_DIRECTORY/fail-health"; exit 1; fi\n' > "$temp/server/hooks/health"
touch "$temp/server/fail-health"
if python3 "$toolkit/deploy.py" --directory "$temp/server" --run 2; then
  echo 'Expected readiness failure' >&2
  exit 1
fi
actual=$(docker inspect --format '{{.Image}}' "$(docker compose -p simple-pipe-test -f "$temp/server/compose.yml" ps -q web)")
test "$old" = "$actual"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["run"] == 1' "$temp/server/.simple-pipe/current.json"
echo 'Disposable Compose promotion and rollback passed'
