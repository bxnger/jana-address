"""КАТО — второй источник справочника и сверка с OpenStreetMap.

Источник: «Классификатор административно-территориальных объектов КАТО НК РК 11-2025»,
Бюро национальной статистики (stat.gov.kz), файл Excel. Скачивается один раз
(нужен интернет), дальше всё работает без сети:

    python scripts/build_kato.py                 # скачать (если нет) и встроить
    python scripts/build_kato.py --no-download   # из data/raw/kato/*.xlsx

Что делает:
  1. читает xlsx: код (te) и уровни ab/cd/ef/hij, название по-русски и по-казахски;
  2. раскладывает по видам: область, район, город, район города, населённый пункт
     (сельские округа и административные единицы «Г.А.» пропускаются);
  3. пишет таблицу `kato` в data/gazetteer.sqlite;
  4. сверяет с OSM и пишет отчёт docs/KATO_CROSSCHECK.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.text import norm  # noqa: E402

DB = ROOT / "data" / "gazetteer.sqlite"
SEED = ROOT / "data" / "seed" / "kz_admin.json"
RAW = ROOT / "data" / "raw" / "kato"
REPORT = ROOT / "docs" / "KATO_CROSSCHECK.md"
PAGE = "https://stat.gov.kz/ru/classifiers/statistical/21/"

_PREFIX = re.compile(r"^(г\.\s*а\.|г\.|с\.|п\.|пгт\.?|ст\.|рзд\.|уч\.|кр\.|отг\.|отд\.|жив\.|крес\.|маш\.)\s*", re.I)
_DISTRICT = re.compile(r"\s+район$", re.I)


def download() -> Path:
    import requests
    RAW.mkdir(parents=True, exist_ok=True)
    html = requests.get(PAGE, timeout=60).text
    m = re.search(r'href="(/upload/[^"]*КАТО[^"]*\.xlsx)"', html)
    if not m:
        sys.exit("на странице stat.gov.kz не найден Excel-файл КАТО")
    url = "https://stat.gov.kz" + m.group(1)
    out = RAW / Path(m.group(1)).name
    out.write_bytes(requests.get(url, timeout=120).content)
    print(f"[download] {url} -> {out}")
    return out


def latest_xlsx() -> Path:
    files = sorted(RAW.glob("*.xlsx"))
    if not files:
        sys.exit(f"нет файла КАТО в {RAW}; запустите без --no-download")
    return files[-1]


def read_rows(path: Path) -> list[tuple]:
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = [str(c).strip().lower() for c in rows[0]]
    need = ["te", "ab", "cd", "ef", "hij", "kaz_name", "rus_name"]
    if not all(n in head for n in need):
        sys.exit(f"неожиданная структура файла КАТО: {head}")
    idx = {n: head.index(n) for n in need}
    return [tuple(r[idx[n]] for n in need) for r in rows[1:] if r[idx["te"]]]


def clean(name: str) -> str:
    return re.sub(r"\s+", " ", _PREFIX.sub("", (name or "").strip())).strip()


def province_map(rows) -> dict[str, str]:
    """Код области (ab) -> каноническое имя из seed: по казахскому названию («Абай облысы»)."""
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    # «Алматы облысы» и «Алматы қ.» без суффикса совпадают — поэтому области и города респ. значения раздельно
    oblasts = {norm(re.sub(r"облысы$", "", p["kk"])): p["name"] for p in seed["provinces"] if p["kind"] == "oblast"}
    rep_cities = {norm(p["kk"]): p["name"] for p in seed["provinces"] if p["kind"] == "republican_city"}
    out = {}
    for _te, ab, cd, ef, _hij, kk, ru in rows:
        if str(cd) == "00" and str(ef) == "00":
            kk = (kk or "").strip()
            if re.search(r"облысы$", kk):
                out[str(ab)] = oblasts.get(norm(re.sub(r"облысы$", "", kk))) or ru
            else:
                out[str(ab)] = rep_cities.get(norm(re.sub(r"\bқ\.?$", "", kk))) or ru
    return out


def build(rows) -> list[dict]:
    prov = province_map(rows)
    level2 = {}           # (ab, cd) -> строка уровня района/города областного значения
    out = []
    for te, ab, cd, ef, hij, kk, ru in rows:
        ab, cd, ef, hij = str(ab), str(cd), str(ef), str(hij)
        ru = (ru or "").strip()
        rec = {"code": str(te), "name_raw": ru, "name_kk": (kk or "").strip(), "province": prov.get(ab),
               "district": None, "city": None, "kind": None}
        if cd == "00":
            rec["kind"] = "region"
            rec["name"] = prov.get(ab)
            if prov.get(ab) in ("Астана", "Алматы", "Шымкент"):
                rec["kind"], rec["city"] = "city", prov.get(ab)
        elif ef == "00" and hij == "000":
            level2[(ab, cd)] = ru
            if _DISTRICT.search(ru):
                rec["kind"], rec["name"] = "district", _DISTRICT.sub("", ru)
            else:
                continue                                   # «Семей Г.А.» — административная единица
        else:
            parent = level2.get((ab, cd), "")
            parent_district = _DISTRICT.sub("", parent) if _DISTRICT.search(parent) else None
            rec["district"] = parent_district
            if prov.get(ab) in ("Астана", "Алматы", "Шымкент"):
                rec["city"] = prov.get(ab)
            if _DISTRICT.search(ru):                       # район города: «Ауэзовский район»
                rec["kind"], rec["name"] = "city_district", _DISTRICT.sub("", ru)
                rec["city"] = rec["city"] or clean(parent.replace("Г.А.", "")) or None
            elif re.match(r"(?i)г\.(?!\s*а\.)", ru):
                rec["kind"], rec["name"] = "city", clean(ru)
            elif re.search(r"(?i)\b(с\.о\.?|п\.о\.?|а\.о\.?|г\.а\.?)$", ru) or "Г.А." in ru:
                continue                                   # сельский округ / администрация
            elif hij != "000":
                rec["kind"], rec["name"] = "village", clean(ru)
            else:
                continue
        if rec.get("name"):
            out.append(rec)
    return out


def save(recs, db: Path, source: str):
    con = sqlite3.connect(db)
    con.executescript("""
        DROP TABLE IF EXISTS kato;
        CREATE TABLE kato(code TEXT, kind TEXT, name TEXT, name_raw TEXT, name_kk TEXT,
                          province TEXT, district TEXT, city TEXT);
    """)
    con.executemany("INSERT INTO kato VALUES (?,?,?,?,?,?,?,?)",
                    [(r["code"], r["kind"], r["name"], r["name_raw"], r["name_kk"], r["province"], r["district"],
                      r["city"]) for r in recs])
    con.execute("INSERT OR REPLACE INTO meta VALUES ('kato_source', ?)", (source,))
    con.commit()
    con.close()


def crosscheck(recs, db: Path) -> str:
    """Сверка КАТО с OSM: что есть в обоих источниках и где расходятся названия."""
    con = sqlite3.connect(db)
    osm_places = defaultdict(set)
    for name, prov in con.execute("SELECT name, province FROM places"):
        osm_places[norm(name)].add(prov)
    osm_districts = {norm(n) for (n,) in con.execute("SELECT name FROM districts")}
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    seed_cities = {norm(c["name"]) for c in seed["cities"]}
    for c in seed["cities"]:
        seed_cities |= {norm(a) for a in c.get("alt", [])}
    con.close()

    def found_place(r):
        return norm(r["name"]) in osm_places or norm(r["name"]) in seed_cities

    def stem(s):
        return re.sub(r"(ский|цкий|ий|ый)$", "", norm(s))

    lines = ["# Сверка справочников: КАТО ↔ OpenStreetMap", "",
             f"Сгенерировано `scripts/build_kato.py` {dt.date.today():%d.%m.%Y}. КАТО — официальный классификатор "
             "(stat.gov.kz), OSM — наш основной справочник улиц, районов и зданий.", "",
             "| Что | В КАТО | Есть и в OSM / seed | Покрытие |", "|---|---|---|---|"]
    misses = {}
    for kind, title in [("city", "Города"), ("district", "Районы областей"), ("city_district", "Районы городов"),
                        ("village", "Сёла и посёлки")]:
        items = [r for r in recs if r["kind"] == kind]
        if kind in ("district", "city_district"):
            ok = [r for r in items if norm(r["name"]) in osm_districts or stem(r["name"]) in
                  {stem(d) for d in osm_districts}]
        else:
            ok = [r for r in items if found_place(r)]
        miss = [r for r in items if r not in ok]
        misses[title] = miss
        share = len(ok) / len(items) if items else 0
        lines.append(f"| {title} | {len(items)} | {len(ok)} | {share:.0%} |")
    lines += ["", "## Чего нет в OSM (первые 25 в каждой группе)", ""]
    for title, miss in misses.items():
        if miss and title != "Сёла и посёлки":
            names = ", ".join(f"{r['name']} ({r['province']})" for r in miss[:25])
            lines += [f"**{title}:** {names}", ""]
    lines += ["## Как используется", "",
              "- города и районы из КАТО дополняют справочник ядра (сёла — нет: их названия часто совпадают "
              "с улицами, и ядро принимало бы улицу за село);",
              "- в объяснениях разбора у города и района стоит отметка «сверено с КАТО», если название есть в КАТО;",
              "- кандидаты при опечатке в названии города ищутся и по КАТО."]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()
    path = latest_xlsx() if args.no_download or list(RAW.glob("*.xlsx")) else download()
    rows = read_rows(path)
    recs = build(rows)
    source = f"КАТО НК РК 11-2025, stat.gov.kz ({path.name})"
    save(recs, DB, source)
    counts = defaultdict(int)
    for r in recs:
        counts[r["kind"]] += 1
    REPORT.write_text(crosscheck(recs, DB), encoding="utf-8")
    print(f"[done] {len(rows)} строк КАТО -> {dict(counts)}; отчёт: {REPORT}")


if __name__ == "__main__":
    main()
