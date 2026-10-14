#!/usr/bin/env bash
# Install the collector + web as systemd *user* services (no root needed).
#   ./deploy/install-user-services.sh            install, enable and start both
#   ./deploy/install-user-services.sh uninstall  stop, disable and remove them
# Settings come from .env in the repo root (see .env.example).
# To keep them running when you are logged out / after reboot without logging in:
#   loginctl enable-linger "$USER"
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNITS=(aranet-collector.service aranet-web.service)

if [[ "${1:-}" == "uninstall" ]]; then
  systemctl --user disable --now "${UNITS[@]}" 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
  systemctl --user daemon-reload
  echo "removed ${UNITS[*]}"
  exit 0
fi

if [[ "$REPO" =~ [[:space:]] ]]; then
  echo "the repo path must not contain whitespace (systemd unit paths): $REPO" >&2
  exit 1
fi
[[ -x "$REPO/.venv/bin/aranet" ]] || (cd "$REPO" && uv sync --frozen --no-dev)
mkdir -p "$REPO/data" && chmod 700 "$REPO/data"
[[ -f "$REPO/.env" ]] && chmod 600 "$REPO/.env"
mkdir -p "$UNIT_DIR"
repo_sed=$(printf '%s' "$REPO" | sed 's/[\\&#]/\\&/g')  # escape for the sed replacement
for u in "${UNITS[@]}"; do
  sed "s#@REPO@#$repo_sed#g" "$REPO/deploy/systemd/$u" > "$UNIT_DIR/$u"
done
systemctl --user daemon-reload
systemctl --user enable --now "${UNITS[@]}"
systemctl --user --no-pager status "${UNITS[@]}" | grep -E "●|Active:" || true
echo
echo "logs:   journalctl --user -u aranet-collector -f"
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo "note:   lingering is off; services stop when you log out. Enable with: loginctl enable-linger $USER"
fi
