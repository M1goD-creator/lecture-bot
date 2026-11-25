# Lecture bot

Joins Teams lectures from `schedule.txt`, transcribes them with faster-whisper, posts a live
transcript and summaries to Telegram, and answers questions from the archive in `lectures/`.

* **Windows (PC):** `run.bat`. Uses the Teams desktop app (pywinauto) and records the speakers.
* **Linux (server):** `TEAMS_BACKEND=web` (the default on Linux). Teams runs in Chromium
  (Playwright) inside Xvfb, the call plays into a PulseAudio null sink and `parec` records it.
  The browser has no microphone or camera at all. See `teams_web.py`.

## Server setup (vis-gemina VPS)

Runs as user `app` in `/srv/lecture-bot` with `deploy/lecture-bot.service`
(installed by `website_for_projects/tools/deploy.sh setup`).

1. First install (apt packages `python3-venv pulseaudio pulseaudio-utils xvfb xauth ffmpeg`,
   the venv, Chromium with `playwright install chromium` + `install-deps`):

       deploy/install.sh first

2. Copy the private files once (they are not in git):

       scp -i ~/.ssh/vis-gemina .env schedule.txt deploy@62.83.83.149:/tmp/
       ssh -i ~/.ssh/vis-gemina deploy@62.83.83.149 'sudo install -o app -g app -m 600 /tmp/.env /srv/lecture-bot/.env &&
         sudo install -o app -g app -m 644 /tmp/schedule.txt /srv/lecture-bot/schedule.txt && rm /tmp/.env /tmp/schedule.txt'

   In the server `.env` leave `OMNIROUTE_URL` empty (OmniRoute runs on the PC) and set
   `TEAMS_GUEST_NAME`, the name shown in the meeting.
3. Optional, to join signed in instead of as a guest: run `teams_login.py` on the PC, then copy
   `teams-state.json` the same way to `/srv/lecture-bot/teams-state.json` (mode 600).
4. Updates: `deploy/install.sh`. Logs: `journalctl -u lecture-bot -f`.

Notes

* Only direct meeting links work on the server (`teams.microsoft.com/meet/…` or
  `/l/meetup-join/…`); channel links need the desktop app.
* As a guest the bot waits in the lobby until someone lets it in.
* There is no GPU on the server, so Whisper falls back to the `small` model on the CPU.
* `/shutdown` exits with code 75 and systemd leaves it off; start it again from the hub.
* Stop the bot on the PC before starting it on the server: one Telegram token, one instance.
