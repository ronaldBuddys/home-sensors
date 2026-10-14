#!/usr/bin/env bash
# Dev process control without systemd: ./scripts/devctl.sh {start|stop|restart|status} [collector|web|web-sim|all]
# Uses pid files in data/ (never pattern-matching kills). For production use deploy/systemd.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077  # logs and pid files (logs contain device addresses and readings)
mkdir -p data
BIN=.venv/bin/aranet
LOG_MAX_BYTES=$((5 * 1024 * 1024))  # rotate data/<name>.log to .log.1 on start beyond this

cmd_for() {
  case "$1" in
    collector) echo "$BIN collect" ;;
    web)       echo "$BIN serve --port 8050" ;;
    web-sim)   echo "env ARANET_DB_URL=sqlite:///data/sim.sqlite $BIN serve --port 8051" ;;
  esac
}

rotate_log() {
  local log="data/$1.log"
  if [[ -f "$log" ]] && (( $(stat -c %s "$log") > LOG_MAX_BYTES )); then mv -f "$log" "$log.1"; fi
}

running() { [[ -f "data/$1.pid" ]] && kill -0 "$(cat "data/$1.pid")" 2>/dev/null; }

start() {
  if running "$1"; then echo "$1 already running (pid $(cat "data/$1.pid"))"; return; fi
  rotate_log "$1"
  # shellcheck disable=SC2046
  setsid nohup $(cmd_for "$1") >> "data/$1.log" 2>&1 < /dev/null &
  echo $! > "data/$1.pid"
  echo "$1 started (pid $!, log data/$1.log)"
}

stop() {
  if running "$1"; then
    local pid; pid=$(cat "data/$1.pid")
    kill -INT "$pid"
    for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    kill -0 "$pid" 2>/dev/null && kill -TERM "$pid"
    echo "$1 stopped"
  else
    echo "$1 not running"
  fi
  rm -f "data/$1.pid"
}

status() { if running "$1"; then echo "$1: running (pid $(cat "data/$1.pid"))"; else echo "$1: stopped"; fi; }

action=${1:-status}; target=${2:-all}
services=(collector web web-sim)
if [[ "$target" != all ]]; then
  case "$target" in collector|web|web-sim) services=("$target") ;; *) echo "unknown service $target" >&2; exit 2 ;; esac
fi
for s in "${services[@]}"; do
  case "$action" in
    start) start "$s" ;; stop) stop "$s" ;; restart) stop "$s"; start "$s" ;; status) status "$s" ;;
    *) echo "usage: $0 {start|stop|restart|status} [collector|web|web-sim|all]"; exit 2 ;;
  esac
done
