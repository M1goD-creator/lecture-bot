"""Server (Linux) backend for the lecture bot.

On Windows the bot drives the Teams desktop app (pywinauto) and records the speakers
(soundcard loopback). A server has neither, so here:

* Teams runs in Chromium driven by Playwright (as a guest with TEAMS_GUEST_NAME, or signed in
  if TEAMS_STORAGE_STATE points to a saved login made with teams_login.py);
* the browser plays the call into a PulseAudio null sink, and parec records that sink's monitor;
* slides and /screenshot are screenshots of the browser page.

The browser never gets a microphone or camera: no permission is granted and no fake devices
are used, so the bot can't make a sound in the lecture even if a mute toggle is missed.

Playwright's sync API must stay on one thread, so every browser action goes through
Browser.call(), which runs it on a dedicated thread.
"""
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SINK = os.getenv("PULSE_SINK_NAME", "lecture")
GUEST_NAME = os.getenv("TEAMS_GUEST_NAME", "George (notes)")
STATE_FILE = os.getenv("TEAMS_STORAGE_STATE") or os.path.join(HERE, "teams-state.json")
HEADLESS = os.getenv("BROWSER_HEADLESS", "0") == "1"      # default: a normal window inside Xvfb
SR = 16000

# ───────────────────────────── PulseAudio ─────────────────────────────


def _pactl(*args, check=True):
    return subprocess.run(["pactl", *args], capture_output=True, text=True, check=check, timeout=20)


def pulse_setup():
    """Starts a per-user PulseAudio if needed and makes the null sink the default output."""
    for tool in ("pactl", "parec", "pulseaudio"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} not found: apt install pulseaudio pulseaudio-utils")
    run = os.environ.get("XDG_RUNTIME_DIR")
    if run:
        os.makedirs(run, mode=0o700, exist_ok=True)
        os.chmod(run, 0o700)
    if _pactl("info", check=False).returncode != 0:
        subprocess.run(["pulseaudio", "--start", "--exit-idle-time=-1", "--log-target=stderr"],
                       check=False, timeout=30)
        for _ in range(20):
            if _pactl("info", check=False).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("PulseAudio did not start")
    sinks = _pactl("list", "short", "sinks").stdout
    if not re.search(rf"^\d+\s+{re.escape(SINK)}\s", sinks, re.M):
        _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
               "sink_properties=device.description=LectureBot")
    _pactl("set-default-sink", SINK)
    os.environ["PULSE_SINK"] = SINK                      # Chromium inherits it
    print(f"Audio: PulseAudio null sink '{SINK}'")


def audio_reader(stop, on_chunk):
    """Calls on_chunk(np.float32 array) about once a second until stop is set."""
    import numpy as np
    while not stop.is_set():
        p = subprocess.Popen(["parec", "-d", f"{SINK}.monitor", f"--rate={SR}", "--channels=1",
                              "--format=float32le", "--latency-msec=200"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        print("Recording from:", f"{SINK}.monitor")
        try:
            while not stop.is_set():
                data = p.stdout.read(SR * 4)
                if not data:
                    break
                n = len(data) // 4 * 4
                on_chunk(np.frombuffer(data[:n], dtype=np.float32).copy())
        finally:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        if not stop.is_set():
            print("parec stopped, restarting in 3 s")
            stop.wait(3)


# ───────────────────────────── Teams in the browser ─────────────────────────────

CONTINUE_BROWSER = re.compile(r"continue on this browser|join on the web( instead)?|use the web app( instead)?|"
                              r"продолжить в этом браузере|присоединиться (через|в) браузер", re.I)
NO_AV = re.compile(r"continue without audio or video|продолжить без (звука|аудио)", re.I)
JOIN_NOW = re.compile(r"^\s*(join now|присоединиться сейчас|подключиться сейчас|participer maintenant|"
                      r"jetzt teilnehmen|unirse ahora)\s*$", re.I)
LEAVE = re.compile(r"^\s*(leave|hang up|покинуть|выйти|положить трубку)\b", re.I)
REJOIN = re.compile(r"^\s*(rejoin|присоединиться снова|подключиться снова)\s*$", re.I)
LOBBY = re.compile(r"let you in soon|someone in the meeting should let you in|waiting for (the )?(organizer|someone)|"
                   r"вас скоро впустят|ожидайте, пока|ждём, пока", re.I)
ENDED = re.compile(r"you left the meeting|the meeting has ended|meeting ended|call ended|you've been removed|"
                   r"вы покинули собрание|собрание завершено|звонок завершен", re.I)
SIGNIN = re.compile(r"sign in to join the meeting|need you to sign in again|you need to be signed in|"
                    r"pick an account|войдите, чтобы присоединиться|необходимо войти", re.I)
MIC_CAM = re.compile(r"mic|микрофон|camera|камер|video|видео", re.I)
NAME_HINT = re.compile(r"name|имя", re.I)

# ── automatic Microsoft sign-in (the university asks for the password again about once a day) ──
ENV_FILE = os.getenv("TEAMS_ENV_FILE") or os.path.join(HERE, ".env")
LOGIN_HOSTS = tuple(h.strip() for h in os.getenv(
    "TEAMS_LOGIN_HOSTS", "login.microsoftonline.com,login.microsoft.com,login.live.com").split(",") if h.strip())
SIGNIN_BTN = re.compile(r"^\s*(sign in|войти)\s*$", re.I)
MFA = re.compile(r"approve sign.?in request|enter (the )?code|verify your identity|authenticator|"
                 r"more information required|we('ve)? (texted|sent)|security key|two.?step|"
                 r"verify it'?s you|подтвердите (вход|личность)|введите код|двухэтап", re.I)
BAD_PW = re.compile(r"password is incorrect|incorrect password|account or password|неверн(ый|ая) (пароль|учетн)", re.I)
KMSI = re.compile(r"stay signed in\?|оставаться в системе\?|не выходить из системы\?", re.I)
# "Let's keep your account secure" — an MFA set-up nudge that can still be skipped (Next, then Skip setup)
NUDGE = re.compile(r"keep your account secure|another way to verify|set up another way|"
                   r"start by getting the app|security info|защитите (свою )?учетную запись", re.I)
SKIP = re.compile(r"^\s*(skip setup|skip for now|skip|ask (me )?later|not now|пропустить( настройку)?|"
                  r"напомнить позже|preskoči( podešavanje)?)\s*$", re.I)
NEXT = re.compile(r"^\s*(next|далее|dalje|sledeće)\s*$", re.I)
LOGIN_FORM = "input[name=loginfmt]:visible, input[type=email]:visible, input[name=passwd]:visible, #KmsiCheckboxField"


def login_secrets():
    """(email, password) from the .env file, read fresh each time: no restart after adding them.
    Never log or print these."""
    vals = {}
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(ENV_FILE) if os.path.exists(ENV_FILE) else {}
    except Exception:
        pass
    email = (vals.get("TEAMS_EMAIL") or os.getenv("TEAMS_EMAIL") or "").strip()
    pw = vals.get("TEAMS_PASSWORD") or os.getenv("TEAMS_PASSWORD") or ""
    return email, pw


def web_url(link):
    """Meeting links open a 'use the app or the browser?' page; these flags ask for the browser."""
    if "/l/meetup-join/" in link and "msLaunch" not in link:
        link += ("&" if "?" in link else "?") + "msLaunch=false&directDl=false&suppressPrompt=true&anon=true"
    return link


class Browser:
    def __init__(self):
        self.q = queue.Queue()
        self.thread = None
        self.page = None
        self.ctx = None
        self.lock = threading.Lock()

    # Runs on the browser thread.
    def _run(self, ready, failed):
        try:
            from playwright.sync_api import sync_playwright
            p = sync_playwright().start()
            browser = p.chromium.launch(headless=HEADLESS, args=[
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run", "--no-default-browser-check", "--lang=en-US",
                "--window-size=1600,900"])
            ua = (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  f"Chrome/{browser.version} Safari/537.36")     # no "HeadlessChrome": Teams rejects it
            state = STATE_FILE if os.path.exists(STATE_FILE) else None
            self.ctx = browser.new_context(storage_state=state, user_agent=ua, locale="en-US",
                                           viewport={"width": 1600, "height": 900})
            print("Browser: Chromium", browser.version, "(signed in)" if state else "(guest)")
        except Exception as e:
            failed.append(e)
            ready.set()
            return
        ready.set()
        try:
            while True:
                fn, box, done = self.q.get()
                if fn is None:
                    break
                try:
                    box["r"] = fn()
                except Exception as e:                # handed back to the caller
                    box["e"] = e
                done.set()
        finally:
            try:
                browser.close()
                p.stop()
            except Exception:
                pass

    def call(self, fn, timeout=180):
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                ready, failed = threading.Event(), []
                self.page = None
                self.q = queue.Queue()
                self.thread = threading.Thread(target=self._run, args=(ready, failed), daemon=True)
                self.thread.start()
                if not ready.wait(90):
                    raise RuntimeError("browser did not start")
                if failed:
                    raise failed[0]
        box, done = {}, threading.Event()
        self.q.put((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError("browser action timed out")
        if "e" in box:
            raise box["e"]
        return box.get("r")

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def close(self):
        if self.alive():
            self.q.put((None, None, None))
            self.thread.join(15)

    # ---- helpers below run on the browser thread ----
    def _page(self):
        if self.page is None or self.page.is_closed():
            self.page = self.ctx.new_page()
        return self.page

    def _find(self, role, rx):
        """First visible element with that role and accessible name, in any frame."""
        for fr in self._page().frames:
            try:
                loc = fr.get_by_role(role, name=rx)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
            except Exception:
                continue
        return None

    def _has_text(self, rx):
        for fr in self._page().frames:
            try:
                if rx.search(fr.locator("body").inner_text(timeout=3000) or ""):
                    return True
            except Exception:
                continue
        return False

    def _mute(self):
        """Turns off any mic/camera toggle that is on. Returns names of what it clicked."""
        clicked = []
        for fr in self._page().frames:
            for role in ("switch", "checkbox"):
                try:
                    loc = fr.get_by_role(role, name=MIC_CAM)
                    for i in range(min(loc.count(), 6)):
                        el = loc.nth(i)
                        if el.is_visible() and (el.get_attribute("aria-checked") == "true" or el.is_checked()):
                            el.click(timeout=3000)
                            clicked.append((el.get_attribute("aria-label") or role)[:40])
                except Exception:
                    continue
        return clicked

    def _fill_name(self):
        for fr in self._page().frames:
            try:
                loc = fr.get_by_placeholder(NAME_HINT)
                if loc.count() and loc.first.is_visible() and not loc.first.input_value():
                    loc.first.fill(GUEST_NAME)
                    return True
            except Exception:
                continue
        return False

    # ---- public API (any thread) ----
    def open_meeting(self, link):
        def go():
            pg = self._page()
            pg.goto(web_url(link), wait_until="domcontentloaded", timeout=60000)
            pg.bring_to_front()
        self.call(go)

    def step(self):
        """One pass over the page: clicks whatever moves us towards the call.
        Returns (state, muted) where state is in_call, lobby, joining, prejoin, launcher, ended,
        signin (the meeting needs a login and the saved one expired) or unknown."""
        def go():
            if self._find("button", LEAVE):
                return "in_call", []
            if self._has_text(LOBBY):
                return "lobby", []
            b = self._find("button", CONTINUE_BROWSER) or self._find("link", CONTINUE_BROWSER)
            if b:
                b.click(timeout=5000)
                return "launcher", []
            b = self._find("button", NO_AV)
            if b:
                b.click(timeout=5000)
                return "prejoin", []
            jn = self._find("button", JOIN_NOW)
            if jn:
                self._fill_name()
                off = self._mute()
                if jn.is_enabled():
                    jn.click(timeout=5000)
                    return "joining", off
                return "prejoin", off
            if self._find("button", REJOIN) or self._has_text(ENDED):
                return "ended", []
            if "login.microsoftonline.com" in self._page().url or self._has_text(SIGNIN):
                return "signin", []                   # the saved login expired
            return "unknown", []
        return self.call(go)

    def in_call(self):
        if not self.alive():
            return False
        return self.call(lambda: bool(self.page and not self.page.is_closed() and self._find("button", LEAVE)))

    def leave(self, still_mine=None):
        """still_mine() is checked on the browser thread: if the next lecture has already
        opened its meeting in this page, leaving would close that one instead."""
        if not self.alive():
            return False

        def go():
            if not self.page or self.page.is_closed():
                return False
            if still_mine is not None and not still_mine():
                return False
            b = self._find("button", LEAVE)
            if b:
                b.click(timeout=5000)
                self.page.wait_for_timeout(1500)
            self.page.goto("about:blank")
            return bool(b)
        return self.call(go)

    def screenshot(self):
        """PIL image of the meeting page, or None if no meeting page is open."""
        if not self.alive():
            return None

        def go():
            if not self.page or self.page.is_closed() or self.page.url in ("", "about:blank"):
                return None
            return self.page.screenshot(type="png", timeout=15000)
        data = self.call(go, timeout=60)
        if not data:
            return None
        from PIL import Image
        return Image.open(io.BytesIO(data)).convert("RGB")

    def save_state(self):
        """Keeps a signed-in session fresh (Microsoft rotates tokens). Guests have nothing to save."""
        if not self.alive() or not os.path.exists(STATE_FILE):
            return

        def go():
            st = self.ctx.storage_state()
            tmp = STATE_FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # a login: owner only
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(st, f)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, STATE_FILE)
        self.call(go)

    def auto_login(self, timeout=120):
        """Signs in again with TEAMS_EMAIL / TEAMS_PASSWORD from .env when Microsoft asks.
        Returns ok | no_password | no_email | mfa | bad_password | failed.
        Stops at any code / Authenticator step: that needs George's phone."""
        email, pw = login_secrets()
        if not pw:
            return "no_password"

        def on_login(pg):
            host = re.sub(r"^https?://([^/:]+).*", r"\1", pg.url or "")
            if any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS):
                return True
            try:
                return pg.locator(LOGIN_FORM).count() > 0
            except Exception:
                return False

        def body(pg):
            try:
                return pg.locator("body").inner_text(timeout=3000) or ""
            except Exception:
                return ""

        def submit(pg):
            b = pg.locator("#idSIButton9:visible, input[type=submit]:visible, button[type=submit]:visible")
            if b.count():
                b.first.click(timeout=5000)
            else:
                pg.keyboard.press("Enter")

        def control(pg, rx):
            for role in ("link", "button"):
                try:
                    loc = pg.get_by_role(role, name=rx)
                    for i in range(min(loc.count(), 4)):
                        if loc.nth(i).is_visible():
                            return loc.nth(i)
                except Exception:
                    continue
            b = pg.locator("input[type=submit]:visible, input[type=button]:visible")
            for i in range(min(b.count(), 4)):
                if rx.search(b.nth(i).get_attribute("value") or ""):
                    return b.nth(i)
            return None

        def go():
            pg = self._page()
            t0, typed_pw, was_login, teams_ok, nexts = time.time(), False, False, 0, 0
            while time.time() - t0 < timeout:
                for other in self.ctx.pages:              # Microsoft sometimes opens the login in a popup
                    if other is not pg and not other.is_closed() and on_login(other):
                        pg = other
                text = body(pg)
                s = control(pg, SKIP) if typed_pw else None
                if s:                                     # any MFA set-up page that offers "Skip setup"
                    was_login, teams_ok = True, 0
                    s.click(timeout=5000)
                    pg.wait_for_timeout(3000)
                    continue
                if typed_pw and NUDGE.search(text):       # MFA set-up nudge: Next until Skip shows up
                    was_login, teams_ok = True, 0
                    n = control(pg, NEXT)
                    if not n or nexts >= 3:
                        return "mfa"                      # no way to skip: needs George's phone
                    n.click(timeout=5000)
                    nexts += 1
                    pg.wait_for_timeout(3000)
                    continue
                if on_login(pg):
                    was_login, teams_ok = True, 0
                    pw_box = pg.locator("input[name=passwd]:visible, input[type=password]:visible")
                    em_box = pg.locator("input[name=loginfmt]:visible, input[type=email]:visible")
                    if typed_pw and BAD_PW.search(text):
                        return "bad_password"
                    if pw_box.count():
                        if typed_pw:                      # still on the password page: wrong or slow
                            if BAD_PW.search(text):
                                return "bad_password"
                        else:
                            pw_box.first.fill(pw)
                            submit(pg)
                            typed_pw = True
                    elif em_box.count():
                        if not em_box.first.input_value():
                            if not email:
                                return "no_email"
                            em_box.first.fill(email)
                        submit(pg)
                    elif KMSI.search(text):              # "Stay signed in?" — yes, and don't ask again
                        cb = pg.locator("#KmsiCheckboxField")
                        if cb.count() and not cb.first.is_checked():
                            cb.first.check(timeout=3000)
                        submit(pg)
                    elif MFA.search(text):
                        return "mfa"
                    elif email and pg.get_by_text(email, exact=False).count():   # "Pick an account"
                        pg.get_by_text(email, exact=False).first.click(timeout=5000)
                else:
                    if was_login and MFA.search(text):
                        return "mfa"
                    if was_login and not SIGNIN.search(text):
                        teams_ok += 1                     # back in Teams and it no longer asks
                        if teams_ok >= 2:
                            return "ok"
                    else:
                        b = self._find("button", SIGNIN_BTN) or self._find("link", SIGNIN_BTN)
                        if b:
                            b.click(timeout=5000)
                        elif not was_login and not SIGNIN.search(text) and time.time() - t0 > 20:
                            return "ok"                   # nothing asked for a login after all
                pg.wait_for_timeout(2500)
            return "failed"

        try:
            return self.call(go, timeout=timeout + 60)
        except Exception as e:                            # type only: messages could echo typed text
            print("auto_login:", type(e).__name__)
            return "failed"


BROWSER = Browser()
