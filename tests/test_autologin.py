# Offline test of the automatic Microsoft sign-in (fake pages, fake password).
# Run: .venv\Scripts\python tests\test_autologin.py
import os, sys, threading, http.server, functools, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
envf = os.path.join(tempfile.mkdtemp(), ".env")
os.environ.update(TEAMS_BACKEND="web", TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="1", BROWSER_HEADLESS="1",
                  TEAMS_STORAGE_STATE=os.path.join(HERE, "nostate.json"), TEAMS_ENV_FILE=envf,
                  TEAMS_LOGIN_HOSTS="login.invalid")
sys.path.insert(0, os.path.dirname(HERE))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(http.server.SimpleHTTPRequestHandler, directory=HERE))
threading.Thread(target=srv.serve_forever, daemon=True).start()
import teams_web as tw

base = f"http://127.0.0.1:{srv.server_address[1]}/fake_mslogin.html"


def run(mode, pw):
    with open(envf, "w") as f:
        f.write(f"TEAMS_EMAIL='student@example.test'\n" + (f"TEAMS_PASSWORD='{pw}'\n" if pw else ""))
    tw.BROWSER.open_meeting(f"{base}?mode={mode}")
    return tw.BROWSER.auto_login(timeout=40)


results = {
    "ok": run("ok", "secret-test-pw"),
    "mfa": run("mfa", "secret-test-pw"),
    "nudge -> ok": run("nudge", "secret-test-pw"),
    "nudge without skip -> mfa": run("nudge_noskip", "secret-test-pw"),
    "bad_password": run("ok", "wrong-pw"),
    "no_password": run("ok", ""),
}
tw.BROWSER.close()
srv.shutdown()
print(results)
for case, got in results.items():
    want = case.split(" -> ")[-1]
    assert got == want, f"{case}: expected {want}, got {got}"
print("OK: auto login handles ok / mfa / wrong password / no password")
