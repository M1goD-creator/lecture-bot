# Offline test: summary layout for scheduled / early (/summary) / topic paths, balanced HTML even with
# * and _ inside code, and Telegram network errors that must not raise.
# Run: .venv\Scripts\python tests\test_format.py
import os, sys, threading, time
from html.parser import HTMLParser
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="1", TEAMS_BACKEND="app")
sys.path.insert(0, os.path.dirname(HERE))
import bot

ALLOWED = {"b", "i", "s", "code", "pre", "a", "blockquote"}


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.errors.append(f"tag {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unmatched </{tag}> (open: {self.stack})")
        else:
            self.stack.pop()


def check_html(msg):
    p = Balance()
    p.feed(msg)
    p.close()
    assert not p.errors and not p.stack, f"bad HTML: {p.errors} {p.stack}\n{msg}"


RAW = """КОРОТКО: Указатели и массивы, *p и p_a_b.
🎯 Тема: Указатели в C
💡 Главные идеи
• Переменная `int *p_a` хранит адрес; `p_b_c` — другая, *важно* и _тоже_.
• Выражение `a*b*c` и `x_1 * y_2` не курсив.
🧩 Код
```c
int *p_a = &a_b; *p_a = c*d*e; /* _x_ */
```
⚠️ Важно для экзамена
• Не путать `*p++` и `(*p)++`.
📌 ДЗ и дедлайны
• Задача про `arr_2d`, к понедельнику.
СЛАЙДЫ: нет
"""

# 1) the formatter itself: balanced tags, code kept literal
msgs, parsed = bot.format_summary(RAW, "📝 PCP · лекция · часть 1 · 10:00–10:45", "Principles of Computer Programming (лекция)")
for m in msgs:
    check_html(m)
joined = "\n".join(msgs)
assert "<code>int *p_a</code>" in joined and "c*d*e; /* _x_ */" in joined, joined
assert "<blockquote expandable>" in joined and "Коротко:" in joined

# 2) early summary (/summary) goes through the same layout as the scheduled one
sent = []
bot.summarize = lambda client, text, slides, label: (RAW, [])
bot.tg_send_html = lambda parts, **kw: (sent.append(list(parts)), [1])[1]
bot.tg_send = lambda *a, **kw: []
bot.tg_pin = bot.tg_photos = bot.tg_delete = lambda *a, **kw: 0
bot.STATUS.push = lambda force=False: None


class Rec:
    def take_slides(self):
        return []


sess = bot.Session.__new__(bot.Session)
sess.title, sess.parts, sess.lock, sess.part_no = "Principles of Computer Programming (лекция)", ["слово"] * 40, threading.Lock(), 0
sess.chunk_start, sess.msg_ids, sess.summaries, sess.archive = time.time() - 600, [], [], os.path.join(HERE, "_tmp_archive")
os.makedirs(sess.archive, exist_ok=True)
threads = []
bot.flush_chunk(sess, Rec(), None, threads, " (досрочно)")
for th in threads:
    th.join(10)
early = "\n".join(sent[0])
assert "досрочно" in early and "Коротко:" in early and "<blockquote expandable>" in early, sent[0]

# 3) /topic uses the same layout
bot.build_context = lambda q, budget=None: "архив"
bot.llm_text = lambda system, msgs, **kw: RAW
res = bot.topic_summary("указатели")
assert isinstance(res, list) and "Коротко:" in res[0] and "Сводка по теме" in res[0], res
for m in res:
    check_html(m)

# 4) Telegram unreachable: no exception, just a failed response
import requests
orig = requests.post
requests.post = lambda *a, **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("slow"))
bot.time.sleep = lambda s: None
r = bot._tg_post({"chat_id": "1", "text": "x"})
requests.post = orig
assert not r.ok

import shutil
shutil.rmtree(sess.archive, ignore_errors=True)
print("OK: layout same for scheduled / early / topic, HTML balanced, Telegram errors don't raise")
