"""Saves a Teams login for the server bot (optional: without it the bot joins as a guest).

Run on your PC:  .venv\\Scripts\\python teams_login.py
A Chromium window opens on Teams. Sign in with your university account. As soon as Teams has
loaded, the login is written to teams-state.json and the window closes by itself (or press
Enter here to save earlier). Copy that file to the server (/srv/lecture-bot/teams-state.json).
It's a password-equivalent: never commit or share it. The bot refreshes it after every lecture.
"""
import os
import re
import sys
import threading
import time

from playwright.sync_api import sync_playwright

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "teams-state.json")
WAIT_MIN = 20
SAVE_NOW = OUT + ".save"                                  # create this file to save right away
SIGNIN = re.compile(r"sign in again|sign in to join|need to be signed in", re.I)


def signed_in(page):
    """Teams has loaded for a signed-in user: the left rail shows Chat."""
    if os.path.exists(SAVE_NOW):
        return True
    if "teams.microsoft.com" not in page.url:
        return False
    try:                                                  # MSAL keeps the tokens in localStorage
        has_tokens = page.evaluate("() => Object.keys(localStorage).some(k => /refreshtoken/i.test(k))")
        return bool(has_tokens) and not page.get_by_text(SIGNIN).filter(visible=True).count()
    except Exception:
        return False


enter = threading.Event()
if sys.stdin and sys.stdin.isatty():
    threading.Thread(target=lambda: (input(), enter.set()), daemon=True).start()

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(locale="en-US")
    page = ctx.new_page()
    page.goto("https://teams.microsoft.com/")
    print(f"Sign in in the browser window (waiting up to {WAIT_MIN} min)…")
    t_end = time.time() + WAIT_MIN * 60
    while time.time() < t_end and not enter.is_set() and not signed_in(page):
        page.wait_for_timeout(2000)
    if not enter.is_set() and not signed_in(page):
        browser.close()
        sys.exit("Not signed in, nothing saved.")
    page.wait_for_timeout(15000)                          # let Teams finish storing its tokens
    ctx.storage_state(path=OUT)
    if os.path.exists(SAVE_NOW):
        os.remove(SAVE_NOW)
    browser.close()
print("Saved:", OUT)
