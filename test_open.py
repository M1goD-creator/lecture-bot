import re, time
import bot
line = next(l for l in open("schedule.txt", encoding="utf-8-sig") if "Computer organization" in l and "|" in l)
link = line.split("|", 1)[1].strip()
print("Ссылка из расписания:", link[:70], "...")
print("Открываю канал...")
bot.open_teams_link(link)
for i in range(6):
    time.sleep(5)
    from pywinauto import Desktop
    titles = [w.window_text() for w in Desktop(backend="uia").windows()
              if "computer organization" in (w.window_text() or "").lower()]
    print(f"{(i+1)*5} с: окна с предметом ->", titles or "нет")
    if titles:
        break
b = bot.find_button(bot.JOIN_BTN, "Computer organization")
print("Кнопка Join:", b.window_text() if b else "НЕТ (или сейчас нет встречи)")
