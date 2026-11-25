import io
from pywinauto import Desktop
d = Desktop(backend="uia")
out = io.StringIO()
for w in d.windows():
    t = (w.window_text() or "").strip()
    if not t or t in ("Taskbar", "Windows PowerShell") or t.startswith("teams_ui"):
        continue
    out.write(f"\n# окно: {t[:120]}\n")
    seen = set()
    for ctype in ("Button", "Hyperlink", "MenuItem", "TabItem"):
        try:
            for c in w.descendants(control_type=ctype):
                n = (c.window_text() or "").strip()
                if n and n not in seen:
                    seen.add(n)
                    out.write(f"[{ctype}] {n[:80]}\n")
        except Exception as e:
            out.write(f"ошибка {ctype}: {e}\n")
open("teams_ui.txt", "w", encoding="utf-8").write(out.getvalue())
print("Готово: teams_ui.txt,", len(out.getvalue()), "символов")
