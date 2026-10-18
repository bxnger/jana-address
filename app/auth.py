"""Учётные записи операторов: хранение и проверка паролей (только stdlib).

Пароли не хранятся — только соль и хеш PBKDF2-SHA256. Файл пользователей:
data/users.json (путь можно переопределить через JANA_USERS):

    {"users": {"operator": {"role": "operator", "name": "Оператор", "salt": "...", "hash": "..."}}}

Добавить пользователя или сменить пароль:  python scripts/add_user.py <логин> <роль> "<имя>"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("JANA_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
USERS_PATH = Path(os.environ.get("JANA_USERS", DATA_DIR / "users.json"))
ROLES = {"operator": "Оператор", "admin": "Администратор"}
_ITERATIONS = 200_000


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()


def make_record(password: str, role: str, name: str) -> dict:
    if role not in ROLES:
        raise ValueError(f"роль должна быть одной из: {', '.join(ROLES)}")
    salt = secrets.token_hex(16)
    return {"role": role, "name": name, "salt": salt, "hash": _hash(password, salt)}


def load_users(path: Path = USERS_PATH) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("users", {})


def save_users(users: dict, path: Path = USERS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=1), encoding="utf-8")


def authenticate(login: str, password: str, users: dict | None = None) -> dict | None:
    """-> {'login', 'role', 'name'} или None. Сравнение хешей — за постоянное время."""
    users = load_users() if users is None else users
    rec = users.get((login or "").strip().lower())
    if not rec or not password:
        # всё равно считаем хеш, чтобы по времени ответа нельзя было угадать логин
        _hash(password or "", "00" * 16)
        return None
    if hmac.compare_digest(_hash(password, rec["salt"]), rec["hash"]):
        return {"login": login.strip().lower(), "role": rec["role"], "name": rec.get("name") or login}
    return None
