"""Пайплайн: ядро разбора (core/) -> дозаполнение по OSM -> ответ.

Ядро само знает города, районы и микрорайоны из data/core_gazetteer.json
(экспорт нашего справочника). Поверх него добавляем то, чего в ядре нет:
улица -> район / тип улицы / микрорайон по OSM.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from .explain import explain
from .gazetteer import DATA_DIR, Gazetteer, get_gazetteer
from .noise import strip_noise
from .text import norm

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("JANA_GAZETTEER", str(DATA_DIR / "core_gazetteer.json"))
sys.path.insert(0, str(ROOT / "core"))

import address_parser as core  # noqa: E402
import gazetteer as core_gz  # noqa: E402

from .learning import get_learned  # noqa: E402  (после core в sys.path: учит и ядро)

log = logging.getLogger(__name__)

FIELDS = [f for f in core.FIELD_ORDER if f not in ("id", "raw_address", "confidence")]
OUT_ORDER = core.FIELD_ORDER

# Флаги ядра, после которых поле взято из справочника, а не из строки
_FLAG_SOURCE = {"city_from_district": "city"}
# Флаги ядра, после которых поле взято из строки, хотя написано иначе ("Абай ауданы" -> "Абайский")
_FLAG_TEXT = {"district_canonical": "district"}


def core_status() -> dict:
    return {"external_loaded": core_gz.EXTERNAL_LOADED, "external_error": core_gz.EXTERNAL_ERROR,
            "path": os.environ["JANA_GAZETTEER"]}


def _sources(res: dict, flags: list[str], normalized: str) -> dict:
    """Помечает, откуда поле: из строки или восстановлено по справочнику."""
    text = (normalized or "").lower()
    src = {}
    for f in FIELDS:
        v = res.get(f)
        if not v:
            continue
        src[f] = "text"
        if f in ("province", "city", "district") and str(v).lower().split()[0][:5] not in text:
            src[f] = "gazetteer"
    for flag, f in _FLAG_SOURCE.items():
        if flag in flags and f in src:
            src[f] = "gazetteer"
    for flag, f in _FLAG_TEXT.items():
        if flag in flags and f in src:
            src[f] = "text"
    return src


def enrich_osm(res: dict, src: dict, gz: Gazetteer) -> str | None:
    """Здание -> район; улица -> район / тип / мкр; мкр -> район (OSM).
    Возвращает, каким путём восстановлен район:
    learned_house | learned_street | building | building_base | street | living_area."""
    via = None
    # 0. Выученное на правках операторов — решение человека, проверяем первым
    if not res.get("district"):
        d, how = get_learned().district_for(res.get("city"), res.get("street_name"), res.get("house"))
        if d:
            res["district"] = d; src["district"] = "learned"; via = how
    b = gz.building_info(res.get("street_name"), res.get("house"), res.get("city"), res.get("street_type"))
    if b and not res.get("district") and b["district"]:
        res["district"] = b["district"]; src["district"] = "gazetteer"
        via = "building" if b["exact"] else "building_base"
    info = gz.street_info(res.get("street_name"), res.get("city"))
    if info:
        if not res.get("district") and info["district"] and not info["ambiguous"]:
            res["district"] = info["district"]; src["district"] = "gazetteer"; via = "street"
        if not res.get("street_type") and info["street_type"]:
            res["street_type"] = info["street_type"]; src["street_type"] = "gazetteer"
        if not res.get("living_area") and info["living_area"] and res.get("street_type") != "микрорайон":
            res["living_area"] = info["living_area"]; src["living_area"] = "gazetteer"
    for name in (res.get("street_name") if res.get("street_type") == "микрорайон" else None,
                 res.get("living_area")):
        if res.get("district") or not name:
            continue
        rec, _ = gz.find_living_area(name, res.get("city"))
        if rec and rec.get("district"):
            res["district"] = rec["district"]; src["district"] = "gazetteer"; via = "living_area"
    return via


def courier_info(extras: dict, noise: list[str]) -> dict:
    """То, что в строке есть, но адресом не является, — пригодится курьеру (в ответ жюри не идёт):
    получатель и пометка (ядро), телефон и код домофона (очистка шума)."""
    info = {"recipient": extras.get("recipient"), "note": extras.get("courier_note"), "phone": None, "intercom": None}
    for item in noise or []:
        kind, _, value = item.partition(": ")
        if kind == "телефон":
            info["phone"] = value if not info["phone"] else f"{info['phone']}, {value}"
        elif kind == "домофон":
            info["intercom"] = value if not info["intercom"] else f"{info['intercom']}, {value}"
    return {k: v for k, v in info.items() if v}


def _decaps(out: dict, gz: Gazetteer) -> None:
    """«УЛ. САТПАЕВА» -> «Сатпаева»: в 2ГИС названия не капсом. Сначала написание из справочника,
    иначе заглавная только первая буква (как «Кабанбай батыра»)."""
    for f in ("street_name", "living_area"):
        v = out.get(f)
        if not v or not v.isupper() or len(re.sub(r"[\W\d_]", "", v)) < 3:
            continue
        hits = gz.streets.get(out.get("city") or "", {}).get(norm(v)) if f == "street_name" else None
        out[f] = hits[0]["name"] if hits else v[:1] + v[1:].lower()


def process_record(rec: dict, gz: Gazetteer | None = None, debug: bool = False) -> dict:
    gz = gz or get_gazetteer()
    raw = rec.get("raw_address")
    clean, noise = strip_noise(raw)  # телефоны, коды домофона, опечатки в служебных словах
    extras = {}
    try:
        ex = core.explain_address(clean)
        res, flags, leftover, normalized = ex["result"], ex["flags"], ex["leftover"], ex["normalized"]
        extras = ex.get("extras") or {}
    except Exception:  # запись нельзя пропускать
        log.exception("core parse failed: %r", raw)
        res, flags, leftover, normalized = core.parse_address(clean, rec.get("id")), ["core_error"], None, ""
    out = {f: res.get(f) for f in OUT_ORDER}
    out["id"], out["raw_address"] = rec.get("id"), raw
    via = None
    _decaps(out, gz)
    try:
        src = _sources(out, flags, normalized)
        via = enrich_osm(out, src, gz)
    except Exception:
        log.exception("osm enrich failed: %r", raw)
        src = {}
    if debug:
        out["_source"] = src
        out["_district_via"] = via
        out["_noise"] = noise
        out["_flags"] = flags
        out["_leftover"] = leftover
        out["_courier"] = courier_info(extras, noise)
        out["_suggestions"] = suggestions(out, clean, gz)
        kato = gz.in_kato(out.get("city"), out.get("district"))
        ex = explain(out, raw, flags, src, via, noise, leftover, out["_suggestions"], kato)
        out["_explain"] = ex["fields"]
        out["_doubt"] = ex["doubt"]
        out["_notes"] = ex["notes"]
    return out


def suggestions(out: dict, clean: str | None, gz: Gazetteer) -> dict:
    """Кандидаты из справочника для оператора: похожие улицы и города. Никогда не подставляются сами."""
    sug = {}
    name = out.get("street_name")
    if out.get("street_type") == "микрорайон" and name:
        rec, score = gz.find_living_area(name, out.get("city"))
        if rec and score >= 0.97:      # известный микрорайон — кандидаты не нужны
            name = None
    streets = gz.suggest_streets(name, out.get("city"))
    if streets:
        sug["street_name"] = streets
    if out.get("city"):
        # город в строке есть, но в справочнике его нет (опечатка «Алматыы») — предложить похожие
        cities = gz.suggest_cities(out["city"])
        if cities:
            sug["city"] = cities
    elif isinstance(clean, str):
        first = re.split(r"[,;]", clean, maxsplit=1)[0].strip()
        if first and not re.search(r"\d", first):
            cities = gz.suggest_cities(re.sub(r"(?i)^(г\.?|город|пос\.?|с\.)\s*", "", first))
            if cities:
                sug["city"] = cities
    return sug


def process_batch(records: list[dict], debug: bool = False) -> list[dict]:
    gz = get_gazetteer()
    return [process_record(r if isinstance(r, dict) else {"id": None, "raw_address": r}, gz, debug)
            for r in records]
