#!/bin/bash
# Stores your university login on the server so the bot can sign in to Teams by itself
# when Microsoft asks for the password again. Run from Git Bash on the PC:
#   bash deploy/set_teams_password.sh
# The password is typed without echo and goes over ssh on stdin (not on any command line).
set -euo pipefail
SERVER=${VG_SERVER:-deploy@62.83.83.149}
KEY=${VG_KEY:-$HOME/.ssh/vis-gemina}
read -r -p "Университетская почта (логин Teams): " EMAIL
read -r -s -p "Пароль (символы не видны): " PW
echo
printf '%s\n%s\n' "$EMAIL" "$PW" | ssh -i "$KEY" -o BatchMode=yes "$SERVER" \
  'sudo -u app /srv/lecture-bot/.venv/bin/python /srv/lecture-bot/set_login.py'
unset PW
