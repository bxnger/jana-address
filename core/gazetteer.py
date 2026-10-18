# -*- coding: utf-8 -*-
"""
Встроенный минимальный справочник для парсера.

ПОДКЛЮЧЕНИЕ ВНЕШНЕГО СПРАВОЧНИКА
---------------------------------
Полный справочник (КАТО / OSM / data.egov.kz) подключается без правки парсера:
  1) положить JSON по пути из переменной окружения JANA_GAZETTEER
     (по умолчанию ./data/gazetteer.json рядом с этим файлом), формат:
     {
       "cities":        {"Каскелен": "Алматинская область", ...},   # город -> область
       "city_aliases":  {"қаскелең": "Каскелен", ...},              # вариант -> город
       "city_districts":{"Ауэзовский": "Алматы", ...},              # район города -> город
       "rural_districts":{"Карасайский": "Алматинская область", ...},# район области -> область
       "living_areas":  ["Пришахтинск", "Томирис", ...],            # мкр/ЖК без слова-типа
       "district_by_living_area": {"Аксай 3а": "Ауэзовский", ...},  # для восстановления района
       "streets": {"Алматы": {"Абая": "проспект", "Жандосова": "улица", ...}, ...}
                                       # город -> {название без типа -> частый тип | null}
     }
  2) или вызвать gazetteer.load_external(path) один раз при старте сервиса.
Справочник грузится один раз при импорте модуля (не на каждую запись).

ВАЖНО по правилам: восстановленные из справочника поля, которых нет в строке,
в автоматическую метрику не входят (оценивает жюри на защите), а штрафа за них нет.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# --------------------------------------------------------------------------
# Области: каноническое имя в "прямом порядке" + основы для распознавания.
# Канон для новых областей (2022) — спорный: в официальных актах "область Абай",
# в быту "Абайская область". Сверить с sample_output.json и при необходимости
# поменять ТОЛЬКО значения в этом словаре.
# --------------------------------------------------------------------------
PROVINCES: dict[str, list[str]] = {
    "Акмолинская область": ["акмолинск", "акмол", "ақмол"],
    "Актюбинская область": ["актюбинск", "актобе", "ақтөбе"],
    "Алматинская область": ["алматинск", "алматы"],  # "Алматы облысы" = область
    "Атырауская область": ["атырауск", "атырау"],
    "Восточно-Казахстанская область": ["восточно-казахстанск", "восточно казахстанск", "шығыс қазақстан"],
    "Жамбылская область": ["жамбылск", "жамбыл"],
    "Жетысуская область": ["жетысуск", "жетісу", "жетысу"],
    "Западно-Казахстанская область": ["западно-казахстанск", "западно казахстанск", "батыс қазақстан"],
    "Карагандинская область": ["карагандинск", "қарағанды", "караганд"],
    "Костанайская область": ["костанайск", "қостанай", "костанай", "кустанайск"],
    "Кызылординская область": ["кызылординск", "қызылорда", "кызылорд"],
    "Мангистауская область": ["мангистауск", "мангыстауск", "маңғыстау", "мангистау", "мангыстау"],
    "Павлодарская область": ["павлодарск", "павлодар"],
    "Северо-Казахстанская область": ["северо-казахстанск", "северо казахстанск", "солтүстік қазақстан"],
    "Туркестанская область": ["туркестанск", "түркістан", "туркестан", "южно-казахстанск", "южно казахстанск"],
    "Улытауская область": ["улытауск", "ұлытау", "улытау"],
    "Абайская область": ["абайск", "абай"],
}
PROVINCE_ABBR = {
    "вко": "Восточно-Казахстанская область",
    "зко": "Западно-Казахстанская область",
    "ско": "Северо-Казахстанская область",
    "юко": "Туркестанская область",
}

# Города республиканского значения: province == каноническое имя города.
REPUBLIC_CITIES = {
    "алматы": "Алматы", "алма-ата": "Алматы",
    "астана": "Астана", "нур-султан": "Астана", "нурсултан": "Астана", "акмола": "Астана",
    "шымкент": "Шымкент", "чимкент": "Шымкент",
}

# Город -> область. Ключ: нижний регистр, ё->е.
CITY_PROVINCE: dict[str, str] = {
    # Акмолинская
    "кокшетау": "Акмолинская область", "степногорск": "Акмолинская область",
    "щучинск": "Акмолинская область", "атбасар": "Акмолинская область",
    "макинск": "Акмолинская область", "есиль": "Акмолинская область",
    "ерейментау": "Акмолинская область", "державинск": "Акмолинская область",
    "акколь": "Акмолинская область", "степняк": "Акмолинская область",
    "косшы": "Акмолинская область", "косши": "Акмолинская область",
    # Актюбинская
    "актобе": "Актюбинская область", "алга": "Актюбинская область",
    "кандыагаш": "Актюбинская область", "хромтау": "Актюбинская область",
    "шалкар": "Актюбинская область", "эмба": "Актюбинская область",
    "темир": "Актюбинская область", "жем": "Актюбинская область",
    # Алматинская
    "конаев": "Алматинская область", "капчагай": "Алматинская область",
    "капшагай": "Алматинская область", "каскелен": "Алматинская область",
    "талгар": "Алматинская область", "есик": "Алматинская область",
    "иссык": "Алматинская область", "боралдай": "Алматинская область",
    "узынагаш": "Алматинская область", "отеген батыр": "Алматинская область",
    "шамалган": "Алматинская область", "кордай": "Жамбылская область",
    "туздыбастау": "Алматинская область", "жандосов": "Алматинская область",
    # Атырауская
    "атырау": "Атырауская область", "кульсары": "Атырауская область",
    # ВКО
    "усть-каменогорск": "Восточно-Казахстанская область", "риддер": "Восточно-Казахстанская область",
    "алтай": "Восточно-Казахстанская область", "зыряновск": "Восточно-Казахстанская область",
    "серебрянск": "Восточно-Казахстанская область", "шемонаиха": "Восточно-Казахстанская область",
    "зайсан": "Восточно-Казахстанская область",
    # Абай
    "семей": "Абайская область", "семипалатинск": "Абайская область",
    "курчатов": "Абайская область", "аягоз": "Абайская область", "шар": "Абайская область",
    # Жамбылская
    "тараз": "Жамбылская область", "жанатас": "Жамбылская область",
    "каратау": "Жамбылская область", "шу": "Жамбылская область",
    # Жетысу
    "талдыкорган": "Жетысуская область", "текели": "Жетысуская область",
    "жаркент": "Жетысуская область", "сарканд": "Жетысуская область",
    "ушарал": "Жетысуская область", "уштобе": "Жетысуская область",
    # ЗКО
    "уральск": "Западно-Казахстанская область", "аксай": "Западно-Казахстанская область",
    # Карагандинская
    "караганда": "Карагандинская область", "темиртау": "Карагандинская область",
    "шахтинск": "Карагандинская область", "сарань": "Карагандинская область",
    "абай": "Карагандинская область", "балхаш": "Карагандинская область",
    "приозерск": "Карагандинская область", "каркаралинск": "Карагандинская область",
    # Улытау
    "жезказган": "Улытауская область", "сатпаев": "Улытауская область", "каражал": "Улытауская область",
    # Костанайская
    "костанай": "Костанайская область", "рудный": "Костанайская область",
    "лисаковск": "Костанайская область", "аркалык": "Костанайская область",
    "житикара": "Костанайская область", "тобыл": "Костанайская область",
    # Кызылординская
    "кызылорда": "Кызылординская область", "аральск": "Кызылординская область",
    "казалинск": "Кызылординская область",
    # Мангистауская
    "актау": "Мангистауская область", "жанаозен": "Мангистауская область",
    "форт-шевченко": "Мангистауская область",
    # Павлодарская
    "павлодар": "Павлодарская область", "экибастуз": "Павлодарская область",
    "аксу": "Павлодарская область",
    # СКО
    "петропавловск": "Северо-Казахстанская область", "булаево": "Северо-Казахстанская область",
    "мамлютка": "Северо-Казахстанская область", "сергеевка": "Северо-Казахстанская область",
    "тайынша": "Северо-Казахстанская область",
    # Туркестанская
    "туркестан": "Туркестанская область", "кентау": "Туркестанская область",
    "арыс": "Туркестанская область", "ленгер": "Туркестанская область",
    "сарыагаш": "Туркестанская область", "жетысай": "Туркестанская область",
    "шардара": "Туркестанская область",
}

# Казахские/латинские/старые написания -> русское имя города
CITY_ALIASES: dict[str, str] = {
    "ақтөбе": "Актобе", "қарағанды": "Караганда", "қостанай": "Костанай",
    "қызылорда": "Кызылорда", "ақтау": "Актау", "орал": "Уральск",
    "өскемен": "Усть-Каменогорск", "петропавл": "Петропавловск",
    "талдықорған": "Талдыкорган", "түркістан": "Туркестан", "көкшетау": "Кокшетау",
    "жезқазған": "Жезказган", "қонаев": "Конаев", "теміртау": "Темиртау",
    "екібастұз": "Экибастуз", "жаңаөзен": "Жанаозен", "қаскелең": "Каскелен",
    "кустанай": "Костанай", "almaty": "Алматы", "astana": "Астана", "shymkent": "Шымкент",
    # Старые названия -> текущие, как в 2ГИС (ответ организаторов на чекпоинте: «писать как в 2ГИС»).
    # Неоднозначные старые имена сюда НЕ входят: «Шевченко» (Актау) — частая улица,
    # «Акмола», «Целиноград» — область и район.
    "нур-султан": "Астана", "нурсултан": "Астана", "нұр-сұлтан": "Астана", "нұрсұлтан": "Астана",
    "алма-ата": "Алматы", "алмата": "Алматы", "чимкент": "Шымкент",
    "семипалатинск": "Семей", "капчагай": "Конаев", "капшагай": "Конаев", "актюбинск": "Актобе",
    "кокчетав": "Кокшетау", "гурьев": "Атырау", "джамбул": "Тараз", "зыряновск": "Алтай",
    "лениногорск": "Риддер", "иссык": "Есик", "бурундай": "Боралдай", "новый узень": "Жанаозен", "байконыр": "Байконур",
}

# Города, которые безопасно узнавать БЕЗ слова "город" в начале строки
# ("Шымкент Тауке хана 12"). Неоднозначные (Аксай — и город, и мкр в Алматы;
# Абай — и город, и улица) сюда НЕ входят.
SAFE_IMPLICIT_CITIES = {
    "алматы", "астана", "нур-султан", "шымкент", "актобе", "караганда", "кокшетау",
    "тараз", "кызылорда", "актау", "павлодар", "петропавловск", "атырау", "уральск",
    "усть-каменогорск", "семей", "костанай", "талдыкорган", "туркестан", "конаев",
    "жезказган", "темиртау", "экибастуз", "рудный", "каскелен", "талгар", "боралдай",
}

# Районы городов республиканского значения -> город (для восстановления city/province)
CITY_DISTRICTS: dict[str, str] = {
    "алатауский": "Алматы", "алмалинский": "Алматы", "ауэзовский": "Алматы",
    "бостандыкский": "Алматы", "жетысуский": "Алматы", "медеуский": "Алматы",
    "наурызбайский": "Алматы", "турксибский": "Алматы",
    "алматинский": "Астана", "есильский": "Астана", "сарыаркинский": "Астана",
    "байконырский": "Астана", "байконурский": "Астана", "нура": "Астана",
    "абайский": "Шымкент", "аль-фарабийский": "Шымкент", "енбекшинский": "Шымкент",
    "каратауский": "Шымкент", "туранский": "Шымкент",
}

# Районы областей -> область (неполный список — остальное из OSM)
RURAL_DISTRICTS: dict[str, str] = {
    "илийский": "Алматинская область", "карасайский": "Алматинская область",
    "талгарский": "Алматинская область", "енбекшиказахский": "Алматинская область",
    "жамбылский": None,  # есть в нескольких областях — без контекста не определить
    "целиноградский": "Акмолинская область", "бурабайский": "Акмолинская область",
    "сайрамский": "Туркестанская область", "толебийский": "Туркестанская область",
}

# Жилые массивы/мкр/ЖК, которые пишут БЕЗ слова-типа. Остальные — из OSM.
KNOWN_LIVING_AREAS: set[str] = {"пришахтинск", "майкудук", "юго-восток"}

DISTRICT_BY_LIVING_AREA: dict[str, str] = {}
# Крупные города (встроенный список до подмешивания OSM): такое название без улицы — точно город,
# а мелкий пункт («Курмангазы дом 8», «Жансугуров, 10г») без улицы — скорее улица.
MAJOR_CITIES = frozenset(CITY_PROVINCE) | frozenset(k.lower().replace("ё", "е") for k in REPUBLIC_CITIES)
_EXT_CITY_DISTRICTS: set[str] = set()                 # районы городов из внешнего справочника
_DISTRICT_DISPLAY: dict[str, str] = {}                 # ключ района -> написание из внешнего справочника


# Названия, которые одновременно являются городом И мкр/улицей/районом. Без явного
# слова "город/г./пос." их НЕ считаем городом и область по ним НЕ выводим.
_EMBEDDED_ALIASES: dict[str, str] = {}   # заполняется ниже копией встроенных CITY_ALIASES

AMBIGUOUS_CITY_NAMES = {
    "аксай",   # город в ЗКО и мкр Аксай-1..5 в Алматы
    "абай",    # город в Карагандинской обл., улица/проспект Абая, область Абай
    "алтай", "шу", "аксу", "есиль", "темир", "жем", "шар", "тобыл", "арыс", "каратау",
    "нура",    # район Астаны и река
}


def _key(s: str) -> str:
    return s.lower().replace("ё", "е").strip()


EXTERNAL_ERROR: str | None = None   # причина, если файл есть, но не загрузился


def load_external(path: str | os.PathLike) -> bool:
    """Подмешивает внешний справочник. Возвращает True, если загружен.
    Отсутствующий, пустой, битый файл или неверная структура НЕ роняют парсер:
    возвращается False, причина — в gazetteer.EXTERNAL_ERROR, работает встроенный минимум."""
    global EXTERNAL_ERROR
    p = Path(path)
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig") or "null")
    except (OSError, ValueError) as e:
        EXTERNAL_ERROR = f"{p}: не читается как JSON ({e})"
        return False
    if not isinstance(data, dict):
        EXTERNAL_ERROR = f"{p}: ожидался JSON-объект, получено {type(data).__name__}"
        return False
    snapshot = _snapshot()
    try:
        _merge(data)
    except (AttributeError, TypeError, ValueError) as e:
        _restore(snapshot)   # частично битый файл не оставляет "половину" справочника
        EXTERNAL_ERROR = f"{p}: неверная структура ({e})"
        return False
    EXTERNAL_ERROR = None
    return True


_MUTABLE = ("CITY_PROVINCE", "CITY_ALIASES", "CITY_DISTRICTS", "RURAL_DISTRICTS",
            "KNOWN_LIVING_AREAS", "DISTRICT_BY_LIVING_AREA", "STREETS", "_STREET_LAST_WORD",
            "LIVING_AREA_BASES", "MULTIWORD_STREETS", "_ALL_STREET_KEYS", "_DISTRICT_DISPLAY", "_EXT_CITY_DISTRICTS")


def _snapshot() -> dict:
    import copy
    return {n: copy.deepcopy(globals()[n]) for n in _MUTABLE}


def _restore(snap: dict) -> None:
    for n, v in snap.items():
        obj = globals()[n]
        obj.clear()
        obj.update(v)


def _merge(data: dict) -> None:
    for city, prov in (data.get("cities") or {}).items():
        CITY_PROVINCE[_key(city)] = prov
    for alias, city in (data.get("city_aliases") or {}).items():
        CITY_ALIASES[_key(alias)] = city
    for d, city in (data.get("city_districts") or {}).items():
        CITY_DISTRICTS[_key(d)] = city
        _DISTRICT_DISPLAY[_key(d)] = d
        _EXT_CITY_DISTRICTS.add(_key(d))
    for d, prov in (data.get("rural_districts") or {}).items():
        RURAL_DISTRICTS[_key(d)] = prov
        _DISTRICT_DISPLAY[_key(d)] = d
    for la in data.get("living_areas") or []:
        KNOWN_LIVING_AREAS.add(_key(la))
    for la, d in (data.get("district_by_living_area") or {}).items():
        DISTRICT_BY_LIVING_AREA[_key(la)] = d
    _post_merge(data)


# --------------------------------------------------------------------------
# Улицы по городам (ключ "streets" внешнего справочника) и основы названий мкр.
# Встроенного списка улиц нет: без внешнего файла is_known_street() -> False,
# has_streets(city) -> False, и парсер ведёт себя как раньше.
# --------------------------------------------------------------------------
STREETS: dict[str, dict[str, str | None]] = {}        # город -> {улица -> тип}
_STREET_LAST_WORD: dict[str, set[str]] = {}            # город -> последние слова названий
LIVING_AREA_BASES: set[str] = set()                    # "самал" из "Самал-1", "Самал 2"
_ALL_STREET_KEYS: set[str] = set()                     # названия улиц всех городов (без типа)
MULTIWORD_STREETS: dict[str, set[tuple[str, ...]]] = {}  # город -> слова названий из 2+ слов (без «и»)



def _la_base(name: str) -> str | None:
    """'Самал-1' / 'Аксай 3А' / 'Самал микрорайон' -> 'самал'; без номера/типа -> None."""
    k = _key(name)
    b = re.sub(r"(?:\s+(?:микрорайон|мкр\.?|шағын ауданы|жилой массив))+$", "", k)
    b = re.sub(r"[\s-]+\d+[а-яёa-z]?$", "", b).strip()
    if b == k or len(b) < 4 or not re.fullmatch(r"[^\W\d_]+(?:[\s-][^\W\d_]+)?", b):
        return None
    return b


def street_tokens(name: str) -> tuple[str, ...]:
    """'Керей и Жанибек хандар' / 'Керей, Жанибек хандар' -> ('керей', 'жанибек', 'хандар')."""
    return tuple(t for t in re.findall(r"[^\W_]+", _key(name)) if t != "и")


def _add_streets(city: str, streets: dict) -> None:
    ck = _key(canonical_city(city))
    tgt = STREETS.setdefault(ck, {})
    last = _STREET_LAST_WORD.setdefault(ck, set())
    for name, typ in streets.items():
        if not isinstance(name, str):
            raise TypeError(f"streets[{city!r}]: название не строка")
        nk = _key(name)
        if typ is not None and not isinstance(typ, str):
            raise TypeError(f"streets[{city!r}][{name!r}]: тип не строка")
        for k in {nk, nk.replace(".", " ").replace("  ", " ").strip()}:   # "н. торгаева" и "н торгаева"
            if k not in tgt or tgt[k] is None:
                tgt[k] = typ
            _ALL_STREET_KEYS.add(k)
        last.add(nk.split()[-1] if nk.split() else nk)
        toks = street_tokens(name)
        if len(toks) >= 2 and "(" not in name:
            MULTIWORD_STREETS.setdefault(ck, set()).add(toks)


def _post_merge(data: dict) -> None:
    streets = data.get("streets") or {}
    if not isinstance(streets, dict):
        raise TypeError("streets: ожидался объект")
    for city, d in streets.items():
        if not isinstance(d, dict):
            raise TypeError(f"streets[{city!r}]: ожидался объект")
        _add_streets(city, d)
    for la in KNOWN_LIVING_AREAS:
        b = _la_base(la)
        if b:
            LIVING_AREA_BASES.add(b)




# --------------------------------------------------------------------------
# lookup-функции, которыми пользуется парсер
# --------------------------------------------------------------------------

def province_from_text(word: str) -> str | None:
    """'актюбинская' / 'ақтөбе облысы' / 'ВКО' -> 'Актюбинская область'."""
    k = _key(word)
    if k in PROVINCE_ABBR:
        return PROVINCE_ABBR[k]
    for canon, stems in PROVINCES.items():
        for st in stems:
            if k.startswith(st):
                return canon
    return None


def canonical_city(name: str) -> str:
    """Казахское/латинское написание -> русское; иначе как в строке.
    Встроенные алиасы (Нур-Султан -> Астана, Ақтөбе -> Актобе) применяются всегда. Алиасы из
    внешнего файла — только если написанное название само не является населённым пунктом
    ("Тобыл", "Арал" — реальные города, переименовывать их нельзя)."""
    k = _key(name)
    if k in _EMBEDDED_ALIASES:
        return _EMBEDDED_ALIASES[k]
    if k in REPUBLIC_CITIES:
        return REPUBLIC_CITIES[k] if name.isupper() else name
    if k in CITY_PROVINCE:
        # «КАРАГАНДА» -> «Караганда», «УСТЬ-КАМЕНОГОРСК» -> «Усть-Каменогорск»: в 2ГИС не капсом
        return "-".join(p[:1].upper() + p[1:].lower() for p in name.split("-")) if name.isupper() else name
    if k in CITY_ALIASES:
        return CITY_ALIASES[k]
    return name


def is_known_city(name: str) -> bool:
    k = _key(canonical_city(name))
    return k in CITY_PROVINCE or k in REPUBLIC_CITIES


def province_for_city(name: str) -> str | None:
    k = _key(canonical_city(name))
    if k in REPUBLIC_CITIES:
        return REPUBLIC_CITIES[k]
    return CITY_PROVINCE.get(k)


def city_for_district(district: str) -> str | None:
    return CITY_DISTRICTS.get(_key(district))


_KAZ_FOLD = str.maketrans("әғқңөұүһіэё", "агкноуухиее")


def _district_stem(name: str) -> str:
    k = _key(name).translate(_KAZ_FOLD).replace(" ", "").replace("-", "").replace("ь", "").replace("ъ", "")
    k = k.replace("ы", "и")
    for suf in ("инский", "ынский", "енский", "ский", "цкий", "ий", "ый"):
        if k.endswith(suf) and len(k) - len(suf) >= 3:
            return k[: -len(suf)]
    return k


def canonical_district(name: str | None, city: str | None = None) -> str | None:
    """Казахское/краткое название района -> как в справочнике (2ГИС): 'Абай ауданы' -> 'Абайский',
    'Әуезов' -> 'Ауэзовский', 'Қарасай' -> 'Карасайский'. Только при единственном совпадении
    (с учётом города, если он известен); иначе None — тогда оставляем как в строке."""
    if not name:
        return None
    k = _key(name)
    for d in (CITY_DISTRICTS, RURAL_DISTRICTS):
        if k in d:
            return None                     # уже известное написание
    stem = _district_stem(name)
    stem2 = stem[:-1] if stem[-1:] in "аыиуо" and len(stem) > 4 else stem      # 'алмалы' -> 'алмал'
    ck = _key(canonical_city(city)) if city else None
    city_has = bool(ck) and any(_key(o) == ck for o in CITY_DISTRICTS.values())
    pools = [CITY_DISTRICTS] if city_has else [CITY_DISTRICTS, RURAL_DISTRICTS]
    cands = [(dk, _district_stem(dk)) for d in pools for dk, owner in d.items()
             if not (ck and d is CITY_DISTRICTS and _key(owner) != ck)]
    hits = {dk for dk, ds in cands if ds in (stem, stem2)}
    if not hits and len(stem) >= 5:
        hits = {dk for dk, ds in cands if ds.startswith(stem2) and len(ds) - len(stem2) <= 2}
    if city_has and any(_key(CITY_DISTRICTS[k_]) == ck for k_ in _EXT_CITY_DISTRICTS if k_ in CITY_DISTRICTS):
        hits = {h for h in hits if h in _EXT_CITY_DISTRICTS}     # внешний справочник (OSM, сверен с 2ГИС) важнее
    if len(hits) > 1:
        ext = {h for h in hits if h in _DISTRICT_DISPLAY}
        hits = ext if len(ext) == 1 else set()
    if len(hits) != 1:
        return None
    hit = hits.pop()
    return _DISTRICT_DISPLAY.get(hit) or "-".join(p[:1].upper() + p[1:] for p in hit.split("-"))


def province_for_district(district: str) -> str | None:
    city = city_for_district(district)
    if city:
        return province_for_city(city)
    return RURAL_DISTRICTS.get(_key(district))


def is_known_living_area(name: str) -> bool:
    return _key(name) in KNOWN_LIVING_AREAS


def is_ambiguous_city(name: str) -> bool:
    return _key(canonical_city(name)) in AMBIGUOUS_CITY_NAMES


def _street_key(name: str) -> str:
    return re.sub(r"\s+", " ", _key(name).replace(".", " ")).strip()


def has_streets(city: str | None) -> bool:
    """Есть ли в справочнике улицы для этого города (иначе проверять нечего)."""
    return bool(city) and bool(STREETS.get(_key(canonical_city(city))))


def is_known_street(name: str | None, city: str | None, exact: bool = False) -> bool:
    """Улица есть в справочнике города. Точное совпадение названия без типа
    ('Абая'), либо совпадение с последним словом ('Назарбаева' ~ 'Нурсултана Назарбаева').
    exact=True — только точное совпадение (для решений «город это или улица»).
    Без города -> False: угадывать город по улице не беремся."""
    if not name or not city:
        return False
    ck = _key(canonical_city(city))
    d = STREETS.get(ck)
    if not d:
        return False
    nk = _street_key(name)
    if nk in d or _key(name) in d:
        return True
    if "," in name and street_tokens(name) in MULTIWORD_STREETS.get(ck, ()):   # 'Керей, Жанибек хандар' ~ 'Керей и Жанибек хандар'
        return True
    return not exact and nk in _STREET_LAST_WORD.get(ck, ())


def street_type_for(name: str | None, city: str | None) -> str | None:
    """Частый тип улицы по справочнику — только при ТОЧНОМ совпадении названия и известном городе."""
    if not name or not city:
        return None
    d = STREETS.get(_key(canonical_city(city))) or {}
    return d.get(_key(name)) or d.get(_street_key(name))


def is_major_city(name: str | None) -> bool:
    return bool(name) and _key(canonical_city(name)) in MAJOR_CITIES


def is_known_district(name: str | None) -> bool:
    k = _key(name or "")
    return bool(k) and (k in CITY_DISTRICTS or k in RURAL_DISTRICTS)


def is_living_area_base(name: str) -> bool:
    """'Самал' — основа известных мкр 'Самал-1', 'Самал 2' (точного 'Самал' в списке может не быть)."""
    k = _key(name)
    return k in KNOWN_LIVING_AREAS or k in LIVING_AREA_BASES


_EMBEDDED_ALIASES.update({_key(k): v for k, v in CITY_ALIASES.items()})

def is_street_anywhere(name: str | None) -> bool:
    """Название (без типа) есть улицей хотя бы в одном городе справочника."""
    if not name:
        return False
    return _key(name) in _ALL_STREET_KEYS or _street_key(name) in _ALL_STREET_KEYS


def is_known_multiword_street(*parts: str) -> bool:
    """Склейка частей ('Керей', 'Жанибек хандар') есть как название улицы хотя бы в одном городе
    (запятая внутри названия: 'ул. Керей, Жанибек хандар, 18')."""
    toks = street_tokens(" ".join(parts))
    return len(toks) >= 2 and any(toks in v for v in MULTIWORD_STREETS.values())


# Загрузка в конце модуля: к этому моменту определены все функции, нужные _merge().
EXTERNAL_LOADED = load_external(
    os.environ.get("JANA_GAZETTEER", Path(__file__).resolve().parent / "data" / "gazetteer.json")
)
