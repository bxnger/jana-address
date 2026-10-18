"""Объяснимость: почему каждое поле получило своё значение (или осталось пустым).

Для каждого поля — короткая фраза человеческим языком и статус:
  ok      — значение есть и подтверждено (ключевое слово, справочник, правка оператора);
  doubt   — значение есть, но парсер сомневается: это поле стоит проверить;
  missing — значения нет, и объяснено почему.

Объяснения строятся из того, что пайплайн и так знает: флаги ядра, источник поля
(строка / справочник / правка), путь восстановления района, вырезанный шум,
неразобранный остаток и кандидаты из справочника. Никакой «магии» сверху.
"""
from __future__ import annotations

import re

FIELDS = ["province", "city", "district", "living_area", "street_type", "street_name",
          "house", "apartment", "entrance", "floor", "landmark"]

# Флаг ядра -> (поле, которое из-за него сомнительно, причина человеческим языком)
DOUBT_FLAGS = {
    "street_not_in_gazetteer": ("street_name", "такой улицы нет в справочнике этого города"),
    "street_long": ("street_name", "подозрительно длинное название — возможно, попал лишний текст"),
    "street_comma_split": ("street_name", "после улицы остался лишний сегмент — возможно, угол двух улиц"),
    "street_from_city": ("street_name", "единственное название при доме принято за улицу"),
    "recipient_guess": ("street_name", "рядом убрано слово, похожее на имя получателя"),
    "city_by_position": ("city", "угадан по положению в строке"),
    "city_ambiguous": ("city", "название бывает и городом, и мкр/улицей"),
    "city_street_swapped": ("city", "город и улица в строке переставлены"),
    "province_city_conflict": ("province", "область в строке не совпадает с областью города"),
    "province_from_ambiguous_city": ("province", "выведена по неоднозначному названию города"),
    "apartment_hyphen": ("apartment", "через дефис после дома («150-45») — принятое соглашение"),
    "apartment_bare_number": ("apartment", "голое число после дома"),
    "unit_direction_default": ("house", "номер без слова «дом» — по порядку чисел"),
    "building_part": ("house", "в номере есть корпус/блок"),
    "living_area_demoted": ("living_area", "мкр без типа рядом с улицей"),
}

_UNIT_WORDS = {
    "apartment": r"кв(?:артира)?|квартра|flat|kv|apt|пәтер|офис|оф",
    "entrance": r"подъезд|подьезд|под\.?|п-д|кіреберіс|pod|entrance",
    "floor": r"этаж|эт\.?|қабат|floor|etazh",
    "house": r"дом|д\.|үй|dom|house",
}
_LANDMARK_MARKERS = r"магазин|маг\.|тц|трц|бц|возле|напротив|рядом|около|школа|аптека|рынок|базар|qazpost|казпочта"
_VIA_TEXT = {
    "building": "по номеру дома: это здание есть в справочнике OSM со своим районом",
    "building_base": "по дому без литеры/дроби: у всех таких домов на улице один район",
    "street": "по улице: в справочнике она целиком в одном районе",
    "living_area": "по микрорайону: он целиком в одном районе",
    "learned_house": "правка оператора для этого дома",
    "learned_street": "правка оператора для этой улицы",
}


def _keyword_near(raw: str, field: str, value: str) -> str | None:
    """Какое ключевое слово стоит рядом с номером: «кв 70», «4подьезд», «9 этаж»."""
    num = re.escape(value)
    words = _UNIT_WORDS[field]
    m = (re.search(rf"(?i)({words})\s*[-#№:.]?\s*{num}(?![\w/])", raw)
         or re.search(rf"(?i)(?<![\w/]){num}\s*-?(?:й|ый)?\s*({words})", raw))
    return m.group(1) if m else None


def explain(out: dict, raw: str | None, flags: list[str], src: dict, via: str | None,
            noise: list[str], leftover, suggestions: dict, kato: dict | None = None) -> dict:
    """-> {"fields": {поле: {"status", "text"}}, "doubt": [поля], "notes": [общие замечания]}."""
    raw = raw if isinstance(raw, str) else ""
    flags = set(flags or [])
    reasons: dict[str, str] = {}
    for fl in sorted(flags):
        if fl in DOUBT_FLAGS:
            f, why = DOUBT_FLAGS[fl]
            reasons.setdefault(f, why)
    doubt = set(reasons)
    fields: dict[str, dict] = {}

    def put(f, status, text):
        fields[f] = {"status": status, "text": text}

    v = out.get
    # --- область ---
    if v("province"):
        if src.get("province") == "gazetteer":
            put("province", "ok", f"восстановлена по городу «{v('city')}» из справочника" if v("city")
                else "восстановлена по району из справочника")
        else:
            put("province", "ok", "указана в строке")
        if "province_city_conflict" in flags:
            put("province", "doubt", "в строке область не совпадает с областью города — оставлена как написано")
        elif "province_from_ambiguous_city" in flags:
            put("province", "doubt", "выведена по названию, которое бывает и городом, и улицей/мкр")
    else:
        put("province", "missing", "не определена: в строке нет ни области, ни узнаваемого города")

    # --- город ---
    if v("city"):
        if "city_from_district" in flags or src.get("city") == "gazetteer":
            put("city", "ok", f"по району «{v('district')}»: в справочнике он относится к этому городу")
        elif "city_marker" in flags:
            put("city", "ok", "найден по слову «г.» / «город» / «қаласы»")
        elif "city_no_comma" in flags:
            put("city", "ok", "отделён от улицы без запятой: остаток — известная улица этого города")
        elif "city_by_position" in flags:
            put("city", "doubt", "угадан по положению в строке (стоит перед улицей)")
        elif "city_ambiguous" in flags:
            put("city", "doubt", "название бывает и городом, и микрорайоном/улицей")
        else:
            put("city", "ok", "крупный город узнан по названию")
    else:
        put("city", "missing", "не определён: в строке нет города, а по одной улице его не восстановить "
                               "(одноимённые улицы есть во многих городах)")

    # --- район ---
    if v("district"):
        if src.get("district") in ("gazetteer", "learned") and via in _VIA_TEXT:
            put("district", "ok", f"в строке нет — восстановлен {_VIA_TEXT[via]}")
        elif "district_canonical" in flags:
            put("district", "ok", "указан в строке по-казахски, приведён к названию из справочника")
        else:
            put("district", "ok", "указан в строке")
    elif v("city") and suggestions.get("city"):
        put("district", "missing", "не восстановлен: город не найден в справочнике (см. похожие варианты)")
    elif v("city") and v("street_name"):
        put("district", "missing", "не восстановлен: улица проходит через несколько районов или её/дома нет "
                                   "в справочнике — лучше пусто, чем неверный район")
    else:
        put("district", "missing", "не определён: недостаточно данных (нет города или улицы)")

    # --- микрорайон / ЖК ---
    if v("living_area"):
        put("living_area", "ok", "указан в строке отдельно от улицы" if src.get("living_area") == "text"
            else "восстановлен по улице из справочника")
    else:
        put("living_area", "missing", "в строке нет микрорайона/ЖК отдельно от улицы")

    # --- тип улицы ---
    if v("street_type"):
        put("street_type", "ok", "взят из справочника улиц (в строке не указан)"
            if src.get("street_type") == "gazetteer" else "по слову типа в строке («ул.», «пр.», «мкр»…)")
    else:
        put("street_type", "missing", "в строке тип не указан, а в справочнике улица неоднозначна или её нет")

    # --- улица ---
    if v("street_name"):
        if "street_not_in_gazetteer" in flags:
            sug = suggestions.get("street_name") or []
            tail = (" Похожие в справочнике: " + ", ".join(f"«{s['value']}»" for s in sug)) if sug else ""
            put("street_name", "doubt", "такой улицы нет в справочнике этого города — возможна опечатка." + tail)
        elif "street_comma_in_name" in flags:
            put("street_name", "ok", "запятая внутри названия: такое название есть в справочнике улиц")
        elif "street_in_gazetteer" in flags:
            put("street_name", "ok", "есть в справочнике улиц города; записано как в строке")
        elif "area_number_absorbed" in flags:
            put("street_name", "ok", "номер микрорайона — часть названия («Аксай 3а»), записано как в строке")
        elif "street_long" in flags:
            put("street_name", "doubt", "подозрительно длинное название — возможно, в него попал лишний текст")
        else:
            put("street_name", "ok", "текст перед номером дома; записано как в строке, без исправлений")
    else:
        put("street_name", "missing", "не найдена: в строке нет текста, похожего на улицу или микрорайон")

    # --- дом, квартира, подъезд, этаж ---
    for f, what in (("house", "дом"), ("apartment", "квартира"), ("entrance", "подъезд"), ("floor", "этаж")):
        val = v(f)
        if not val:
            put(f, "missing", f"в строке нет — {what} не указан")
            continue
        kw = _keyword_near(raw, f, str(val))
        if f == "house" and "unit_direction_default" in flags and not kw:
            put(f, "doubt", "номер без слова «дом» — принят за дом по порядку чисел")
        elif f == "apartment" and "apartment_hyphen" in flags:
            put(f, "doubt", "через дефис после дома («150-45» → дом 150, кв 45) — принятое соглашение")
        elif f == "apartment" and "apartment_bare_number" in flags:
            put(f, "doubt", "голое число после дома — принято за квартиру")
        elif kw:
            put(f, "ok", f"по ключевому слову «{kw}»")
        elif f == "house":
            frac = " с дробью/литерой" if re.search(r"[/a-zа-я]", str(val), re.I) else ""
            put(f, "ok", f"номер{frac} сразу после названия улицы")
        else:
            put(f, "ok", "по порядку чисел в строке")

    # --- ориентир ---
    if v("landmark"):
        m = re.match(rf"(?i)\s*({_LANDMARK_MARKERS})", str(v("landmark")))
        put("landmark", "ok", f"выделен по маркеру «{m.group(1)}»" if m else "выделен как ориентир")
    else:
        put("landmark", "missing", "ориентира в строке нет")

    notes = []
    if leftover:
        left = " | ".join(leftover) if isinstance(leftover, list) else str(leftover)
        notes.append(f"Не отнесено ни к одному полю: «{left}»")
    for item in noise or []:
        kind, _, value = item.partition(": ")
        notes.append({"телефон": "Телефон убран из разбора (передан курьеру)",
                      "домофон": "Код домофона убран из разбора (передан курьеру)",
                      "опечатка": f"Исправлена опечатка в служебном слове: «{value}»"}.get(kind, item))
    for f in ("street_name", "house"):
        if not v(f):
            doubt.add(f)
    # район, восстановленный по сомнительной улице или дому, наследует сомнение
    if v("district") and src.get("district") == "gazetteer" and doubt & {"street_name", "house", "city"}:
        doubt.add("district")
        reasons.setdefault("district", "восстановлен по улице/дому, в которых есть сомнение")
    # сверка с официальным классификатором КАТО (stat.gov.kz)
    for f in ("city", "district"):
        if (kato or {}).get(f) and fields[f]["status"] != "missing":
            fields[f]["text"] += " · сверено с КАТО"
    for f in doubt:
        if f in fields and fields[f]["status"] == "ok":
            fields[f]["status"] = "doubt"
            if f in reasons and reasons[f] not in fields[f]["text"]:
                fields[f]["text"] += f"; но {reasons[f]}"
    return {"fields": fields, "doubt": [f for f in FIELDS if f in doubt], "notes": notes}
