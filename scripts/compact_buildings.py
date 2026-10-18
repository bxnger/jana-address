"""Ужимает здания OSM до того, что реально нужно при разборе.

    python scripts/compact_buildings.py        (build_gazetteer.py вызывает его сам)

Полная таблица `buildings` (≈1,2 млн зданий, ≈200 МБ) уходит в
data/raw/buildings_full.sqlite — она нужна только генератору синтетики.
В data/gazetteer.sqlite остаётся `buildings_idx`: дома только там, где улица
сама по себе район не даёт (проходит через несколько районов, или в городе
несколько улиц с таким названием, или её нет в справочнике улиц). Тип улицы
сохраняется: «проспект Абая 10» и «улица Абая 10» — разные здания. Ключ улицы
нормализован заранее (app.text.norm), чтобы старт API не тратил на это время.
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.text import norm  # noqa: E402

DB = ROOT / "data" / "gazetteer.sqlite"
FULL = ROOT / "data" / "raw" / "buildings_full.sqlite"


def main(db: Path = DB, full: Path = FULL):
    con = sqlite3.connect(db)
    fresh = con.execute("SELECT 1 FROM sqlite_master WHERE name='buildings'").fetchone()
    if fresh:
        # Свежая сборка: полную таблицу — в отдельный файл для генератора синтетики
        full.parent.mkdir(parents=True, exist_ok=True)
        full.unlink(missing_ok=True)
        con.execute("ATTACH DATABASE ? AS full", (str(full),))
        con.execute("CREATE TABLE full.buildings AS SELECT * FROM buildings")
        con.commit()
        con.execute("DETACH DATABASE full")
        con.execute("DROP TABLE buildings")
        con.commit()
    elif not full.exists():
        print("[skip] зданий нет ни в справочнике, ни в", full)
        return
    street_districts = defaultdict(set)
    for name, city, district in con.execute("SELECT name, city, district FROM streets"):
        street_districts[(city, norm(name))].add(district)

    rows = sqlite3.connect(full).execute(
        "SELECT street, street_type, house, city, district FROM buildings").fetchall()
    kept = []
    for street, stype, house, city, district in rows:
        if not district:
            continue
        key = norm(street)
        if street_districts.get((city, key)) == {district}:
            continue  # улица с таким названием целиком в одном районе — дом ничего не добавляет
        kept.append((city, key, stype, house, district))

    con.executescript("""
        DROP TABLE IF EXISTS buildings_idx;
        CREATE TABLE buildings_idx(city TEXT, street_key TEXT, street_type TEXT, house TEXT, district TEXT);
    """)
    con.executemany("INSERT INTO buildings_idx VALUES (?,?,?,?,?)", kept)
    con.commit()
    con.execute("VACUUM")
    con.close()
    print(f"[done] зданий всего {len(rows)}, в рабочем справочнике {len(kept)}; "
          f"{db.name} = {db.stat().st_size / 2**20:.1f} МБ, полная таблица -> {full}")


if __name__ == "__main__":
    main()
