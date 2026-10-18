"""Вход в интерфейс и защита приёма правок."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import api, learning  # noqa: E402
from app.auth import authenticate, make_record  # noqa: E402

USERS = {"op": make_record("secret-1", "operator", "Оператор"), "boss": make_record("secret-2", "admin", "Админ")}


def test_password_is_not_stored():
    rec = USERS["op"]
    assert "secret-1" not in str(rec) and len(rec["hash"]) == 64


@pytest.mark.parametrize("login,password,role", [("op", "secret-1", "operator"), (" BOSS ", "secret-2", "admin")])
def test_login_ok(login, password, role):
    assert authenticate(login, password, USERS)["role"] == role


@pytest.mark.parametrize("login,password", [("op", "wrong"), ("nobody", "secret-1"), ("op", ""), ("", "")])
def test_login_rejected(login, password):
    assert authenticate(login, password, USERS) is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(learning, "_LEARNED", learning.Learned(tmp_path / "c.jsonl"))
    with TestClient(api.app) as c:
        yield c


REC = [{"id": 1, "raw_address": "г. Алматы, ул. Тестовая 1", "city": "Алматы", "street_name": "Тестовая",
        "house": "1", "district": "Медеуский"}]


def test_batch_never_needs_token(client, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "t0ken")
    r = client.post("/api/v1/parse/batch", json=[{"id": 1, "raw_address": "Кулманова 133"}])
    assert r.status_code == 200


def test_corrections_need_token_when_set(client, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", "t0ken")
    assert client.post("/api/v1/corrections", json=REC).status_code == 401
    assert client.post("/api/v1/corrections", json=REC, headers={"X-API-Key": "nope"}).status_code == 401
    ok = client.post("/api/v1/corrections", json=REC, headers={"X-API-Key": "t0ken", "X-Operator": "op"})
    assert ok.status_code == 200


def test_journal_keeps_author(client, monkeypatch):
    monkeypatch.setattr(api, "API_TOKEN", None)
    client.post("/api/v1/corrections", json=REC, headers={"X-Operator": "op"})
    journal = client.get("/api/v1/corrections").json()
    assert journal[0]["corrected_by"] == "op" and journal[0]["district"] == "Медеуский"
