import bot
b = bot.find_button(bot.JOIN_BTN, "Computer organization")
print("Нашёл кнопку:", b.window_text() if b else "НЕТ")
