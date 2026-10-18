"""Проверка автономной работы (сеть отключена, как на финальном прогоне).

    docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d --force-recreate
    python scripts/offline_check.py

Всё выполняется внутри контейнеров через `docker compose exec`, так как
internal-сеть не пробрасывает порты на хост.
"""
import subprocess
import sys
import time

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.offline.yml"]

CHECK_EGRESS = """
import urllib.request
try:
    urllib.request.urlopen("https://download.geofabrik.de", timeout=5)
    print("FAIL: интернет доступен")
except Exception as e:
    print("OK: интернета нет (" + type(e).__name__ + ")")
"""

CHECK_API = """
import json, time, urllib.request
h = json.load(urllib.request.urlopen("http://localhost:8000/health"))
assert h["core_gazetteer"]["external_loaded"], h
print("OK: справочник ядра загружен, улиц OSM:", h["gazetteer"]["streets"])
samples = ["город Алматы/Алматы қаласы, Мкр Аксай 3а, 59, кв 70",
           "Туран 55/6 магазин береке 4подьезд 9этаж",
           "Нурсити 2 микрорайон 11дом 5 подьезд 239 кв",
           "Турксибский район, Рыбинский переулок, дом 4", None, ""]
recs = [{"id": f"r{i}", "raw_address": samples[i % len(samples)]} for i in range(1000)]
req = urllib.request.Request("http://localhost:8000/api/v1/parse/batch", json.dumps(recs).encode(),
                             {"Content-Type": "application/json"})
t = time.time(); out = json.load(urllib.request.urlopen(req)); dt = time.time() - t
assert len(out) == 1000 and [o["id"] for o in out] == [r["id"] for r in recs]
assert out[0]["district"] == "Ауэзовский" and out[1]["house"] == "55/6"
print(f"OK: 1000 записей за {dt:.2f} с (лимит 300 с)")
"""

CHECK_UI = """
import urllib.request
assert urllib.request.urlopen("http://localhost:8501/_stcore/health").read() == b"ok"
assert b'"status":"ok"' in urllib.request.urlopen("http://api:8000/health").read().replace(b" ", b"")
print("OK: UI жив и видит API")
"""


def run(service, code):
    r = subprocess.run([*COMPOSE, "exec", "-T", service, "python", "-c", code],
                       capture_output=True, text=True, encoding="utf-8")
    print((r.stdout + r.stderr).strip())
    return r.returncode == 0 and "FAIL" not in r.stdout


def wait_ready(timeout=180):
    t = time.time()
    while time.time() - t < timeout:
        ok = subprocess.run([*COMPOSE, "exec", "-T", "api", "python", "-c",
                             "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"],
                            capture_output=True).returncode == 0
        if ok:
            return time.time() - t
        time.sleep(1)
    return None


if __name__ == "__main__":
    ready = wait_ready()
    print(f"API готов через {ready:.0f} с после проверки" if ready is not None else "FAIL: API не поднялся за 3 мин")
    results = [ready is not None, run("api", CHECK_EGRESS), run("api", CHECK_API), run("ui", CHECK_UI)]
    print("\nИТОГ:", "всё работает без сети" if all(results) else "ЕСТЬ ПРОБЛЕМЫ")
    sys.exit(0 if all(results) else 1)
