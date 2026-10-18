"""Интерфейс: загрузить файл -> посмотреть разбор -> поправить неуверенные записи -> скачать."""
from __future__ import annotations

import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import ROLES, authenticate  # noqa: E402

API_URL = os.environ.get("API_URL", "http://localhost:8000")
API_TOKEN = os.environ.get("JANA_API_TOKEN") or None
CORRECTIONS = Path(os.environ.get("JANA_DATA_DIR", "data")) / "corrections" / "corrections.jsonl"
DEMO = Path(os.environ.get("JANA_DATA_DIR", "data")) / "demo" / "demo_input.json"
FIELDS = ["province", "city", "district", "living_area", "street_type", "street_name",
          "house", "apartment", "entrance", "floor", "landmark"]
LABELS = {"province": "Область", "city": "Город", "district": "Район", "living_area": "Мкр / ЖК",
          "street_type": "Тип улицы", "street_name": "Улица", "house": "Дом", "apartment": "Кв.",
          "entrance": "Подъезд", "floor": "Этаж", "landmark": "Ориентир"}
# Флаги ядра разбора (core/address_parser.py) человеческим языком
FLAG_LABELS = {
    "street_not_in_gazetteer": "улицы нет в справочнике города", "street_untyped": "тип улицы не указан",
    "city_implicit": "город без «г.»", "city_by_position": "город угадан по положению",
    "city_no_comma": "город слит с улицей", "city_ambiguous": "название — и город, и мкр/улица",
    "city_street_swapped": "город и улица переставлены", "city_from_district": "город по району",
    "province_city_conflict": "область не совпадает с городом",
    "province_from_ambiguous_city": "область по неоднозначному городу",
    "apartment_hyphen": "квартира через дефис", "apartment_bare_number": "квартира — голое число",
    "unit_direction_default": "номер без слова «кв/дом»", "area_number_absorbed": "номер вошёл в название мкр",
    "street_long": "подозрительно длинная улица", "truncated": "строка обрезана",
    "street_type_from_gazetteer": "тип улицы из справочника",
    "living_area_from_gazetteer": "мкр из справочника",
    "street_in_gazetteer": "улица есть в справочнике", "house_by_keyword": "дом по слову «дом»",
    "city_marker": "город со словом «г.»",
    "street_comma_split": "после улицы лишний сегмент (запятая в названии?)",
    "street_comma_in_name": "запятая внутри названия улицы",
    "street_from_city": "единственное название при доме — принято за улицу",
    "recipient_name": "убрано имя получателя",
    "recipient_guess": "убрано слово, похожее на имя",
    "recipient_marker": "получатель после «получатель/для/звонить»",
    "courier_note": "пометка курьеру",
    "district_canonical": "район по-казахски → как в справочнике",
    "living_area_demoted": "мкр без типа рядом с улицей",
    "street_untyped_city": "улица без типа",
    "building_part": "корпус/блок в номере",
    "core_error": "ошибка разбора",
}
# Флаги, которые говорят о хорошем — в сводку «почему не уверены» не идут
OK_FLAGS = {"street_in_gazetteer", "house_by_keyword", "city_marker", "street_type_from_gazetteer",
            "living_area_from_gazetteer", "city_from_district", "recipient_marker", "courier_note",
            "district_canonical", "street_comma_in_name"}
COURIER_LABELS = {"recipient": "👤 получатель", "phone": "📞", "intercom": "🔔", "note": "📝"}
# Цвета подсветки разбора (полупрозрачные — читаются и в светлой, и в тёмной теме)
HL_COLORS = {"province": "#8b5cf6", "city": "#3b82f6", "district": "#06b6d4", "living_area": "#14b8a6",
             "street_name": "#22c55e", "house": "#eab308", "apartment": "#f97316", "entrance": "#ec4899",
             "floor": "#ef4444", "landmark": "#a3a3a3", "recipient": "#64748b"}
_UNIT_HINT = {"apartment": r"(?:кв(?:артира)?|квартра|flat|kv|пәтер|офис|оф)\.?\s*[-#№:]?\s*",
              "entrance": r"(?:подъезд|подьезд|под\.?|п-д|п\.|кіреберіс|pod)\s*[-#№:]?\s*",
              "floor": r"(?:этаж|эт\.?|етаж|қабат|floor)\s*[-#№:]?\s*",
              "house": r"(?:дом|д\.|үй|dom)\s*[-#№:]?\s*"}
_UNIT_AFTER = {"apartment": r"\s*-?\s*(?:кв(?:артира)?|квартра|пәтер)",
               "entrance": r"\s*-?(?:й|ый)?\s*(?:подъезд|подьезд|п-д)",
               "floor": r"\s*-?(?:й|ый)?\s*(?:этаж|эт|етаж|қабат)", "house": r"\s*(?:дом|үй)"}
SOURCE_LABELS = {"text": "из строки", "gazetteer": "📚 справочник OSM", "learned": "🧠 выучено на правках"}
VIA_LABELS = {"building": "по номеру дома", "building_base": "по дому без литеры", "street": "по улице",
              "living_area": "по микрорайону", "learned_house": "правка оператора для этого дома",
              "learned_street": "правка оператора для этой улицы"}
STREET_TYPES = ["улица", "проспект", "микрорайон", "переулок", "квартал", "бульвар", "шоссе",
                "проезд", "тупик", "трасса", "набережная", "площадь"]

st.set_page_config(page_title="Jana Address", page_icon="📮", layout="wide")

st.markdown("""
<style>
:root { --ja: #2F5BEA; --ja-soft: #EEF2FF; --ja-ink: #14213D; }
html, body, [class*="css"] { font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
.block-container { padding-top: 2.2rem; max-width: 1280px; }
h1, h2, h3 { color: var(--ja-ink); letter-spacing: -0.01em; }
section[data-testid="stSidebar"] { background: linear-gradient(180deg, #F6F8FF 0%, #EEF2FF 100%);
  border-right: 1px solid #E3E8FA; }
section[data-testid="stSidebar"] h1 { font-size: 1.35rem; }
div[data-testid="stMetric"] { background: #fff; border: 1px solid #E3E8FA; border-radius: 12px; padding: 10px 14px;
  box-shadow: 0 1px 2px rgba(20,33,61,.04); }
div[data-testid="stMetricValue"] { color: var(--ja); font-weight: 700; }
.stButton > button, .stDownloadButton > button { border-radius: 10px; font-weight: 600; min-height: 2.6rem; }
.stButton > button[kind="primary"] { background: var(--ja); border-color: var(--ja); }
.stButton > button[kind="primary"]:hover { background: #2349C4; border-color: #2349C4; }
.stTextInput input, .stTextArea textarea { border-radius: 10px; }
.stTextInput input:focus, .stTextArea textarea:focus { border-color: var(--ja) !important;
  box-shadow: 0 0 0 2px rgba(47,91,234,.18) !important; }
button[role="tab"] { font-weight: 600; }
button[role="tab"][aria-selected="true"] { color: var(--ja); }
div[data-baseweb="tab-highlight"] { background-color: var(--ja) !important; }
div[data-testid="stDataFrame"] { border: 1px solid #E3E8FA; border-radius: 12px; overflow: hidden; }
div[data-testid="stAlert"] { border-radius: 12px; }
div[data-testid="stSlider"] [role="slider"] { background-color: var(--ja); }
div[data-testid="stExpander"] { border-radius: 12px; border-color: #E3E8FA; }
div[data-testid="stDecoration"] { display: none; }
[data-testid="stFileUploaderDropzoneInstructions"] > div { font-size: 0 !important; }
[data-testid="stFileUploaderDropzoneInstructions"] > div > * { display: none !important; }
[data-testid="stFileUploaderDropzoneInstructions"] > div::before {
  content: "Перетащите файл сюда"; font-size: 1rem; font-weight: 600; display: block; color: var(--ja-ink); }
[data-testid="stFileUploaderDropzoneInstructions"] > div::after {
  content: "JSON, CSV или TXT · до 200 МБ"; font-size: .8rem; color: #6B7280; display: block; }
[data-testid="stFileUploaderDropzone"] button { font-size: 0 !important; }
[data-testid="stFileUploaderDropzone"] button::after { content: "Выбрать файл"; font-size: .9rem; }
.ja-steps { display: flex; gap: 12px; flex-wrap: wrap; margin: .4rem 0 1rem; }
.ja-step { flex: 1 1 200px; background: #fff; border: 1px solid #E3E8FA; border-radius: 12px; padding: 12px 14px; }
.ja-step b { color: var(--ja); font-size: 1.1rem; margin-right: 6px; }
.ja-step span { color: #4B5563; font-size: .88rem; display: block; margin-top: 2px; }
.ja-hero { background: linear-gradient(120deg, #14213D 0%, #2F5BEA 100%); color: #fff; border-radius: 16px;
  padding: 18px 22px; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center;
  flex-wrap: wrap; gap: 10px; }
.ja-hero h2 { color: #fff; margin: 0; font-size: 1.45rem; }
.ja-hero p { margin: 2px 0 0; color: #C7D2FE; font-size: .92rem; }
.ja-badges { display: flex; gap: 8px; flex-wrap: wrap; }
.ja-badge { background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.25); border-radius: 999px;
  padding: 4px 12px; font-size: .82rem; color: #fff; white-space: nowrap; }
.ja-badge.ok { background: #16A34A; border-color: #16A34A; }
.ja-fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 10px; margin: .6rem 0; }
.ja-field { border: 1px solid #E3E8FA; border-left: 5px solid #16A34A; border-radius: 10px; padding: 8px 12px;
  background: #fff; }
.ja-field.doubt { border-left-color: #F59E0B; background: #FFFBEB; }
.ja-field.missing { border-left-color: #CBD5E1; background: #F8FAFC; }
.ja-field .lbl { font-size: .75rem; color: #6B7280; text-transform: uppercase; letter-spacing: .04em; }
.ja-field .val { font-size: 1.05rem; font-weight: 700; color: var(--ja-ink); margin: 1px 0 3px; }
.ja-field.missing .val { color: #94A3B8; font-weight: 500; }
.ja-field .why { font-size: .82rem; color: #4B5563; line-height: 1.35; }
.ja-note { font-size: .85rem; color: #4B5563; background: #F1F5F9; border-radius: 8px; padding: 6px 10px;
  margin: 4px 0; }
@media (max-width: 640px) { .block-container { padding: 1rem .8rem; } }
</style>
""", unsafe_allow_html=True)


# ---------- Вход ----------
def login_screen():
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.title("📮 Jana Address")
        st.caption("Проверка и правка разобранных адресов. Войдите под учётной записью оператора.")
        with st.form("login"):
            login = st.text_input("Логин")
            password = st.text_input("Пароль", type="password")
            if st.form_submit_button("Войти", type="primary", use_container_width=True):
                user = authenticate(login, password)
                if user:
                    st.session_state.user = user
                    st.rerun()
                st.error("Неверный логин или пароль")
    st.stop()


USER = st.session_state.get("user")
if not USER:
    login_screen()


def api_headers() -> dict:
    """Кто правит (логин латиницей — в заголовках только ASCII) и токен, если он задан."""
    h = {"X-Operator": USER["login"]}
    if API_TOKEN:
        h["X-API-Key"] = API_TOKEN
    return h


# ---------- API ----------
@st.cache_data(ttl=5, show_spinner=False)
def api_health():
    try:
        return requests.get(f"{API_URL}/health", timeout=5).json()
    except Exception as e:
        return {"status": "down", "error": str(e)}


def api_batch(records: list[dict]) -> list[dict]:
    out = []
    for i in range(0, len(records), 500):  # кусками, чтобы видеть прогресс
        r = requests.post(f"{API_URL}/api/v1/parse/batch", params={"debug": "true"},
                          json=records[i:i + 500], timeout=600)
        r.raise_for_status()
        out.extend(r.json())
    return out


def read_upload(f) -> list[dict]:
    name = f.name.lower()
    raw = f.getvalue()
    if name.endswith(".json"):
        data = json.loads(raw.decode("utf-8-sig"))
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [data])
        return [d if isinstance(d, dict) else {"id": i, "raw_address": str(d)} for i, d in enumerate(data)]
    if name.endswith((".csv", ".xlsx")):
        df = pd.read_csv(io.BytesIO(raw)) if name.endswith(".csv") else pd.read_excel(io.BytesIO(raw))
        col = "raw_address" if "raw_address" in df else df.columns[-1]
        ids = df["id"] if "id" in df else range(len(df))
        return [{"id": i, "raw_address": str(a)} for i, a in zip(ids, df[col], strict=True)]
    lines = [line.strip() for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    return [{"id": f"line_{i + 1}", "raw_address": line} for i, line in enumerate(lines)]


def source_label(s: str) -> str:
    """'osm:https://…/kazakhstan-latest.osm.pbf (2026-10-16T22:33:54)' -> 'OpenStreetMap (16.10.2026)'."""
    if s.startswith("osm:"):
        m = re.search(r"\((\d{4})-(\d{2})-(\d{2})", s)
        return f"OpenStreetMap ({m.group(3)}.{m.group(2)}.{m.group(1)})" if m else "OpenStreetMap"
    if s.startswith("КАТО"):
        return "КАТО (stat.gov.kz)"
    if s.startswith("seed:"):
        return "ручной справочник областей"
    return s


def courier_text(c: dict | None) -> str:
    return " · ".join(f"{COURIER_LABELS[k]} {v}" for k, v in (c or {}).items() if k in COURIER_LABELS)


def highlight(raw: str, r: dict) -> str:
    """Исходная строка с подсвеченными частями: какой кусок в какое поле ушёл (наглядно для оператора)."""
    taken: list[tuple[int, int, str]] = []

    def free(a, b):
        return all(b <= s or a >= e for s, e, _ in taken)

    def mark(field, pattern):
        for m in re.finditer(pattern, raw, re.I):
            if m.end() > m.start() and free(m.start(), m.end()):
                taken.append((m.start(), m.end(), field))
                return True
        return False

    vals = dict(r)
    vals["recipient"] = (r.get("_courier") or {}).get("recipient")
    for f in ["landmark", "recipient", "district", "living_area", "street_name", "city", "province"]:
        v = vals.get(f)
        if not v or mark(f, rf"(?<!\w){re.escape(str(v))}(?!\w)"):
            continue
        if f == "province":
            mark(f, rf"(?<!\w){re.escape(str(v).split()[0][:6])}\w*(?:\s+обл\w*\.?)?")
        elif f == "district":      # "Абай ауданы" -> "Абайский": подсвечиваем написанное в строке
            mark(f, r"(?<!\w)[^\W\d_][\w-]*\s+(?:ауданы|район[а-я]*|р-н|р-он)\.?")
    for f in ["apartment", "entrance", "floor", "house"]:
        v = vals.get(f)
        if not v:
            continue
        num = re.escape(str(v))
        for pattern in (rf"(?<!\w){_UNIT_HINT[f]}{num}(?![\w/])", rf"(?<![\w/]){num}{_UNIT_AFTER[f]}\w*",
                        rf"(?<![\w/]){num}(?![\w/])"):
            if mark(f, pattern):
                break
    out, pos = [], 0
    for s, e, f in sorted(taken):
        out.append(html.escape(raw[pos:s]))
        c = HL_COLORS[f]
        title = LABELS.get(f, "Получатель")
        out.append(f'<span title="{title}" style="background:{c}33;border-bottom:2px solid {c};'
                   f'border-radius:4px;padding:1px 3px">{html.escape(raw[s:e])}'
                   f'<sub style="color:{c};font-size:0.65em;margin-left:2px">{title}</sub></span>')
        pos = e
    out.append(html.escape(raw[pos:]))
    return "".join(out)


def to_df(results: list[dict]) -> pd.DataFrame:
    rows = []
    for r in results:
        src = r.get("_source", {})
        row = {k: r.get(k) for k in ["id", "raw_address", *FIELDS, "confidence"]}
        row["из справочника"] = ", ".join(LABELS[f] for f, s in src.items() if s in ("gazetteer", "learned"))
        from_text = "из строки" if src.get("district") == "text" else ""
        row["район найден"] = VIA_LABELS.get(r.get("_district_via"), from_text)
        bad = [f for f in (r.get("_flags") or []) if f not in OK_FLAGS]
        row["почему не уверены"] = ", ".join(FLAG_LABELS.get(f, f) for f in bad)
        row["_flags"] = bad
        left = r.get("_leftover")
        row["не разобрано"] = " | ".join(left) if isinstance(left, list) else (left or "")
        row["курьеру"] = courier_text(r.get("_courier"))
        row["⚠ сомнения"] = ", ".join(LABELS[f] for f in (r.get("_doubt") or []) if f in LABELS)
        row["верно"] = False
        row["исправлено"] = False
        # для карточек проверки и фильтров (в таблицу и выгрузку не идут)
        row["_explain"] = r.get("_explain") or {}
        row["_doubt"] = r.get("_doubt") or []
        row["_suggestions"] = r.get("_suggestions") or {}
        row["_notes"] = r.get("_notes") or []
        row["_courier"] = r.get("_courier") or {}
        row["_via"] = r.get("_district_via")
        rows.append(row)
    # Сначала самые неуверенные: их и надо проверять руками
    return pd.DataFrame(rows).sort_values("confidence", kind="stable")


INTERNAL = ["_flags", "_explain", "_doubt", "_suggestions", "_notes", "_courier", "_via"]


def needs_review(df: pd.DataFrame) -> pd.Series:
    """Очередь на проверку: уверенность ниже порога или хотя бы одно поле под сомнением."""
    return (df["confidence"] < st.session_state.get("threshold", 0.7)) | (df["_doubt"].map(len) > 0)
STATUS_ICON = {"ok": "✅", "doubt": "⚠️", "missing": "—"}


def field_cards(r: dict, explain: dict, doubt_first: bool = True) -> str:
    """Карточки полей: зелёная — уверенно, жёлтая — сомнение (проверить), серая — нет данных; с объяснением."""
    order = [f for f in FIELDS if f in explain]
    if doubt_first:
        rank = {"doubt": 0, "ok": 1, "missing": 2}
        order.sort(key=lambda f: rank.get(explain[f]["status"], 3))
    cards = []
    for f in order:
        e = explain[f]
        val = r.get(f)
        val = "—" if val is None or (isinstance(val, float) and pd.isna(val)) or val == "" else val
        cards.append(f'<div class="ja-field {e["status"]}"><div class="lbl">{STATUS_ICON[e["status"]]} '
                     f'{html.escape(LABELS[f])}</div><div class="val">{html.escape(str(val))}</div>'
                     f'<div class="why">{html.escape(e["text"])}</div></div>')
    return '<div class="ja-fields">' + "".join(cards) + "</div>"


def save_corrections(rows: pd.DataFrame) -> str:
    """Отдаёт правки в API: он сохраняет их и сразу учит справочник. Возвращает текст для пользователя."""
    recs = [{k: (None if pd.isna(r[k]) or r[k] == "" else str(r[k])) for k in ["id", "raw_address", *FIELDS]}
            for _, r in rows.iterrows()]
    try:
        resp = requests.post(f"{API_URL}/api/v1/corrections", json=recs, headers=api_headers(), timeout=30)
        resp.raise_for_status()
        learned = resp.json()["learned"]
    except Exception as e:
        # API недоступен — правки не теряем, пишем локально
        CORRECTIONS.parent.mkdir(parents=True, exist_ok=True)
        with CORRECTIONS.open("a", encoding="utf-8") as f:
            for rec in recs:
                rec["corrected_at"] = dt.datetime.now().isoformat(timespec="seconds")
                rec["corrected_by"] = USER["login"]
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return f"Сохранено правок: {len(recs)} (локально, API недоступен: {e})"
    parts = []
    if learned["districts"]:
        parts.append(f"районов для адресов: {learned['districts']}")
    if learned["living_areas"]:
        parts.append("мкр/ЖК: " + ", ".join(learned["living_areas"]))
    tail = f" Справочник выучил — {'; '.join(parts)}." if parts else ""
    return f"Сохранено правок: {len(recs)}.{tail}"


def export_json(df: pd.DataFrame) -> bytes:
    cols = ["id", "raw_address", *FIELDS, "confidence"]
    clean = df[cols].astype(object).where(df[cols].notna(), None)
    recs = clean.to_dict(orient="records")
    for r in recs:
        for f in FIELDS:
            if r[f] == "":
                r[f] = None
    return json.dumps(recs, ensure_ascii=False, indent=2).encode("utf-8")


# ---------- Sidebar ----------
with st.sidebar:
    st.title("📮 Jana Address")
    u1, u2 = st.columns([3, 2])
    role = ROLES.get(USER["role"], USER["role"])
    u1.markdown(f"👤 **{USER['name']}**" + (f"  \n{role}" if role != USER["name"] else ""))
    if u2.button("Выйти", use_container_width=True):
        st.session_state.clear()
        st.rerun()
    h = api_health()
    if h.get("status") == "ok":
        g = h["gazetteer"]
        st.success("API работает")
        st.caption("Справочник")
        c1, c2 = st.columns(2)
        c1.metric("Городов / н.п.", f"{g.get('cities', 0):,}".replace(",", " "))
        c2.metric("Районов", f"{g.get('districts', 0):,}".replace(",", " "))
        c1.metric("Мкр / ЖК", f"{g.get('living_areas', 0):,}".replace(",", " "))
        c2.metric("Улиц", f"{g.get('streets', 0):,}".replace(",", " "))
        st.caption("Источники: " + " · ".join(source_label(s) for s in g.get("sources", [])))
        lr = h.get("learned") or {}
        if lr.get("corrections"):
            st.caption(f"🧠 Выучено на правках: {lr['corrections']} правок → районов для {lr['houses']} домов "
                       f"и {lr['streets']} улиц, мкр/ЖК: {lr['living_areas']}")
    else:
        st.error(f"API недоступен: {API_URL}")
    threshold = st.slider("Порог «неуверенной» записи", 0.0, 1.0, 0.7, 0.05, key="threshold",
                          help="Записи с уверенностью ниже порога считаются неуверенными и показываются первыми")

IS_ADMIN = USER["role"] == "admin"

# ---------- Шапка ----------
_g = h.get("gazetteer", {}) if h.get("status") == "ok" else {}
_net = h.get("internet")
_net_badge = ('<span class="ja-badge ok">🔒 Офлайн: интернета нет — всё работает</span>' if _net is False
              else '<span class="ja-badge">🔒 Работает без интернета · сейчас сеть есть, но не используется</span>')


def _num(n) -> str:
    return f"{n or 0:,}".replace(",", " ")


st.markdown(
    '<div class="ja-hero"><div><h2>📮 Jana Address</h2>'
    '<p>Адрес одной строкой → 14 полей для логистики, с объяснением каждого решения</p></div>'
    f'<div class="ja-badges">{_net_badge}'
    f'<span class="ja-badge">📚 {_num(_g.get("streets"))} улиц · {_num(_g.get("buildings"))} зданий OSM</span>'
    '<span class="ja-badge">🧠 учится на правках</span></div></div>',
    unsafe_allow_html=True)
_hc1, _hc2 = st.columns([2, 7])
if _hc1.button("🔄 Проверить сеть", help="Статус сети кешируется на 5 секунд — нажмите после отключения "
               "или включения интернета, чтобы не ждать"):
    api_health.clear()
    st.rerun()

tabs = st.tabs(["📂 Пакетный разбор", "🧭 Умная проверка", "🔎 Один адрес", "📊 Качество",
                *(["📜 Журнал правок"] if IS_ADMIN else [])])
tab_batch, tab_review, tab_one, tab_quality = tabs[0], tabs[1], tabs[2], tabs[3]

# ---------- Пакет ----------
with tab_batch:
    up = st.file_uploader("Файл с адресами: JSON (как sample_input.json), CSV или TXT (по строке на адрес)",
                          type=["json", "csv", "txt"])
    if st.session_state.get("df") is None:
        st.markdown(
            '<div class="ja-steps">'
            '<div class="ja-step"><b>1</b>Загрузите файл<span>JSON, CSV или TXT — по одному адресу в строке. '
            'Нет файла? Нажмите «демо-набор» ниже.</span></div>'
            '<div class="ja-step"><b>2</b>Проверьте сомнительное<span>Сверху — записи, в которых парсер не уверен. '
            'Цветом видно, что стало какой частью адреса.</span></div>'
            '<div class="ja-step"><b>3</b>Поправьте и скачайте<span>Двойной клик по ячейке — правка. '
            'Результат — JSON в формате задания или CSV.</span></div></div>',
            unsafe_allow_html=True)
    demo = st.button(f"📋 Загрузить демо-набор ({DEMO.name})", disabled=not DEMO.exists())
    st.caption("Демо-набор: примеры из задания и живые адреса — чтобы посмотреть работу без своего файла.")

    def run_batch(recs, file_id):
        with st.spinner(f"Разбираем {len(recs)} адресов…"):
            t = time.time()
            try:
                st.session_state.df = to_df(api_batch(recs))
                st.session_state.elapsed = time.time() - t
                st.session_state.file_id = file_id
            except Exception as e:
                st.error(f"Ошибка API: {e}")

    if up and st.session_state.get("file_id") != (up.name, up.size):
        run_batch(read_upload(up), (up.name, up.size))
    elif demo:
        run_batch(json.loads(DEMO.read_text(encoding="utf-8")), ("demo", 0))

    df: pd.DataFrame | None = st.session_state.get("df")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)
    if df is not None:
        low = df["confidence"] < threshold
        m = st.columns(5)
        m[0].metric("Записей", len(df))
        m[1].metric("Неуверенных", int(low.sum()))
        m[2].metric("Ср. уверенность", f"{df['confidence'].mean():.2f}")
        m[3].metric("Исправлено", int(df["исправлено"].sum()))
        el = st.session_state.get("elapsed", 0)
        m[4].metric("Время", f"{el:.1f} с" if el >= 1 else f"{el * 1000:.0f} мс")

        if low.any():
            reasons = pd.Series([FLAG_LABELS.get(f, f) for fl in df.loc[low, "_flags"] for f in fl])
            with st.expander(f"Почему записи неуверенные ({int(low.sum())})"):
                if reasons.empty:
                    st.caption("Флагов нет — уверенность снижена по совокупности полей.")
                else:
                    counts = reasons.value_counts().rename_axis("Причина").reset_index(name="Записей")
                    st.dataframe(counts, hide_index=True, use_container_width=True)

        with st.expander("📈 Для логистики: города, районы и что уточнить у клиента"):
            def _missing(r):
                need = []
                if not r.get("city"):
                    need.append("город")
                if not r.get("house") and r.get("street_type") not in ("квартал",):
                    need.append("дом")
                if r.get("house") and not r.get("apartment") and (r.get("entrance") or r.get("floor")):
                    need.append("квартиру")
                return ", ".join(need)
            ask = df.apply(_missing, axis=1)
            a1, a2, a3 = st.columns(3)
            a1.metric("Готовы к доставке", int((ask == "").sum()))
            a2.metric("Нужно уточнить у клиента", int((ask != "").sum()))
            a3.metric("Район известен", f"{df['district'].notna().mean():.0%}")
            g1, g2 = st.columns(2)
            by_city = df["city"].fillna("не определён").value_counts().head(10)
            g1.caption("Заказы по городам")
            g1.bar_chart(by_city, horizontal=True)
            by_dist = df.loc[df["district"].notna(), "district"].value_counts().head(10)
            g2.caption("Заказы по районам")
            g2.bar_chart(by_dist, horizontal=True)
            todo = df.loc[ask != "", ["id", "raw_address"]].assign(уточнить=ask[ask != ""])
            if not todo.empty:
                st.caption("Кому написать до выезда курьера, чтобы он не звонил на месте:")
                st.dataframe(todo.assign(сообщение="Здравствуйте! Уточните, пожалуйста, " + todo["уточнить"]
                                         + " для доставки Jana Post."),
                             hide_index=True, use_container_width=True, height=220)

        c1, c2 = st.columns([1, 3])
        only_low = c1.toggle("Только неуверенные", value=True)
        query = c2.text_input("Поиск по строке", placeholder="например: Абая")
        view = df[low] if only_low else df
        if query:
            hit = view[view["raw_address"].str.contains(query, case=False, na=False, regex=False)]
            if hit.empty and only_low:      # среди неуверенных нет — ищем по всем, а не показываем пустоту
                hit = df[df["raw_address"].str.contains(query, case=False, na=False, regex=False)]
                if not hit.empty:
                    st.info(f"Среди неуверенных «{query}» нет — показаны совпадения по всем записям")
            view = hit

        # ---- Фильтры ----
        FKEYS = {"f_letter": "Все", "f_city": [], "f_district": [], "f_conf": (0.0, 1.0), "f_doubt": [],
                 "f_via": [], "f_left": False, "f_courier": False}

        def _reset_filters():
            for k_, v_ in FKEYS.items():
                st.session_state[k_] = v_

        active = sum(1 for k_, v_ in FKEYS.items() if k_ in st.session_state and st.session_state[k_] != v_)
        LAT2CYR = str.maketrans("ABCEHKMOPTXY", "АВСЕНКМОРТХУ")

        def first_letter(s):
            m = re.search(r"[A-Za-zА-Яа-яЁёӘәӨөҮүҰұҚқҒғҢңІі]", str(s or ""))
            return m.group(0).upper().translate(LAT2CYR) if m else None

        title = "🎛 Фильтры и сортировка" + (f" · активно: {active}" if active else "")
        with st.expander(title, expanded=bool(active)):
            l1, l2 = st.columns([1, 3])
            by = l1.radio("Буква по полю", ["Улица", "Город", "Район"], horizontal=True, key="f_by",
                          on_change=lambda: st.session_state.update(f_letter="Все"))
            by_col = {"Улица": "street_name", "Город": "city", "Район": "district"}[by]
            lc = df[by_col].map(first_letter).value_counts()
            letters = sorted(lc.index)
            if st.session_state.get("f_letter") not in ["Все", *letters]:
                st.session_state.f_letter = "Все"
            letter = l2.pills(f"Первая буква ({by.lower()})", ["Все", *letters], key="f_letter",
                              format_func=lambda x: x if x == "Все" else f"{x} · {lc[x]}")
            f1, f2 = st.columns(2)
            cc = df["city"].value_counts()
            st.session_state.f_city = [c for c in st.session_state.get("f_city", []) if c in cc.index]
            sel_city = f1.multiselect("Город", list(cc.index), placeholder="все города", key="f_city",
                                      format_func=lambda x: f"{x} ({cc[x]})")
            in_city = df[df["city"].isin(sel_city)] if sel_city else df
            dc = in_city["district"].value_counts()
            st.session_state.f_district = [d for d in st.session_state.get("f_district", []) if d in dc.index]
            sel_district = f2.multiselect("Район", list(dc.index), placeholder="все районы", key="f_district",
                                          format_func=lambda x: f"{x} ({dc.get(x, 0)})")
            f3, f4 = st.columns(2)
            conf = f3.slider("Уверенность", 0.0, 1.0, (0.0, 1.0), 0.05, key="f_conf")
            sel_doubt = f4.multiselect("Сомнение в поле", [LABELS[f] for f in FIELDS], key="f_doubt",
                                       placeholder="любые", help="Записи, где именно это поле стоит проверить")
            f5, f6, f7 = st.columns(3)
            sel_via = f5.multiselect("Как найден район", ["из строки", *VIA_LABELS.values()], placeholder="любой",
                                     key="f_via")
            only_left = f6.checkbox("Есть неразобранный текст", key="f_left")
            only_courier = f7.checkbox("Есть пометка курьеру", key="f_courier")
            s1, s2 = st.columns([3, 1])
            sort_by = s1.radio("Сортировка", ["Сначала сомнительные", "Улица А→Я", "Город А→Я"], horizontal=True)
            s2.button("✖ Сбросить фильтры", on_click=_reset_filters, disabled=not active, use_container_width=True)

        if letter and letter != "Все":
            view = view[view[by_col].map(first_letter) == letter]
        if sel_city:
            view = view[view["city"].isin(sel_city)]
        if sel_district:
            view = view[view["district"].isin(sel_district)]
        view = view[view["confidence"].between(*conf)]
        if sel_doubt:
            view = view[view["⚠ сомнения"].map(lambda s: any(d in s.split(", ") for d in sel_doubt))]
        if sel_via:
            view = view[view["район найден"].isin(sel_via)]
        if only_left:
            view = view[view["не разобрано"] != ""]
        if only_courier:
            view = view[view["курьеру"] != ""]
        if sort_by == "Улица А→Я":
            view = view.sort_values("street_name", key=lambda s: s.fillna("").str.lower())
        elif sort_by == "Город А→Я":
            view = view.sort_values(["city", "street_name"], key=lambda s: s.fillna("").str.lower())
        st.caption(f"Показано записей: {len(view)} из {len(df)}")

        st.caption("Двойной клик по ячейке — правка; галочка «Верно» — принять разбор как есть. "
                   "Проверенная запись получает уверенность 1.0 и уходит из списка неуверенных.")
        edited = st.data_editor(
            view.drop(columns=INTERNAL),
            key=f"editor_{only_low}_{query}_{threshold}_{len(view)}",
            use_container_width=True,
            height=min(560, 35 * (len(view) + 1) + 3),
            hide_index=True,
            # Сначала то, что нужно для проверки: строка, уверенность, остаток, потом поля для правки
            column_order=["верно", "raw_address", "confidence", "⚠ сомнения", "не разобрано", "street_name", "house",
                          "apartment",
                          "entrance", "floor", "city", "province", "district", "район найден", "living_area",
                          "street_type", "landmark", "курьеру", "почему не уверены", "из справочника", "id",
                          "исправлено"],
            disabled=["id", "raw_address", "confidence", "из справочника", "почему не уверены",
                      "не разобрано", "район найден", "исправлено", "курьеру", "⚠ сомнения"],
            column_config={
                "⚠ сомнения": st.column_config.TextColumn(
                    "⚠ Проверить поля", help="Какие именно поля под сомнением — их и стоит проверить"),
                "верно": st.column_config.CheckboxColumn("✓ Верно", help="Разбор правильный — принять как есть"),
                "район найден": st.column_config.TextColumn(help="Как восстановлен район, которого нет в строке"),
                "raw_address": st.column_config.TextColumn("Исходная строка", width="medium"),
                "не разобрано": st.column_config.TextColumn(
                    "⚠️ Не разобрано", help="Часть строки, которую парсер не отнёс ни к одному полю"),
                "почему не уверены": st.column_config.TextColumn(help="Флаги ядра разбора, снизившие confidence"),
                "курьеру": st.column_config.TextColumn(
                    "📦 Курьеру", help="Не адрес, но полезно курьеру: получатель, телефон, домофон, пометка"),
                "confidence": st.column_config.ProgressColumn("Уверенность", min_value=0, max_value=1, format="%.2f"),
                "street_type": st.column_config.SelectboxColumn(LABELS["street_type"], options=STREET_TYPES),
                **{f: st.column_config.TextColumn(LABELS[f]) for f in FIELDS if f != "street_type"},
            },
        )
        b1, b2, b3 = st.columns(3)
        if b1.button("💾 Сохранить правки", type="primary"):
            changed = []
            for idx in edited.index:
                fixed = not edited.loc[idx, FIELDS].fillna("").equals(df.loc[idx, FIELDS].fillna(""))
                if fixed or bool(edited.loc[idx, "верно"]):
                    df.loc[idx, FIELDS] = edited.loc[idx, FIELDS]
                    df.loc[idx, "confidence"] = 1.0
                    df.loc[idx, "исправлено"] = True
                    changed.append(idx)
            if changed:
                st.session_state.flash = save_corrections(df.loc[changed])
                st.session_state.df = df
                st.rerun()
            else:
                st.info("Изменений нет")
        b2.download_button("⬇️ Скачать JSON", export_json(df), "output.json", "application/json")
        csv = df.drop(columns=[*INTERNAL, "верно"]).to_csv(index=False).encode("utf-8-sig")
        b3.download_button("⬇️ Скачать CSV", csv, "output.csv", "text/csv")

# ---------- Один адрес ----------
with tab_one:
    st.session_state.setdefault("one_q", "город Алматы/Алматы қаласы, Мкр Аксай 3а, 59, кв 70")

    def _use_candidate(old: str, new: str):
        cur = st.session_state.one_q
        i = cur.lower().find(old.lower())
        if i >= 0:
            st.session_state.one_q = cur[:i] + new + cur[i + len(old):]

    q = st.text_input("Адрес", key="one_q")
    if q:
        try:
            r = requests.get(f"{API_URL}/api/v1/parse", params={"q": q}, timeout=10).json()
            src = r.get("_source", {})
            st.markdown(f'<div style="font-size:1.15rem;line-height:2.1">{highlight(q, r)}</div>',
                        unsafe_allow_html=True)
            k1, k2, k3 = st.columns([1, 1, 3])
            k1.metric("Уверенность", f"{r['confidence']:.2f}")
            k2.metric("Проверить полей", len(r.get("_doubt") or []))
            if r.get("_courier"):
                k3.info("📦 Курьеру: " + courier_text(r["_courier"]))
            st.markdown("**Почему так — объяснение каждого поля**")
            st.markdown(field_cards(r, r.get("_explain") or {}), unsafe_allow_html=True)
            for fld, cands in (r.get("_suggestions") or {}).items():
                old = r.get(fld) or ""
                st.markdown(f"💡 **Возможно, вы имели в виду** ({LABELS[fld].lower()} «{html.escape(old)}» "
                            "нет в справочнике или есть похожие). Нажмите, чтобы подставить в строку:")
                cols = st.columns(min(len(cands), 4) or 1)
                for i, c in enumerate(cands[:4]):
                    cols[i].button(f"{c['value']}  ·  {c['score']:.0%}", key=f"sug_{fld}_{i}",
                                   on_click=_use_candidate, args=(old, c["value"]),
                                   disabled=not old or old.lower() not in q.lower(), use_container_width=True)
                st.caption("Сама система ничего не подменяет: в ответ идёт то, что написано, пока оператор не выберет.")
            for note in r.get("_notes") or []:
                st.markdown(f'<div class="ja-note">ℹ️ {html.escape(note)}</div>', unsafe_allow_html=True)
            with st.expander("Ответ в формате жюри (JSON)"):
                st.json({k: v for k, v in r.items() if not k.startswith("_")})
        except Exception as e:
            st.error(f"Ошибка API: {e}")


# ---------- Умная проверка: по одной записи, сомнительные поля подсвечены ----------
with tab_review:
    df = st.session_state.get("df")
    if df is None:
        st.info("Сначала загрузите файл или демо-набор на вкладке «📂 Пакетный разбор».")
    else:
        queue = df[needs_review(df)]
        queue = queue[~queue["исправлено"]].sort_values("confidence", kind="stable")
        done = int(df["исправлено"].sum())
        st.progress(done / max(1, done + len(queue)),
                    text=f"Проверено {done} · осталось {len(queue)} сомнительных записей (самые сомнительные первыми)")
        if queue.empty:
            st.success("🎉 Все сомнительные записи проверены. Результат можно скачать на вкладке «Пакетный разбор».")
        else:
            pos = min(st.session_state.get("review_pos", 0), len(queue) - 1)
            idx = queue.index[pos]
            row = df.loc[idx]
            rec = {f: (None if pd.isna(row[f]) else row[f]) for f in FIELDS}
            st.markdown(f"**Запись {pos + 1} из {len(queue)}** · уверенность {row['confidence']:.2f}")
            st.markdown(f'<div style="font-size:1.15rem;line-height:2.1">'
                        f'{highlight(row["raw_address"], {**rec, "_courier": row["_courier"]})}</div>',
                        unsafe_allow_html=True)
            st.markdown(field_cards(rec, row["_explain"]), unsafe_allow_html=True)
            for note in row["_notes"]:
                st.markdown(f'<div class="ja-note">ℹ️ {html.escape(note)}</div>', unsafe_allow_html=True)

            doubtful = [f for f in FIELDS if row["_explain"].get(f, {}).get("status") == "doubt"]
            editable = doubtful + [f for f in ("street_name", "house", "city", "district") if f not in doubtful]
            st.markdown("**Исправьте сомнительные поля** (остальные можно оставить как есть):")
            new_vals = {}
            cols = st.columns(min(4, len(editable)))
            for i, f in enumerate(editable):
                key = f"rv_{idx}_{f}"
                cands = row["_suggestions"].get(f) or []
                if key not in st.session_state:
                    st.session_state[key] = rec[f] or ""
                with cols[i % len(cols)]:
                    new_vals[f] = st.text_input(("⚠️ " if f in doubtful else "") + LABELS[f], key=key)
                    for c in cands:
                        if st.button(f"💡 {c['value']} ({c['score']:.0%})", key=f"{key}_{c['value']}",
                                     help="Кандидат из справочника — подставится только по вашему нажатию"):
                            st.session_state[key] = c["value"]
                            st.rerun()

            a1, a2, a3, a4 = st.columns(4)

            def commit(values: dict):
                for f, val in values.items():
                    df.loc[idx, f] = val or None
                df.loc[idx, "confidence"] = 1.0
                df.loc[idx, "исправлено"] = True
                st.session_state.flash = save_corrections(df.loc[[idx]])
                st.session_state.df = df
                st.rerun()

            if a1.button("✅ Верно, как есть", use_container_width=True):
                commit({})
            if a2.button("💾 Сохранить исправления", type="primary", use_container_width=True):
                commit({f: v.strip() for f, v in new_vals.items() if (v or "") != (rec[f] or "")})
            if a3.button("⏭ Пропустить", use_container_width=True):
                st.session_state.review_pos = (pos + 1) % len(queue)
                st.rerun()
            if a4.button("⏮ Назад", use_container_width=True, disabled=pos == 0):
                st.session_state.review_pos = pos - 1
                st.rerun()
            if msg := st.session_state.pop("flash", None):
                st.success(msg)


# ---------- Качество: анализ ошибок и скорости ----------
BENCH = Path(os.environ.get("JANA_DATA_DIR", "data")) / "demo" / "benchmark_gold.json"


def show_report(rep: dict):
    s = rep["summary"]
    m = st.columns(5)
    m[0].metric("Оценка по весам жюри", f"{rep['score']:.1%}" if rep["score"] is not None else "—")
    m[1].metric("Записей", rep["records"])
    m[2].metric("С ошибкой", rep["records_with_errors"])
    m[3].metric("На ручную проверку", f"{s['needs_review']} ({s['needs_review_share']:.0%})")
    m[4].metric("Скорость на 1000", f"{s['seconds_per_1000']:.2f} с")
    st.caption("Группы как у жюри: адрес 60% (область, город, улица, дом), детали 25% (подъезд, этаж), "
               "справочные 15% (район, мкр). Квартира не оценивается (ответ организаторов) — показана справочно.")
    g = st.columns(3)
    for col, (name, title) in zip(g, [("address", "Адрес · 60%"), ("details", "Детали · 25%"),
                                      ("reference", "Справочные · 15%")], strict=True):
        acc = rep["groups"][name]["accuracy"]
        col.metric(title, f"{acc:.1%}" if acc is not None else "нет данных")
    rows = [{"Поле": LABELS[f], "Точность": a["accuracy"], "Оценено": a["total"], "Верно": a["ok"],
             "Неверно": a["wrong"], "Пусто": a["null"]} for f, a in rep["fields"].items() if a["total"]]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, column_config={
        "Точность": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent")})
    if rep["examples"]:
        st.markdown("**Самые частые ошибки — с примерами**")
        ex = [{"Поле": LABELS[f], "Строка": e["raw_address"], "Получили": e["got"], "Ожидали": e["expected"]}
              for f, items in rep["examples"].items() for e in items]
        st.dataframe(pd.DataFrame(ex), hide_index=True, use_container_width=True)
    else:
        st.success("Ошибок нет.")


with tab_quality:
    st.markdown("#### Текущий пакет")
    df = st.session_state.get("df")
    if df is None:
        st.caption("Загрузите файл или демо-набор — здесь появится сводка по нему.")
    else:
        n = len(df)
        el = st.session_state.get("elapsed", 0) or 0
        q1, q2, q3 = st.columns(3)
        need = int((needs_review(df) & ~df["исправлено"]).sum())
        q1.metric("Требуют проверки", f"{need} из {n}",
                  help="Уверенность ниже порога или поле под сомнением — та же очередь, что в «Умной проверке»")
        q2.metric("Проверено вручную", int(df["исправлено"].sum()))
        q3.metric("Время на 1000 (в интерфейсе)", f"{el / n * 1000:.1f} с" if n else "—",
                  help="С объяснениями и кандидатами. Чистый API для жюри быстрее — см. проверку по эталону")
        fill = pd.DataFrame({"Поле": [LABELS[f] for f in FIELDS],
                             "Заполнено": [df[f].notna().mean() for f in FIELDS],
                             "Под сомнением": [df["_doubt"].map(lambda d, f=f: f in d).mean() for f in FIELDS]})
        st.dataframe(fill, hide_index=True, use_container_width=True, column_config={
            "Заполнено": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent"),
            "Под сомнением": st.column_config.ProgressColumn(min_value=0, max_value=1, format="percent")})

    st.markdown("#### Проверка по эталону")
    st.caption("Файл с правильными ответами в формате sample_output.json — система разберёт raw_address "
               "и сравнит каждое поле по правилам жюри.")
    gold_file = st.file_uploader("Эталон (JSON)", type=["json"], key="gold_up")
    run_bench = st.button(f"▶ Контрольный набор ({BENCH.name}: 1000 «грязных» адресов)", disabled=not BENCH.exists())
    gold = None
    if gold_file is not None:
        gold = json.loads(gold_file.getvalue().decode("utf-8-sig"))
    elif run_bench:
        gold = json.loads(BENCH.read_text(encoding="utf-8"))
    if gold is not None:
        with st.spinner(f"Разбираем и сравниваем {len(gold)} записей…"):
            try:
                resp = requests.post(f"{API_URL}/api/v1/evaluate", json=gold, params={"threshold": threshold},
                                     timeout=600)
                resp.raise_for_status()
                st.session_state.report = resp.json()
            except Exception as e:
                st.error(f"Ошибка API: {e}")
    if st.session_state.get("report"):
        show_report(st.session_state.report)
        st.caption("Контрольный набор — наша синтетика из реальных улиц и домов OSM с «грязью» реальных заказов "
                   "(телефоны, имена, опечатки). Это не официальная метрика: на реальных адресах будет ниже.")


# ---------- Журнал правок (только администратор) ----------
if IS_ADMIN:
    with tabs[4]:
        st.caption("Кто, когда и что исправил. Эти правки учат справочник, поэтому журнал позволяет "
                   "проверить работу операторов и найти ошибочную правку.")
        try:
            resp = requests.get(f"{API_URL}/api/v1/corrections", params={"limit": 500},
                                headers=api_headers(), timeout=10)
            resp.raise_for_status()
            journal = resp.json()
        except Exception as e:
            journal = None
            st.error(f"Не удалось получить журнал: {e}")
        if journal == []:
            st.info("Правок пока нет.")
        elif journal:
            jdf = pd.DataFrame(journal)
            by = jdf["corrected_by"].fillna("—") if "corrected_by" in jdf else pd.Series(["—"] * len(jdf))
            m = st.columns(3)
            m[0].metric("Правок", len(jdf))
            m[1].metric("Операторов", by.nunique())
            m[2].metric("С районом", int(jdf["district"].notna().sum()) if "district" in jdf else 0)
            cols = [c for c in ["corrected_at", "corrected_by", "raw_address", "city", "district",
                                "street_name", "house", "living_area"] if c in jdf]
            st.dataframe(jdf[cols], hide_index=True, use_container_width=True, column_config={
                "corrected_at": st.column_config.TextColumn("Когда (UTC)"),
                "corrected_by": st.column_config.TextColumn("Кто"),
                "raw_address": st.column_config.TextColumn("Исходная строка", width="large"),
                **{f: st.column_config.TextColumn(LABELS[f]) for f in FIELDS if f in cols},
            })
