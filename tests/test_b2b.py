# Offline tests against tests/fake_teams.html (no Teams, no Telegram): .venvScriptspython tests<name>.py
import os, sys, threading, time, http.server, functools
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update(TEAMS_BACKEND="web", TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="1", BROWSER_HEADLESS="1", TEAMS_STORAGE_STATE=os.path.join(HERE, "nostate.json"))
sys.path.insert(0, os.path.dirname(HERE))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=HERE))
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
import bot
sent = []
bot.tg_send = lambda t, fmt=True: sent.append(t) or []
bot.is_meeting_link = lambda l: True
link = f"http://127.0.0.1:{port}/fake_teams.html"
now = datetime.now(timezone.utc)
ev1 = {"key": "a", "title": "Lecture", "start": now, "end": now + timedelta(minutes=5), "link": link + "?lecture"}
ev2 = {"key": "b", "title": "Practice", "start": now, "end": now + timedelta(minutes=5), "link": link + "?practice"}
bot.start_join(ev1)
t0 = time.time()
while not any("Зашёл" in s for s in sent) and time.time() - t0 < 60: time.sleep(0.5)
# like main(): end of lecture sets the old stop, then the next pair starts right away
bot.CURRENT["jstop"].set(); bot.start_join(ev2)
t0 = time.time()
while sum("Зашёл" in s for s in sent) < 2 and time.time() - t0 < 90: time.sleep(0.5)
b = bot.tw.BROWSER
url = b.call(lambda: b.page.url)
print("joins:", sum("Зашёл" in s for s in sent), "page:", url.split("?")[-1], "in call:", b.in_call())
assert url.endswith("?practice") and b.in_call()
bot.CURRENT["jstop"].set(); time.sleep(3); b.close(); print("B2B OK")
