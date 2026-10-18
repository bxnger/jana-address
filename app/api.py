"""REST API. Основной метод для жюри: POST /api/v1/parse/batch"""
from __future__ import annotations

import hmac
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .gazetteer import get_gazetteer
from .learning import get_learned
from .pipeline import core_status, process_batch, process_record
from .quality import batch_summary, compare

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jana.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    t = time.time()
    gz = get_gazetteer()  # справочник грузится до первого запроса
    log.info("gazetteer loaded in %.1fs: %s", time.time() - t, gz.stats())
    cs = core_status()
    if cs["external_loaded"]:
        log.info("core gazetteer loaded: %s", cs["path"])
    else:
        log.warning("core gazetteer NOT loaded (%s): %s — работает встроенный минимум",
                    cs["path"], cs["external_error"] or "файл не найден")
    get_learned().load(gz)  # знания из прошлых правок операторов
    yield


class UTF8JSONResponse(JSONResponse):
    # Явная кодировка: без неё часть клиентов (PowerShell 5.1) читает кириллицу как latin-1
    media_type = "application/json; charset=utf-8"


DESCRIPTION = """
Разбор адреса, написанного одной строкой, на поля для логистики.

**Для проверки:** `POST /api/v1/parse/batch` — вход в формате `sample_input.json`,
выход в формате `sample_output.json`, в том же порядке и той же длины.
Нажмите «Try it out» — пример уже подставлен.

Работает без интернета: справочник (OpenStreetMap по Казахстану) лежит внутри образа.
"""

TAG_PARSE, TAG_LEARN, TAG_SERVICE = "Разбор адресов", "Правки и обучение справочника", "Служебное"

app = FastAPI(title="Jana Address API", version="1.0", lifespan=lifespan, description=DESCRIPTION,
              default_response_class=UTF8JSONResponse,
              openapi_tags=[{"name": TAG_PARSE, "description": "Методы для разбора — их использует жюри"},
                            {"name": TAG_LEARN, "description": "Правки операторов из интерфейса"},
                            {"name": TAG_SERVICE}])


class Record(BaseModel):
    id: Any = None
    raw_address: str | None = None


class AddressResult(BaseModel):
    """Ответ на одну запись — ровно поля из sample_output.json. Не определено — null."""
    id: Any = Field(None, description="id из входной записи, без изменений")
    raw_address: str | None = Field(None, description="исходная строка, без изменений")
    province: str | None = Field(None, description="область или город респ. значения: «Актюбинская область», «Алматы»")
    city: str | None = Field(None, description="город без «г.» и казахской половины, текущее название как в 2ГИС")
    district: str | None = Field(None, description="район без слова «район»; если нет в строке — из справочника")
    living_area: str | None = Field(None, description="микрорайон или ЖК, когда улица указана отдельно")
    street_type: str | None = Field(None, description="улица / проспект / микрорайон / переулок / квартал …")
    street_name: str | None = Field(None, description="название без типа, ровно как в строке")
    house: str | None = Field(None, description="номер дома с дробью и литерой: «1/2», «12а»")
    apartment: str | None = Field(None, description="квартира, только номер")
    entrance: str | None = Field(None, description="подъезд, только номер")
    floor: str | None = Field(None, description="этаж, только номер")
    landmark: str | None = Field(None, description="ориентир, если был в строке: «магазин береке»")
    confidence: float = Field(..., description="уверенность 0..1 (эвристика; низкая — запись стоит проверить)")


BATCH_EXAMPLES = {
    "jury": {
        "summary": "Пример жюри и сложные строки из ТЗ",
        "value": [
            {"id": "rec_00417", "raw_address": "город Алматы/Алматы қаласы, Мкр Аксай 3а, 59, кв 70"},
            {"id": "d2", "raw_address": "Туран 55/6 магазин береке 4подьезд 9этаж"},
            {"id": "d3", "raw_address": "г. Алматы, ул. Жандосова 5, кв 12"},
            {"id": "d4", "raw_address": "66квартал 12дом 69квартира"},
        ],
    },
}


def _records(payload: Any) -> list[dict]:
    """Принимает список записей, {"records": [...]}, или любой объект с одним списком внутри."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "raw_address" in payload:
            return [payload]
        for v in payload.values():
            if isinstance(v, list):
                return v
    raise HTTPException(422, "Ожидается список записей {id, raw_address}")


_NET = {"t": 0.0, "up": None}


def internet_available() -> bool:
    """Есть ли сейчас выход в интернет (кэш 5 с). Для демонстрации: сеть отключена — сервис работает."""
    if time.time() - _NET["t"] > 5:
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=0.5).close()
            _NET["up"] = True
        except OSError:
            _NET["up"] = False
        _NET["t"] = time.time()
    return bool(_NET["up"])


@app.get("/health", tags=[TAG_SERVICE])
def health():
    """Статус сервиса, размер справочника, что выучено на правках и есть ли сейчас интернет
    (он не нужен: `internet: false` и при этом всё работает — это и есть автономность)."""
    return {"status": "ok", "gazetteer": get_gazetteer().stats(), "core_gazetteer": core_status(),
            "learned": get_learned().stats(), "internet": internet_available(), "needs_internet": False}


@app.post("/api/v1/evaluate", tags=[TAG_PARSE], summary="Проверка качества по эталону")
def evaluate(payload: Any = Body(..., openapi_examples={"gold": {
        "summary": "Размеченные записи в формате sample_output.json",
        "value": [{"id": "rec_00417", "raw_address": "город Алматы/Алматы қаласы, Мкр Аксай 3а, 59, кв 70",
                   "province": "Алматы", "city": "Алматы", "district": "Ауэзовский", "living_area": None,
                   "street_type": "микрорайон", "street_name": "Аксай 3а", "house": "59", "apartment": "70",
                   "entrance": None, "floor": None, "landmark": None}]}}),
             threshold: float = Query(0.7, ge=0, le=1)):
    """Разбирает `raw_address` из эталона и сравнивает по правилам жюри: точность каждого поля и групп
    с весами 60/25/15, частые ошибки с примерами, сколько записей требует проверки, скорость на 1000.
    Можно передать и готовые ответы: `{"predictions": [...], "gold": [...]}`."""
    if isinstance(payload, dict) and isinstance(payload.get("gold"), list):
        gold, preds = payload["gold"], payload.get("predictions")
    else:
        gold, preds = _records(payload), None
    gold = [g for g in gold if isinstance(g, dict)]
    inputs = [{"id": g.get("id"), "raw_address": g.get("raw_address")} for g in gold]
    t = time.perf_counter()
    fast = process_batch(inputs)                # скорость — как у жюри, без отладочных полей
    elapsed = time.perf_counter() - t
    parsed = process_batch(inputs, debug=True)  # сомнения по полям — для сводки
    report = compare(preds if isinstance(preds, list) else fast, gold)
    report["summary"] = batch_summary(parsed, elapsed, threshold)
    return report


API_TOKEN = os.environ.get("JANA_API_TOKEN") or None


def _check_token(x_api_key: str | None):
    """Правки учат справочник, поэтому принимать их только от интерфейса, если задан токен.
    Пакетный разбор для жюри токена не требует."""
    if API_TOKEN and not (x_api_key and hmac.compare_digest(x_api_key, API_TOKEN)):
        raise HTTPException(401, "Нужен заголовок X-API-Key")


@app.post("/api/v1/corrections", tags=[TAG_LEARN])
def add_corrections(payload: Any = Body(...), x_api_key: str | None = Header(None),
                    x_operator: str | None = Header(None)):
    """Ручные правки из интерфейса: сохраняются с автором (X-Operator) и сразу пополняют справочник.
    Вход — записи в формате ответа (id, raw_address и исправленные поля)."""
    _check_token(x_api_key)
    recs = [r for r in _records(payload) if isinstance(r, dict)]
    learned = get_learned().add(recs, get_gazetteer(), author=x_operator)
    log.info("corrections: %d records by %s, learned %s", len(recs), x_operator or "?", learned)
    return {"saved": len(recs), "learned": learned, "total": get_learned().stats()}


@app.get("/api/v1/corrections", tags=[TAG_LEARN])
def list_corrections(limit: int = Query(200, ge=1, le=5000), x_api_key: str | None = Header(None)):
    """Журнал правок (новые сверху): кто, когда и что исправил."""
    _check_token(x_api_key)
    return get_learned().recent(limit)


@app.get("/api/v1/learned", tags=[TAG_LEARN])
def learned_stats():
    """Что справочник выучил на правках операторов."""
    return get_learned().stats()


@app.post("/api/v1/parse/batch", tags=[TAG_PARSE], summary="Пакетный разбор (метод для жюри)",
          responses={200: {"model": list[AddressResult], "description": "Список той же длины и в том же порядке"}})
def parse_batch(payload: Any = Body(..., openapi_examples=BATCH_EXAMPLES),
                debug: bool = Query(False, description="true — добавить источник полей, флаги и остаток")):
    """Вход — список `{"id", "raw_address"}` (как `sample_input.json`; принимается и `{"records": [...]}`).
    Выход — список записей в формате `sample_output.json`. Ни одна запись не теряется: на пустую или
    битую строку возвращаются поля `null` и низкая уверенность."""
    recs = [r if isinstance(r, dict) else {"id": None, "raw_address": str(r)} for r in _records(payload)]
    t = time.time()
    out = process_batch(recs, debug=debug)
    log.info("batch %d records in %.2fs", len(recs), time.time() - t)
    return UTF8JSONResponse(out)


@app.post("/api/v1/parse", tags=[TAG_PARSE], summary="Один адрес (JSON)")
def parse_one(rec: Record, debug: bool = Query(True)):
    """Разбор одной записи `{"id", "raw_address"}`; по умолчанию с отладочными полями."""
    return process_record(rec.model_dump(), debug=debug)


@app.get("/api/v1/parse", tags=[TAG_PARSE], summary="Один адрес (строкой в запросе)")
def parse_query(q: str = Query(..., examples=["г. Алматы, ул. Жандосова 5, кв 12"]), debug: bool = Query(True)):
    """Быстрая проверка: адрес в параметре `q`."""
    return process_record({"id": None, "raw_address": q}, debug=debug)


@app.post("/api/v1/parse/file", tags=[TAG_PARSE], summary="Пакетный разбор файлом",
          responses={200: {"model": list[AddressResult]}})
async def parse_file(file: UploadFile = File(...), debug: bool = Query(False)):
    """Загрузка JSON-файла в формате sample_input.json."""
    try:
        payload = json.loads((await file.read()).decode("utf-8-sig"))
    except Exception as e:
        raise HTTPException(400, f"Не удалось прочитать JSON: {e}") from e
    return UTF8JSONResponse(process_batch(_records(payload), debug=debug))
