"""Справочник: seed JSON + SQLite из OpenStreetMap, поиск с rapidfuzz.

Всё грузится в память один раз при старте (несколько секунд), дальше —
только словари и rapidfuzz, без обращений к сети.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process

from .text import clean_living_area, house_key, norm

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("JANA_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
SEED_PATH = DATA_DIR / "seed" / "kz_admin.json"
DB_PATH = DATA_DIR / "gazetteer.sqlite"

_NUM = re.compile(r"\d+")
_DISTRICT_SUFFIX = re.compile(r"(ский|цкий|ской|ский район|ий|ый)$")


def _stem(key: str) -> str:
    """'ауэзовский' -> 'ауэзов', чтобы ловить и 'Ауэзов ауданы'."""
    return _DISTRICT_SUFFIX.sub("", key) if len(key) > 6 else key


@dataclass
class _Index:
    """Ключ (нормализованная строка) -> запись; плюс список ключей для fuzzy."""
    by_key: dict = field(default_factory=dict)

    def add(self, names, record):
        for n in names:
            if n:
                k = norm(n)
                if k:
                    self.by_key.setdefault(k, record)

    def lookup(self, text: str, cutoff: int = 90):
        k = norm(text)
        if not k:
            return None, 0.0
        if k in self.by_key:
            return self.by_key[k], 1.0
        if len(k) < 4 or not self.by_key:
            return None, 0.0
        hit = process.extractOne(k, self.by_key.keys(), scorer=fuzz.ratio, score_cutoff=cutoff)
        if hit:
            return self.by_key[hit[0]], hit[1] / 100
        return None, 0.0


class Gazetteer:
    def __init__(self, seed_path: Path = SEED_PATH, db_path: Path = DB_PATH):
        self.provinces = _Index()
        self.cities = _Index()
        self.districts: dict[str | None, _Index] = defaultdict(_Index)  # city/province -> index
        self.districts_all = _Index()
        self.living_areas: dict[str | None, _Index] = defaultdict(_Index)
        self.living_areas_all = _Index()
        self.streets: dict[str, dict[str, list[dict]]] = defaultdict(dict)  # city -> key -> [rec]
        # (город, улица, дом) и (город, улица, номер без литеры/дроби) -> ((тип улицы, район), ...)
        # Несколько пар — когда в городе есть «проспект Абая» и «улица Абая» с одинаковыми номерами
        self.buildings: dict[tuple, tuple] = {}
        self.buildings_base: dict[tuple, tuple] = {}
        self.kato_places: set[str] = set()
        self.kato_districts: set[str] = set()
        self.sources: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)
        self._load_seed(seed_path)
        if db_path.exists():
            self._load_db(db_path)
        else:
            log.warning("gazetteer.sqlite не найден (%s) — работаем только на seed", db_path)

    # ---------- загрузка ----------
    def _load_seed(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        for p in data["provinces"]:
            rec = {"name": p["name"], "kind": p["kind"]}
            short = p["name"].replace(" область", "")
            self.provinces.add([p["name"], short, p["kk"], p["kk"].replace(" облысы", "")], rec)
            self.counts["provinces"] += 1
        for c in data["cities"]:
            rec = {"name": c["name"], "province": c["province"]}
            self.cities.add([c["name"], c["kk"], *c.get("alt", [])], rec)
            self.counts["cities"] += 1
        for d in data["districts"]:
            self._add_district(d["name"], d.get("kk"), d.get("city"), d.get("province"), d.get("alt", []))
        for la in data.get("living_areas", []):
            self._add_living_area(la["name"], la.get("city"), la.get("district"))
        self.sources.append(f"seed:{path.name}")

    def _load_db(self, path: Path):
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        for r in con.execute("SELECT name, name_kk, province FROM places WHERE province IS NOT NULL"):
            self.cities.add([r["name"], r["name_kk"]], {"name": r["name"], "province": r["province"]})
            self.counts["cities"] += 1
        for r in con.execute("SELECT name, name_kk, city, province FROM districts"):
            self._add_district(r["name"], r["name_kk"], r["city"], r["province"], [])
        for r in con.execute("SELECT name, city, district FROM living_areas"):
            self._add_living_area(r["name"], r["city"], r["district"])
        for r in con.execute("SELECT name, street_type, city, district, living_area FROM streets"):
            self._add_street(dict(r))
        if _has_table(con, "buildings_idx"):
            # Только дома, которые дают больше, чем улица (scripts/compact_buildings.py);
            # ключ улицы уже нормализован при сборке
            intern = {}  # одна строка на город/тип/район — экономит память
            for city, skey, stype, house, district in con.execute(
                    "SELECT city, street_key, street_type, house, district FROM buildings_idx"):
                pair = intern.setdefault((stype, district), (stype, district))
                city = intern.setdefault(city, city)
                for d, key in ((self.buildings, (city, skey, house)),
                               (self.buildings_base, (city, skey, _NUM.match(house).group(0)))):
                    cur = d.get(key, ())
                    if pair not in cur:
                        d[key] = cur + (pair,)
            self.counts["buildings"] = len(self.buildings)
        if _has_table(con, "kato"):
            # КАТО (stat.gov.kz) — второй источник: сверка названий и населённые пункты, которых нет в OSM
            for kind, name, prov in con.execute("SELECT kind, name, province FROM kato"):
                if kind in ("city", "village") and name:
                    self.kato_places.add(norm(name))
                    self.cities.add([name], {"name": name, "province": prov})   # не перекрывает OSM/seed
                elif kind in ("district", "city_district") and name:
                    self.kato_districts.add(norm(name))
                    self.kato_districts.add(_stem(norm(name)))
                    self.counts["kato_districts"] += 1
            self.counts["kato_places"] = len(self.kato_places)
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall()) if _has_table(con, "meta") else {}
        con.close()
        if meta.get("kato_source"):
            self.sources.append(meta["kato_source"])
        self.sources.append(f"osm:{meta.get('source', path.name)} ({meta.get('built_at', '?')})")

    def _add_district(self, name, kk, city, province, alt):
        rec = {"name": name, "city": city, "province": province}
        names = [name, kk, *alt]
        names += [_stem(norm(n)) for n in names if n]
        self.districts[city or province].add(names, rec)
        self.districts_all.add(names, rec)
        self.counts["districts"] += 1

    def _add_living_area(self, name, city, district):
        short = clean_living_area(name)
        rec = {"name": short, "city": city, "district": district}
        self.living_areas[city].add([name, short], rec)
        self.living_areas_all.add([name, short], rec)
        self.counts["living_areas"] += 1

    def _add_street(self, rec):
        if not rec.get("city"):
            return
        self.streets[rec["city"]].setdefault(norm(rec["name"]), []).append(rec)
        self.counts["streets"] += 1

    # ---------- поиск ----------
    def find_province(self, text: str):
        rec, score = self.provinces.lookup(re.sub(r"(?i)\b(обл(асть|\.)?|облысы)\b", "", text))
        return (rec["name"], score) if rec else (None, 0.0)

    def find_city(self, text: str):
        """-> (city, province, score)"""
        t = re.sub(r"(?i)\b(город|гор|г|қаласы|қ|пос[её]лок|пос|п|село|с|ауыл|ау|пгт)\b\.?", " ", text)
        rec, score = self.cities.lookup(t)
        return (rec["name"], rec["province"], score) if rec else (None, None, 0.0)

    def find_district(self, text: str, city: str | None = None, province: str | None = None):
        t = re.sub(r"(?i)\b(район|р-н|р-он|ауданы|ауд)\b\.?", " ", text)
        for scope in (city, province):
            if scope and scope in self.districts:
                rec, score = self.districts[scope].lookup(t)
                if not rec:
                    rec, score = self.districts[scope].lookup(_stem(norm(t)))
                if rec:
                    return rec, score
        if city or province:
            return None, 0.0
        rec, score = self.districts_all.lookup(t, cutoff=95)
        return rec, score

    def find_living_area(self, text: str, city: str | None = None):
        idx = self.living_areas.get(city) if city else self.living_areas_all
        if not idx:
            return None, 0.0
        rec, score = idx.lookup(text, cutoff=92)
        return (rec, score) if rec else (None, 0.0)

    def building_info(self, street: str | None, house: str | None, city: str | None,
                      street_type: str | None = None):
        """Здание по адресу -> {'district', 'exact'} или None.

        Сначала точный номер дома («12а»), затем номер без литеры и дроби («12а» -> «12»).
        Если в строке указан тип улицы, здания с улицы другого типа не подходят
        («пр. Абая 10» не возьмёт район «ул. Абая 10»). Район ставится, только если
        все подходящие здания в одном районе.
        """
        h = house_key(house)
        if not (street and h and city):
            return None
        s = norm(street)
        if not street_type:
            # «Абая 10» без типа в городе, где есть и проспект, и улица Абая, — не угадываем
            types = {r.get("street_type") for r in self.streets.get(city, {}).get(s, [])} - {None}
            if len(types) > 1:
                return None
        for exact, pairs in ((True, self.buildings.get((city, s, h))),
                             (False, self.buildings_base.get((city, s, _NUM.match(h).group(0))))):
            if not pairs:
                continue
            if street_type:
                pairs = [p for p in pairs if p[0] in (street_type, None)]
            districts = {p[1] for p in pairs}
            if len(districts) == 1:
                return {"district": districts.pop(), "exact": exact}
            return None  # здания с таким адресом в разных районах — не угадываем
        return None

    def in_kato(self, city: str | None = None, district: str | None = None) -> dict:
        """Есть ли город / район в официальном классификаторе КАТО."""
        out = {}
        if city:
            out["city"] = norm(city) in self.kato_places
        if district:
            k = norm(district)
            out["district"] = k in self.kato_districts or _stem(k) in self.kato_districts
        return out

    def suggest_streets(self, name: str | None, city: str | None, limit: int = 3) -> list[dict]:
        """Похожие улицы этого города — кандидаты для оператора. Ничего не подменяет:
        решение принимает человек. Пусто, если улица и так есть в справочнике."""
        if not (name and city and city in self.streets):
            return []
        key, streets = norm(name), self.streets[city]
        if key in streets or len(key) < 3:
            return []
        # обычное сходство строк: WRatio переоценивает короткие названия («А», «Б» для «Абаяя»)
        hits = process.extract(key, streets.keys(), scorer=fuzz.ratio, limit=limit * 3, score_cutoff=75)
        out, seen = [], set()
        for k, score, _ in hits:
            if len(k) < 3:
                continue
            rec = streets[k][0]
            label = rec["name"]
            if label in seen:
                continue
            seen.add(label)
            out.append({"value": label, "type": rec.get("street_type"), "score": round(score / 100, 2)})
            if len(out) == limit:
                break
        return out

    def suggest_cities(self, text: str | None, limit: int = 3) -> list[dict]:
        """Похожие населённые пункты — когда город в строке не узнан (опечатка, сокращение)."""
        if not text:
            return []
        key = norm(text)
        if len(key) < 3 or key in self.cities.by_key:
            return []
        hits = process.extract(key, self.cities.by_key.keys(), scorer=fuzz.ratio, limit=limit * 3, score_cutoff=80)
        out, seen = [], set()
        for k, score, _ in hits:
            rec = self.cities.by_key[k]
            if rec["name"] in seen:
                continue
            seen.add(rec["name"])
            out.append({"value": rec["name"], "province": rec.get("province"), "score": round(score / 100, 2)})
            if len(out) == limit:
                break
        return out

    def street_info(self, name: str, city: str | None):
        """Ищет улицу в городе -> {'district','street_type','living_area'} или None."""
        if not name or not city or city not in self.streets:
            return None
        return _street_lookup(self, city, norm(name))

    def stats(self) -> dict:
        return {"sources": self.sources, **self.counts,
                "cities_with_streets": len(self.streets)}


@lru_cache(maxsize=50_000)
def _street_cached(gz_id: int, city: str, key: str):
    gz = _REGISTRY[gz_id]
    streets = gz.streets[city]
    recs = streets.get(key)
    score = 1.0
    if not recs and len(key) >= 4:
        hit = process.extractOne(key, streets.keys(), scorer=fuzz.ratio, score_cutoff=90)
        if hit:
            recs, score = streets[hit[0]], hit[1] / 100
    if not recs:
        return None
    # Если одна улица в нескольких районах — берём самый частый район
    districts = [r["district"] for r in recs if r.get("district")]
    district = max(set(districts), key=districts.count) if districts else None
    types = [r["street_type"] for r in recs if r.get("street_type")]
    ambiguous = len(set(districts)) > 1
    # Микрорайон по улице — только если улица целиком в одном районе и одном мкр
    las = {r["living_area"] for r in recs if r.get("living_area")}
    return {
        "district": district,
        "street_type": max(set(types), key=types.count) if types else None,
        "living_area": clean_living_area(las.pop()) if len(las) == 1 and not ambiguous else None,
        "score": score,
        "ambiguous": ambiguous,
    }


_REGISTRY: dict[int, Gazetteer] = {}


def _street_lookup(gz: Gazetteer, city: str, key: str):
    _REGISTRY[id(gz)] = gz
    return _street_cached(id(gz), city, key)


def _has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


@lru_cache(maxsize=1)
def get_gazetteer() -> Gazetteer:
    return Gazetteer()
