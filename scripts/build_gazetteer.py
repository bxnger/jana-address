"""Сборка справочника из OpenStreetMap (дамп Geofabrik по Казахстану) в SQLite.

Запуск (нужен интернет только для скачивания дампа):
    docker compose --profile build run --rm gazetteer-builder
или локально:
    pip install -r requirements-build.txt
    python scripts/build_gazetteer.py

Что извлекаем:
  * admin_level=4  — области и города респ. значения (полигоны)
  * admin_level=6..9 с «район/ауданы» — районы (полигоны)
  * place=city/town/village (+ полигоны городов) — населённые пункты
  * place=suburb/neighbourhood/quarter, landuse=residential с именем — микрорайоны и ЖК
  * highway=* с именем — улицы (средняя точка линии)
  * addr:street + addr:housenumber — здания (точный район для пары «улица + дом»)
Затем каждой улице и микрорайону назначаются область, город и район
через point-in-polygon (shapely STRtree).
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import osmium
import requests
from shapely import wkb
from shapely.geometry import Point
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
PBF_URL = "https://download.geofabrik.de/asia/kazakhstan-latest.osm.pbf"

STREET_HIGHWAYS = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "living_street", "pedestrian", "service", "road",
    "trunk_link", "primary_link", "secondary_link", "tertiary_link",
}
SETTLEMENTS = {"city", "town", "village", "hamlet"}
# Названия областей в OSM, которые расходятся с каноном ядра парсера
PROVINCE_FIX = {"Улутауская область": "Улытауская область"}
LIVING = {"suburb", "neighbourhood", "quarter"}

# Тип улицы из названия OSM: (регулярка, канонический тип). Порядок важен.
STREET_TYPES = [
    (r"тұйық көшесі|тупик", "тупик"),
    (r"шағын ауданы|микрорайон|мкр\.?|мкрн\.?", "микрорайон"),
    (r"даңғылы|проспект|пр-т\.?|просп\.?", "проспект"),
    (r"көшесі|улица|ул\.?", "улица"),
    (r"тас жолы|шоссе", "шоссе"),
    (r"бульвары|бульвар", "бульвар"),
    (r"переулок|пер\.?|қиылысы", "переулок"),
    (r"проезд|өткелі", "проезд"),
    (r"алаңы|площадь", "площадь"),
    (r"жағалауы|набережная", "набережная"),
    (r"квартал|орамы", "квартал"),
    (r"трасса|тракт", "трасса"),
]
_TYPE_RE = [(re.compile(rf"(?i)(^|\s)({p})(\s|$)"), t) for p, t in STREET_TYPES]


def split_street(name: str) -> tuple[str, str | None]:
    """'улица Абая' -> ('Абая', 'улица'); 'Абай даңғылы' -> ('Абай', 'проспект')."""
    for rx, t in _TYPE_RE:
        m = rx.search(name)
        if m:
            bare = (name[: m.start(2)] + name[m.end(2):]).strip(" ,.")
            if bare:
                return re.sub(r"\s+", " ", bare), t
    return name.strip(), None


def house_key(h: str | None) -> str | None:
    """'12 А' -> '12а', '33/1' -> '33/1'. Списки и диапазоны («12;14», «12-14») не берём.
    Та же нормализация — в app/gazetteer.py (house_key)."""
    if not h:
        return None
    h = re.sub(r"\s+", "", h.lower().replace("ё", "е"))
    return h if re.fullmatch(r"\d{1,4}[а-яa-z]?(/\d{1,4}[а-яa-z]?)?", h) else None


def ru_name(tags) -> str | None:
    return tags.get("name:ru") or tags.get("name")


def clean_district(name: str) -> str:
    return re.sub(r"(?i)\s*(район|ауданы|р-н)\s*", " ", name).strip()


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.wkbf = osmium.geom.WKBFactory()
        self.admin = []        # (level, name, name_kk, geom)
        self.city_polys = []   # (name, geom)
        self.living_polys = [] # (name, kind, geom)
        self.places = []       # (name, name_kk, kind, Point, population)
        self.living_nodes = [] # (name, kind, Point)
        self.streets = []      # (osm_name, Point)
        self.buildings = []    # (addr:street, house, Point)

    def _building(self, tags, pt):
        street, house = tags.get("addr:street"), house_key(tags.get("addr:housenumber"))
        if street and house:
            self.buildings.append((street, house, pt))

    def node(self, n):
        if "addr:housenumber" in n.tags:
            self._building(n.tags, Point(n.location.lon, n.location.lat))
        place = n.tags.get("place")
        if not place:
            return
        name = ru_name(n.tags)
        if not name:
            return
        pt = Point(n.location.lon, n.location.lat)
        if place in SETTLEMENTS:
            pop = n.tags.get("population", "0")
            pop = int(pop) if pop.isdigit() else 0
            self.places.append((name, n.tags.get("name:kk"), place, pt, pop))
        elif place in LIVING:
            self.living_nodes.append((name, place, pt))

    def way(self, w):
        if "addr:housenumber" in w.tags and len(w.nodes) > 0:
            # Здание: центр по вершинам контура (полигон не собираем — быстрее)
            try:
                xs = [nd.location.lon for nd in w.nodes]
                ys = [nd.location.lat for nd in w.nodes]
                self._building(w.tags, Point(sum(xs) / len(xs), sum(ys) / len(ys)))
            except osmium.InvalidLocationError:
                pass
        hw = w.tags.get("highway")
        if hw not in STREET_HIGHWAYS:
            return
        name = ru_name(w.tags)
        if not name or len(w.nodes) == 0:
            return
        try:
            loc = w.nodes[len(w.nodes) // 2].location
            self.streets.append((name, Point(loc.lon, loc.lat)))
        except osmium.InvalidLocationError:
            pass

    def area(self, a):
        tags = a.tags
        name = ru_name(tags)
        if not name:
            return
        try:
            geom = wkb.loads(self.wkbf.create_multipolygon(a), hex=True)
        except Exception:
            return
        if tags.get("boundary") == "administrative" and tags.get("admin_level", "").isdigit():
            lvl = int(tags["admin_level"])
            if 4 <= lvl <= 9:
                self.admin.append((lvl, name, tags.get("name:kk"), geom))
        place = tags.get("place")
        if place in ("city", "town"):
            self.city_polys.append((name, geom))
        elif place in LIVING:
            self.living_polys.append((name, place, geom))
        elif tags.get("landuse") == "residential":
            self.living_polys.append((name, "residential", geom))


class PolyIndex:
    """Быстрый point-in-polygon: возвращает самый маленький содержащий полигон."""

    def __init__(self, items):  # items: [(payload, geom)]
        self.items = items
        self.tree = STRtree([g for _, g in items]) if items else None

    def find(self, pt):
        if not self.tree:
            return None
        hits = [i for i in self.tree.query(pt) if self.items[i][1].contains(pt)]
        if not hits:
            return None
        return self.items[min(hits, key=lambda i: self.items[i][1].area)][0]


def download(dest: Path):
    if dest.exists() and dest.stat().st_size > 10_000_000:
        print(f"[skip] дамп уже есть: {dest} ({dest.stat().st_size // 2**20} MB)")
        return
    print(f"[download] {PBF_URL}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(PBF_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(dest.with_suffix(".part"), "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                print(f"\r  {done // 2**20}/{total // 2**20} MB", end="", flush=True)
    dest.with_suffix(".part").rename(dest)
    print()


def build(pbf: Path, out: Path):
    t0 = time.time()
    c = Collector()
    print("[osm] читаем дамп (2 прохода, несколько минут)…")
    c.apply_file(str(pbf), locations=True, idx="flex_mem")
    print(f"[osm] admin={len(c.admin)} places={len(c.places)} living={len(c.living_polys)}+{len(c.living_nodes)} "
          f"street_segments={len(c.streets)}  ({time.time() - t0:.0f}s)")

    provinces = PolyIndex([(PROVINCE_FIX.get(name, name), g) for lvl, name, _, g in c.admin if lvl == 4])
    district_items = [((clean_district(name), kk and clean_district(kk)), g)
                      for lvl, name, kk, g in c.admin
                      if lvl >= 6 and re.search(r"(?i)район|ауданы", name)]
    districts = PolyIndex(district_items)
    city_names = {p[0] for p in c.places if p[2] in ("city", "town")}
    # Полигоны городов: place=city/town + административные границы с именем города
    city_polys = PolyIndex(c.city_polys + [(name, g) for lvl, name, _, g in c.admin
                                           if lvl >= 6 and name in city_names])
    living_polys = PolyIndex([((n, k), g) for n, k, g in c.living_polys])

    settle_pts = [p for p in c.places]
    settle_tree = STRtree([p[3] for p in settle_pts])
    radius = {"city": 0.15, "town": 0.07, "village": 0.03, "hamlet": 0.015}  # градусы ~ 15/7/3/1.5 км

    def nearest_settlement(pt):
        best = None
        for i in settle_tree.query(pt.buffer(0.15)):
            name, _, kind, ppt, pop = settle_pts[i]
            d = pt.distance(ppt)
            if d <= radius[kind]:
                key = (d / radius[kind], -pop)
                if best is None or key < best[0]:
                    best = (key, name)
        return best[1] if best else None

    def city_of(pt):
        return city_polys.find(pt) or nearest_settlement(pt)

    # Город респ. значения сам является «областью»
    def locate(pt):
        prov = provinces.find(pt)
        d = districts.find(pt)
        return prov, city_of(pt), d[0] if d else None

    con = sqlite3.connect(out.with_suffix(".tmp"))
    con.executescript("""
        DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS districts;
        DROP TABLE IF EXISTS living_areas; DROP TABLE IF EXISTS streets; DROP TABLE IF EXISTS meta;
        DROP TABLE IF EXISTS buildings;
        CREATE TABLE buildings(street TEXT, street_type TEXT, house TEXT, city TEXT, district TEXT,
                               living_area TEXT, province TEXT, lat REAL, lon REAL);
        CREATE TABLE places(name TEXT, name_kk TEXT, kind TEXT, province TEXT, district TEXT,
                            lat REAL, lon REAL, population INT);
        CREATE TABLE districts(name TEXT, name_kk TEXT, city TEXT, province TEXT);
        CREATE TABLE living_areas(name TEXT, kind TEXT, city TEXT, district TEXT, province TEXT, lat REAL, lon REAL);
        CREATE TABLE streets(name TEXT, street_type TEXT, osm_name TEXT, city TEXT, district TEXT,
                             living_area TEXT, province TEXT, segments INT, lat REAL, lon REAL);
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    """)

    for name, kk, kind, pt, pop in c.places:
        prov, _, dist = locate(pt)
        con.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?)",
                    (name, kk, kind, prov, dist, pt.y, pt.x, pop))

    for (name, kk), g in district_items:
        rp = g.representative_point()
        prov = provinces.find(rp)
        city = city_polys.find(rp)
        # Район внутри города респ. значения: город = область
        if prov in ("Алматы", "Астана", "Шымкент"):
            city = prov
        con.execute("INSERT INTO districts VALUES (?,?,?,?)", (name, kk, city, prov))

    seen_living = set()
    for name, kind, g in c.living_polys:
        rp = g.representative_point()
        prov, city, dist = locate(rp)
        if (name, city) not in seen_living:
            seen_living.add((name, city))
            con.execute("INSERT INTO living_areas VALUES (?,?,?,?,?,?,?)",
                        (name, kind, city, dist, prov, rp.y, rp.x))
    for name, kind, pt in c.living_nodes:
        prov, city, dist = locate(pt)
        if (name, city) not in seen_living:
            seen_living.add((name, city))
            con.execute("INSERT INTO living_areas VALUES (?,?,?,?,?,?,?)",
                        (name, kind, city, dist, prov, pt.y, pt.x))

    # Улицы: сегменты одной улицы схлопываем по (имя, город, район)
    agg: dict[tuple, list] = defaultdict(list)
    for i, (osm_name, pt) in enumerate(c.streets):
        prov, city, dist = locate(pt)
        if not city:
            continue
        la = living_polys.find(pt)
        agg[(osm_name, city, dist)].append((pt, la[0] if la else None, prov))
        if i % 100_000 == 0:
            print(f"\r[geo] улицы: {i}/{len(c.streets)}", end="", flush=True)
    print()
    for (osm_name, city, dist), pts in agg.items():
        bare, stype = split_street(osm_name)
        las = Counter(p[1] for p in pts if p[1])
        prov = Counter(p[2] for p in pts).most_common(1)[0][0]
        mid = pts[len(pts) // 2][0]
        con.execute("INSERT INTO streets VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (bare, stype, osm_name, city, dist, las.most_common(1)[0][0] if las else None,
                     prov, len(pts), mid.y, mid.x))

    # Здания с адресом: точный район и мкр для пары «улица + дом»
    seen_b = set()
    for i, (street, house, pt) in enumerate(c.buildings):
        bare, stype = split_street(street)
        prov, city, dist = locate(pt)
        if not city or (bare, house, city) in seen_b:
            continue
        seen_b.add((bare, house, city))
        la = living_polys.find(pt)
        con.execute("INSERT INTO buildings VALUES (?,?,?,?,?,?,?,?,?)",
                    (bare, stype, house, city, dist, la[0] if la else None, prov, pt.y, pt.x))
        if i % 100_000 == 0:
            print(f"\r[geo] здания: {i}/{len(c.buildings)}", end="", flush=True)
    print()
    con.execute("CREATE INDEX ix_buildings ON buildings(city, street, house)")

    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("source", PBF_URL), ("pbf_file", pbf.name),
        ("built_at", dt.datetime.now().isoformat(timespec="seconds")),
    ])
    con.execute("CREATE INDEX ix_streets_city ON streets(city, name)")
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("places", "districts", "living_areas", "streets", "buildings")}
    con.close()
    out.with_suffix(".tmp").replace(out)
    print(f"[done] {out}  {counts}  ({time.time() - t0:.0f}s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", type=Path, default=ROOT / "data" / "raw" / "kazakhstan-latest.osm.pbf")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "gazetteer.sqlite")
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()
    if not args.no_download:
        download(args.pbf)
    if not args.pbf.exists():
        sys.exit(f"нет дампа {args.pbf}")
    build(args.pbf, args.out)
    # Здания: в рабочий справочник — только полезные, полная таблица — в data/raw/
    sys.path.insert(0, str(ROOT))
    from scripts.compact_buildings import main as compact
    compact(args.out)


if __name__ == "__main__":
    main()
