"""
Генератор синтетических «безумных» адресов.

Это НЕ train.json и не оценка качества: строки собраны из шаблонов по паттернам,
перечисленным в задании (казахские ключевые слова, латиница, опечатки, двойные дроби,
невидимые символы, повтор города). Эталон известен по построению, поэтому генератор
годится для поиска падений парсера. Найденные баги закреплены в test_crazy.py.

    python tests/gen_crazy.py            # сводка по полям и первые расхождения
    python tests/gen_crazy.py --dump f   # сохранить строки с эталоном в JSON
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CITIES = ["Алматы", "Астана", "Шымкент", "Караганда", "Актобе", "Павлодар", "Атырау"]
STREETS = ["Абая", "Сатпаева", "Жандосова", "Толе би", "Кунаева", "Байтурсынова", "8 Марта"]
HOUSES = ["15", "12а", "12/1", "33/1б", "7", "105", "12/1/2"]

APT_KW = ["кв", "кв.", "квартира", "пәтер", "kv", "flat", "квартра", "кв-ра"]
ENT_KW = ["подъезд", "подьезд", "под.", "кіреберіс", "pod", "п-д"]
FLOOR_KW = ["этаж", "эт.", "этж", "қабат", "etazh"]
HOUSE_KW = ["д.", "дом", "үй", "dom", ""]
STREET_KW = ["ул.", "улица", "", "көшесі"]     # "көшесі" ставится после названия
INVIS = ["\u200b", "\ufeff", "\u2060", "\u200d"]   # невидимые; NBSP — это пробел, внутрь слова не вставляем


def _unit(kw: str, val: str, rnd: random.Random) -> str:
    if kw in ("үй", "пәтер", "кіреберіс", "қабат"):          # казахский порядок: число перед словом
        return f"{val} {kw}"
    sep = rnd.choice([" ", "", ". "]) if not kw.endswith(".") else rnd.choice(["", " "])
    return f"{kw}{sep}{val}" if kw else val


def make(rnd: random.Random) -> tuple[str, dict]:
    city = rnd.choice(CITIES)
    street = rnd.choice(STREETS)
    house = rnd.choice(HOUSES)
    apt = str(rnd.randint(1, 300)) + rnd.choice(["", "", "", "а"])
    ent = str(rnd.randint(1, 9)) if rnd.random() < 0.5 else None
    floor = str(rnd.randint(1, 20)) if rnd.random() < 0.5 else None
    gold = {"city": city, "street_name": street, "house": house, "apartment": apt,
            "entrance": ent, "floor": floor}

    skw = rnd.choice(STREET_KW)
    street_part = f"{street} {skw}" if skw == "көшесі" else (f"{skw} {street}" if skw else street)
    hkw = rnd.choice(HOUSE_KW)
    parts = [street_part, _unit(hkw, house, rnd)]
    if ent:
        parts.append(_unit(rnd.choice(ENT_KW), ent, rnd))
    if floor:
        parts.append(_unit(rnd.choice(FLOOR_KW), floor, rnd))
    parts.append(_unit(rnd.choice(APT_KW), apt, rnd))
    city_part = rnd.choice([f"г. {city}", f"г.{city}", city, f"{city} қаласы"])
    raw = ", ".join([city_part] + parts)
    if rnd.random() < 0.15:                                  # повтор города
        raw = f"{city}, {raw}"
    if rnd.random() < 0.2:                                   # невидимые символы
        i = rnd.randrange(len(raw))
        raw = raw[:i] + rnd.choice(INVIS) + raw[i:]
    return raw, gold


def generate(n: int = 250, seed: int = 2026) -> list[dict]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        raw, gold = make(rnd)
        out.append({"id": f"crazy_{i:03d}", "raw": raw, "gold": gold})
    return out


def run(cases: list[dict]) -> tuple[dict, list]:
    from address_parser import parse_address
    from evaluate import norm_value
    stats, bad = {}, []
    for c in cases:
        r = parse_address(c["raw"], c["id"])
        for f, v in c["gold"].items():
            ok = norm_value(r[f], strip_slash=False) == norm_value(v, strip_slash=False)
            s = stats.setdefault(f, [0, 0])
            s[0] += ok
            s[1] += 1
            if not ok:
                bad.append((c["id"], c["raw"], f, v, r[f]))
    return stats, bad


if __name__ == "__main__":
    cases = generate()
    if "--dump" in sys.argv:
        Path(sys.argv[sys.argv.index("--dump") + 1]).write_text(
            json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    stats, bad = run(cases)
    for f, (ok, n) in stats.items():
        print(f"{f:12s} {ok}/{n}  {100 * ok / n:.1f}%")
    print(f"расхождений: {len(bad)}")
    for b in bad[:40]:
        print(b)
