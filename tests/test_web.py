# Offline tests against tests/fake_teams.html (no Teams, no Telegram): .venvScriptspython tests<name>.py
import os, sys, threading, time, http.server, functools
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update(TEAMS_BACKEND="web", TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="1", BROWSER_HEADLESS="1",
                  TEAMS_GUEST_NAME="Test Bot", TEAMS_STORAGE_STATE=os.path.join(HERE, "nostate.json"),
                  REJOIN_AFTER_MIN="0")
sys.path.insert(0, os.path.dirname(HERE))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=HERE))
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
import bot
sent = []
bot.tg_send = lambda t, fmt=True: (sent.append(t), print("TG:", t))[0] and []
bot.requests.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network in tests"))
bot.is_meeting_link = lambda l: True
class Fast(threading.Event):
    def wait(self, t=None): return super().wait(min(t or 0, 1))
stop = Fast()
link = f"http://127.0.0.1:{port}/fake_teams.html"
ev = {"key": "t", "title": "Test lecture", "start": datetime.now(timezone.utc), "end": datetime.now(timezone.utc) + timedelta(minutes=3), "link": link}
bot.CURRENT["jstop"] = stop
th = threading.Thread(target=bot.web_auto_join, args=(ev, stop)); th.start()
def wait_for(pred, t=40):
    t0 = time.time()
    while time.time() - t0 < t:
        if pred(): return True
        time.sleep(0.5)
    return False
assert wait_for(lambda: any("Зашёл" in s for s in sent)), sent
pg = bot.tw.BROWSER
muted = pg.call(lambda: pg.page.evaluate("[window.joinedMuted, window.who]"))
print("joined muted / name:", muted); assert muted == [True, "Test Bot"]
assert any("лобби" in s for s in sent)
img = pg.screenshot(); print("screenshot", img.size)
bot.LAST_VOICE[0] = time.time() - 3600
pg.call(lambda: pg.page.evaluate("dropCall()"))
assert wait_for(lambda: any("оборвался" in s for s in sent)), sent
pg.call(lambda: pg.page.evaluate("1"))
assert wait_for(lambda: any("Снова в звонке" in s for s in sent), 60), sent
stop.set(); th.join(30)
url = pg.call(lambda: pg.page.url); print("after stop:", url); assert url == "about:blank"
# /leave and /screenshot commands when not in a call
print("leave reply:", bot.handle_command("/leave"))
pg.close()
print("ALL OK")
