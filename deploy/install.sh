#!/bin/bash
# Copies the committed bot to the server and restarts it. .env, lectures/, schedule.txt edits
# made on the server, teams-state.json, the venv and caches are kept.
#   deploy/install.sh            update code + Python packages, restart
#   deploy/install.sh first      also apt packages, venv, Chromium (first time only)
set -euo pipefail
SERVER=${VG_SERVER:-deploy@62.83.83.149}
KEY=${VG_KEY:-$HOME/.ssh/vis-gemina}
cd "$(dirname "$0")/.."
remote() { ssh -i "$KEY" -o BatchMode=yes "$SERVER" "$@"; }

if [ "${1:-}" = first ]; then
  remote 'sudo apt-get update -q && sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
      python3 python3-venv pulseaudio pulseaudio-utils xvfb xauth ffmpeg &&
    sudo mkdir -p /srv/lecture-bot && sudo chown app:app /srv/lecture-bot &&
    sudo -u app python3 -m venv /srv/lecture-bot/.venv'
fi

git archive HEAD bot.py teams_web.py teams_login.py set_login.py repost.py requirements-server.txt .env.example | remote '
  cd /srv/lecture-bot && sudo tar -x -C /srv/lecture-bot --no-same-owner &&
  sudo chown -R app:app /srv/lecture-bot &&
  sudo -u app env HOME=/srv/lecture-bot/.home .venv/bin/pip install -q -r requirements-server.txt &&
  sudo -u app env HOME=/srv/lecture-bot/.home PLAYWRIGHT_BROWSERS_PATH=/srv/lecture-bot/.browsers \
    .venv/bin/python -m playwright install chromium &&
  sudo .venv/bin/python -m playwright install-deps chromium >/dev/null &&
  sudo systemctl enable --quiet lecture-bot && sudo systemctl restart lecture-bot'
echo "deployed: lecture-bot"
