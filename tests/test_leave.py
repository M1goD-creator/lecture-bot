# Offline tests against tests/fake_teams.html (no Teams, no Telegram): .venvScriptspython tests<name>.py
import os, sys, threading, time, http.server, functools
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update(TEAMS_BACKEND="web", TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="1", BROWSER_HEADLESS="1",
                  TEAMS_STORAGE_STATE=os.path.join(HERE, "nostate.json"), REJOIN_AFTER_MIN="0")
sys.path.insert(0, os.path.dirname(HERE))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=HERE))
threading.Thread(target=srv.serve_forever, daemon=True).start()
import bot
sent = []
bot.tg_send = lambda t, fmt=True: sent.append(t) or []
bot.is_meeting_link = lambda l: True
_wait = threading.Event.wait
threading.Event.wait = lambda self, t=None: _wait(self, 1 if t in (10, 15, 20) else t)   # 10-20 s loop pauses -> 1 s
now = datetime.now(timezone.utc)
ev = {"key": "e", "title": "English", "start": now, "end": now + timedelta(minutes=10), "link": f"http://127.0.0.1:{srv.server_address[1]}/fake_teams.html"}
def until(pred, t=60):
    t0 = time.time()
    while time.time() - t0 < t:
        if pred(): return True
        time.sleep(0.3)
    return False
bot.LAST_VOICE[0] = time.time() - 3600          # long silence: an unexpected drop would rejoin at once
bot.start_join(ev)
assert until(lambda: any("Зашёл" in s for s in sent)), sent
print("/leave ->", bot.handle_command("/leave"))
n = len(sent)
time.sleep(25)                                   # ~25 loop passes
later = sent[n:]
print("after leave:", later)
assert not any("оборвался" in s or "Снова в звонке" in s or "Зашёл" in s for s in later), later
assert not bot.tw.BROWSER.in_call()
print("/join ->", bot.handle_command("/join"))
assert until(lambda: any("Зашёл" in s for s in sent[n:]), 60), sent[n:]
assert bot.tw.BROWSER.in_call()
# an unexpected drop still rejoins
m = len(sent)
b = bot.tw.BROWSER; b.call(lambda: b.page.evaluate("dropCall()"))
assert until(lambda: any("оборвался" in s for s in sent[m:]), 60), sent[m:]
bot.CURRENT["jstop"].set(); time.sleep(3); b.close()
print("LEAVE TEST OK")
