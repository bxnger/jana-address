"""Экспорт нашего справочника (SQLite из OSM + seed) в формат ядра парсера (core/gazetteer.py).

    python scripts/export_core_gazetteer.py   ->  data/core_gazetteer.json

Парсер считает городом любой сегмент без цифр, если тот есть в справочнике,
поэтому всё неоднозначное отсеиваем:
  * сёла, чьё имя совпадает с микрорайоном, улицей или районом, а также сёла
    с одинаковым именем в разных областях;
  * хутора (hamlet) не берём вовсе;
  * микрорайоны, чьё имя является частой улицей (есть в 3 и более городах).

Дополнительный ключ `streets` ({город: {улица: тип}}) текущее ядро игнорирует;
он нужен для is_known_street().
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.text import clean_living_area as clean_la  # noqa: E402

DB = ROOT / "data" / "gazetteer.sqlite"
SEED = ROOT / "data" / "seed" / "kz_admin.json"
OUT = ROOT / "data" / "core_gazetteer.json"

REPUBLICAN = {"Алматы", "Астана", "Шымкент"}
PROVINCE_FIX = {"Улутауская область": "Улытауская область"}
# Старые названия, которые совпадают с улицей, областью или районом: в алиасы не берём,
# иначе «ул. Шевченко» стала бы городом Актау
AMBIGUOUS_OLD_NAMES = {"Шевченко", "Акмола", "Целиноград"}


def key(s: str) -> str:
    return s.lower().replace("ё", "е").strip()


def main():
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    con = sqlite3.connect(DB)

    def fixp(p):
        return PROVINCE_FIX.get(p, p)

    street_cities = defaultdict(set)
    for name, city in con.execute("SELECT name, city FROM streets"):
        street_cities[key(name)].add(city)

    # --- районы ---
    city_districts: dict[str, str] = {}
    rural = defaultdict(set)
    for d in seed["districts"]:
        if d.get("city"):
            city_districts[d["name"]] = d["city"]
        else:
            rural[d["name"]].add(d["province"])
    for name, prov in con.execute("SELECT name, province FROM districts"):
        prov = fixp(prov)
        if prov in REPUBLICAN:
            city_districts.setdefault(name, prov)
        elif prov and re.search(r"(ский|цкий)$", name):
            rural[name].add(prov)
    rural_districts = {n: (next(iter(p)) if len(p) == 1 else None)
                       for n, p in rural.items() if n not in city_districts}
    district_keys = {key(n) for n in [*city_districts, *rural_districts]}

    # --- микрорайоны / ЖК ---
    la_district = defaultdict(set)
    living = set()
    for name, dist in con.execute("SELECT name, district FROM living_areas WHERE city IS NOT NULL"):
        n = clean_la(name)
        if len(n) < 4 or re.fullmatch(r"[\d\W]+", n) or len(street_cities.get(key(n), ())) >= 3:
            continue
        living.add(n)
        if dist:
            la_district[n].add(dist)
    living_keys = {key(n) for n in living}

    # --- населённые пункты ---
    cities: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for c in seed["cities"]:
        cities[c["name"]] = c["province"]
        # Казахское написание и старые названия приводим к текущему, как в 2ГИС
        # (ответ организаторов): «Нур-Султан» -> «Астана», «Семипалатинск» -> «Семей»
        if c.get("kk") and key(c["kk"]) != key(c["name"]):
            aliases[c["kk"]] = c["name"]
        for a in c.get("alt", []):
            if a not in AMBIGUOUS_OLD_NAMES:
                aliases[a] = c["name"]
    # КАТО (официальный классификатор): города и районы, которых нет в OSM (Байконыр, Уилский район…).
    # Сёла из КАТО в ядро не идут — их названия часто совпадают с улицами.
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='kato'").fetchone():
        for kind, name, prov in con.execute("SELECT kind, name, province FROM kato WHERE kind IN ('city','district')"):
            if kind == "city" and name and prov:
                cities.setdefault(name, prov)
            elif kind == "district" and name and prov and name not in city_districts:
                rural.setdefault(name, set()).add(prov)
        rural_districts = {n: (next(iter(p)) if len(p) == 1 else None)
                           for n, p in rural.items() if n not in city_districts}
        district_keys = {key(n) for n in [*city_districts, *rural_districts]}
    village_prov = defaultdict(set)
    for name, kk, kind, prov in con.execute(
            "SELECT name, name_kk, kind, province FROM places WHERE province IS NOT NULL"):
        prov = fixp(prov)
        if kind in ("city", "town"):
            cities.setdefault(name, prov)
            if kk and key(kk) != key(name):
                aliases.setdefault(kk, name)
        elif kind == "village":
            village_prov[name].add(prov)
    skipped = 0
    for name, provs in village_prov.items():
        k = key(name)
        if name in cities:
            continue
        if len(provs) > 1 or k in living_keys or k in street_cities or k in district_keys or len(k) < 4:
            skipped += 1
            continue
        cities[name] = next(iter(provs))
    # Алиас, который сам — название другого населённого пункта, это не перевод, а другое место
    # («Тобыл» ≠ «Тобол», «Арал» ≠ «Аральск»): такие алиасы не экспортируем
    # (только города и посёлки: село-тёзка не повод терять «Орал» → Уральск)
    own_names = {key(n) for n, _, kind, _ in con.execute(
        "SELECT name, name_kk, kind, province FROM places WHERE kind IN ('city','town')")}
    dropped = [a for a, c in aliases.items() if key(a) in own_names and key(a) != key(c)]
    for a in dropped:
        del aliases[a]
    # Город-микрорайон: из списка мкр убираем названия городов
    city_keys = {key(c) for c in cities} | {key(a) for a in aliases}
    living = {n for n in living if key(n) not in city_keys}

    # Улицы по городам: {город: {название без типа: самый частый тип или null}}.
    # Для ядра: «есть ли такая улица в этом городе» и тип улицы, если в строке его нет.
    type_votes = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for name, stype, city, segs in con.execute("SELECT name, street_type, city, segments FROM streets"):
        if not re.search(r"[^\W\d_]{2,}", name):
            continue
        type_votes[city][name][stype] += segs or 1
    streets = {city: {n: max(v, key=lambda t: (t is not None, v[t])) for n, v in sorted(names.items())}
               for city, names in sorted(type_votes.items())}

    out = {
        "_meta": {"source": "OpenStreetMap (Geofabrik kazakhstan-latest) + seed/kz_admin.json",
                  "built_from": DB.name},
        "cities": dict(sorted(cities.items())),
        "city_aliases": dict(sorted(aliases.items())),
        "city_districts": dict(sorted(city_districts.items())),
        "rural_districts": dict(sorted(rural_districts.items())),
        "living_areas": sorted(living),
        "district_by_living_area": {n: next(iter(d)) for n, d in sorted(la_district.items())
                                    if len(d) == 1 and n in living},
        "streets": streets,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {OUT}: " + ", ".join(f"{k}={len(v)}" for k, v in out.items() if k != "_meta")
          + f" (street names={sum(map(len, streets.values()))}), villages skipped as ambiguous={skipped}"
          + f", aliases dropped as other places={len(dropped)} {dropped[:6]}"
          + f", {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
