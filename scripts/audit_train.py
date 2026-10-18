"""Аудит на реальных данных (train.json) — всё считается локально, данные никуда не уходят.

    python scripts/audit_train.py путь/к/train.json [--sample 40] [--out audit_report.md]

Эталона в train.json нет, поэтому скрипт не считает «точность». Он показывает, где парсер сомневается
или оставляет часть строки неразобранной — именно эти строки стоит просмотреть глазами и исправить.
Отчёт содержит куски реальных адресов: не публикуйте его и не кладите в репозиторий.
"""
import argparse
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import process_batch  # noqa: E402

FIELDS = ["province", "city", "district", "living_area", "street_name", "house", "apartment", "entrance",
          "floor", "landmark"]


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [data])
    return [r if isinstance(r, dict) else {"id": None, "raw_address": r} for r in data]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("train")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--out", default="audit_report.md")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    recs = load(Path(a.train))
    t = time.time()
    res = process_batch(recs, debug=True)
    dt = time.time() - t
    n = len(res)
    L = [f"# Аудит train.json: {n} записей, разбор {dt:.1f} с ({n / max(dt, 1e-9):.0f} зап./с)\n"]

    L.append("## Заполненность полей\n")
    L.append("| поле | заполнено |\n|---|---|")
    for f in FIELDS:
        k = sum(1 for r in res if r.get(f))
        L.append(f"| {f} | {k} ({100 * k / n:.1f}%) |")

    low = [r for r in res if (r.get("confidence") or 0) < 0.7]
    L.append(f"\nНеуверенных (<0.70): **{len(low)}** ({100 * len(low) / n:.1f}%)\n")

    left = [r for r in res if (r.get("_leftover") or "").strip()]
    L.append(f"Строк с неразобранным остатком: **{len(left)}** ({100 * len(left) / n:.1f}%)\n")
    cnt = Counter(r["_leftover"].strip().lower() for r in left)
    L.append("### Частые остатки (кандидаты на новые правила)\n")
    for s, c in cnt.most_common(40):
        L.append(f"- `{s}` — {c}")

    flags = Counter(f for r in res for f in (r.get("_flags") or []))
    L.append("\n### Флаги\n")
    for f, c in flags.most_common(25):
        L.append(f"- {f}: {c}")

    for title, cond in (("нет улицы", lambda r: not r.get("street_name")),
                        ("нет дома", lambda r: not r.get("house")),
                        ("нет города", lambda r: not r.get("city"))):
        bad = [r for r in res if cond(r)]
        L.append(f"\n## {title}: {len(bad)} ({100 * len(bad) / n:.1f}%) — первые 15\n")
        for r in bad[:15]:
            L.append(f"- `{r['raw_address']}`")

    rnd = random.Random(a.seed)
    L.append(f"\n## Случайные {a.sample} записей для ручной проверки\n")
    L.append("Отметьте, где разбор неверный: это и есть ваша реальная оценка точности.\n")
    for r in rnd.sample(res, min(a.sample, n)):
        parts = {f: r.get(f) for f in FIELDS if r.get(f)}
        L.append(f"- `{r['raw_address']}`\n  - {json.dumps(parts, ensure_ascii=False)} · conf {r.get('confidence')}")

    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:30]))
    print(f"\nПолный отчёт: {a.out}")


if __name__ == "__main__":
    main()
