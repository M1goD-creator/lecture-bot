"""Server side of deploy/set_teams_password.sh: reads the e-mail and password from stdin
(two lines) and stores them in .env as TEAMS_EMAIL / TEAMS_PASSWORD (mode 600).
Never prints the password."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")


def quote(v):
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"


def main():
    lines = sys.stdin.read().split("\n")
    email = lines[0].strip() if lines else ""
    pw = lines[1].rstrip("\r") if len(lines) > 1 else ""
    if not email or not pw:
        sys.exit("Нужны и почта, и пароль — ничего не изменил.")
    old = open(ENV, encoding="utf-8").read().splitlines() if os.path.exists(ENV) else []
    keep = [l for l in old if not l.startswith(("TEAMS_EMAIL=", "TEAMS_PASSWORD="))]
    keep += [f"TEAMS_EMAIL={quote(email)}", f"TEAMS_PASSWORD={quote(pw)}"]
    tmp = ENV + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)
    from dotenv import dotenv_values
    back = dotenv_values(ENV)
    ok = back.get("TEAMS_EMAIL") == email and back.get("TEAMS_PASSWORD") == pw
    print("✅ Сохранил вход в .env на сервере (только для владельца)." if ok
          else "⚠️ Записал, но при проверке значение не совпало — напиши Claude.")


if __name__ == "__main__":
    main()
