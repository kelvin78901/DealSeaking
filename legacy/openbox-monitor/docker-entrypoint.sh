#!/bin/sh
set -e

APP_DIR="${OBM_APP_DIR:-/app}"
DATA_DIR="${OBM_DATA_DIR:-/data}"
PORT="${OBM_PORT:-8787}"
CONFIG="$DATA_DIR/config.yaml"

if [ ! -w "$DATA_DIR" ]; then
  echo "!! $DATA_DIR 不可写。宿主机上执行：sudo chown -R 1000:1000 <挂载目录>" >&2
  exit 1
fi

if [ ! -f "$CONFIG" ]; then
  cp "$APP_DIR/config.example.yaml" "$CONFIG"
  echo ">> 已生成默认配置 $CONFIG（宿主机上是 ./data/config.yaml）。"
  echo ">> 改完会在下一轮自动生效，不用重启容器。"
fi

# Default: run the daemon. Any other args are passed to the CLI, e.g.
#   docker compose run --rm openbox-monitor status
#   docker compose run --rm openbox-monitor test-notify
if [ "$#" -eq 0 ]; then
  exec python -m obm -c "$CONFIG" daemon --host 0.0.0.0 --port "$PORT"
fi
exec python -m obm -c "$CONFIG" "$@"
