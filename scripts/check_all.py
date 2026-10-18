"""Все проверки одной командой — перед сдачей и после каждого нового ядра.

    python scripts/check_all.py            # тесты, синтетика, живой набор
    python scripts/check_all.py --docker   # плюс сборка образа и проверка без сети

Нужны: pytest, rapidfuzz, fastapi, httpx (для --docker — Docker).
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "JANA_GAZETTEER": str(ROOT / "data" / "core_gazetteer.json")}


def run(cmd, cwd=ROOT, timeout=900):
    t = time.time()
    r = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return r.returncode, r.stdout + r.stderr, time.time() - t


def last(pattern, text, default="?"):
    m = re.findall(pattern, text)
    return m[-1] if m else default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docker", action="store_true", help="собрать образ и проверить работу без сети")
    ap.add_argument("--seed", type=int, default=2027, help="seed синтетики (берите тот, что не открывали при правках)")
    args = ap.parse_args()
    results = []

    code, out, dt = run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"])
    results.append(("Интеграция (tests/)", code == 0, last(r"(\d+ passed[^\n]*)", out), dt))

    code, out, dt = run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT / "core")
    results.append(("Ядро (core/tests)", code == 0, last(r"(\d+ passed[^\n]*)", out), dt))

    code, out, dt = run([PY, "scripts/gen_synthetic.py", "--n", "3000", "--seed", str(args.seed)])
    ok = code == 0
    if ok:
        code, out, dt2 = run([PY, "evaluate.py", "--parser", "../scripts/pipeline_parser.py",
                              "--input", "../data/synthetic/synth_input.json",
                              "--gold", "../data/synthetic/synth_gold.json", "--show-errors", "0"], cwd=ROOT / "core")
        dt += dt2
        ok = code == 0
    results.append((f"Синтетика 3000, seed {args.seed}", ok, last(r"ОЦЕНКА:\s*([\d.]+%)", out), dt))

    code, out, dt = run([PY, "scripts/synth_breakdown.py"])
    results.append(("Восстановление района", code == 0,
                    last(r"(верно \d+%, неверно \d+%, не заполнено \d+%)", out), dt))

    # «Грязный» набор: телефоны, имена, домофон, опечатки, №, капс (последним — он перезаписывает synthetic/)
    code, out, dt = run([PY, "scripts/gen_synthetic.py", "--n", "3000", "--seed", str(args.seed + 1), "--hard"])
    if code == 0:
        code, out, dt2 = run([PY, "evaluate.py", "--parser", "../scripts/pipeline_parser.py",
                              "--input", "../data/synthetic/synth_input.json",
                              "--gold", "../data/synthetic/synth_gold.json", "--show-errors", "0"], cwd=ROOT / "core")
        dt += dt2
    results.append((f"Грязная синтетика, seed {args.seed + 1}", code == 0, last(r"ОЦЕНКА:\s*([\d.]+%)", out), dt))

    code, out, dt = run([PY, "evaluate.py", "--parser", "../scripts/pipeline_parser.py",
                         "--cases", "tests/live.json", "--show-errors", "0"], cwd=ROOT / "core")
    results.append(("Живой набор", code == 0, last(r"ОЦЕНКА:\s*([\d.]+%)", out), dt))

    if args.docker:
        code, out, dt = run(["docker", "compose", "build"], timeout=1800)
        results.append(("Сборка образа", code == 0, "ok" if code == 0 else out[-300:], dt))
        compose = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.offline.yml"]
        run([*compose, "up", "-d", "--force-recreate"], timeout=600)
        code, out, dt = run([PY, "scripts/offline_check.py"], timeout=600)
        results.append(("Docker без сети", code == 0, last(r"(1000 записей за [\d.]+ с)", out), dt))
        run([*compose, "down"])
        run(["docker", "compose", "up", "-d"])

    print(f"\n{'проверка':<32}{'итог':<6}{'результат':<46}время")
    for name, ok, res, dt in results:
        print(f"{name:<32}{'OK' if ok else 'FAIL':<6}{str(res)[:45]:<46}{dt:.0f} с")
    sys.exit(0 if all(ok for _, ok, _, _ in results) else 1)


if __name__ == "__main__":
    main()
