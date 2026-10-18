"""Добавить оператора или сменить пароль.

    python scripts/add_user.py operator operator "Оператор"
    python scripts/add_user.py admin admin "Администратор"

Пароль спрашивается без отображения на экране (или передаётся через JANA_NEW_PASSWORD).
Пишет в data/users.json — в файле хранятся только соль и хеш.
"""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import ROLES, load_users, make_record, save_users  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit(f"использование: add_user.py <логин> <роль: {'|'.join(ROLES)}> [\"Имя\"]")
    login, role = sys.argv[1].strip().lower(), sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else login
    password = os.environ.get("JANA_NEW_PASSWORD") or getpass.getpass(f"Пароль для {login}: ")
    if len(password) < 6:
        sys.exit("пароль слишком короткий (минимум 6 символов)")
    users = load_users()
    users[login] = make_record(password, role, name)
    save_users(users)
    print(f"[ok] {login} ({ROLES[role]}) сохранён")


if __name__ == "__main__":
    main()
