import re
src = open("bot.py", encoding="utf-8").read()
if "_to_tg_html" in src:
    raise SystemExit("Уже применено")
new = r'''import re, html

def _to_tg_html(text):
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", t)
    t = re.sub(r"(?m)^#+\s*(.+)$", r"<b>\1</b>", t)
    return t


def _split(text, limit=3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def tg_send(text):
    for part in _split(text):
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": _to_tg_html(part),
                                     "parse_mode": "HTML"}, timeout=30)
        if not r.ok:
            r = requests.post(url, json={"chat_id": TG_CHAT, "text": part}, timeout=30)
            if not r.ok:
                print("Telegram error:", r.text)


'''
a = src.index("def tg_send(text):")
b = src.index("class Recorder:")
open("bot.py", "w", encoding="utf-8").write(src[:a] + new + src[b:])
print("Готово")
