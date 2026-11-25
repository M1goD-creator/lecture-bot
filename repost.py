"""Re-sends every saved lecture's summaries to Telegram in the current layout (oldest first).

  .venv/bin/python repost.py --dry-run     count lectures / messages / model calls, send nothing
  .venv/bin/python repost.py               do it (silent messages, paced, resumable)

Summaries already in the new format are reused. Old-format parts are regenerated from the saved
transcript for that time range (or rewritten from the old summary if the transcript slice is tiny).
Lectures with a transcript but no summaries get new ones. One wrap-up per lecture with 2+ parts,
homework as its own message. New texts go to summaries.v2.md; summaries.md is never changed.
Progress is kept in lectures/.repost-progress.json, so a rerun continues where it stopped.
Run it while the bot is idle (no class): it uses the same Telegram chat and model limits.
"""
import json
import os
import re
import sys
import time
from datetime import datetime

import bot

DRY = "--dry-run" in sys.argv
PACE = float(os.getenv("REPOST_PACE_SEC", "20"))          # pause after each model call
SEND_PACE = 3.5                                            # Telegram: ~20 messages a minute per chat
PROGRESS = os.path.join(bot.DATA_DIR, ".repost-progress.json")
MIN_WORDS = 150
REWRITE = ("Ниже старый конспект части лекции (другой формат). Перепиши его в требуемый формат, "
           "ничего не добавляя от себя.\n\n")
STATS = {"calls": 0, "sends": 0}


def load_progress():
    try:
        return json.load(open(PROGRESS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_progress(p):
    if DRY:
        return
    tmp = PROGRESS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROGRESS)


def transcript_lines(path):
    """[(minute_of_day, text)] from lines like '[12:34] text'."""
    out = []
    fp = os.path.join(path, "transcript.txt")
    if not os.path.exists(fp):
        return out
    for line in open(fp, encoding="utf-8"):
        m = re.match(r"\[(\d\d):(\d\d)\]\s*(.*)", line)
        if m and m[3].strip():
            out.append((int(m[1]) * 60 + int(m[2]), m[3].strip()))
    return out


def slice_text(lines, t0, t1):
    return " ".join(t for m, t in lines if t0 <= m <= t1)


def mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_new(body):
    return "КОРОТКО" in body[:500].upper()


def llm(system, text):
    STATS["calls"] += 1
    if DRY:
        return ""
    for attempt in range(4):
        try:
            out = bot.llm_text(system, [("user", text)], timeout=240, chat=False).strip()
            time.sleep(PACE)
            return out
        except Exception as e:
            wait = 120 * (attempt + 1)
            print(f"  model busy ({type(e).__name__}), waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError("model unavailable")


def send(msgs):
    STATS["sends"] += len(msgs)
    ids = []
    if DRY:
        return ids
    for m in msgs:
        ids += bot.tg_send_html([m], silent=True)
        time.sleep(SEND_PACE)
    return ids


def plan_lecture(path):
    """[(label, t0, t1, raw_or_None, old_body)] parts and an existing wrap-up raw (or None)."""
    parts, wrap = [], None
    sp = os.path.join(path, "summaries.md")
    lines = transcript_lines(path)
    if os.path.exists(sp):
        chunks = re.split(r"(?m)^## (.*)$", open(sp, encoding="utf-8").read())
        for label, body in zip(chunks[1::2], chunks[2::2]):
            body = re.sub(r"^\s*📝 Конспект .*\n", "", body.strip())
            if label.startswith("📚"):
                wrap = body if is_new(body) else None
                continue
            m = re.search(r"\((\d\d:\d\d)–(\d\d:\d\d)\)", label)
            t0, t1 = (m[1], m[2]) if m else ("", "")
            parts.append([label, t0, t1, body if is_new(body) else None, body])
    elif lines and sum(len(t.split()) for _, t in lines) >= MIN_WORDS:
        start = lines[0][0]                                 # no summaries yet: 45-minute pieces
        while start <= lines[-1][0]:
            end = start + 44
            if len(slice_text(lines, start, end).split()) >= 30:
                t0, t1 = f"{start // 60:02d}:{start % 60:02d}", f"{end // 60:02d}:{end % 60:02d}"
                parts.append([f"({t0}–{t1})", t0, t1, None, ""])
            start = end + 1
    return parts, wrap, lines


def main():
    prog = load_progress()
    dirs = sorted(d for d in os.listdir(bot.DATA_DIR) if os.path.isdir(os.path.join(bot.DATA_DIR, d)))
    todo = []
    for d in dirs:
        parts, wrap, lines = plan_lecture(os.path.join(bot.DATA_DIR, d))
        if parts:
            todo.append((d, parts, wrap, lines))
    n_parts = sum(len(p) for _, p, _, _ in todo)
    print(f"lectures: {len(todo)}, parts: {n_parts}, "
          f"already new: {sum(1 for _, p, _, _ in todo for x in p if x[3])}")
    if not prog.get("intro"):
        send(["🗂 <b>Архив конспектов в новом виде</b>\nВсе сохранённые лекции по порядку, от старых к новым."])
        prog["intro"] = True
        save_progress(prog)
    for d, parts, wrap, lines in todo:
        if prog.get(d) == "done":
            continue
        path = os.path.join(bot.DATA_DIR, d)
        meta = json.load(open(os.path.join(path, "meta.json"), encoding="utf-8"))
        title = meta.get("title", d[16:])
        when = datetime.strptime(d[:15], "%Y-%m-%d_%H%M")
        day = f"{when.day} {bot._MONTHS[when.month - 1]}"
        print(f"== {d}: {len(parts)} parts")
        raws = []
        for i, (label, t0, t1, raw, old) in enumerate(parts, 1):
            if raw is None:
                text = slice_text(lines, mins(t0), mins(t1)) if t0 else ""
                if len(text.split()) >= MIN_WORDS:
                    raw = llm(bot.SYSTEM, "Транскрипт:\n\n" + text)
                elif old.strip():
                    raw = llm(bot.SYSTEM, REWRITE + old)
                else:
                    continue
            raws.append((i, raw))
            if DRY:
                STATS["sends"] += 2                           # summary (+ maybe homework)
                continue
            header = bot.summary_header(title, f"{day} · часть {i} · {t0}–{t1}")
            msgs, parsed = bot.format_summary(raw, header, title, "из архива", when=when)
            send(msgs)
            hw = bot.homework(parsed)
            if hw:
                short, kind, name = bot.subject_parts(title)
                ids = send([f"📌 <b>ДЗ · {bot.html.escape(short or name)} · {day}</b>\n\n"
                            + bot._to_tg_html(hw).strip() + "\n\n#ДЗ " + bot.html.escape(bot.hashtags(title, when))])
                if ids:
                    prog["last_hw"] = ids[0]
            with open(os.path.join(path, "summaries.v2.md"), "a", encoding="utf-8") as f:
                f.write(f"## {label}\n\n{raw.strip()}\n\n")
        if len(raws) >= 2:
            if wrap is None:
                joined = "\n\n".join(f"=== Часть {n} ===\n{r}" for n, r in raws)
                wrap = llm(bot.FINAL_SYSTEM, joined)
            if DRY:
                STATS["sends"] += 1
            else:
                t0, t1 = parts[0][1], parts[-1][2]
                header = bot.summary_header(title, f"{day} · итог пары · {t0}–{t1}", icon="📚")
                msgs, _ = bot.format_summary(wrap, header, title, f"собрано из {len(raws)} частей", when=when)
                send(msgs)
                with open(os.path.join(path, "summaries.v2.md"), "a", encoding="utf-8") as f:
                    f.write(f"## 📚 итог пары · {title}\n\n{wrap.strip()}\n\n")
        prog[d] = "done"
        save_progress(prog)
    if not DRY and prog.get("last_hw") and not prog.get("pinned"):
        bot.tg_pin(prog["last_hw"])                         # only the newest homework stays pinned
        prog["pinned"] = True
        save_progress(prog)
    if not DRY:
        print("REPOST DONE")
    est = STATS["calls"] * (PACE + 25) + STATS["sends"] * SEND_PACE
    print(f"model calls: {STATS['calls']}, messages: ~{STATS['sends']}, time: ~{est / 60:.0f} min"
          + (" (dry run, nothing sent)" if DRY else ""))


if __name__ == "__main__":
    main()
