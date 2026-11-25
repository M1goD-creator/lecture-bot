# Offline test: an expired login shows "Sign in to join"; the bot must say so once in Telegram.
# Run: .venv\Scripts\python tests\test_signin.py
import os, sys, threading, time, http.server, functools
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update(TEAMS_BACKEND="web", TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="1", BROWSER_HEADLESS="1",
                  TEAMS_STORAGE_STATE=os.path.join(HERE, "nostate.json"))
sys.path.insert(0, os.path.dirname(HERE))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=HERE))
threading.Thread(target=srv.serve_forever, daemon=True).start()
import bot
sent = []
bot.tg_send = lambda t, fmt=True, **kw: (sent.append(t), print("TG:", t))[0] and []
bot.is_meeting_link = lambda l: True


class Fast(threading.Event):
    def wait(self, t=None): return super().wait(min(t or 0, 1))


stop = Fast()
now = datetime.now(timezone.utc)
ev = {"key": "s", "title": "Signin test", "start": now, "end": now + timedelta(seconds=40),
      "link": f"http://127.0.0.1:{srv.server_address[1]}/fake_signin.html"}
bot.CURRENT["jstop"] = stop
th = threading.Thread(target=bot.web_auto_join, args=(ev, stop))
th.start()
t0 = time.time()
while time.time() - t0 < 30 and not any("сохранённый вход истёк" in s for s in sent):
    time.sleep(0.5)
stop.set()
th.join(60)
bot.tw.BROWSER.close()
n = sum("сохранённый вход истёк" in s for s in sent)
assert n == 1, f"expected one sign-in alert, got {n}: {sent}"
print("OK: sign-in alert sent once")
