"""Lecture bot v2.

* читает календарь (ICS), в начале лекции в Teams открывает ссылку и пробует нажать «Присоединиться»;
* пока идёт лекция, каждые LIVE_SEC секунд присылает в Telegram расшифровку того, что говорит преподаватель;
* каждые CHUNK_MINUTES минут присылает конспект на русском (по расшифровке + слайдам с экрана),
  после конспекта удаляет из чата сообщения с расшифровкой (сама расшифровка остаётся в папке lectures/);
* /summary — досрочный конспект, запись при этом продолжается;
* отвечает на обычные вопросы в чате и делает сводку по теме (/topic) по всем сохранённым лекциям.

Без ICS_URL (или с флагом --now) просто пишет всё подряд, как раньше.
"""
import json
import base64
import html
import io
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
import warnings
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore", message="data discontinuity")   # soundcard: пропуск пары миллисекунд звука, не ошибка

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
CHUNK_MIN = float(os.getenv("CHUNK_MINUTES", "45"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
LANG = os.getenv("LECTURE_LANG", "en")                 # язык лекции: en / ru / ...
PROVIDER = os.getenv("PROVIDER", "gemini")             # gemini | anthropic
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
# у каждой модели свой бесплатный дневной лимит: конспекты идут по цепочке «основная → запасные»,
# вопросы из чата — только на лёгких моделях, чтобы не съедать лимит конспектов
SUMMARY_MODELS = [GEMINI_MODEL] + [m.strip() for m in os.getenv(
    "GEMINI_FALLBACKS", "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash").split(",") if m.strip() and m.strip() != GEMINI_MODEL]
CHAT_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_CHAT_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",") if m.strip()]
# запасной путь, когда все модели Gemini упёрлись в лимит: локальный шлюз OmniRoute (пусто = выключено)
OMNIROUTE_URL = os.getenv("OMNIROUTE_URL", "").strip().rstrip("/")
OMNIROUTE_KEY = os.getenv("OMNIROUTE_API_KEY", "").strip()
OMNIROUTE_MODEL = os.getenv("OMNIROUTE_MODEL", "auto").strip()
SHOT_EVERY = int(os.getenv("SCREENSHOT_EVERY_SEC", "30"))
MAX_SLIDES = int(os.getenv("MAX_SLIDES_PER_CHUNK", "10"))
MONITOR = int(os.getenv("MONITOR", "1"))

ICS_URL = os.getenv("ICS_URL", "").strip().replace("webcal://", "https://")
JOIN_LEAD_MIN = float(os.getenv("JOIN_LEAD_MIN", "2"))      # за сколько минут до начала заходить
END_GRACE_MIN = float(os.getenv("END_GRACE_MIN", "15"))     # сколько ждать после конца по расписанию
AUTO_CLICK = os.getenv("AUTO_CLICK", "1") == "1"
LIVE = os.getenv("LIVE", "1") == "1"                        # присылать расшифровку в реальном времени
LIVE_SEC = int(os.getenv("LIVE_SEC", "30"))
BREAK_MIN = float(os.getenv("BREAK_MIN", "5"))              # столько минут тишины = перерыв, шлём конспект части
JOIN_RETRY_SEC = float(os.getenv("JOIN_RETRY_SEC", "45"))   # пауза между попытками войти, если встречи ещё нет
PIN = os.getenv("PIN", "all").lower()                       # all — закреплять все конспекты, last — только финальный, off — не закреплять
REJOIN = os.getenv("REJOIN", "1") == "1"                    # перезаходить, если после перерыва звонок закончился
REJOIN_AFTER_MIN = float(os.getenv("REJOIN_AFTER_MIN", "10"))
DELETE_LIVE = os.getenv("DELETE_LIVE", "1") == "1"          # удалять расшифровку из чата после конспекта
# app — Teams desktop app + звук колонок (Windows); web — Teams в Chromium + PulseAudio (сервер, Linux)
WEB = os.getenv("TEAMS_BACKEND", "app" if sys.platform == "win32" else "web").lower() == "web"
if WEB:
    import teams_web as tw
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "lectures")
CONTEXT_CHARS = int(os.getenv("CONTEXT_CHARS", "400000"))   # сколько текста лекций давать модели на вопрос
LAST_VOICE = [time.time()]                                  # когда последний раз слышали речь
SR = 16000
CLIENT = [None]                                             # клиент Anthropic, если PROVIDER=anthropic


# ───────────────────────────── Telegram ─────────────────────────────
# LaTeX -> Unicode: модели любят писать формулы как $\frac{a}{b}$, а Telegram их не рисует
_TEX_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "ne": "≠", "gg": "≫", "ll": "≪", "neq": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝", "infty": "∞",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "implies": "⇒",
    "leftrightarrow": "↔", "Leftrightarrow": "⇔", "iff": "⇔", "mapsto": "↦",
    "sum": "∑", "prod": "∏", "int": "∫", "oint": "∮", "partial": "∂", "nabla": "∇",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃", "neg": "¬",
    "land": "∧", "wedge": "∧", "lor": "∨", "vee": "∨", "oplus": "⊕", "otimes": "⊗", "circ": "∘",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°", "ldots": "…", "cdots": "⋯", "dots": "…",
    "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "langle": "⟨", "rangle": "⟩",
    "mathbb{R}": "ℝ", "mathbb{N}": "ℕ", "mathbb{Z}": "ℤ", "mathbb{Q}": "ℚ", "mathbb{C}": "ℂ",
    "log": "log", "ln": "ln", "lg": "lg", "sin": "sin", "cos": "cos", "tan": "tan", "tg": "tg", "ctg": "ctg",
    "lim": "lim", "max": "max", "min": "min", "exp": "exp", "det": "det", "mod": "mod", "bmod": "mod",
    "quad": "  ", "qquad": "    ", ",": " ", ";": " ", ":": " ", "!": "", " ": " ",
    "%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", "\\": "\n",
}
_SUP = dict(zip("0123456789+-=()abcdefghijklmnoprstuvwxyzABDEGHIJKLMNOPRTUVW",
                "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ"))
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))
_BR = r"\{((?:[^{}]|\{[^{}]*\})*)\}"                    # {…} с одним уровнем вложенности


def _script(s, table, mark):
    s = s.strip()
    if s and all(c in table for c in s):
        return "".join(table[c] for c in s)
    return f"{mark}{s}" if re.fullmatch(r"\w+", s) else f"{mark}({s})"   # заглавных индексов в Unicode нет: U_F


def _wrap(s):
    s = s.strip()
    return s if not re.search(r"[\s+\-−·×/∕]", s) else f"({s})"


def _tex(s):
    """Переводит кусок LaTeX в читаемый текст с Unicode."""
    for _ in range(4):                                   # вложенные \frac{\sqrt{..}}{..}
        s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname|textbf|mbox|boldsymbol)\s*" + _BR, r"\1", s)
        s = re.sub(r"\\[dt]?frac\s*" + _BR + r"\s*" + _BR, lambda m: f"{_wrap(m[1])}∕{_wrap(m[2])}", s)
        s = re.sub(r"\\sqrt\s*\[([^\]]+)\]\s*" + _BR, lambda m: _script(m[1], _SUP, "^") + "√" + _wrap(m[2]), s)
        s = re.sub(r"\\sqrt\s*" + _BR, lambda m: "√" + _wrap(m[1]), s)
        s = re.sub(r"\\(?:overline|bar)\s*" + _BR, lambda m: m[1] + "\u0305", s)
        s = re.sub(r"\\(?:vec)\s*" + _BR, lambda m: m[1] + "\u20d7", s)
        s = re.sub(r"\\hat\s*" + _BR, lambda m: m[1] + "\u0302", s)
    s = re.sub(r"\\sqrt\s*(\w)", r"√\1", s)
    s = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\s*([()\[\]|.]|\\[{}|])",
               lambda m: "" if m[1] == "." else m[1].lstrip("\\"), s)
    s = s.replace("\\mathbb{R}", "ℝ").replace("\\mathbb{N}", "ℕ").replace("\\mathbb{Z}", "ℤ") \
         .replace("\\mathbb{Q}", "ℚ").replace("\\mathbb{C}", "ℂ")
    s = re.sub(r"\\([A-Za-z]+|[,;:! %$&#_{}\\])",
               lambda m: _TEX_SYMBOLS.get(m[1], m[0]), s)
    s = re.sub(r"\^\s*" + _BR, lambda m: _script(m[1], _SUP, "^"), s)
    s = re.sub(r"\^\s*(\w)", lambda m: _script(m[1], _SUP, "^"), s)
    s = re.sub(r"_\s*" + _BR, lambda m: _script(m[1], _SUB, "_"), s)
    s = re.sub(r"(?<=\S)_(\w)(?!\w)", lambda m: _script(m[1], _SUB, "_"), s)
    s = re.sub(r"(?<!\\)[{}]", "", s)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def delatex(text):
    """Убирает LaTeX из ответа модели: формулы в $…$, \\(…\\), \\[…\\] и одиночные команды → Unicode."""
    text = re.sub(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]", lambda m: _tex(m[1] or m[2]), text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: _tex(m[1]), text, flags=re.S)
    text = re.sub(r"(?<![\\\w$])\$(?=[^\s$\d])([^$\n]+?)(?<=\S)\$(?![\w$])", lambda m: _tex(m[1]), text)
    text = re.sub(r"(?<![\\$])\$([^$\n]*[\\^_{][^$\n]*?)\$", lambda m: _tex(m[1]), text)   # $2 U_{peak}$
    if "\\" in text:                                     # формулы без $ — только сами команды
        text = re.sub(r"\\[dt]?frac\s*" + _BR + r"\s*" + _BR + r"|\\sqrt\s*(?:\[[^\]]+\])?\s*" + _BR +
                      r"|\\(?:text|mathrm|mathbf|mathbb|operatorname)\s*" + _BR + r"|\\[A-Za-z]+",
                      lambda m: _tex(m[0]) if not m[0][1:] in ("n", "t") else m[0], text)
    return text


def _to_tg_html(text):
    t = html.escape(delatex(text), quote=False)
    stash = []                                        # code is set aside so * and _ inside it stay literal

    def keep(s):
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"
    t = re.sub(r"```([\w+-]*)\n?(.*?)```",
               lambda m: keep((f'<pre><code class="language-{m[1].lower()}">' if m[1] else "<pre><code>")
                              + m[2].strip("\n") + "</code></pre>"), t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{m[1]}</code>"), t)
    t = _md_inline(t)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m[1])], t)


def _md_inline(t):
    t = re.sub(r"(?m)^\s*([-*_])(?:\s*\1){2,}\s*$", "", t)               # --- / *** — разделители
    t = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*#*$",
               lambda m: "<b>" + m[1].replace("**", "").replace("__", "") + "</b>", t)
    t = re.sub(r"(?m)^(\s*)[\*\-+•]\s+", r"\1• ", t)
    t = re.sub(r"(?m)^&gt;\s?", "▎ ", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?![\s*])([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?<![\w_])_(?![\s_])([^_\n]+?)(?<!\s)_(?![\w_])", r"<i>\1</i>", t)
    t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    t = t.replace("**", "")                                               # непарные звёздочки
    return re.sub(r"\n{3,}", "\n\n", t)


def _html_to_plain(t):
    return html.unescape(re.sub(r"</?(?:b|i|s|code|pre|a|blockquote)(?: [^>]*)?>", "", t))


def _split(text, limit=3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:                      # очень длинная строка без переносов
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


class _NoResponse:
    """Stands in for a response when Telegram could not be reached at all."""
    ok, status_code, text = False, 0, "network error"

    def json(self):
        return {}


def _tg_post(payload):
    """Never raises: a slow or unreachable Telegram must not kill the recording threads."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = _NoResponse()
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            print("Telegram:", type(e).__name__)
            r = _NoResponse()
            time.sleep(3)
            continue
        if r.status_code == 429:                      # слишком часто — ждём, сколько просит Telegram
            try:
                wait = r.json()["parameters"]["retry_after"]
            except Exception:
                wait = 5
            time.sleep(wait + 1)
            continue
        return r
    return r


_LOUD = ("⚠", "🔑", "⌛", "❗")                          # service messages that still ring


def tg_send(text, fmt=True, silent=None, markup=None):
    """Отправляет текст (длинный — несколькими сообщениями). Возвращает id отправленных сообщений.
    silent=None: service messages (fmt=False) arrive without sound, except warnings (⚠️ 🔑 ⌛)."""
    if silent is None:
        silent = not fmt and not text.lstrip().startswith(_LOUD)
    parts = [_to_tg_html(p) for p in _split(text)] if fmt else _split(text)
    return tg_send_html(parts, silent=silent, markup=markup, html_mode=fmt)


def tg_send_html(parts, silent=False, markup=None, html_mode=True):
    """Sends ready message bodies (one message each); markup goes on the last one."""
    ids = []
    for i, body in enumerate(parts):
        payload = {"chat_id": TG_CHAT, "text": body, "link_preview_options": {"is_disabled": True}}
        if html_mode:
            payload["parse_mode"] = "HTML"
        if silent:
            payload["disable_notification"] = True
        if markup and i == len(parts) - 1:
            payload["reply_markup"] = markup
        r = _tg_post(payload)
        if not r.ok and html_mode and r.status_code == 400 and re.search(r"</?i>", body):
            print("Telegram HTML rejected, retrying without italics:", r.text[:200])
            payload["text"] = re.sub(r"</?i>", "", body)      # italics are the usual culprit; keep the layout
            r = _tg_post(payload)
        if not r.ok and html_mode and r.status_code == 400:   # кривая разметка — шлём без тегов, но и без ** и LaTeX
            print("Telegram HTML rejected:", r.text[:200])
            payload.pop("parse_mode")
            payload["text"] = _html_to_plain(body)
            r = _tg_post(payload)
        if not r.ok:
            print("Telegram error:", r.text[:300])
            continue
        try:
            ids.append(r.json()["result"]["message_id"])
        except Exception:
            pass
    return ids


def tg_edit(message_id, text, markup=None):
    """Edits a plain-text bot message. Returns False if it is gone (deleted / too old)."""
    payload = {"chat_id": TG_CHAT, "message_id": message_id, "text": text,
               "reply_markup": markup or {"inline_keyboard": []}}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/editMessageText", json=payload, timeout=30)
    except requests.exceptions.RequestException as e:
        print("edit:", type(e).__name__)
        return True                                   # network hiccup: keep the message, retry later
    return r.ok or "not modified" in r.text


def tg_photos(images, captions, silent=True):
    """Sends 1–10 PIL images as one album (each with its caption)."""
    images = images[:10]
    if not images:
        return
    files, media = {}, []
    for i, img in enumerate(images):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=75)
        files[f"p{i}"] = (f"p{i}.jpg", buf.getvalue())
        media.append({"type": "photo", "media": f"attach://p{i}", "caption": (captions[i] or "")[:1000]})
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    try:
        if len(images) == 1:
            r = requests.post(base + "/sendPhoto", files={"photo": files["p0"]}, timeout=60,
                              data={"chat_id": TG_CHAT, "caption": media[0]["caption"],
                                    "disable_notification": str(silent).lower()})
        else:
            r = requests.post(base + "/sendMediaGroup", files=files, timeout=90,
                              data={"chat_id": TG_CHAT, "media": json.dumps(media, ensure_ascii=False),
                                    "disable_notification": str(silent).lower()})
        if not r.ok:
            print("photos:", r.text[:200])
    except requests.exceptions.RequestException as e:
        print("photos:", type(e).__name__)


def tg_delete(ids):
    """Удаляет сообщения бота из чата (по 100 за раз). Возвращает, сколько удалось удалить."""
    base, done = f"https://api.telegram.org/bot{TG_TOKEN}", 0
    ids = list(ids)
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        try:
            r = requests.post(base + "/deleteMessages", json={"chat_id": TG_CHAT, "message_ids": batch}, timeout=30)
            if r.ok:
                done += len(batch)
                continue
            for mid in batch:                         # старый API или часть уже удалена — по одному
                r = requests.post(base + "/deleteMessage", json={"chat_id": TG_CHAT, "message_id": mid}, timeout=30)
                done += r.ok
        except requests.exceptions.RequestException as e:
            print("Не удалось удалить сообщения:", type(e).__name__)
    return done


def tg_typing():
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": TG_CHAT, "action": "typing"}, timeout=10)
    except requests.exceptions.RequestException:
        pass


def tg_pin(message_id):
    """Закрепляет сообщение без звукового уведомления."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/pinChatMessage",
                          json={"chat_id": TG_CHAT, "message_id": message_id,
                                "disable_notification": True}, timeout=30)
        if not r.ok:
            print("Не удалось закрепить:", r.text[:300])
    except requests.exceptions.RequestException as e:
        print("Не удалось закрепить:", type(e).__name__)


BUTTONS = {"inline_keyboard": [
    [{"text": "📝 Конспект сейчас", "callback_data": "/summary"},
     {"text": "📷 Скриншот", "callback_data": "/screenshot"}],
    [{"text": "🔄 Обновить", "callback_data": "/refresh"},
     {"text": "🚪 Выйти из звонка", "callback_data": "/leave"}]]}


def _mins(sec):
    m = int(sec // 60)
    return f"{m // 60} ч {m % 60:02d} мин" if m >= 60 else f"{m} мин"


class Status:
    """One live status message per class, edited in place instead of a stream of service messages."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mid = None
        self.reset()

    def reset(self):
        self.title, self.call, self.rec_start, self.next_sum, self.parts = None, None, None, None, 0
        self.last_text, self.last_push = None, 0.0

    def render(self):
        rec = self.rec_start is not None
        head = (self.call or "⚪️ Не в звонке") + (" · 🎙 пишу" if rec else " · запись не идёт")
        lines = [head]
        if self.title:
            lines.append(f"📘 {self.title}")
        if rec:
            quiet = int((time.time() - LAST_VOICE[0]) / 60)
            lines.append(f"⏱ {_mins(time.time() - self.rec_start)} · "
                         + ("слышу речь" if quiet < 1 else f"речь {quiet} мин назад"))
            nxt = time.strftime("%H:%M", time.localtime(self.next_sum)) if self.next_sum else "—"
            lines.append(f"📝 конспектов: {self.parts} · следующий в {nxt}")
        lines.append(f"обновлено {time.strftime('%H:%M')}")
        return "\n".join(lines)

    def push(self, force=False):
        """Creates or edits the status message (at most once a minute unless forced)."""
        with self.lock:
            if not force and time.time() - self.last_push < 55:
                return
            text = self.render()
            self.last_push = time.time()
            if self.mid and tg_edit(self.mid, text, BUTTONS):
                self.last_text = text
                return
            ids = tg_send(text, fmt=False, silent=True, markup=BUTTONS)
            self.mid = ids[0] if ids else None
            self.last_text = text

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.push(force=True)

    def repost(self):
        """Fresh copy at the bottom of the chat (the old one is deleted)."""
        with self.lock:
            old, self.mid = self.mid, None
        if old:
            tg_delete([old])
        self.push(force=True)

    def finish(self, summary_count):
        """Turns the status into a short final line and forgets it."""
        with self.lock:
            dur = _mins(time.time() - self.rec_start) if self.rec_start else ""
            text = f"⏹ Запись закончена: {self.title or 'запись'}" + (f" · {dur}" if dur else "") \
                + f" · конспектов: {summary_count}"
            mid = self.mid
            self.mid = None
            self.reset()
        if not (mid and tg_edit(mid, text)):
            tg_send(text, fmt=False, silent=True)


STATUS = Status()


# ───────────────────────────── Whisper ─────────────────────────────
def _add_cuda_dlls():
    for pkg in ("cublas", "cudnn"):
        p = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", pkg, "bin")
        if os.path.isdir(p):
            os.add_dll_directory(p)
            os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]


def load_whisper():
    _add_cuda_dlls()
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
        print("Whisper: GPU")
        return m
    except Exception as e:
        print(f"Whisper GPU failed ({e}); falling back to CPU")
        small = "small.en" if (LANG == "en" and "turbo" in WHISPER_MODEL) else \
            ("small" if "turbo" in WHISPER_MODEL else WHISPER_MODEL)
        return WhisperModel(small, device="cpu", compute_type="int8")


def transcribe(model, audio, prompt=None):
    segs, _ = model.transcribe(audio, language=LANG, vad_filter=True, beam_size=5,
                               initial_prompt=prompt or None,
                               condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segs).strip()


# ───────────────────────────── Запись звука и экрана ─────────────────────────────
class Recorder:
    """Пишет звук системы и слайды (когда экран заметно меняется)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.audio = []
        self.slides = []
        self.last_img = None
        self.stop = threading.Event()

    def audio_len(self):
        with self.lock:
            return sum(len(a) for a in self.audio)

    def take_audio(self):
        import numpy as np
        with self.lock:
            a = np.concatenate(self.audio) if self.audio else np.zeros(0, np.float32)
            self.audio = []
        return a

    def take_slides(self):
        with self.lock:
            s, self.slides = self.slides, []
        return s

    def _put_audio(self, a):
        with self.lock:
            self.audio.append(a)

    def audio_loop(self):
        if WEB:
            return tw.audio_reader(self.stop, self._put_audio)
        import numpy as np
        import soundcard as sc
        spk = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
        print("Recording from:", spk.name)
        with spk.recorder(samplerate=SR, channels=1) as rec:
            while not self.stop.is_set():
                data = rec.record(numframes=SR)  # 1 с
                with self.lock:
                    self.audio.append(data[:, 0].astype(np.float32))

    def screen_loop(self):
        if WEB:
            return self._browser_screen_loop()
        import mss
        from PIL import Image
        with mss.MSS() as sct:
            mon = sct.monitors[MONITOR]
            while not self.stop.is_set():
                shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.rgb)
                img.thumbnail((1280, 1280))
                if self._changed(img):
                    with self.lock:
                        self.slides.append(img)
                    self.last_img = img
                self.stop.wait(SHOT_EVERY)

    def _browser_screen_loop(self):
        """Сервер: слайды — снимки страницы звонка в браузере."""
        while not self.stop.is_set():
            try:
                img = tw.BROWSER.screenshot()
            except Exception as e:
                print("screenshot:", e)
                img = None
            if img is not None:
                img.thumbnail((1280, 1280))
                if self._changed(img):
                    with self.lock:
                        self.slides.append(img)
                    self.last_img = img
            self.stop.wait(SHOT_EVERY)

    def _changed(self, img):
        import numpy as np
        from PIL import ImageChops
        if self.last_img is None:
            return True
        a = img.convert("L").resize((160, 90))
        b = self.last_img.convert("L").resize((160, 90))
        diff = np.asarray(ImageChops.difference(a, b), dtype=np.float32)
        return diff.mean() > 3.0


def pick_slides(slides):
    if len(slides) <= MAX_SLIDES:
        return slides
    import numpy as np
    idx = np.linspace(0, len(slides) - 1, MAX_SLIDES).astype(int)
    return [slides[i] for i in idx]


# ───────────────────────────── Конспект (LLM) ─────────────────────────────
SYSTEM = (
    "Ты помощник студента. Тебе дают транскрипт фрагмента лекции "
    "(автоматическое распознавание, возможны ошибки в терминах) и скриншоты слайдов. "
    "Составь конспект на русском. Английские термины оставляй в скобках при первом упоминании. "
    "Исправляй очевидные ошибки распознавания по слайдам. Ничего не выдумывай. Без вступлений и заключений, "
    "без таблиц. Пункты через «•», важные слова жирным через **. "
    "Формулы пиши без LaTeX и без знаков $: обычным текстом с Unicode-символами (x², xₙ, √x, a/b, ≤, ≥, ≠, ·, α, π, ∑, ∫).\n\n"
    "Строго такой формат, заголовки разделов — отдельной строкой ровно как здесь. "
    "Раздел, для которого в лекции ничего нет, пропусти целиком (кроме «КОРОТКО» и «🎯 Тема»):\n"
    "КОРОТКО: 2–3 предложения — суть этого куска лекции.\n"
    "🎯 Тема: одна строка.\n"
    "💡 Главные идеи\n• …\n"
    "📖 Определения и формулы\n• …\n"
    "🧩 Код\nкороткое пояснение и код в блоках ```c … ``` (язык — тот, что на лекции); код не пиши внутри пунктов\n"
    "🔍 Примеры\n• …\n"
    "⚠️ Важно для экзамена\n• что преподаватель подчеркнул, частые ошибки\n"
    "📌 ДЗ и дедлайны\n• только если реально упоминались: что сделать и к какому сроку\n"
    "❓ Неясно\n• места, где распознавание непонятно или стоит уточнить у преподавателя\n"
    "СЛАЙДЫ: номера самых полезных слайдов (до 4) с разделом, к которому они относятся, "
    "например «2 — Главные идеи; 5 — Код». Если слайдов нет или они бесполезны — «СЛАЙДЫ: нет»."
)
FINAL_SYSTEM = (
    SYSTEM.split("\nСЛАЙДЫ:", 1)[0].replace("КОРОТКО: 2–3 предложения — суть этого куска лекции.",
                                           "КОРОТКО: 3–4 предложения — суть всей пары.")
    + "Раздел «СЛАЙДЫ» не пиши.\n\nВАЖНО: сейчас тебе дают не транскрипт, а конспекты нескольких частей "
    "одной пары. Объедини их в один итоговый конспект всей пары: убери повторы, сохрани всё важное, код и ДЗ."
)

SECTION_ICONS = ("🎯", "💡", "📖", "🧩", "🔍", "⚠", "📌", "❓")
_NO_HW = re.compile(r"^\W*(нет|не упоминал|не было|отсутству)", re.I)
_KINDS = {"лекция": "лекция", "практика": "практика", "лекция и практика": "лекция и практика"}
_MONTHS = "янв фев мар апр мая июн июл авг сен окт ноя дек".split()


def subject_parts(title):
    """'Principles of Computer Programming (практика)' -> ('PCP', 'практика', 'Principles of …')."""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", title or "")
    name, kind = (m[1], m[2]) if m else ((title or "").strip(), "")
    words = [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", name) if w.lower() not in ("of", "and", "the", "и")]
    short = "".join(w[0].upper() for w in words) if len(words) >= 3 else name
    if name.lower() in ("запись", ""):
        short = ""
    return short, kind, name


def hashtags(title, when=None):
    short, kind, _ = subject_parts(title)
    when = when or datetime.now()
    tags = []
    if short:
        tags.append("#" + re.sub(r"\W+", "", short))
    if kind:
        tags.append("#" + re.sub(r"\W+", "_", kind).strip("_"))
    tags.append(f"#{_MONTHS[when.month - 1]}{when.day:02d}")
    return " ".join(tags)


def parse_summary(raw):
    """Splits the model answer into {'short', 'topic', 'sections': [(head, body)], 'slides': [(n, head)]}."""
    out = {"short": "", "topic": "", "sections": [], "slides": []}
    cur = None
    for line in raw.strip().splitlines():
        bare = line.strip().strip("*#").strip()
        low = bare.lower()
        if low.startswith("коротко"):
            out["short"] = bare.split(":", 1)[1].strip() if ":" in bare else ""
            cur = "short"
            continue
        if low.startswith("слайды"):
            for n, head in re.findall(r"(\d+)\s*[—–-]\s*([^;,\n]+)", bare):
                out["slides"].append((int(n), head.strip()))
            cur = None
            continue
        if bare.startswith("🎯"):
            out["topic"] = re.sub(r"^🎯\s*(Тема\s*:?)?\s*", "", bare)
            cur = "topic"
            continue
        if bare.startswith(SECTION_ICONS) and len(bare) < 60:
            out["sections"].append([bare, []])
            cur = "sec"
            continue
        if cur == "sec":
            out["sections"][-1][1].append(line)
        elif cur == "short" and line.strip():
            out["short"] = (out["short"] + " " + line.strip()).strip()
        elif cur == "topic" and line.strip():
            out["topic"] = (out["topic"] + " " + line.strip()).strip()
        elif cur is None and line.strip() and not out["sections"]:
            out["sections"].append(["", [line]])      # old-style answer without markers
            cur = "sec"
    out["sections"] = [(h, "\n".join(b).strip()) for h, b in out["sections"] if "\n".join(b).strip()]
    return out


def _vis_len(h):
    return len(html.unescape(re.sub(r"<[^>]+>", "", h)))


def _chunks(text, limit):
    """Splits markdown by lines into pieces of at most ~limit chars, never inside a ``` block."""
    out, cur, fence = [], [], None
    for line in text.splitlines():
        if sum(len(x) + 1 for x in cur) + len(line) > limit and cur:
            if fence is not None:
                cur.append("```")
            out.append("\n".join(cur))
            cur = [f"```{fence}"] if fence is not None else []
        cur.append(line)
        m = re.match(r"^\s*```([\w+-]*)", line)
        if m:
            fence = None if fence is not None else m[1]
    if cur:
        out.append("\n".join(cur))
    return out


def format_summary(raw, header, title, note="", when=None):
    """Builds Telegram HTML messages: header + short + topic visible, details folded, code visible."""
    p = parse_summary(raw)
    head = f"<b>{html.escape(header)}</b>" + (f"\n<i>{html.escape(note)}</i>" if note else "")
    top = [head]
    if p["short"]:
        top.append("<b>Коротко:</b> " + _to_tg_html(p["short"]).strip())
    if p["topic"]:
        top.append("🎯 <b>Тема:</b> " + _to_tg_html(p["topic"]).strip())
    folded, visible = [], []
    for h, body in p["sections"]:
        if h.startswith("🧩"):
            visible.append((h, body))
        else:
            folded.append((h, body))
    blocks = ["\n\n".join(top)]
    fold_md = "\n\n".join((f"**{h}**\n" if h else "") + b for h, b in folded)
    for piece in _chunks(fold_md, 3000) if fold_md else []:
        inner = _to_tg_html(piece).strip()
        inner = re.sub(r"<pre><code[^>]*>(.*?)</code></pre>", r"<code>\1</code>", inner, flags=re.S)  # no <pre> in quotes
        blocks.append(f"<blockquote expandable>{inner}</blockquote>")
    for h, body in visible:
        for i, piece in enumerate(_chunks(body, 3300)):
            blocks.append(((f"<b>{html.escape(h)}</b>\n") if i == 0 else "") + _to_tg_html(piece).strip())
    blocks.append(html.escape(hashtags(title, when)))
    msgs, cur = [], ""
    for b in blocks:                                   # pack blocks into messages under the 4096 limit
        if cur and _vis_len(cur) + _vis_len(b) + 2 > 3800:
            msgs.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs, p


def homework(p):
    for h, body in p["sections"]:
        if h.startswith("📌") and body and not _NO_HW.match(body):
            return body
    return ""


def _jpeg_b64(img):
    img = img.copy()
    img.thumbnail((1024, 1024))                     # слайды поменьше — запрос уходит быстрее
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def _llm_claude(client, transcript, slides):
    content = []
    for n, i in enumerate(slides, 1):
        content.append({"type": "text", "text": f"Слайд {n}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": _jpeg_b64(i)}})
    content.append({"type": "text", "text": f"Транскрипт:\n\n{transcript}"})
    resp = client.messages.create(model=CLAUDE_MODEL, max_tokens=4000, system=SYSTEM,
                                  messages=[{"role": "user", "content": content}])
    return resp.content[0].text


class LLMBusy(RuntimeError):
    """Все модели упёрлись в лимит или недоступны."""


_MODEL_PAUSED = {}                                          # модель -> до какого времени её не трогать
_RETRY_RE = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", re.I)


def _retry_after(r):
    """Сколько секунд Gemini просит подождать после 429."""
    try:
        m = _RETRY_RE.search(r.json()["error"]["message"])
        if m:
            return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))
    except Exception:
        pass
    return 60.0


def gemini_generate(body, models, timeout=150, patient=True):
    """Пробует модели по очереди. Модель, упёршуюся в лимит, откладывает до сброса лимита.
    patient=False (вопросы в чате): без долгих ожиданий, ответ или LLMBusy за секунды."""
    last = "нет доступных моделей"
    for model in models:
        if _MODEL_PAUSED.get(model, 0) > time.time():
            continue
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3 if patient else 2):
            try:
                r = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]}, json=body,
                                  timeout=timeout if patient else min(timeout, 90))
            except requests.exceptions.RequestException as e:
                last = f"{model}: {type(e).__name__}"
                time.sleep((5 if patient else 2) * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = _retry_after(r)
                last = f"{model}: лимит запросов"
                if wait > (60 if patient else 10):              # дневной/долгий лимит — к следующей модели
                    _MODEL_PAUSED[model] = time.time() + wait
                    print(f"Gemini {model}: лимит, жду {wait / 60:.0f} мин, беру следующую модель")
                    break
                time.sleep(wait + 1)
                continue
            if r.status_code in (500, 503):
                last = f"{model}: HTTP {r.status_code}"
                time.sleep((10 if patient else 2) * (attempt + 1))
                continue
            if r.status_code == 404:                            # модель убрали — больше не пробуем
                _MODEL_PAUSED[model] = time.time() + 86400
                last = f"{model}: нет такой модели"
                break
            if not r.ok:                                        # без ключа в тексте ошибки
                raise RuntimeError(f"Gemini {model} {r.status_code}: {r.text[:400]}")
            try:
                return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError):
                last = f"{model}: пустой ответ"
                break
    if OMNIROUTE_URL:                                       # все модели Gemini заняты — через локальный шлюз
        try:
            # шлюз сам перебирает модели, это долго: конспекту даём запас, чату — нет
            return omniroute_generate(body, timeout=max(timeout, 300) if patient else min(timeout, 90))
        except Exception as e:
            last = f"{last}; OmniRoute: {type(e).__name__} {str(e)[:120]}"
    raise LLMBusy(f"Gemini unavailable: {last}")


OMNIROUTE_MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "omniroute-model.txt")


def omniroute_model():
    """На сервере модель выбирают в веб-хабе: он пишет её в omniroute-model.txt (пусто — auto).
    На ПК хаб передаёт её через OMNIROUTE_MODEL при запуске."""
    try:
        with open(OMNIROUTE_MODEL_FILE, encoding="utf-8") as f:
            return f.read().strip() or "auto"
    except OSError:
        return OMNIROUTE_MODEL


def omniroute_generate(body, timeout=150):
    """Тот же запрос через OmniRoute (OpenAI-формат). Только текст: слайды запасным моделям не шлём."""
    messages = []
    system = "".join(p.get("text", "") for p in body.get("system_instruction", {}).get("parts", []))
    if system:
        messages.append({"role": "system", "content": system})
    for c in body.get("contents", []):
        text = "".join(p.get("text", "") for p in c.get("parts", []))
        if text:
            messages.append({"role": "assistant" if c.get("role") == "model" else "user", "content": text})
    headers = {"Authorization": f"Bearer {OMNIROUTE_KEY}"} if OMNIROUTE_KEY else {}
    r = requests.post(f"{OMNIROUTE_URL}/chat/completions", headers=headers, timeout=timeout,
                      json={"model": omniroute_model(), "messages": messages})
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}")
    text = r.json()["choices"][0]["message"].get("content") or ""
    if not text.strip():
        raise RuntimeError("пустой ответ")
    print("Gemini занят, ответ получен через OmniRoute:", r.json().get("model", "?"))
    return text


def _llm_gemini(transcript, slides, timeout=150, tries=3):
    parts = []
    for n, i in enumerate(slides, 1):
        parts.append({"text": f"Слайд {n}"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": _jpeg_b64(i)}})
    parts.append({"text": f"Транскрипт:\n\n{transcript}"})
    body = {"system_instruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": parts}]}
    return gemini_generate(body, SUMMARY_MODELS, timeout=timeout)


def summarize(client, transcript, slides, label):
    """Returns (raw model answer, slides the model actually saw). Answer format: see SYSTEM."""
    slides = pick_slides(slides)
    if PROVIDER == "anthropic":
        text = _llm_claude(client, transcript, slides)
    else:
        try:
            text = _llm_gemini(transcript, slides)
        except RuntimeError as e:
            if "unavailable" not in str(e) or not slides:
                raise
            print("Конспект со слайдами не вышел, пробую без слайдов:", e)
            tg_send(f"⏳ Конспект {label} задерживается: Gemini перегружен, пробую ещё раз.", fmt=False)
            return _llm_gemini(transcript, [], timeout=120, tries=2), []
    return text, slides


def llm_text(system, messages, timeout=180, chat=True):
    """Текстовый запрос к модели. messages — [(role, text)], role: user / assistant.
    chat=True — вопросы из чата: лёгкие модели и быстрый отказ, чтобы не тратить лимит конспектов."""
    if PROVIDER == "anthropic":
        resp = CLIENT[0].messages.create(model=CLAUDE_MODEL, max_tokens=6000, system=system,
                                         messages=[{"role": r, "content": t} for r, t in messages])
        return resp.content[0].text
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if r == "assistant" else "user", "parts": [{"text": t}]}
                         for r, t in messages]}
    return gemini_generate(body, CHAT_MODELS if chat else SUMMARY_MODELS, timeout=timeout, patient=not chat)


# ───────────────────────────── Архив лекций (папка lectures/) ─────────────────────────────
def _safe_name(s):
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', " ", s).strip()[:80] or "lecture"


def archive_open(title, start=None):
    """Создаёт папку лекции: lectures/2026-10-12_1300 Название/ и возвращает путь."""
    start = start or datetime.now().astimezone()
    path = os.path.join(DATA_DIR, f"{start.strftime('%Y-%m-%d_%H%M')} {_safe_name(title)}")
    os.makedirs(path, exist_ok=True)
    meta = os.path.join(path, "meta.json")
    if not os.path.exists(meta):
        with open(meta, "w", encoding="utf-8") as f:
            json.dump({"title": title, "start": start.isoformat(timespec="minutes")}, f, ensure_ascii=False)
    return path


def archive_append(path, text, when=None):
    with open(os.path.join(path, "transcript.txt"), "a", encoding="utf-8") as f:
        f.write(f"[{(when or time.strftime('%H:%M'))}] {text}\n")


def archive_summary(path, label, text):
    with open(os.path.join(path, "summaries.md"), "a", encoding="utf-8") as f:
        f.write(f"## {label}\n\n{text.strip()}\n\n")


def load_lectures():
    """Все сохранённые лекции, от старых к новым: [{title, start, summaries, transcript}]."""
    out = []
    if not os.path.isdir(DATA_DIR):
        return out
    for name in sorted(os.listdir(DATA_DIR)):
        p = os.path.join(DATA_DIR, name)
        if not os.path.isdir(p):
            continue

        def read(fn):
            fp = os.path.join(p, fn)
            return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
        try:
            meta = json.loads(read("meta.json") or "{}")
        except ValueError:
            meta = {}
        out.append({"title": meta.get("title", name), "start": meta.get("start", name[:15]),
                    "summaries": read("summaries.md"), "transcript": read("transcript.txt")})
    return out


def _words(s):
    return {w[:6] for w in re.findall(r"\w{3,}", s.lower())}


def build_context(query, budget=None):
    """Собирает текст лекций для модели: все конспекты, а расшифровки — самые подходящие к запросу куски."""
    budget = budget or CONTEXT_CHARS
    lects = load_lectures()
    if not lects:
        return ""
    head = [f"### {l['start']} · {l['title']}\n{l['summaries'].strip() or '(конспектов нет)'}" for l in lects]
    summaries = "\n\n".join(head)
    if len(summaries) > budget * 0.6:             # слишком много — оставляем самые свежие
        summaries = summaries[-int(budget * 0.6):]
    room = budget - len(summaries)
    q = _words(query)
    if sum(len(l["transcript"]) for l in lects) > room:       # всё не влезет — ищем по ключевым словам
        try:                                                    # лекции на английском, вопрос по-русски
            q |= _words(llm_text("Выпиши через пробел 10-20 ключевых слов для поиска по теме запроса "
                                 "на английском и на русском, включая термины и синонимы. Только слова.",
                                 [("user", query)], timeout=60))
        except Exception as e:
            print("ключевые слова:", e)
    chunks = []
    for l in lects:
        lines, cur = l["transcript"].splitlines(), ""
        for ln in lines + [None]:
            if ln is None or len(cur) > 3000:
                if cur.strip():
                    score = len(q & _words(cur))
                    chunks.append((score, l["start"], l["title"], cur))
                cur = ""
            if ln is not None:
                cur += ln + "\n"
    chunks.sort(key=lambda c: (c[0], c[1]), reverse=True)
    picked, used = [], 0
    for score, start, title, txt in chunks:
        if used + len(txt) > room:
            break
        picked.append((start, title, txt))
        used += len(txt)
    picked.sort()
    parts = [f"# Конспекты лекций\n\n{summaries}"]
    if picked:
        parts.append("# Фрагменты расшифровок (дословно, автораспознавание)\n\n" +
                     "\n\n".join(f"--- {s} · {t}\n{x}" for s, t, x in picked))
    return "\n\n".join(parts)


ASK_SYSTEM = (
    "Ты помощник студента в Telegram. У тебя есть архив его лекций: конспекты и фрагменты "
    "расшифровок (автоматическое распознавание, лекции на английском). Отвечай по-русски, кратко и по делу. "
    "Если вопрос про лекции — опирайся на архив и указывай, с какой лекции (дата, предмет) информация. "
    "Если в архиве этого нет — так и скажи, а потом можешь ответить из общих знаний, пометив это. "
    "Пиши простым текстом: жирный через **, пункты через «•», без таблиц."
    " Формулы пиши без LaTeX и без знаков $: обычным текстом с Unicode-символами (x², xₙ, √x, a/b, ≤, ≥, ≠, ·, α, π, ∑, ∫)."
)
TOPIC_SYSTEM = (
    "Ты помощник студента. По архиву его лекций (конспекты и расшифровки) составь подробную сводку "
    "на русском по запрошенной теме: всё, что об этом говорилось на всех лекциях, в логичном порядке, "
    "с определениями, формулами, примерами и заданиями. После каждого блока в скобках укажи дату и предмет "
    "лекции. Английские термины оставляй в скобках. Ничего не выдумывай: если по теме в архиве ничего нет, "
    "так и скажи (в «КОРОТКО»). Пункты через «•», важные слова жирным через **, без таблиц."
    " Формулы пиши без LaTeX и без знаков $: обычным текстом с Unicode-символами (x², xₙ, √x, a/b, ≤, ≥, ≠, ·, α, π, ∑, ∫).\n\n"
    + SYSTEM.split("\n\n", 1)[1].split("\nСЛАЙДЫ:", 1)[0].replace(
        "КОРОТКО: 2–3 предложения — суть этого куска лекции.", "КОРОТКО: 2–3 предложения — суть темы.")
    + "Раздел «СЛАЙДЫ» не пиши."
)
HISTORY = []                                                # последние вопросы/ответы для уточнений


def ask(question):
    ctx = build_context(question)
    intro = f"Архив лекций:\n\n{ctx}" if ctx else "Архив лекций пока пуст."
    msgs = [("user", intro), ("assistant", "Архив прочитал. Жду вопрос.")] + HISTORY[-8:] + [("user", question)]
    answer = llm_text(ASK_SYSTEM, msgs).strip()
    HISTORY.extend([("user", question), ("assistant", answer)])
    del HISTORY[:-12]
    return answer


def topic_summary(topic):
    ctx = build_context(topic)
    if not ctx:
        return "Архив лекций пока пуст: сводку делать не из чего."
    text = llm_text(TOPIC_SYSTEM, [("user", f"Архив лекций:\n\n{ctx}\n\nТема: {topic}")]).strip()
    msgs, _ = format_summary(text, f"📚 Сводка по теме «{topic}»", "", "по всем сохранённым лекциям")
    return msgs                                       # ready HTML messages, same layout as summaries


# ───────────────────────────── Календарь (ICS) ─────────────────────────────
TEAMS_RE = re.compile(r"https://teams\.(?:microsoft\.com/l/meetup-join|live\.com/meet)/[^\s<>\"'\)\]]+")
SAFELINK_RE = re.compile(r"https://[\w.\-]*safelinks\.protection\.outlook\.com/\?[^\s<>\"']+")


def find_teams_link(text):
    """Ищет ссылку на встречу Teams (в том числе внутри Outlook Safe Links)."""
    if not text:
        return None
    text = str(text)

    def unwrap(m):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(m.group(0)).query)
        return q.get("url", [m.group(0)])[0]

    text = SAFELINK_RE.sub(unwrap, text)
    m = TEAMS_RE.search(text)
    return m.group(0).rstrip(".,;") if m else None


def fetch_events(now=None, ics_text=None):
    """Возвращает события Teams на ближайшие ~сутки: [{key,title,start,end,link}]."""
    import icalendar
    import recurring_ical_events
    if ics_text is None:
        r = requests.get(ICS_URL, timeout=30)
        r.raise_for_status()
        ics_text = r.content
    cal = icalendar.Calendar.from_ical(ics_text)
    now = now or datetime.now(timezone.utc)
    out = []
    for ev in recurring_ical_events.of(cal).between(now - timedelta(hours=8), now + timedelta(hours=24)):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start, end = ev.decoded("DTSTART"), ev.decoded("DTEND") if ev.get("DTEND") else None
        if not isinstance(start, datetime):           # событие на весь день
            continue
        if start.tzinfo is None:
            start = start.astimezone()
        if end is None:
            end = start + timedelta(hours=1)
        elif end.tzinfo is None:
            end = end.astimezone()
        if end - start > timedelta(hours=6):
            continue
        link = None
        for field in ("X-MICROSOFT-SKYPETEAMSMEETINGURL", "LOCATION", "DESCRIPTION", "URL",
                      "X-ALT-DESC", "SUMMARY"):
            link = find_teams_link(ev.get(field))
            if link:
                break
        if not link:
            continue
        out.append({"key": f"{ev.get('UID', '')}@{start.isoformat()}",
                    "title": str(ev.get("SUMMARY", "Лекция")), "start": start, "end": end, "link": link})
    out.sort(key=lambda e: e["start"])
    return out


def due_event(events, handled, now, lead_min=None):
    """Событие, к которому пора подключаться (до начала меньше lead, конец ещё не прошёл)."""
    lead = timedelta(minutes=JOIN_LEAD_MIN if lead_min is None else lead_min)
    for e in events:
        if e["key"] not in handled and e["start"] - lead <= now < e["end"]:
            return e
    return None


# ───────────────────────────── Вход в Teams ─────────────────────────────
def is_meeting_link(link):
    """Прямая ссылка на встречу (а не на канал): teams.microsoft.com/meet/… или /l/meetup-join/…"""
    return bool(link and re.search(r"teams\.(microsoft\.com|live\.com)/(meet/|l/meetup-join)", link))


def open_teams_link(link):
    url = link
    if re.search(r"teams\.microsoft\.com/(l/(meetup-join|channel|team|chat)|meet/)", link):
        url = link.replace("https://", "msteams://", 1)    # сразу в приложение Teams
    try:
        os.startfile(url)                                  # Windows
    except Exception as e:
        print("msteams:// не открылся, пробую браузер:", e)
        os.startfile(link)


JOIN_BTN = re.compile(r"^(join now|join|join a schedule[d]? meeting|присоединиться( сейчас)?|"
                      r"подключиться( сейчас)?|participer maintenant|jetzt teilnehmen|unirse ahora)$", re.I)
JOIN_NOW = re.compile(r"^(join now|присоединиться сейчас|подключиться сейчас|"
                      r"participer maintenant|jetzt teilnehmen|unirse ahora)$", re.I)


def _click(btn):
    try:
        btn.invoke()
    except Exception:
        btn.click_input()


def find_button(pattern, title_has=None):
    """Ищет кнопку по имени в окнах Teams. title_has — часть названия окна (например, предмет)."""
    from pywinauto import Desktop
    kw = title_has.lower() if title_has and title_has != "*" else None   # "*" — любое окно Teams
    for w in Desktop(backend="uia").windows():
        t = w.window_text() or ""
        if kw:
            if kw not in t.lower():
                continue
        elif "teams" not in t.lower() and " | " not in t:     # окна Teams называются «… | …»
            continue
        try:
            for b in w.descendants(control_type="Button"):
                if pattern.match((b.window_text() or "").strip()):
                    return b
        except Exception as e:
            print("поиск кнопки:", e)
    return None


MIC_CAM = re.compile(r"mic|микрофон|camera|камер|video|видео|^(un)?mute$", re.I)
TURN_OFF = re.compile(r"^(mute( mic(rophone)?)?|turn (the |my )?(mic(rophone)?|camera|video) off|"
                      r"turn off (the |my )?(mic(rophone)?|camera|video)|stop (my )?video|"
                      r"выключить (микрофон|камеру)|отключить (микрофон|камеру))$", re.I)
TURN_ON = re.compile(r"unmute|turn .*\bon\b|start (my )?video|включить|\boff$", re.I)   # уже выключено / кнопка «включить»


def mute_av():
    """Выключает микрофон и камеру в окнах Teams. Возвращает названия нажатых элементов."""
    from pywinauto import Desktop
    clicked, seen = [], []
    for w in Desktop(backend="uia").windows():
        t = w.window_text() or ""
        if "teams" not in t.lower() and " | " not in t:
            continue
        try:
            ctrls = list(w.descendants(control_type="Button")) + list(w.descendants(control_type="CheckBox"))
        except Exception as e:
            print("mute_av:", e)
            continue
        for c in ctrls:
            n = (c.window_text() or "").strip()
            if not n or not MIC_CAM.search(n):
                continue
            state = None
            try:
                state = c.get_toggle_state()
            except Exception:
                pass
            seen.append(f"{n[:50]!r} state={state}")
            if TURN_ON.search(n) and not TURN_OFF.match(n):
                continue
            if TURN_OFF.match(n) or state == 1:
                try:
                    _click(c)
                    clicked.append(n[:50])
                except Exception as e:
                    print("mute_av click:", e)
    print("Микрофон/камера, найденные элементы:", seen)
    return clicked


def click_join_now(btn):
    """Выключает микрофон и камеру, жмёт «Join now», потом ещё раз проверяет, что всё выключено."""
    off = []
    try:
        off += mute_av()
    except Exception as e:
        print("mute_av:", e)
    _click(btn)
    time.sleep(6)
    try:
        off += mute_av()
    except Exception as e:
        print("mute_av:", e)
    return off


def auto_join(ev, stop):
    """Открывает канал/встречу и, пока идёт время лекции, ищет и нажимает «Join»."""
    STATUS.update(call=f"🟡 Захожу в звонок (старт {ev['start'].astimezone().strftime('%H:%M')})",
                  title=STATUS.title or ev["title"])
    if WEB:
        return web_auto_join(ev, stop)
    if ev.get("link"):
        try:
            open_teams_link(ev["link"])
        except Exception as e:
            tg_send(f"⚠️ Не смог открыть ссылку: {e}", fmt=False)
    if not AUTO_CLICK:
        tg_send("👉 Нажми «Join» в Teams сам, запись уже идёт.", fmt=False)
        return
    try:
        import pywinauto  # noqa: F401
    except Exception as e:
        tg_send(f"👉 Автоклик недоступен ({e}). Нажми «Join» сам, запись уже идёт.", fmt=False)
        return
    t0, warned, fails = time.time(), False, 0
    direct = is_meeting_link(ev.get("link"))
    title_has = "*" if direct else ev.get("kw")                # окно встречи не называется по предмету
    while not stop.is_set() and datetime.now(timezone.utc) < ev["end"]:
        try:
            leave = find_button(LEAVE_BTN)
            if leave and direct and CURRENT.get("joined") not in (None, ev["link"]):
                _click(leave)                                  # сидим в звонке прошлой пары — выходим
                tg_send("🚪 Вышел из прошлого звонка, захожу в новый.", fmt=False)
                time.sleep(5)
                open_teams_link(ev["link"])
                stop.wait(10)
                continue
            if leave:                                          # уже в нужном звонке
                tg_send("✅ Уже нахожусь в звонке — продолжаю запись.", fmt=False)
                return _rejoin_watch(ev, stop)
            btn = find_button(JOIN_NOW if direct else JOIN_BTN, title_has)   # по прямой ссылке — только экран «Join now» этой встречи
            if btn and JOIN_NOW.match((btn.window_text() or "").strip()):
                off = click_join_now(btn)                      # сразу экран «Join now»: сначала глушим микрофон
                CURRENT["joined"] = ev.get("link")
                tg_send("✅ Зашёл на лекцию. " + ("🔇 Выключил: " + ", ".join(off) if off else
                        "⚠️ Не нашёл, как выключить микрофон/камеру — проверь сам."), fmt=False)
                return _rejoin_watch(ev, stop)
            if btn:
                _click(btn)
                ok, t_end = False, time.time() + 40            # экран перед входом: «Join now»
                while time.time() < t_end:
                    b2 = find_button(JOIN_NOW)
                    if b2:
                        off = click_join_now(b2)
                        CURRENT["joined"] = ev.get("link")
                        ok = True
                        break
                    time.sleep(2)
                if ok:
                    tg_send("✅ Зашёл на лекцию. " + ("🔇 Выключил: " + ", ".join(off) if off else
                            "⚠️ Не нашёл, как выключить микрофон/камеру — проверь сам."), fmt=False)
                    return _rejoin_watch(ev, stop)
                fails += 1                                     # встреча, видимо, ещё не началась — не сдаёмся
                if fails == 1:
                    tg_send("⏳ Кнопка «Join» есть, но войти пока нельзя: преподаватель, видимо, ещё "
                            "не начал. Пробую каждую минуту до конца пары.", fmt=False)
                if ev.get("link") and fails % 5 == 0:
                    try:
                        open_teams_link(ev["link"])            # вернуться в канал, если клик увёл на другую страницу
                    except Exception as e:
                        print("open_teams_link:", e)
                stop.wait(JOIN_RETRY_SEC)
        except Exception as e:
            print("auto_join:", e)
        if not warned and fails == 0 and time.time() - t0 > 600:
            warned = True
            tg_send("⏳ Кнопки «Join» пока нет — преподаватель, видимо, ещё не начал. Жду дальше.", fmt=False)
        stop.wait(15)
    if not stop.is_set():
        tg_send(f"⌛ Время пары «{ev['title']}» вышло, а зайти не удалось.", fmt=False)


AUTOLOGIN = {"last": 0.0, "blocked": False}
AUTOLOGIN_RETRY_SEC = float(os.getenv("AUTOLOGIN_RETRY_SEC", "900"))
AUTOLOGIN_WHY = {
    "no_password": "Сам войти не могу: в .env на сервере нет пароля (deploy/set_teams_password.sh).",
    "no_email": "Сам войти не могу: в .env на сервере нет TEAMS_EMAIL (deploy/set_teams_password.sh).",
    "mfa": "Пароль я ввёл, но Microsoft просит код или подтверждение в телефоне — это может только ты.",
    "bad_password": "Microsoft не принял пароль из .env. Больше пробовать не буду, чтобы не заблокировать "
                    "аккаунт; поправь пароль (deploy/set_teams_password.sh) и перезапусти бота.",
    "failed": "Сам войти не получилось (страница входа выглядела непривычно).",
}


def try_auto_login():
    """Enters the password from .env when Microsoft asks; at most once per AUTOLOGIN_RETRY_SEC,
    and never again after a rejected password (to avoid locking the account)."""
    if AUTOLOGIN["blocked"]:
        return "bad_password"
    recent = time.time() - AUTOLOGIN["last"] < AUTOLOGIN_RETRY_SEC
    if recent and not (AUTOLOGIN.get("res") == "ok" and AUTOLOGIN.get("streak", 0) < 2):
        return "skip"                                  # pause after a failure, or after 2 quick re-asks
    AUTOLOGIN["streak"] = AUTOLOGIN.get("streak", 0) + 1 if recent else 1
    AUTOLOGIN["last"] = time.time()
    STATUS.update(call="🔑 Вход в Teams истёк, вхожу заново…")
    res = tw.BROWSER.auto_login()
    print("auto login:", res)
    AUTOLOGIN["res"] = res
    if res == "bad_password":
        AUTOLOGIN["blocked"] = True
    return res


def web_auto_join(ev, stop):
    """Сервер: заходит в звонок через Teams в браузере, ждёт в лобби, перезаходит после обрыва."""
    link = ev.get("link")
    if not link or not is_meeting_link(link):
        tg_send("⚠️ На сервере захожу только по прямой ссылке на встречу (teams.microsoft.com/meet/… или "
                "/l/meetup-join/…). Для этой пары её нет в schedule.txt — запись звука идёт, но звонка не будет.",
                fmt=False)
        return
    try:
        tw.BROWSER.open_meeting(link)
    except Exception as e:
        tg_send(f"⚠️ Браузер не открыл встречу: {type(e).__name__}: {str(e)[:200]}", fmt=False)
    t0 = time.time()
    joined, ever, lobby_told, waited_told, signin_told = False, False, False, False, False
    last_state, reopens = None, 0
    last_open, last_rejoin = time.time(), 0.0
    while not stop.is_set() and datetime.now(timezone.utc) < ev["end"]:
        if CURRENT.get("manual_leave"):                # вышли сами (/leave): не перезаходим до /join
            if joined:
                joined = False
                CURRENT["joined"] = None
            stop.wait(10)
            continue
        try:
            state, off = tw.BROWSER.step()
        except Exception as e:
            print("web join:", type(e).__name__, str(e)[:200])
            state, off = "error", []
        if state != last_state:                        # one log line per change: shows where it is stuck
            print("web join state:", state)
            last_state = state
        if state == "in_call":
            if not joined:
                joined, lobby_told = True, False
                CURRENT["joined"] = link
                STATUS.update(call="🟢 В звонке")         # no mic, no camera: nobody hears or sees the bot
                if ever:
                    stop.wait(20)
                    continue
                ever = True
        elif state == "signin":
            res = try_auto_login()
            if res == "ok":
                signin_told = False
                STATUS.update(call="🟡 Заново вошёл в Teams, захожу в звонок")
                stop.wait(20)                          # let Teams store its new tokens before saving them
                try:                                   # after a re-login the old Teams page stays on its
                    tw.BROWSER.save_state()            # home screen and ignores meeting links: start the
                    tw.BROWSER.close()                 # browser afresh with the new login instead
                    tw.BROWSER.open_meeting(link)
                    last_open = time.time()
                except Exception as e:
                    print("after login:", type(e).__name__)
                stop.wait(10)
                continue
            if not signin_told:                        # waiting won't help: the saved login expired
                signin_told = True
                STATUS.update(call="🔑 Teams просит войти заново")
                tg_send("🔑 Teams на сервере просит войти заново, сохранённый вход истёк. "
                        + AUTOLOGIN_WHY.get(res, "") + "\nЧто сделать: запусти на ПК teams_login.py "
                        "и скопируй teams-state.json на сервер (см. README), потом перезапусти бота.", fmt=False)
        elif state == "lobby":
            if not lobby_told:
                lobby_told = True
                STATUS.update(call="🚪 В лобби, жду, пока впустят")
        elif joined:                                   # были в звонке, а теперь нет
            quiet = (time.time() - LAST_VOICE[0]) / 60
            if REJOIN and quiet >= REJOIN_AFTER_MIN and time.time() - last_rejoin > 180:
                last_rejoin = time.time()
                joined = False
                CURRENT["joined"] = None
                print("Звонок пропал, перезахожу")
                try:
                    tw.BROWSER.open_meeting(link)
                    STATUS.update(call="🟡 Звонок оборвался, захожу снова")
                except Exception as e:
                    print("rejoin:", e)
        else:
            if state in ("unknown", "ended", "error") and time.time() - last_open > 300:
                last_open = time.time()                # страница застряла — открываем ссылку заново
                reopens += 1
                try:
                    if reopens % 3 == 0:               # same page keeps failing: restart the browser
                        print("Браузер застрял, перезапускаю браузер")
                        tw.BROWSER.save_state()
                        tw.BROWSER.close()
                    tw.BROWSER.open_meeting(link)
                except Exception as e:
                    print("open_meeting:", e)
            if not waited_told and time.time() - t0 > 600:
                waited_told = True
                STATUS.update(call="⏳ Встреча, видимо, ещё не началась, пробую войти")
        stop.wait(20 if joined else 10)
    if CURRENT.get("jstop") is stop:                   # нас не сменила другая пара — выходим из звонка
        try:
            tw.BROWSER.leave(still_mine=lambda: CURRENT.get("jstop") is stop)
            tw.BROWSER.save_state()
        except Exception as e:
            print("leave:", e)
        CURRENT["joined"] = None
    if not ever and not stop.is_set():
        tg_send(f"⌛ Время пары «{ev['title']}» вышло, а зайти не удалось.", fmt=False)


def leave_call():
    """Нажимает «Leave». True — если был в звонке."""
    if WEB:
        return tw.BROWSER.leave()
    btn = find_button(LEAVE_BTN)
    if btn:
        _click(btn)
    return bool(btn)


def _rejoin_watch(ev, stop):
    """После входа: если долго тихо (перерыв кончился, а звонка нет) — пробуем зайти ещё раз."""
    if not REJOIN:
        return
    last_try = 0.0
    direct = is_meeting_link(ev.get("link"))
    while not stop.wait(20) and datetime.now(timezone.utc) < ev["end"]:
        quiet = (time.time() - LAST_VOICE[0]) / 60
        if CURRENT.get("manual_leave") or quiet < REJOIN_AFTER_MIN or time.time() - last_try < 180:
            continue
        try:
            if find_button(LEAVE_BTN):                     # всё ещё в звонке, просто тихо
                continue
            btn = find_button(JOIN_NOW, "*") if direct else find_button(JOIN_BTN, ev.get("kw"))
            if not btn:
                if direct:                                 # окно встречи закрылось — открываем ссылку снова
                    last_try = time.time()
                    open_teams_link(ev["link"])
                continue
            last_try = time.time()
            if JOIN_NOW.match((btn.window_text() or "").strip()):
                off = click_join_now(btn)
                tg_send("↩️ Перезашёл в звонок после перерыва." + (" 🔇" if off else ""), fmt=False)
                continue
            _click(btn)
            t_end = time.time() + 40
            while time.time() < t_end:
                b2 = find_button(JOIN_NOW)
                if b2:
                    off = click_join_now(b2)
                    tg_send("↩️ Перезашёл в звонок после перерыва." + (" 🔇" if off else ""), fmt=False)
                    break
                time.sleep(2)
        except Exception as e:
            print("rejoin:", e)


# ───────────────────────────── Расписание из файла schedule.txt ─────────────────────────────
DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
SCHEDULE_FILE = os.getenv("SCHEDULE_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.txt")
PERIOD_RE = re.compile(r"^\[\s*(\d{1,2}\.\d{1,2}\.\d{4})?\s*[-–]\s*(\d{1,2}\.\d{1,2}\.\d{4})?\s*\]$")
SCHED_RE = re.compile(r"^(\S+)\s+(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s+(.+)$")


def schedule_events(path=SCHEDULE_FILE, now=None, text=None):
    """Строки вида: пн 10:00-11:30 Computer organization | https://teams.../l/channel/... (ссылка необязательна)."""
    if text is None:
        if not os.path.exists(path):
            return []
        text = open(path, encoding="utf-8-sig").read()
    now = (now or datetime.now()).astimezone()
    out = []
    period = (None, None)               # текущий период [с-по], действует до следующей строки [..]
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.replace(" ", "") == "[]":
            period = (None, None)
            continue
        pm = PERIOD_RE.match(line)
        if pm:
            f = lambda v: datetime.strptime(v, "%d.%m.%Y").date() if v else None
            period = (f(pm.group(1)), f(pm.group(2)))
            continue
        if (period[0] and now.date() < period[0]) or (period[1] and now.date() > period[1]):
            continue
        m = SCHED_RE.match(line)
        if not m:
            print("Строка расписания не понята:", line)
            continue
        d = m.group(1).lower()
        wd = DAYS.get(d, DAYS.get(d[:2], DAYS.get(d[:3])))
        if wd is None or wd != now.weekday():
            continue
        h1, m1, h2, m2 = (int(m.group(i)) for i in (2, 3, 4, 5))
        start = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
        end = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
        left, _, link = m.group(6).partition("|")
        kw, _, label = left.partition("::")            # «название :: практика | ссылка»
        kw, label, link = kw.strip(), label.strip(), link.strip() or None
        out.append({"key": f"sched:{kw}:{start.isoformat()}", "title": f"{kw} ({label})" if label else kw,
                    "start": start, "end": end, "link": link, "kw": kw})
    out.sort(key=lambda e: e["start"])
    return out


# ───────────────────────────── Сессия (одна лекция) ─────────────────────────────
class Session:
    def __init__(self, title, end_time=None):
        self.title = title
        self.end_time = end_time
        self.parts = []                 # расшифровка текущего 45-минутного куска
        self.lock = threading.Lock()
        self.last_voice = time.time()
        self.part_no = 0
        self.chunk_start = time.time()
        self.tail = ""                  # хвост расшифровки — подсказка для Whisper
        self.msg_ids = []               # сообщения с расшифровкой, которые удалим после конспекта
        self.summaries = []             # (part_no, raw summary) — для итога всей пары
        self.archive = archive_open(title)

    def add(self, text):
        with self.lock:
            self.parts.append(text)
            self.tail = (self.tail + " " + text)[-300:]
            self.last_voice = time.time()
            LAST_VOICE[0] = self.last_voice
        try:
            archive_append(self.archive, text)
        except OSError as e:
            print("Не смог сохранить расшифровку:", e)

    def add_ids(self, ids):
        with self.lock:
            self.msg_ids.extend(ids)

    def words(self):
        with self.lock:
            return sum(len(p.split()) for p in self.parts)

    def take_text(self):
        with self.lock:
            t = " ".join(self.parts)
            self.parts = []
        return t

    def take_ids(self):
        with self.lock:
            ids, self.msg_ids = self.msg_ids, []
        return ids


def transcriber_loop(rec, sess, model, stop):
    """Каждые LIVE_SEC секунд расшифровывает накопленный звук и шлёт в Telegram."""
    while not stop.is_set():
        stop.wait(LIVE_SEC)
        final = stop.is_set()
        if rec.audio_len() < SR * (1 if final else 3):
            continue                                  # слишком мало звука — копим дальше
        audio = rec.take_audio()
        try:
            text = transcribe(model, audio, sess.tail)
        except Exception as e:
            print("Transcribe error:", e)
            continue
        if not text:
            continue
        sess.add(text)
        print("»", text[:120])
        if LIVE:
            try:                                      # a Telegram hiccup must never stop the transcription
                sess.add_ids(tg_send(f"🗣 {time.strftime('%H:%M')}  {text}", fmt=False))
            except Exception as e:
                print("live send:", type(e).__name__)


def flush_chunk(sess, rec, client, threads, label_tail=""):
    text = sess.take_text()
    live_ids = sess.take_ids()
    slides = rec.take_slides()
    sess.part_no += 1
    t0 = time.strftime("%H:%M", time.localtime(sess.chunk_start))
    t1 = time.strftime("%H:%M")
    sess.chunk_start = time.time()
    label = f"#{sess.part_no} ({t0}–{t1}){label_tail} · {sess.title}"
    if len(text.split()) < 30:
        print("Мало речи в куске, конспект пропущен")
        sess.part_no -= 1
        sess.add_ids(live_ids)                        # удалим вместе со следующим конспектом
        return
    part_no = sess.part_no
    header = summary_header(sess.title, f"часть {part_no} · {t0}–{t1}")
    note = label_tail.strip(" ()")

    def job():
        try:
            raw, seen = summarize(client, text, slides, label)
            msgs, parsed = format_summary(raw, header, sess.title, note)
            ids = tg_send_html(msgs)
            if not ids:                               # Telegram unreachable: one more try a bit later
                time.sleep(30)
                ids = tg_send_html(msgs)
            print("Sent" if ids else "NOT sent (Telegram)", label)
            with sess.lock:
                sess.summaries.append((part_no, raw))
            STATUS.update(parts=len(sess.summaries))
            try:
                archive_summary(sess.archive, label, raw)
            except OSError as e:
                print("Не смог сохранить конспект:", e)
            if ids and (PIN == "all" or (PIN == "last" and "финал" in label_tail)):
                tg_pin(ids[0])
            send_slides(parsed, seen)
            send_homework(parsed, sess.title)
            if ids and DELETE_LIVE and live_ids:      # конспект в чате — расшифровку можно убрать
                print(f"Удалено сообщений с расшифровкой: {tg_delete(live_ids)}/{len(live_ids)}")
        except Exception as e:
            print("Error:", e)
            sess.add_ids(live_ids)                    # конспекта нет — расшифровку не трогаем
            with sess.lock:                           # текст вернём: он попадёт в следующий конспект
                sess.parts.insert(0, text)
            tg_send(f"⚠️ Ошибка при конспекте {label}: {e}\n"
                    "Текст не потерян: он войдёт в следующий конспект (или /summary).", fmt=False)

    th = threading.Thread(target=job)
    th.start()
    threads.append(th)


def summary_header(title, tail, icon="📝"):
    """'📝 PCP · практика · часть 1 · 12:38–13:23'."""
    short, kind, _ = subject_parts(title)
    return " · ".join(x for x in (f"{icon} {short or 'Конспект'}", kind, tail) if x)


def send_slides(parsed, seen):
    """Album of the slides the model picked, captioned with their section."""
    pics, caps = [], []
    for n, head in parsed.get("slides", [])[:4]:
        if 1 <= n <= len(seen) and seen[n - 1] not in pics:
            pics.append(seen[n - 1])
            caps.append(f"🖼 К разделу «{head}»")
    if pics:
        tg_photos(pics, caps)


def send_homework(parsed, title):
    """Homework / deadlines as their own pinned message, so they are easy to find later."""
    hw = homework(parsed)
    if not hw:
        return
    short, kind, name = subject_parts(title)
    now = datetime.now()
    head = f"📌 <b>ДЗ · {html.escape(short or name or 'пара')} · {now.day} {_MONTHS[now.month - 1]}</b>"
    ids = tg_send_html([head + "\n\n" + _to_tg_html(hw).strip() + "\n\n#ДЗ " + html.escape(hashtags(title))])
    if ids:
        tg_pin(ids[0])


def final_summary(sess, started):
    """One overview of the whole class, merged from its part summaries."""
    with sess.lock:
        parts = sorted(sess.summaries)
    if len(parts) < 2:
        return
    joined = "\n\n".join(f"=== Часть {n} ===\n{raw}" for n, raw in parts)
    try:
        raw = llm_text(FINAL_SYSTEM, [("user", joined)], timeout=240, chat=False)
    except Exception as e:
        print("Итог пары не вышел:", e)
        return
    t0 = time.strftime("%H:%M", time.localtime(started))
    header = summary_header(sess.title, f"итог пары · {t0}–{time.strftime('%H:%M')}", icon="📚")
    msgs, _ = format_summary(raw, header, sess.title, f"собрано из {len(parts)} частей")
    ids = tg_send_html(msgs)
    if ids and PIN != "off":
        tg_pin(ids[0])
    try:
        archive_summary(sess.archive, f"📚 итог пары · {sess.title}", raw)
    except OSError as e:
        print("Не смог сохранить итог:", e)


def run_session(model, client, title, end_time=None, hard_end=False):
    rec = Recorder()
    sess = Session(title, end_time)
    stop = threading.Event()
    threading.Thread(target=rec.audio_loop, daemon=True).start()
    threading.Thread(target=rec.screen_loop, daemon=True).start()
    tt = threading.Thread(target=transcriber_loop, args=(rec, sess, model, stop))
    tt.start()
    threads = []
    REQUEST.pop("flush", None)
    REQUEST.pop("stop_rec", None)
    started = time.time()
    print(f"Запись: {title}. Конспект каждые {CHUNK_MIN:g} мин. Ctrl+C — остановить и дослать остаток.")
    next_sum = time.time() + CHUNK_MIN * 60
    STATUS.update(title=title, rec_start=started, next_sum=next_sum, parts=0)
    try:
        while True:
            time.sleep(1)
            STATUS.next_sum = next_sum
            STATUS.push()                               # edits the live status about once a minute
            if REQUEST.pop("stop_rec", None):           # /stop или «стоп» в чате
                print("Запись остановлена из Telegram")
                if CURRENT.get("joined"):
                    try:
                        leave_call()
                    except Exception as e:
                        print("leave:", e)
                    CURRENT["joined"] = None
                break
            if REQUEST.pop("flush", None):              # /summary — досрочный конспект, запись идёт дальше
                flush_chunk(sess, rec, client, threads, " (досрочно)")
                next_sum = time.time() + CHUNK_MIN * 60
            elif time.time() >= next_sum:
                flush_chunk(sess, rec, client, threads)
                next_sum += CHUNK_MIN * 60
            elif (time.time() - sess.last_voice) / 60 >= BREAK_MIN and sess.words() >= 150:
                flush_chunk(sess, rec, client, threads, " (перед перерывом)")
                next_sum = time.time() + CHUNK_MIN * 60
            if end_time is not None:
                now = datetime.now(timezone.utc)
                over = (now - end_time).total_seconds() / 60
                silent = (time.time() - sess.last_voice) / 60
                if over >= END_GRACE_MIN or (over >= 0 and (silent >= 3 or hard_end)):
                    print("Лекция закончилась")
                    break
    except KeyboardInterrupt:
        print("Останавливаюсь, обрабатываю остаток...")
        interrupted = True
    else:
        interrupted = False
    rec.stop.set()
    stop.set()
    tt.join()
    flush_chunk(sess, rec, client, threads, " (финал)")
    for th in threads:
        th.join()
    final_summary(sess, started)
    STATUS.finish(len(sess.summaries))
    if interrupted:
        raise KeyboardInterrupt


# ───────────────────────────── Управление из Telegram ─────────────────────────────
CURRENT = {"ev": None, "jstop": None, "active": False}      # текущая пара, остановка попыток входа
REQUEST = {}                                                # запросы из Telegram главному циклу
LEAVE_BTN = re.compile(r"^(leave|hang up|покинуть|положить трубку)$", re.I)
HELP = ("Команды:\n"
        "/status — что сейчас делает бот\n"
        "/screenshot — снимок экрана компьютера\n"
        "/join [предмет] — зайти в звонок (например /join mathematics)\n"
        "/leave — выйти из текущего звонка в Teams\n"
        "/record [предмет] — начать запись сейчас (без расписания)\n"
        "/summary — конспект прямо сейчас, запись продолжится\n"
        "/stop (или просто «стоп») — закончить запись этой пары с итоговым конспектом\n"
        "/shutdown — выключить бота совсем (включить снова — из хаба)\n"
        "/topic тема — сводка по теме со всех лекций\n"
        "/lectures — какие лекции сохранены\n"
        "/resend [N] — заново прислать конспекты последней (или N-й с конца) лекции\n\n"
        "Любое сообщение без слэша — вопрос: отвечу по материалам лекций.")


def start_join(ev):
    """Запускает (или перезапускает) попытки войти в звонок для события ev."""
    old = CURRENT.get("jstop")
    if old:
        old.set()
    st = threading.Event()
    CURRENT["jstop"], CURRENT["ev"] = st, ev
    CURRENT["manual_leave"] = False                    # /join или новая пара снова разрешают перезаход
    threading.Thread(target=auto_join, args=(ev, st), daemon=True).start()


def lookup_subject(name):
    """Ищет в schedule.txt предмет со ссылкой на канал (по части названия, в любой день)."""
    name = name.lower().strip()
    if not name or not os.path.exists(SCHEDULE_FILE):
        return None
    for line in open(SCHEDULE_FILE, encoding="utf-8-sig").read().splitlines():
        m = SCHED_RE.match(line.strip())
        if not m:
            continue
        left, _, link = m.group(6).partition("|")
        kw = left.partition("::")[0].strip()
        if name in kw.lower() and link.strip():
            return kw, link.strip()
    return None


def _manual_event(arg, hours=3):
    now = datetime.now(timezone.utc)
    found = lookup_subject(arg) if arg else None
    if found:
        kw, link = found
        title, ev_kw, ev_link = kw, kw, link
    else:
        title, ev_kw, ev_link = (arg or "запись"), (arg or None), None
    end = now + timedelta(hours=hours)
    try:                                               # stop in time for the next scheduled class
        nxt = [e["start"] for e in schedule_events() if now < e["start"] < end]
        if nxt:
            end = min(nxt)
    except Exception as e:
        print("schedule:", e)
    return {"key": f"manual:{time.time()}", "title": title, "start": now, "end": end,
            "kw": ev_kw, "link": ev_link}, bool(found)


def send_screenshot():
    if WEB:
        img = tw.BROWSER.screenshot()
        if img is None:
            tg_send("📷 Браузер сейчас не в звонке — снимать нечего.", fmt=False)
            return
    else:
        import mss
        from PIL import Image
        with mss.MSS() as sct:
            shot = sct.grab(sct.monitors[MONITOR])
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_CHAT},
                      files={"photo": ("screen.jpg", buf.getvalue())}, timeout=60)
    if not r.ok:
        print("sendPhoto:", r.text[:200])


def handle_command(text):
    """Разбирает команду из Telegram. Возвращает ответ (или None, если уже ответили сами)."""
    parts = text.strip().split(None, 1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/refresh":                              # 🔄 button: just redraw the status in place
        STATUS.push(force=True)
        return None
    if cmd == "/status":
        ev = CURRENT.get("ev")
        if CURRENT.get("active") and STATUS.rec_start:
            STATUS.repost()                            # fresh status (with buttons) at the bottom of the chat
            return None
        if CURRENT.get("active"):
            quiet = int((time.time() - LAST_VOICE[0]) / 60)
            return (f"🎙 Идёт запись: {ev['title'] if ev else '—'}\nПоследняя речь {quiet} мин назад."
                    if quiet else f"🎙 Идёт запись: {ev['title'] if ev else '—'}\nСейчас слышу речь.")
        nxt = [e for e in schedule_events() if e["end"] > datetime.now(timezone.utc)]
        return "💤 Сейчас записи нет." + (f"\nБлижайшая пара: {nxt[0]['title']} в {nxt[0]['start'].strftime('%H:%M')}" if nxt else "")
    if cmd == "/screenshot":
        send_screenshot()
        return None
    if cmd == "/leave":
        CURRENT["manual_leave"] = True                 # сам вышел — это не обрыв, перезаходить не надо
        if not leave_call():
            return "Кнопку «Leave» не вижу — возможно, бот не в звонке. Сам заходить снова не буду; зайти: /join."
        CURRENT["joined"] = None
        STATUS.update(call="⚪️ Вышел из звонка (/join — зайти снова)")
        return ("🚪 Вышел из звонка и сам заходить снова не буду. Запись этой пары продолжается до конца "
                "по расписанию, итоговый конспект придёт в конце (сразу закончить: /stop). Зайти снова: /join.")
    if cmd == "/join":
        if arg:
            ev, found = _manual_event(arg)
            if not found:
                return f"В schedule.txt нет предмета «{arg}» со ссылкой на канал."
            if CURRENT.get("active") and CURRENT.get("ev"):
                ev["end"] = CURRENT["ev"]["end"]
        elif CURRENT.get("ev"):
            ev = CURRENT["ev"]
        else:
            return "Сейчас нет пары. Укажи предмет: /join mathematics"
        if arg and not CURRENT.get("active"):          # nothing recording: let the main loop join AND record
            REQUEST["ev"] = ev
            return f"➡️ Захожу в «{ev['title']}» и начинаю запись…"
        start_join(ev)
        return f"➡️ Захожу в «{ev['title']}»…"
    if cmd == "/record":
        if CURRENT.get("active"):
            return "Запись уже идёт."
        ev, found = _manual_event(arg)
        REQUEST["ev"] = ev
        return f"🔴 Начинаю запись: «{ev['title']}»" + (" и захожу в звонок…" if found else ".")
    if cmd == "/stop":
        if not CURRENT.get("active"):
            return "Сейчас ничего не записываю. Выключить бота совсем: /shutdown"
        REQUEST["stop_rec"] = True
        if CURRENT.get("jstop"):
            CURRENT["jstop"].set()
        return "⏹ Заканчиваю запись этой пары, пришлю итоговый конспект. Следующую пару по расписанию запишу как обычно."
    if cmd == "/shutdown":
        _SHUTDOWN[0] = True                             # на сервере: systemd не перезапустит (код 75)
        open(STOP_FILE, "w").close()
        return "👋 Выключаюсь. Включить снова можно из хаба."
    if cmd == "/summary":
        if not CURRENT.get("active"):
            return "Сейчас ничего не записывается, конспектировать нечего."
        REQUEST["flush"] = True
        return "📝 Делаю конспект того, что было с прошлого конспекта. Запись продолжается."
    if cmd == "/topic":
        if not arg:
            return "Напиши тему: /topic кэш-память"
        run_llm_reply(topic_summary, arg)
        return None
    if cmd == "/lectures":
        lects = load_lectures()
        if not lects:
            return "Сохранённых лекций пока нет."
        lines = [f"• {l['start'].replace('T', ' ')[:16]} {l['title']}" for l in lects[-30:]]
        return f"🗂 Сохранено лекций: {len(lects)} (папка {DATA_DIR})\n" + "\n".join(lines)
    if cmd == "/resend":
        lects = [l for l in load_lectures() if l["summaries"].strip()]
        n = int(arg) if arg.strip().isdigit() else 1
        if not lects or n > len(lects):
            return "Сохранённых конспектов нет." if not lects else f"Лекций с конспектами всего {len(lects)}."
        l = lects[-n]
        chunks = re.split(r"(?m)^## (#\d+ .*|📚 .*)$", l["summaries"])   # [pre, label1, text1, label2, …]
        for label, part in zip(chunks[1::2], chunks[2::2]):
            part = re.sub(r"^\s*📝 Конспект .*\n", "", part.strip())       # old archives repeat the label
            if not part.strip():
                continue
            if "КОРОТКО" in part[:300].upper():
                msgs, _ = format_summary(part, "📝 " + label.split(" · ")[0], l["title"])
                tg_send_html(msgs, silent=True)
            else:
                tg_send(f"📝 {label}\n\n{part}", silent=True)
        return None
    return "Не знаю такую команду.\n\n" + HELP


STOP_WORDS = re.compile(r"^\s*(стоп|остановись|останови запись|хватит|stop)\s*[.!]*\s*$", re.I)


def run_llm_reply(fn, arg):
    """Долгий запрос к модели — в отдельном потоке, чтобы команды не ждали."""
    def job():
        tg_typing()
        try:
            res = fn(arg)
            if isinstance(res, list):                  # already formatted (e.g. /topic)
                tg_send_html(res)
            else:
                tg_send(res)
        except LLMBusy as e:
            print("Ответ в чат не вышел:", e)
            tg_send("😴 Я тут, но модель для ответов сейчас упёрлась в лимит бесплатных запросов. "
                    "Попробуй чуть позже. Конспекты лекций это не задевает: они идут на других моделях.", fmt=False)
        except Exception as e:
            print("Ответ в чат не вышел:", e)
            tg_send(f"⚠️ Не получилось ответить: {type(e).__name__}: {str(e)[:200]}", fmt=False)
    threading.Thread(target=job, daemon=True).start()


BUTTON_ACK = {"/summary": "Делаю конспект…", "/screenshot": "Снимаю экран…",
              "/refresh": "Обновил", "/leave": "Выхожу из звонка…"}


def on_button(base, cq):
    """Inline button under the status message: same as typing the command."""
    data = cq.get("data") or ""
    chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
    ok = str(chat) == str(TG_CHAT) and data in BUTTON_ACK
    try:
        requests.post(base + "/answerCallbackQuery", timeout=15,
                      json={"callback_query_id": cq["id"], "text": BUTTON_ACK.get(data, "") if ok else ""})
    except requests.exceptions.RequestException:
        pass
    if not ok:
        return
    try:
        reply = handle_command(data)
    except Exception as e:
        reply = f"⚠️ Ошибка команды: {type(e).__name__}: {str(e)[:200]}"
    if reply:
        tg_send(reply, fmt=False)


def commands_loop():
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    offset = None
    try:
        res = requests.get(base + "/getUpdates", params={"offset": -1, "timeout": 0}, timeout=30).json().get("result", [])
        offset = res[-1]["update_id"] + 1 if res else None
        requests.post(base + "/setMyCommands", json={"commands": [
            {"command": "status", "description": "что делает бот"},
            {"command": "screenshot", "description": "снимок экрана"},
            {"command": "join", "description": "зайти в звонок [предмет]"},
            {"command": "leave", "description": "выйти из звонка"},
            {"command": "record", "description": "начать запись [предмет]"},
            {"command": "summary", "description": "конспект сейчас, запись продолжится"},
            {"command": "stop", "description": "закончить запись этой пары"},
            {"command": "shutdown", "description": "выключить бота"},
            {"command": "topic", "description": "сводка по теме со всех лекций"},
            {"command": "lectures", "description": "сохранённые лекции"},
            {"command": "resend", "description": "заново прислать конспекты лекции [N]"}]}, timeout=30)
    except Exception as e:
        print("commands init:", type(e).__name__)
    while True:
        try:
            r = requests.get(base + "/getUpdates", params={
                "offset": offset, "timeout": 25,
                "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=40)
            if r.status_code == 409:                      # запущен второй экземпляр бота
                time.sleep(10)
                continue
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if u.get("callback_query"):
                    on_button(base, u["callback_query"])
                    continue
                m = u.get("message") or {}
                if str(m.get("chat", {}).get("id")) != str(TG_CHAT):
                    continue                              # команды только от тебя
                txt = (m.get("text") or "").strip()
                if not txt:
                    continue
                if not txt.startswith("/"):                # обычный вопрос
                    if not STOP_WORDS.match(txt):
                        run_llm_reply(ask, txt)
                        continue
                    txt = "/stop"
                try:
                    reply = handle_command(txt)
                except Exception as e:
                    reply = f"⚠️ Ошибка команды: {type(e).__name__}: {str(e)[:200]}"
                if reply:
                    tg_send(reply, fmt=False)
        except Exception as e:
            print("commands:", type(e).__name__)
            time.sleep(5)


# ───────────────────────────── main ─────────────────────────────
def main():
    if WEB:
        tw.pulse_setup()
    model = load_whisper()
    client = None
    if PROVIDER == "anthropic":
        from anthropic import Anthropic
        client = Anthropic()
    CLIENT[0] = client

    threading.Thread(target=commands_loop, daemon=True).start()
    use_sched = os.path.exists(SCHEDULE_FILE)
    if (not ICS_URL and not use_sched) or "--now" in sys.argv:
        try:
            CURRENT["active"] = True
            run_session(model, client, "запись")
        except KeyboardInterrupt:
            pass
        return

    src = " + ".join(x for x, on in (("расписанием", use_sched), ("календарём", bool(ICS_URL))) if on)
    tg_send(f"🗓 Бот запущен и следит за {src}. На лекции зайду сам.", fmt=False)
    print(f"Слежу за {src}. Ctrl+C — выйти.")
    handled, ics_events, fetched = set(), [], 0.0
    try:
        while True:
            if ICS_URL and time.time() - fetched > 600:
                try:
                    ics_events = fetch_events()
                    fetched = time.time()
                    print(f"Календарь обновлён: ближайших встреч Teams — {len(ics_events)}")
                except Exception as e:
                    print("Календарь не загрузился:", e)
                    fetched = time.time() - 540          # повторим через минуту
            events = sorted(ics_events + schedule_events(), key=lambda e: e["start"])
            ev = due_event(events, handled, datetime.now(timezone.utc)) or REQUEST.pop("ev", None)
            if ev:
                handled.add(ev["key"])
                if ev.get("kw") or ev.get("link"):
                    start_join(ev)
                else:
                    CURRENT["ev"] = ev
                follows = any(0 <= (e["start"] - ev["end"]).total_seconds() <= 300
                              for e in events if e is not ev)       # сразу за ней другая пара
                CURRENT["active"] = True
                try:
                    run_session(model, client, ev["title"], ev["end"], hard_end=follows)
                finally:
                    CURRENT["active"] = False
                    if CURRENT.get("jstop"):
                        CURRENT["jstop"].set()
                continue
            for _ in range(20):                     # по секунде — чтобы быстро реагировать на остановку
                time.sleep(1)
    except KeyboardInterrupt:
        print("Выход.")


STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop.request")
_STOP_SENT = [False]
_SHUTDOWN = [False]


def notify_stop(text="⏹ Бот остановлен."):
    """Одно сообщение о завершении работы (при остановке, Ctrl+C или падении)."""
    if _STOP_SENT[0]:
        return
    _STOP_SENT[0] = True
    try:
        tg_send(text, fmt=False)
    except Exception as e:
        print("Не смог отправить сообщение об остановке:", type(e).__name__)


def stop_file_watch():
    """Хаб просит остановиться, создавая файл stop.request: завершаемся как по Ctrl+C."""
    import _thread
    while True:
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass
            print("Получен запрос на остановку")
            _thread.interrupt_main()
            return
        time.sleep(1)


if __name__ == "__main__":
    try:
        os.remove(STOP_FILE)                                # старый запрос от прошлого запуска
    except OSError:
        pass
    threading.Thread(target=stop_file_watch, daemon=True).start()
    if hasattr(signal, "SIGTERM") and sys.platform != "win32":
        def _on_term(*_):                                   # systemctl stop: как Ctrl+C — дослать конспект
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _on_term)
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        notify_stop(f"💥 Бот упал и остановился: {type(e).__name__}: {str(e)[:300]}")
        raise
    finally:
        if WEB:
            try:
                tw.BROWSER.leave()
                tw.BROWSER.save_state()
                tw.BROWSER.close()
            except Exception as e:
                print("browser close:", type(e).__name__)
    notify_stop()
    if _SHUTDOWN[0] and sys.platform != "win32":
        sys.exit(75)
