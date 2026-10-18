"""Справочник учится на ручных правках операторов.

Правка из интерфейса (POST /api/v1/corrections) сохраняется в corrections.jsonl
и сразу превращается в знания:

  * район для (город, улица, дом) и для (город, улица) — если оператор указал район;
  * микрорайоны и ЖК (поле «Мкр / ЖК» или тип «микрорайон») — ядро начинает
    узнавать их в строке, а пайплайн — восстанавливать по ним район.

Выученное только заполняет пустые поля и никогда не спорит с тем, что написано
в строке. При старте API знания поднимаются из того же файла.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path

from .gazetteer import DATA_DIR, Gazetteer
from .text import house_key, norm

log = logging.getLogger(__name__)

CORRECTIONS = Path(os.environ.get("JANA_CORRECTIONS", DATA_DIR / "corrections" / "corrections.jsonl"))
FIELDS = ["province", "city", "district", "living_area", "street_type", "street_name",
          "house", "apartment", "entrance", "floor", "landmark"]
_AMBIG = object()


class Learned:
    def __init__(self, path: Path = CORRECTIONS):
        self.path = path
        self.by_house: dict[tuple, str] = {}          # (город, улица, дом) -> район
        self.by_street: dict[tuple, object] = {}      # (город, улица) -> район | _AMBIG
        self.living_areas: dict[tuple, str | None] = {}  # (город, мкр) -> район | None
        self.n_corrections = 0
        self._lock = threading.Lock()

    # ---------- загрузка и запись ----------
    def load(self, gz: Gazetteer | None = None) -> None:
        if not self.path.exists():
            return
        recs = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                recs.append(json.loads(line))
            except ValueError:
                log.warning("битая строка в %s пропущена", self.path)
        self._learn(recs, gz)
        log.info("learned from %d corrections: %s", len(recs), self.stats())

    def add(self, records: list[dict], gz: Gazetteer | None = None, author: str | None = None) -> dict:
        """Сохраняет правки (с автором) и сразу учится на них. Возвращает, что нового выучено."""
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        clean = []
        for r in records:
            rec = {k: _val(r.get(k)) for k in ["id", "raw_address", *FIELDS]}
            rec["corrected_at"] = now
            rec["corrected_by"] = _val(author)
            clean.append(rec)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for rec in clean:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return self._learn(clean, gz)

    # ---------- обучение ----------
    def _learn(self, records: list[dict], gz: Gazetteer | None) -> dict:
        new = {"districts": 0, "living_areas": []}
        core_las, core_la_district = [], {}
        for r in records:
            self.n_corrections += 1
            city, district = r.get("city"), r.get("district")
            street, house = r.get("street_name"), house_key(r.get("house"))
            if city and street and district:
                s = norm(street)
                if house and self.by_house.get((city, s, house)) != district:
                    self.by_house[(city, s, house)] = district
                    new["districts"] += 1
                # На всю улицу — только если по OSM она целиком в одном районе (или её нет в OSM).
                # Иначе одна правка «Абая 5» разнесла бы свой район на весь проспект Абая.
                if _single_district_street(gz, city, s):
                    prev = self.by_street.get((city, s))
                    self.by_street[(city, s)] = district if prev in (None, district) else _AMBIG
            la = r.get("living_area") or (street if r.get("street_type") == "микрорайон" else None)
            if la and city:
                key = (city, norm(la))
                if key not in self.living_areas:
                    new["living_areas"].append(la)
                if district or key not in self.living_areas:
                    self.living_areas[key] = district or self.living_areas.get(key)
                core_las.append(la)
                if district:
                    core_la_district[la] = district
                if gz is not None:
                    gz._add_living_area(la, city, district)
        if core_las:
            _teach_core(core_las, core_la_district)
        return new

    # ---------- поиск ----------
    def district_for(self, city: str | None, street: str | None, house: str | None):
        """-> (район, 'learned_house' | 'learned_street') или (None, None)."""
        if not (city and street):
            return None, None
        s = norm(street)
        h = house_key(house)
        if h and (city, s, h) in self.by_house:
            return self.by_house[(city, s, h)], "learned_house"
        d = self.by_street.get((city, s))
        if d and d is not _AMBIG:
            return d, "learned_street"
        return None, None

    def recent(self, limit: int = 200) -> list[dict]:
        """Последние правки, новые сверху — для журнала администратора."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def stats(self) -> dict:
        return {"corrections": self.n_corrections, "houses": len(self.by_house),
                "streets": sum(1 for v in self.by_street.values() if v is not _AMBIG),
                "living_areas": len(self.living_areas)}


def _single_district_street(gz: Gazetteer | None, city: str, street_key: str) -> bool:
    """Можно ли переносить район из правки на всю улицу: в OSM улица одна и в одном районе."""
    if gz is None:
        return True
    recs = gz.streets.get(city, {}).get(street_key) or []
    districts = {r.get("district") for r in recs} - {None}
    types = {r.get("street_type") for r in recs} - {None}
    return len(districts) <= 1 and len(types) <= 1


def _val(v):
    if v is None:
        return None
    if isinstance(v, float) and v != v:  # NaN из pandas
        return None
    v = str(v).strip()
    return v or None


def _teach_core(living_areas: list[str], district_by_la: dict[str, str]) -> None:
    """Добавляет мкр/ЖК в справочник ядра на лету (тот же формат, что data/core_gazetteer.json)."""
    try:
        import gazetteer as core_gz  # core/ уже в sys.path (app.pipeline)
        core_gz._merge({"living_areas": living_areas, "district_by_living_area": district_by_la})
    except Exception:  # ядро без этой точки расширения — учимся только на своей стороне
        log.exception("не удалось передать выученные мкр в ядро")


_LEARNED: Learned | None = None


def get_learned() -> Learned:
    global _LEARNED
    if _LEARNED is None:
        _LEARNED = Learned()
    return _LEARNED
