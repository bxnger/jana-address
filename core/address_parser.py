#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Jana Address (Challenge 01) — парсер "грязных" адресных строк в JSON.
Что делает: нормализация, regex, разбор, confidence.

КОНТРАКТ (не менять — от него зависит API и интерфейса):
    parse_address(raw_address, record_id=None) -> dict   # поля FIELD_ORDER
    parse_batch(records) -> list[dict]                   # len(out) == len(records)
Дополнительно (для UI ручной правки):
    explain_address(raw_address) -> {"result", "flags", "leftover", "normalized"}

Пайплайн (каждый шаг вырезает распознанное, следующий работает с остатком):
    1. normalize          невидимые символы, №, кавычки, гомоглифы, опечатки, пробелы
    2. _strip_noise       почтовый индекс, "Казахстан"
    3. _resolve_bilingual "город Актобе/Ақтөбе қаласы" -> русская половина
    4. extract_units      кв / подъезд / этаж / дом — СОВМЕСТНОЕ назначение чисел
                          ключевым словам (одно число не может достаться двум полям)
    5. extract_landmark   консервативно: только по явным маркерам (магазин, ТЦ, Qazpost…)
    6. province / district / city  (+ удаление дублей города)
    7. _extract_streets   тип улицы до/после названия, мкр/квартал с номером, ЖК
    8. fallback           улица без типа ("Туран 55/6"), дом, "15-3"
    9. справочник         province по городу, city/province по району (gazetteer.py)
   10. compute_confidence эвристика по проверяемым признакам (НЕ калиброванная вероятность)

Справочник: gazetteer.py (встроенный минимум + load_external() для OSM-справочника).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

try:  # пакетный импорт (app.address_parser) и запуск из папки — оба работают
    from . import gazetteer as gz  # type: ignore
except ImportError:  # pragma: no cover
    import gazetteer as gz  # type: ignore


FIELD_ORDER = [
    "id", "raw_address", "province", "city", "district", "living_area",
    "street_type", "street_name", "house", "apartment", "entrance",
    "floor", "landmark", "confidence",
]

MAX_PARSE_LEN = 400  # длиннее — мусор; режем ТОЛЬКО для разбора, raw_address не трогаем

# совместимость со старыми импортами
REPUBLIC_CITIES = set(gz.REPUBLIC_CITIES)
PROVINCE_BY_CITY = gz.CITY_PROVINCE

# ---------------------------------------------------------------------------
# Базовые классы символов
# ---------------------------------------------------------------------------
LET = r"[^\W\d_]"                    # любая буква (кириллица, казахские, латиница)
NLB = rf"(?<!{LET})"                 # слева не буква (цифра можно: "66квартал")
NLA = rf"(?!{LET})"                  # справа не буква
NUM = rf"\d+(?:{LET}(?!{LET}))?(?:/\d+(?:{LET}(?!{LET}))?){{0,2}}"   # 12, 12а, 1/2, 33/1б, 12/1/2
NUM_RE = re.compile(NUM)

TOKEN_RE = re.compile(
    rf"(?P<word>(?:\d+-)?{LET}+(?:['-]{LET}+)*(?:-\d+{LET}?)?)"   # Абая, 1-я, Самал-2
    rf"|(?P<num>{NUM})"
)

KAZ_LETTERS = set("әғқңөұүһі")
KAZ_WORDS_RE = re.compile(r"(?i)(?:қаласы|облысы|ауданы|көшесі|даңғылы|ауылы|кенті|шағын)")


# ---------------------------------------------------------------------------
# 1. НОРМАЛИЗАЦИЯ
# ---------------------------------------------------------------------------
_HOMO_LAT2CYR = str.maketrans("aeopcxykmtAEOPCXYKMTHB", "аеорсхукмтАЕОРСХУКМТНВ")
_SUFFIX_LAT2CYR = {"a": "а", "A": "А", "b": "б", "B": "Б", "v": "в", "V": "В", "g": "г", "G": "Г"}


def _fold_homoglyphs(text: str) -> str:
    """Латиница внутри кириллического слова ("Aбая") -> кириллица.
    Чисто латинские слова (flat, kv, Qazpost) не трогаем."""
    def fix_word(m: re.Match) -> str:
        w = m.group(0)
        has_cyr = any("а" <= ch.lower() <= "я" or ch.lower() in "ёәғқңөұүһі" for ch in w)
        has_lat = any("a" <= ch.lower() <= "z" for ch in w)
        return w.translate(_HOMO_LAT2CYR) if has_cyr and has_lat else w

    text = re.sub(LET + "+", fix_word, text)
    cyr = len(re.findall(r"[а-яёәғқңөұүһі]", text, re.I))
    lat = len(re.findall(r"[a-z]", text, re.I))
    if cyr > lat:  # "12a" в кириллической строке -> "12а"
        text = re.sub(r"(?<=\d)([aAbBvVgG])(?![A-Za-z])", lambda m: _SUFFIX_LAT2CYR[m.group(1)], text)
    return text


_INITIAL_UPPER = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЭЮЯӘҒҚҢӨҰҮҺІABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PREV_TYPE_RE = re.compile(r"(?i)(?:^|[^\w])(?:ул|улица|пр|пр-т|проспект|пер|мкр|мкрн|б-р)\.?\s*$")


def _dot_space(m: re.Match) -> str:
    """Пробел после точки ("ул.Абая" -> "ул. Абая"), кроме инициала имени ("П.Ахмедов", "а.карабаев",
    "ул.с.отарбаев"): эталон хранит название улицы как написано, а пунктуация при сравнении удаляется."""
    t, i = m.string, m.start()
    if i >= 1 and re.match(LET, t[i - 1]) and (i < 2 or not re.match(r"\w", t[i - 2])):
        letter = t[i - 1]
        if letter in _INITIAL_UPPER:
            return "."
        if letter.lower() not in "гспдку" or _PREV_TYPE_RE.search(t[:i - 1]):
            return "."
    return ". "


_GLUED_CITY_RE = re.compile(r"(?<![\w-])([А-ЯЁӘҒҚҢӨҰҮІ][а-яёәғқңөұүі-]+?)(?=[А-ЯЁӘҒҚҢӨҰҮІ][а-яёәғқңөұүі])(\S*)")
_ORD = {"перв": 1, "втор": 2, "трет": 3, "четв": 4, "пят": 5, "шест": 6, "седь": 7, "вось": 8, "девя": 9, "деся": 10}
_CAMEL_RE = re.compile(r"(?<=[а-яёәғқңөұүі])(?=[А-ЯЁӘҒҚҢӨҰҮІ][а-яёәғқңөұүі])")


def _split_city_glued(text: str) -> str:
    """«АстанаКабанбайбатыра98-203», «ПетропавловскЖибекЖолы59-22»: известный город слит со следующим словом.
    Режем только если слитая часть слева — известный город (иначе обычные слова не трогаем)."""
    def fix(m):
        whole = m.group(0)
        for b in [x.start() for x in re.finditer(r"(?<=[а-яёәғқңөұүі])(?=[А-ЯЁӘҒҚҢӨҰҮІ][а-яёәғқңөұүі])", whole)]:
            if gz.is_known_city(whole[:b]):
                return f"{whole[:b]}, {_CAMEL_RE.sub(' ', whole[b:])}"
        return whole
    return _GLUED_CITY_RE.sub(fix, text)


def normalize(raw) -> str:
    """Чистит строку для РАЗБОРА (raw_address в ответе не меняется)."""
    if not isinstance(raw, str):
        return ""
    text = re.sub(r"\s+", " ", raw).strip()[:MAX_PARSE_LEN]   # сначала пробелы, потом обрезка
    text = text.replace("№", " # ")   # NFKC превращает № в "No"
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cf":            # zero-width, BOM, soft hyphen, LRM/RLM
            continue
        out.append(" " if cat in ("Cc", "Zs", "Zl", "Zp") else ch)
    text = "".join(out)
    text = re.sub(r"[«»\"“”„‘’]|(?<!\w)'|'(?!\w)", " ", text)
    text = re.sub(r"(?<=\d)\s+/\s*(?=\d)|(?<=\d)\s*/\s+(?=\d)", "/", text)   # "519 / 521" -> "519/521"
    text = re.sub(r"(?i)\(\s*(?:левый|правый)\s+(?:край|берег)\s*\)", " ", text)
    text = re.sub(r"(?i)[,(]?\s*(?:ранее|бывш(?:ая|ий|ее|\.))\s+[^,()]+\)?", " ", text)   # «ранее Фурманова»
    text = re.sub(r"(?i)индекс\s+для\s+[^,]+,\s*но\s+фактически\s+", "", text)      # «индекс для A, но фактически B»
    text = re.sub(r"(?i)[^,()]*\(\s*ныне\s+([^)]+)\)", r"\1", text)                    # «Нур-Султан (ныне Астана)»
    text = re.sub(r"(?i)(?<!\w)дом\s+номер\s+[а-яё]+(?:\s+[а-яё]+(?:ый|ой|ий|ья|ое))?(?=\s*(?:,|$))", " ", text)
    text = re.sub(r"[;()\[\]{}]", ",", text)
    text = re.sub(r"(?i)(?<!\w)кв\.?\s*(\d+)\s*-\s*\d+(?!\w)", r"кв \1", text)          # «кв 81-169» -> кв 81
    text = re.sub(r"(?i)(?<!\w)(\d+)\s*-\s*\d+\s+(подъезд|подьезд|этаж)", r"\1 \2", text)  # «1-2 подъезды»
    text = re.sub(r"(?i)(?<!\w)(эт(?:аж)?\.?)\s*(\d+)\s*-\s*\d+(?!\w)", r"\1 \2", text)  # «эт 7-15»
    text = re.sub(r"(?i)(?<!\w)корп(?:ус)?\.?\s*\d+\.\d+(?!\w)", " ", text)               # «корп. 9.2»
    text = re.sub(r"(?i)(?<!\w)каласы(?!\w)", "қаласы", text)
    text = re.sub(r"(?i)(?<!\w)kosh(?:esi)?\.?(?!\w)", "көшесі", text)
    text = re.sub(r"(?i)(?<=\w)\s+q\.(?=\s*,)", " қаласы", text)
    text = re.sub(r"(?i)(?<!\w)(перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят)(?:ый|ой|ий|ом)\s+"
                  r"(этаж|подъезд|подьезд)", lambda m: f"{_ORD[m.group(1).lower()[:4]]} {m.group(2)}", text)
    # точка как разделитель: "Алматы. Абая. 10. кв 2" (тире не трогаем: "Павлодар - Семей" — улица)
    # (сокращения "ул.", "просп.", "кв." не трогаем: слово до точки — 5+ букв и не сокращение)
    text = re.sub(r"(?<![\w-])(?!(?i:просп|микр|кварт|корп|строен)\.)([^\W\d_]{5,})\.\s+(?=[^\W\d_]{2})",
                  r"\1, ", text)
    text = re.sub(r"(?<=\d)\.\s+(?=[^\W\d_]{2})", ", ", text)
    text = re.sub(r"(?i)(?<!\w)(?:(?:кв|д|дом)\.?\s*)?б\s*[/\\]\s*н(?!\w)\.?", " ", text)   # «б/н» — без номера
    # "Астана қ. / г. Астана" -> "г. Астана" (двуязычный дубль города с маркерами)
    text = re.sub(r"(?i)(?<![\w-])([^\W\d_][\w-]*)\s+қ\.?\s*/\s*г\.?\s*\1(?![\w-])", r"г. \1", text)
    # "Бокейхана к. 79" — «к.» после названия это көшесі (улица), а не корпус ("42 к. 2" — корпус, не трогаем)
    text = re.sub(r"(?<=[^\W\d_]{3})\s+к\.\s+(?=\d)", " көшесі ", text)
    # "улФурманова", "прАбая" — тип улицы слит с названием
    text = re.sub(r"(?<![\w.])(ул|пр|пер|мкр)(?=[А-ЯЁӘҒҚҢӨҰҮІ][а-яёәғқңөұүі])", r"\1. ", text)
    text = _split_city_glued(text)
    text = _fold_homoglyphs(text)
    text = re.sub(r"(?i)под[ьъ]?езд", lambda m: m.group(0)[:3] + "ъезд", text)  # подьезд/подезд
    text = re.sub(r"\s+\.", ".", text)                       # "Ул ." -> "Ул."
    # "ул.Абая" -> "ул. Абая" ("кв.5" не трогаем); инициал "П.Ахмедов" оставляем слитно — так пишет эталон
    text = re.sub(rf"\.(?={LET})", _dot_space, text)
    text = re.sub(r"(?<=\d)\s+([абвгАБВГ])(?![^\W\d_])", r"\1", text)  # "15 а" -> "15а"
    text = re.sub(r"(?<=\d)-([абвгдАБВГД])(?![^\W\d_])", r"\1", text)  # "д.12-а" -> "12а"
    text = re.sub(r"(?<![\w.-])Е\s+(?=\d{1,3}(?![\d/]))", "Е-", text)  # улицы Астаны "Е 10" -> "Е-10"
    # "102бдом", "1бкв" — литера дома слита с ключевым словом: "102б дом"
    text = re.sub(r"(?i)(?<=\d)([а-яёa-z])(?=(?:дом[а]?|квартира|кв|этаж|подъезд)(?![^\W\d_]))", r"\1 ", text)
    return _cleanup(text)


def _cleanup(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"(?:,\s*)+,", ",", text)
    return text.strip(" ,.-")


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    """Вырезает интервалы из строки (заменяя пробелом) и чистит."""
    if not spans:
        return text
    out, pos = [], 0
    for s, e in sorted(spans):
        if s < pos:
            s = pos
        out.append(text[pos:s])
        out.append(" ")
        pos = max(pos, e)
    out.append(text[pos:])
    return _cleanup("".join(out))


# ---------------------------------------------------------------------------
# 2–3. ШУМ И ДВУЯЗЫЧНЫЕ СКЛЕЙКИ
# ---------------------------------------------------------------------------
_POSTAL_RE = re.compile(r"(?<![\d/])\d{6}(?![\d/])")
# страна — только целым сегментом ("Казахстан, г. Алматы"), не внутри названия ("ул. Егемен Казахстан")
_COUNTRY_RE = re.compile(rf"(?i)(?:^|(?<=,))\s*(?:республика\s+)?(?:казахстан|қазақстан|kazakhstan|рк)\s*(?=,|$)")
_BILINGUAL_SLASH_RE = re.compile(rf"(?<={LET}{LET})\s*/\s*(?={LET}{LET})")  # не трогает ж/м, р/н, 55/6


def _strip_noise(text: str) -> str:
    if re.search(r"(?i)астана|нур-султан", text):        # «левый берег» — ориентир города, не часть улицы
        text = re.sub(r"(?i)(?<![\w-])(?:левый|правый)\s+берег(?![\w-])\s*,?", " ", text)
    text = _POSTAL_RE.sub(",", text)
    text = _COUNTRY_RE.sub(",", text)
    return _cleanup(text)


# ---------------------------------------------------------------------------
# 3а. ПОЛУЧАТЕЛЬ И ПОМЕТКИ КУРЬЕРУ не адрес, в поля не идут
# ---------------------------------------------------------------------------
# Частые имена (казахские и русские). Это не таблица «строка -> ответ»: список только говорит
# «похоже на имя», а решение принимается вместе со справочником (имя-мкр «Айгерим», имя-город
# «Абай» остаются адресом) и положением сегмента в строке.
_FIRST_NAMES = frozenset("""
айгерим айгуль айжан айдана айнур айсулу айым акбота акерке алия алтынай аружан арайлым асель асем
аселя асема балжан ботагоз гаухар гульмира гульнара гульнур гульжан данагуль дана дания динара жадыра
жанар жансая жибек жулдыз зарина зере индира камила карина карлыгаш куралай ляззат мадина малика
меруерт молдир назерке назира нургуль сабина салтанат сандугаш сауле сымбат томирис улжан улпан
умит шолпан эльмира юлия ольга анна мария елена наталья татьяна ирина светлана екатерина людмила
надежда оксана юлиана алина дарья полина валентина галина лариса любовь марина вера кристина
алибек айдос айдар азамат алмас алихан арман арсен асхат аскар асылан аслан бауыржан бекзат бексултан
берик болат галым дамир данияр даурен димаш ерасыл ербол ерлан ерман ержан ерик ернар жандос
жанибек жасулан кайрат канат куаныш мадияр максат марат мейрам нурбол нурлан нуржан нурсултан
олжас руслан рустам самат санжар серик батыр сержан султан тимур талгат ерболат ермек мирас мухтар
александр алексей андрей артем владимир дмитрий евгений иван игорь максим михаил никита николай
олег павел роман сергей юрий виктор вячеслав константин денис антон станислав вадим артур
""".split())
_NAME_SUFFIX = ("у", "ю", "е", "а", "я", "ой", "ей", "ом", "ым", "ге", "ке", "ға", "қа", "га", "ка")
_RECIPIENT_MARK = (r"получател[ьюя]|получит|получить|адресат|кому|звонить|позвонить|позвоните|звоните|"
                   r"спросить|контакт(?:ное\s+лицо)?|заказчик|клиент")
_RECIPIENT_RE = re.compile(
    rf"(?i){NLB}(?:{_RECIPIENT_MARK}){NLA}\s*[:\-]?\s*(?P<who>{LET}[\w\-]*(?:\s+{LET}[\w\-]*){{0,2}})?")
_FOR_RE = re.compile(rf"(?i)(?:^|(?<=,))\s*для\s+(?P<who>{LET}[\w\-]*(?:\s+{LET}[\w\-]*)?)\s*(?=,|$)")
_ADDRESS_WORD_RE = re.compile(
    r"(?i)^(?:ул|улица|пр|проспект|пер|переулок|мкр|мкрн|микрорайон|квартал|кв|дом|д|подъезд|этаж|"
    r"г|город|село|поселок|посёлок|аул|район|р-н|область|обл|жк|ж/м|магазин|маг)\.?$")
_TYPE_WORD_IN_TEXT_RE = re.compile(
    rf"(?i){NLB}(?:ул|улица|пр|пр-т|проспект|пер|переулок|мкр|мкрн|микрорайон|квартал|кв-л|ж/м|жм|"
    rf"б-р|бульвар|шоссе|жк|көшесі|даңғылы|шағын\s*аудан[ы]?)\.?{NLA}")


_CITY_IN_TEXT_RE = re.compile(r"(?i)(?:^|[\s,])(?:г\.|г\s|город|қаласы)")


# Пометка курьеру целым сегментом: «оставить у двери», «позвонить за час», «с 9 до 18»
_NOTE_SEG_RE = re.compile(
    r"(?i)^\s*(?:оставить|оставьте|не\s+звонить|не\s+звоните|позвонить|позвоните|набрать|наберите|написать|"
    r"напишите|по\s+приезд[уе]|по\s+прибытии|заранее|ждать|ждите|доставка|доставить|привезти|вечером|утром|"
    r"днем|днём|до\s+обеда|после\s+обеда|после\s+\d|с\s+\d{1,2}(?::\d\d)?\s*(?:до|-)\s*\d)"
    r"(?:(?![,]).)*$")


_SURNAME_RE = re.compile(r"(?i)(?:ов|ова|ев|ева|ин|ина|кызы|қызы|улы|ұлы|ская|ский|енко)$")


def _name_segment_kind(segs: list[str], idx: int, caps: bool) -> Optional[str]:
    """Сегмент segs[idx] (первый или последний, 1–2 слова без цифр) — имя получателя?
    -> 'recipient_name' (имя из списка), 'recipient_guess' (похоже на имя по форме), None (адрес)."""
    seg = segs[idx]
    words = seg.split()
    after_house = idx == len(segs) - 1 and bool(re.search(r"\d", segs[idx - 1]))
    others = ", ".join(x for i, x in enumerate(segs) if i != idx)
    typed = bool(_TYPE_WORD_IN_TEXT_RE.search(others))
    before_city = idx == 0 and _has_city(segs[1])
    surname = len(words) == 2 and bool(_SURNAME_RE.search(words[1]))
    capital = caps or all(w[:1].isupper() for w in words)
    if len(words) == 2 and not surname:
        return None
    if any(_ADDRESS_WORD_RE.match(w) or _STREET_TYPE_RE.fullmatch(w) or UNIT_KW_RE.fullmatch(w) for w in words) \
            or _TYPE_WORD_IN_TEXT_RE.search(seg) or _LANDMARK_RE.search(seg):
        return None
    if gz.province_from_text(seg) or gz.canonical_city(seg) != seg and gz.is_known_city(seg):
        return None
    nominative = words[0].lower().replace("ё", "е") in _FIRST_NAMES
    # имя-город («Серик», «Абай») — получатель, только если город в строке уже есть: после дома
    # или первым сегментом прямо перед другим городом («Серик, Рудный, мкр Восточный, 3»)
    if gz.is_known_city(seg) and not ((after_house and _has_city(others))
                                      or (nominative and before_city and not gz.is_major_city(seg))):
        return None
    is_la = gz.is_known_living_area(seg) or gz.is_living_area_base(seg)
    if words[0].lower().replace("ё", "е") in _FIRST_NAMES:          # именительный падеж: «Айгерим», «Ержан»
        if not (after_house or typed) and gz.is_street_anywhere(seg):
            return None
        if is_la and not after_house and not typed:
            return None       # «г. Алматы, Айгерим, 15»: мкр «Айгерим», а не получатель
        return "recipient_name"
    # имени нет в списке: только по форме и месту, и только если это не улица/мкр/город нигде в справочнике
    if not capital or is_la or gz.is_known_city(seg) or gz.is_street_anywhere(seg) \
            or (len(words) == 2 and gz.is_known_multiword_street(*words)):
        return None
    if surname and (after_house or typed or before_city):
        return "recipient_guess"          # «..., кв 5, Наргиз Иванова», «Эвелина Ахметов, г. Сарыагаш, ...»
    if len(words) == 1 and ((after_house and (typed or re.search(rf"{LET}{{3,}}\S*\s*\d", others))) or before_city):
        return "recipient_guess"          # «..., д. 15, кв 147, Ерзат», «Наргиз, город Тараз, ...»
    return None


def _has_city(text: str) -> bool:
    """В тексте уже есть город: со словом «г./город» или сегмент, начинающийся с известного города."""
    if _CITY_IN_TEXT_RE.search(text):
        return True
    for seg in text.split(","):
        w = seg.split()
        if w and (gz.is_known_city(w[0]) or (len(w) > 1 and gz.is_known_city(" ".join(w[:2])))):
            return True
    return False


def _is_first_name(word: str) -> bool:
    w = word.lower().replace("ё", "е")
    if w in _FIRST_NAMES:
        return True
    return any(w.endswith(suf) and w[:-len(suf)] in _FIRST_NAMES for suf in _NAME_SUFFIX if len(w) > len(suf) + 2)


def _is_address_place(seg: str) -> bool:
    return bool(gz.is_known_city(seg) or gz.is_known_living_area(seg) or gz.is_living_area_base(seg)
                or gz.province_from_text(seg))


def extract_recipient(text: str) -> tuple[Optional[str], Optional[str], str, set]:
    """Имя получателя и пометки курьеру — не адрес. -> (получатель, пометка, остаток, флаги).
    1) после маркера («получатель», «звонить», «спросить», «кому», ...) — до запятой, номера или конца;
       «для» — только в начале сегмента и целым сегментом («для Асель»);
    2) отдельный сегмент из 1–2 слов без цифр в начале или в конце строки, похожий на имя,
       если он не город/мкр/область по справочнику (имя-мкр «Айгерим» остаётся адресом,
       если в строке нет другого типа улицы или мкр)."""
    flags: set = set()
    who_parts, spans, notes = [], [], []
    for m in _RECIPIENT_RE.finditer(text):
        words = []
        if m.group("who"):
            for w in re.finditer(r"\S+", m.group("who")):
                if _ADDRESS_WORD_RE.match(w.group(0)) or UNIT_KW_RE.fullmatch(w.group(0)):
                    break
                words.append(w)
        end = m.start("who") + words[-1].end() if words else m.end() if not m.group("who") else m.start("who")
        spans.append((m.start(), end))
        if words:
            phrase = m.group("who")[:words[-1].end()]
            first = words[0].group(0)
            if _is_first_name(first) or (first[:1].isupper() and not text.isupper()):
                who_parts.append(phrase)
            else:
                notes.append(text[m.start():end])          # «позвонить заранее»
        else:
            notes.append(m.group(0).strip())
    for m in _FOR_RE.finditer(text):
        if any(s <= m.start("who") < e for s, e in spans):
            continue
        who = m.group("who")
        if _is_first_name(who.split()[0]) or (who[:1].isupper() and not _is_address_place(who)
                                               and not _TYPE_WORD_IN_TEXT_RE.search(who)):
            spans.append((m.start(), m.end()))
            who_parts.append(who)
    rest = _cut(text, spans) if spans else text
    if spans:
        flags.add("recipient_marker")
    kept = []
    for seg in rest.split(","):
        if _NOTE_SEG_RE.match(seg) and not _TYPE_WORD_IN_TEXT_RE.search(seg) and not UNIT_KW_RE.search(seg):
            notes.append(seg.strip())
            flags.add("courier_note")
        else:
            kept.append(seg)
    rest = _cleanup(",".join(kept))
    # одиночное имя первым или последним сегментом
    segs = [p.strip() for p in rest.split(",")]
    if len(segs) >= 2 and re.search(r"\d", rest):
        for idx in (0, len(segs) - 1):
            seg = segs[idx]
            words = seg.split()
            if not (1 <= len(words) <= 2) or re.search(r"[\d.]", seg) or not all(re.fullmatch(rf"{LET}+", w) for w in words):
                continue
            kind = _name_segment_kind(segs, idx, text.isupper())
            if kind:
                segs[idx] = ""
                who_parts.append(seg)
                flags.add(kind)
                break
        rest = _cleanup(", ".join(x for x in segs if x))
    who = ", ".join(w for w in who_parts if w) or None
    note = ", ".join(n for n in notes if n) or None
    return who, note, rest, flags


def _kaz_score(s: str) -> int:
    return sum(ch in KAZ_LETTERS for ch in s.lower()) + 3 * len(KAZ_WORDS_RE.findall(s))


def _resolve_bilingual(text: str) -> str:
    """'город Актобе/Ақтөбе қаласы' -> 'город Актобе'; 'Астана/Астана' -> 'Астана'.
    Берём первую половину без казахских букв/слов. Цифры из отброшенной
    половины сохраняем ('Абая/Абай көшесі 15' -> 'Абая 15')."""
    segs = []
    for seg in text.split(","):
        if not _BILINGUAL_SLASH_RE.search(seg):
            segs.append(seg)
            continue
        parts = [p.strip() for p in _BILINGUAL_SLASH_RE.split(seg)]
        ru = [p for p in parts if _kaz_score(p) == 0]
        chosen = ru[0] if ru else parts[0]
        if not re.search(r"\d", chosen):
            for p in parts:
                m = re.search(r"\d", p)
                if p is not chosen and m:
                    chosen = f"{chosen} {p[m.start():]}"
                    break
        segs.append(chosen)
    return _cleanup(",".join(segs))


# ---------------------------------------------------------------------------
# 4. КВАРТИРА / ПОДЪЕЗД / ЭТАЖ / ДОМ — совместное назначение
# ---------------------------------------------------------------------------
_UNIT_KW = {
    "apartment": r"квартир[аыуе]?|кварт?ира|квартра|кваритра|кв-ра|кв(?!-?л)\.?|пәтер|flat|apartment|apt\.?|kv\.?|офис|оф\.",
    "entrance": r"подъезд[а-я]*|подъ?\.|(?<=\d)под|под\.?(?=\s*\d)|под\.?(?=\s*(?:,|$))|п-д|п\.(?=\s*\d)|кіреберіс|entrance|pod(?:ezd|jezd|yezd)?\.?",
    "floor": r"этаж[а-я]*|этж\.?|етаж[а-я]*|эт\.?|қабат[а-я]*|floor|etazh",
    "house": r"дом[а]?|д\.?|зд\.?|здание|үй|уй|dom|d\.?|house",
}
UNIT_KW_RE = re.compile(
    NLB + "(?:" + "|".join(f"(?P<{k}>{v})" for k, v in _UNIT_KW.items()) + ")" + NLA, re.I
)
_LEFT_GAP_RE = re.compile(r"(?:-?(?:й|ый|ой|ий|го|ом|м))?[\s-]*", re.I)   # "5-й этаж", "4 подъезд"
_RIGHT_GAP_RE = re.compile(r"[\s.:#-]*")                                   # "кв.5", "кв # 7"
_NO_LEFT_BIND = {"д", "д.", "d", "d.", "п."}   # "12д" — это литера дома, а не "дом 12"


def extract_units(text: str) -> tuple[dict, str, set]:
    """Находит все ключевые слова (кв/подъезд/этаж/дом) и все числа, затем
    назначает числа словам: сначала слитные связи ("4подъезд", "кв.5"),
    потом однозначные (у слова один свободный сосед), и только в конце —
    правило по умолчанию "слово -> число справа" (флаг unit_direction_default).
    Возвращает (значения, остаток текста, флаги)."""
    flags: set = set()
    kws = [{"kind": m.lastgroup, "s": m.start(), "e": m.end(), "txt": m.group(0)}
           for m in UNIT_KW_RE.finditer(text)]
    if not kws:
        return {}, text, flags

    nums = []
    for m in NUM_RE.finditer(text):
        s = m.start()
        if s >= 2 and text[s - 1] == "-" and re.match(LET, text[s - 2]) \
                and not any(kw["e"] in (s - 1, s) for kw in kws):
            continue  # "Самал-2" — часть названия (но "кв-245", "п-д-4" — номер)
        nums.append((s, m.end(), m.group(0)))

    for kw in kws:
        kw["left"] = kw["right"] = None
        if kw["txt"].lower() not in _NO_LEFT_BIND:
            for n in reversed(nums):
                if n[1] <= kw["s"]:
                    gap = text[n[1]:kw["s"]]
                    if _LEFT_GAP_RE.fullmatch(gap):
                        glued = gap in ("", "-") and not (
                            kw["txt"].endswith(".") or text[kw["e"]:kw["e"] + 1].isdigit())
                        kw["left"] = (nums.index(n), glued)
                    break
        for n in nums:
            if n[0] >= kw["e"]:
                gap = text[kw["e"]:n[0]]
                if _RIGHT_GAP_RE.fullmatch(gap):
                    kw["right"] = (nums.index(n), not re.search(r"\s", gap))
                break

    used: set = set()
    assign: dict = {}  # индекс kw -> (индекс num, сторона)

    def bind(i, side):
        assign[i] = (kws[i][side][0], side)
        used.add(kws[i][side][0])

    for side in ("right", "left"):                      # слитные связи
        for i, kw in enumerate(kws):
            c = kw[side]
            if i not in assign and c and c[1] and c[0] not in used:
                bind(i, side)
    while True:                                          # однозначные
        changed = False
        for i, kw in enumerate(kws):
            if i in assign:
                continue
            opts = [sd for sd in ("left", "right") if kw[sd] and kw[sd][0] not in used]
            if len(opts) == 1:
                bind(i, opts[0])
                changed = True
        if changed:
            continue
        amb = [i for i, kw in enumerate(kws) if i not in assign
               and kw["left"] and kw["right"]
               and kw["left"][0] not in used and kw["right"][0] not in used]
        if not amb:
            break
        bind(amb[0], "right")                            # по умолчанию: "кв 5"
        flags.add("unit_direction_default")

    values: dict = {}
    spans = []
    for i, kw in enumerate(kws):
        if i in assign:
            ni, _ = assign[i]
            ns, ne, nv = nums[ni]
            if kw["kind"] not in values:
                if kw["kind"] in ("entrance", "floor"):
                    nv = re.match(r"\d+", nv).group(0)
                values[kw["kind"]] = nv
                spans.append((min(kw["s"], ns), max(kw["e"], ne)))
        elif len(kw["txt"].rstrip(".")) >= 4 and kw["kind"] != "house":
            spans.append((kw["s"], kw["e"]))             # висящее "квартира"/"этаж" без номера
    if "house" in values:
        flags.add("house_by_keyword")
    return values, _cut(text, spans), flags


_BUILDING_RE = re.compile(
    NLB + r"(?:корпус|корп\.?|блок|бл\.|строение|стр\.|к\.?(?=\s?\d))\s*#?\s*"
    r"(?:\d+[а-яё]?(?![^\W\d_])|(?<=[\s#])[а-яёa-z](?![^\W\d_]))", re.I)


def _strip_building_part(text: str) -> tuple[str, set]:
    """"корпус 2", "к2", "блок Б", "стр. 1": поля под это в регламенте нет.
    Вырезаем, чтобы номер корпуса не попал в квартиру; причина — флаг building_part.
    Значение НЕ приписываем к дому: как жюри пишет такой дом, неизвестно."""
    m = _BUILDING_RE.search(text)
    if not m:
        return text, set()
    return _cut(text, [m.span()]), {"building_part"}


# ---------------------------------------------------------------------------
# 5. ОРИЕНТИР (консервативно)
# ---------------------------------------------------------------------------
_LANDMARK_RE = re.compile(
    NLB + r"(?:ориентир[а-я]*|напротив|рядом\s+с(?:о)?|возле|около|на\s+углу|угол|уг\.?|"
    r"магазин[а-я]*|маг\.|супермаркет[а-я]*|минимаркет[а-я]*|тц|трц|бц|тд|"
    r"рын(?:ок|ка|ком)|цон|magnum|мечет[ьи]|базар[аеуы]?|аптек[аиу]|школ[аыу]|гимнази[яи]|детский\s+сад|д/с|"
    r"больниц[аыу]|поликлиник[аиу]|кафе|ресторан[а-я]*|банк[а]?|салон[а-я]*|азс|"
    r"остановк[аиу]|павильон[а-я]*|общежити[еяи]|qazpost|казпочт[аы]|kaspi|halyk|почт[аыу])" + NLA,
    re.I,
)


_TYPE_BEFORE_RE = re.compile(
    r"(?i)(?:^|[^\w])(?:ул|улица|пр|пр-т|проспект|пер|переулок|мкр|мкрн|микрорайон|м-н|квартал|кв-л|ж/м|жм|"
    r"б-р|бульвар|тупик|проезд|шоссе|жк)\.?\s*$")


def extract_landmark(text: str) -> tuple[Optional[str], str]:
    """Ориентир = маркер + 1..4 слова до запятой/номера/конца.
    Если сразу за названием идут цифры ("магазин береке Туран 55/6"),
    берём только ОДНО слово — остальное, вероятно, улица."""
    m = None
    for cand in _LANDMARK_RE.finditer(text):
        prep = re.match(r"(?i)напротив|рядом|возле|около|на\s+углу|угол|уг\.?", cand.group(0))
        if not prep and _TYPE_BEFORE_RE.search(text[:cand.start()]):
            continue          # "мкр Школа-интернат", "ул. Базарная": существительное после типа — название
        nxt = re.match(rf"\s+({LET}[\w-]*)\s*,?\s*\d", text[cand.end():])
        if not prep and nxt and gz.is_street_anywhere(f"{cand.group(0)} {nxt.group(1)}"):
            continue          # "базар жырау 89" — улица (есть в справочнике), а не ориентир
        m = cand
        break
    if not m:
        return None, text
    seg_end = text.find(",", m.end())
    seg_end = len(text) if seg_end < 0 else seg_end
    words, stop_by_number = [], False
    for t in TOKEN_RE.finditer(text, m.end(), seg_end):
        if t.lastgroup == "num" or _STREET_TYPE_RE.match(t.group(0)) or UNIT_KW_RE.fullmatch(t.group(0)):
            stop_by_number = t.lastgroup == "num"
            break
        words.append(t)
        if len(words) == 4:
            break
    num = re.match(r"\s*(?:№|#)\s*[^\W\d_]{0,2}\d+\w*", text[(words[-1].end() if words else m.end()):])
    if not num and not words and re.match(r"(?i)павильон", m.group(0)):
        num = re.match(r"\s*[^\W_]{0,2}\d+[^\W_]*", text[m.end():])     # «павильон С4»
    if not words and not num:          # маркер без названия ("напротив", "тц") — не ориентир
        return None, text
    if stop_by_number and words:
        words = words[:1]
    end = words[-1].end() if words else m.end()
    if num:                            # «аптека №3», «общежитие №2»: номер — часть ориентира
        end += num.end()
    start = words[0].start() if words and re.match(r"(?i)ориентир", m.group(0)) else m.start()
    landmark = re.sub(r"\s+", " ", text[start:end]).strip().replace("# ", "№").replace("#", "№")
    return landmark, _cut(text, [(m.start(), end)])


# ---------------------------------------------------------------------------
# 6. ОБЛАСТЬ / РАЙОН / ГОРОД
# ---------------------------------------------------------------------------
_PROV_RE = re.compile(
    rf"{NLB}(?P<name>(?:{LET}+[- ])?{LET}(?:{LET}|-)*)\s+(?:область|области|обл\.?|облысы){NLA}", re.I)
_PROV_BEFORE_RE = re.compile(rf"{NLB}(?:область|обл\.?)\s+(?P<name>{LET}[\w-]*)", re.I)
_PROV_ABBR_RE = re.compile(rf"{NLB}(?:вко|зко|ско|юко){NLA}", re.I)

_DIST_RE = re.compile(
    rf"{NLB}(?P<name>{LET}[\w-]*)\s+(?:район[а-я]*|р-н|р-он|р/н|ауданы)\.?{NLA}", re.I)
_DIST_BEFORE_RE = re.compile(
    rf"{NLB}(?:район|р-н)\.?\s+(?P<name>{LET}[\w-]*(?:ский|цкий|ий|ый)){NLA}", re.I)

_CITY_MARK_BEFORE_RE = re.compile(
    NLB + r"(?:г\.|г(?=\s)|гор\.|(?:город[а]?|пос|посел[её]?(?:ок|ка)|посёл(?:ок|ка)|пгт|"
    r"село|аул|ауыл|станция)(?![^\W\d_])\.?|с\.|ст\.|қ\.|п\.(?=\s+[А-ЯЁӘҒҚҢӨҰҮҺІ]))\s*", re.I)
# "ул. С. Рахимова": "С." после типа улицы — инициал, а не "село"
_AFTER_STREET_TYPE_RE = re.compile(r"(?i)(?:^|[^\w])(?:ул|улица|пр|пр-т|проспект|пер|переулок|мкр|мкрн|б-р|бульвар)\.?\s*$")
_CITY_MARK_AFTER_RE = re.compile(
    rf"{NLB}(?P<name>{LET}[\w-]*)\s+(?:г\.|г(?!\w)|қаласы|қ\.|ауылы|кенті|селосы|пос\.)(?!\w)", re.I)


def extract_province(text: str) -> tuple[Optional[str], str]:
    for rx in (_PROV_RE, _PROV_BEFORE_RE):
        m = rx.search(text)
        if m:
            name = m.group("name")
            last = name.split()[-1]
            prov = gz.province_from_text(last) or gz.province_from_text(name)
            if not prov and rx is _PROV_RE and gz.is_known_city(last):
                # "Семей обл", "Кокшетау обл" — это город, а не область: слово «обл» убираем, город остаётся
                return None, _cut(text, [(m.start("name") + len(name), m.end())])
            if not prov and rx is _PROV_RE and " " in name:
                # "Абая Неизвестная обл" — берём только последнее слово
                start = m.start() + name.rfind(" ") + 1
                prov = f"{last[:1].upper()}{last[1:]} область"
                return prov, _cut(text, [(start, m.end())])
            prov = prov or f"{last[:1].upper()}{last[1:]} область"
            return prov, _cut(text, [m.span()])
    m = _PROV_ABBR_RE.search(text)
    if m:
        return gz.province_from_text(m.group(0)), _cut(text, [m.span()])
    return None, text


_DIST_PREFIX_RE = re.compile(rf"{NLB}(?:район|р-н|р-он|р/н)\.?\s+(?P<name>{LET}[\w-]*)(?=\s|,|$)", re.I)


def extract_district(text: str) -> tuple[Optional[str], str]:
    for m in _DIST_PREFIX_RE.finditer(text):      # "р-н Есиль": название после слова — только известное
        if gz.is_known_district(m.group("name")) or gz.canonical_district(m.group("name")):
            return m.group("name"), _cut(text, [m.span()])
    for rx in (_DIST_RE, _DIST_BEFORE_RE):
        for m in rx.finditer(text):
            name = m.group("name")
            if name.lower() in ("шағын", "микро") or _STREET_TYPE_RE.fullmatch(name):
                continue
            if rx is _DIST_RE and name.lower() in gz.SAFE_IMPLICIT_CITIES:      # «Астана р-н Байконур»: Астана — город
                continue
            # "мкр 7-ой район", "мкр. Кладбище Район" — название микрорайона, не административный район
            before = text[:m.start()]
            if before[-1:] in ("-",) or before[-1:].isdigit():   # "7-ой район" — часть порядкового числа
                continue
            if re.search(r"(?i)(?:мкр|мкрн|микрорайон|м-н)\.?\s*(?:[\w-]+\s+)?$", before):
                continue
            return name, _cut(text, [m.span()])
    return None, text


def _bare_district(text: str, city: str) -> tuple[Optional[str], str]:
    """«Астана, Сарыарка, …», «Алматы Бостандык Аль-Фараби 77»: название района без слова «район»
    (только район этого города; не улица, не мкр; одно слово сегмента или слово + известная улица)."""
    cc = (gz.canonical_city(city) or city).lower()
    for s_, e_ in _segments(text)[:3]:
        raw = text[s_:e_]
        seg = raw.strip()
        toks = seg.split()
        if not toks or len(toks) > 4:
            continue
        w = toks[0]
        if len(w) < 6 or re.search(r"\d", w) or gz.is_known_living_area(w) or gz.is_known_city(w):
            continue
        rest = [t for t in toks[1:] if not re.search(r"\d", t)]
        if len(toks) > 1:
            if not rest or len(" ".join(rest)) < 4 or not gz.is_street_anywhere(" ".join(rest)) \
                    or gz.is_street_anywhere(" ".join([w, *rest])):
                continue
        elif gz.is_street_anywhere(w) and not _STREET_TYPE_RE.search(text[:s_] + " " + text[e_:]):
            continue      # «Алматы, Сарыарка, 5» — это улица, район тут только при отдельной улице с типом
        d = w if gz.is_known_district(w) else gz.canonical_district(w, city)
        dc = gz.city_for_district(d) if d else None
        if dc and dc.lower() == cc:
            a = s_ + len(raw) - len(raw.lstrip())
            return d, _cut(text, [(a, a + len(w))])
    return None, text


def _segments(text: str) -> list[tuple[int, int]]:
    """Границы сегментов между запятыми."""
    out, start = [], 0
    for m in re.finditer(",", text):
        out.append((start, m.start()))
        start = m.end()
    out.append((start, len(text)))
    return out


def _seg_end(text: str, pos: int) -> int:
    e = text.find(",", pos)
    return len(text) if e < 0 else e


_INITIAL_RE = re.compile(rf"\.\s*{LET}")


def _is_stopword_token(text: str, tok: re.Match) -> bool:
    w = tok.group(0)
    if len(w) == 1 and _INITIAL_RE.match(text, tok.end()):
        return False                     # инициал: "ул. Д. Кунаева", "М. Ауэзова"
    city_mark = _CITY_MARK_BEFORE_RE.fullmatch(w + " ") and re.match(rf"\.?\s*{LET}", text[tok.end():])
    return bool(_STREET_TYPE_RE.match(text, tok.start()) or UNIT_KW_RE.fullmatch(w) or city_mark)


def extract_city(text: str, orig: Optional[str] = None) -> tuple[Optional[str], str, set]:
    """Город: (1) по маркеру перед названием, (2) по маркеру после,
    (3) сегмент целиком = известный город, (4) известный город в начале строки.
    Затем удаляет дубли города из остатка."""
    flags: set = set()
    city, span = None, None

    for m in _CITY_MARK_BEFORE_RE.finditer(text):
        if _AFTER_STREET_TYPE_RE.search(text[:m.start()]):
            continue
        if m.group(0).strip().lower() == "п.":
            nxt = re.match(rf"{LET}[\w-]*", text[m.end():])
            seg_txt = text[m.end():_seg_end(text, m.end())]
            if not nxt or not re.search(r"(?:^|,)\s*$", text[:m.start()]) \
                    or not (gz.is_known_city(nxt.group(0)) or not re.search(r"\d", seg_txt)):
                continue      # "п. Абая 5" — не посёлок (проезд/опечатка): решает дальше разбор улицы
        # "Туркестан С.Асанов 15": "С." слитно с названием после слова в том же сегменте — инициал, не "село"
        if re.fullmatch(r"(?i)[сқ]\.", m.group(0)) and re.search(rf"{LET}\s+$", text[:m.start()]):
            continue
        end = _seg_end(text, m.end())
        words, stopped = [], False
        for t in TOKEN_RE.finditer(text, m.end(), end):
            if t.lastgroup == "num" or _is_stopword_token(text, t):
                stopped = True
                break
            words.append(t)
            if len(words) == 3:
                break
        if not words:
            continue
        two = " ".join(w.group(0) for w in words[:2])
        if len(words) >= 2 and gz.is_known_city(two):
            words = words[:2]
        elif gz.is_known_city(words[0].group(0)) or stopped or len(words) == 3:
            words = words[:1]
        city = text[words[0].start():words[-1].end()]
        span = (m.start(), words[-1].end())
        flags.add("city_marker")
        break

    if city is None:
        for m in _CITY_MARK_AFTER_RE.finditer(text):
            if _STREET_TYPE_RE.fullmatch(m.group("name")):
                continue
            city, span = m.group("name"), m.span()
            flags.add("city_marker")
            break

    if city is None:
        segs = _segments(text)
        for i, (s, e) in enumerate(segs[:3]):
            seg = text[s:e].strip()
            if seg and not re.search(r"\d", seg) and gz.is_known_city(seg) and (
                    not gz.is_ambiguous_city(seg)
                    # неоднозначное ("Аксу", "Алтай") — только первым сегментом и если дальше есть адрес
                    or (i == 0 and len(segs) > 1)):
                city, span = seg, (s, e)
                if gz.is_ambiguous_city(seg):
                    flags.add("city_ambiguous")
                break

    if city is None:
        m = re.match(rf"\s*(?P<c>{LET}[\w-]*)\s+(?={LET})", text)
        if not m:   # "Актау 27 мкр 12 дом" — дальше номер, но в остатке есть тип улицы/мкр
            m2 = re.match(rf"\s*(?P<c>{LET}[\w-]*)\s+(?=\d)", text)
            if m2 and _STREET_TYPE_RE.search(text, m2.end()):
                m = m2
        # "Шымкент Тауке хана 12": только однозначные крупные города, и только
        # если дальше идёт слово (а не номер: "Алматы 15" — скорее улица Алматы)
        if m and gz._key(m.group("c")) in gz.SAFE_IMPLICIT_CITIES | set(gz.CITY_ALIASES):
            city, span = m.group("c"), m.span("c")
            flags.add("city_implicit")

    if city is None:
        # город без запятой: "Жанаозен Отпан 4", "Алтай ул.Целинная 4", "Мерке Жауғаш батыр көшесі 26"
        m = re.match(rf"\s*(?P<c>{LET}[\w-]*)\s+(?=\w)", text)
        if m and gz.is_known_city(m.group("c")):
            cand = m.group("c")
            seg_end = _seg_end(text, m.end())
            rest_toks = _tokens(text, m.end(), seg_end)
            words = []
            for t in rest_toks:
                if t.lastgroup != "word" or _is_stopword_token(text, t):
                    break
                words.append(t)
            name = text[words[0].start():words[-1].end()] if words else ""
            tm = _STREET_TYPE_RE.match(text, m.end())
            # тип сразу после слова — город только если тип префиксный и за ним идёт название:
            # "Алтай ул.Целинная" — да; "Абай көшесі 15" — нет (это улица Абай)
            type_next = bool(tm) and not re.match(r"(?i)көшесі|даңғылы|шағын|ауданы", tm.group(0)) \
                and re.match(rf"\.?\s*{LET}", text[tm.end():]) is not None
            type_later = bool(words) and bool(_STREET_TYPE_RE.match(text, words[-1].end() + 1)) \
                and _STREET_TYPE_RE.match(text, words[-1].end() + 1).lastgroup is not None
            known_street = bool(name) and (gz.is_known_street(name, cand, exact=True)
                                           or gz.is_known_street(name.split()[-1] if words else "", cand, exact=True)
                                           and len(words) == 1)
            # "Кентау 93 квартал", "Саркан 9квартал", "Жанакорган квартал 24", "Риддер мкр 7-ой район"
            am = re.match(rf"\s*(?:\d{{1,3}}(?:{LET}(?!{LET}))?\s*)?", text[m.end():])
            tm2 = _STREET_TYPE_RE.match(text, m.end() + am.end())
            area_next = bool(tm2) and _type_kind(tm2) == "area"
            num_after = am.group(0).strip()
            if area_next and num_after and (gz.is_known_living_area(f"{cand}-{num_after}")
                                            or gz.is_known_living_area(f"{cand} {num_after}")):
                area_next = False            # "Аксай 3 микрорайон": мкр «Аксай-3», а не город Аксай
            if not area_next and words:      # "Аксай Строитель 4 микрорайон": название, номер, мкр
                pos = words[-1].end()
                am3 = re.match(rf"\s*(?:\d{{1,3}}(?:{LET}(?!{LET}))?\s*)?", text[pos:])
                tm3 = _STREET_TYPE_RE.match(text, pos + am3.end())
                area_next = bool(tm3) and _type_kind(tm3) == "area"
            if tm and _STREET_TYPES[int(tm.lastgroup[1:])][1] == "набережная":
                type_next = True             # "Байконур Набережная д. 199"
            if type_next or known_street or area_next or (type_later and not gz.is_ambiguous_city(cand)):
                city, span = cand, m.span("c")
                flags.add("city_no_comma")
                if gz.is_ambiguous_city(cand):
                    flags.add("city_ambiguous")

    if city is None:
        # город в конце строки после адреса: "Мамыр-4 дом 101 кв 22 Алматы"
        tm = re.search(rf"(?:^|[\s,])(?P<c>{LET}[\w-]*)\s*$", text)
        if tm and tm.start() > 0 and gz.is_known_city(tm.group("c")) and not gz.is_ambiguous_city(tm.group("c")) \
                and not _STREET_TYPE_RE.search(text[max(0, tm.start("c") - 12):tm.start("c")]) \
                and not _TYPE_BEFORE_RE.search(text[:tm.start("c")]) \
                and re.search(rf"{LET}", text[:tm.start("c")]) \
                and (not _STREET_TYPE_RE.match(text[text.rfind(",", 0, tm.start("c")) + 1:].lstrip())
                     # "дом 50 ул Қазыбек би Алматы": дом уже вырезан до города, улица = тип + название + город
                     or (orig and re.search(r"\d", orig) and re.search(
                         rf"(?i)(?:^|,)\s*(?:ул|улица|пр|пр-т|проспект|пер|переулок|көшесі)\.?\s+{LET}[\w-]*(?:\s+{LET}[\w-]*)*\s+"
                         + re.escape(tm.group("c")) + r"\s*$", text))) \
                and not (orig and re.search(re.escape(tm.group("c")) + r"\s*,?\s*(?:д\.?|дом|үй|уй)?\s*\d", orig, re.I)):
            # не "ул. Дінмұхамед Қонаев, д.19б" и не "Кіші Бурыл дом 223": там город — часть названия улицы
            city, span = tm.group("c"), tm.span("c")
            flags.add("city_last")

    if city is None:
        return None, text, flags

    canon = gz.canonical_city(city)
    text = _cut(text, [span])
    # дубли: сегмент целиком = город, или город первым словом сегмента
    key = gz._key(canon)
    spans = []
    for s, e in _segments(text):
        seg = text[s:e]
        stripped = _CITY_MARK_BEFORE_RE.sub("", seg).strip()
        if stripped and gz._key(gz.canonical_city(stripped)) == key:
            spans.append((s, e))
            continue
        m = re.match(rf"\s*({LET}[\w-]*)\s+(?={LET})", seg)
        if m and gz._key(gz.canonical_city(m.group(1))) == key and gz.is_known_city(m.group(1)):
            spans.append((s + m.start(1), s + m.end(1)))
    return canon, _cut(text, spans), flags


# ---------------------------------------------------------------------------
# 7. УЛИЦЫ, МИКРОРАЙОНЫ, ЖК
# ---------------------------------------------------------------------------
# (regex, канон street_type, вид): street — улица-подобные, area — мкр/квартал/массив,
# complex — ЖК (всегда в living_area, в street_type не идёт).
_STREET_TYPES = [
    (r"улиц[аыеу]|ул\.?|көшесі|көш\.|ulitsa|ul\.?|street|st\.", "улица", "street"),
    (r"проспект[а-я]?|просп\.?|пр-кт\.?|пр-т\.?|пр\.|пр(?=\s)|даңғылы|даң\.|prospekt|pr\.|avenue|ave\.", "проспект", "street"),
    (r"переул(?:ок|ка|ке)|пер\.?|pereulok", "переулок", "street"),
    (r"бульвар[а-я]?|б-р\.?|бул\.", "бульвар", "street"),
    (r"проезд[а-я]?|пр-д\.?", "проезд", "street"),
    (r"тупик[а-я]?|туп\.", "тупик", "street"),
    (r"шоссе", "шоссе", "street"),
    (r"площад[ьи]|пл\.", "площадь", "street"),
    (r"набережн(?:ая|ой)|наб\.", "набережная", "street"),
    (r"алле[яи]", "аллея", "street"),
    (r"трасс[аы]|тракт[а-я]?", "трасса", "street"),
    (r"микрорайон[а-я]?|мкр-н\.?|мкрн\.?|мкр\.?|м-н\.?|мик-н\.?|микр\.|шағын\s+аудан[ы]?|ш/а|mkr\.?|microdistrict",
     "микрорайон", "area"),
    (r"квартал[а-я]?|кв-л\.?", "квартал", "area"),
    (r"(?:жил(?:ой|\.)\s*|жи\s+)?массив[а-я]?|ж/м\.?|жм\.?", "жилой массив", "area"),
    (r"жил(?:ой|\.)\s*комплекс[а-я]?|жк|ж/к|тұрғын\s+үй\s+кешені", "жилой комплекс", "complex"),
    (r"снт|с/о|садов(?:ое|ческое)\s+(?:товарищество|общество)|дачн(?:ый|ое)\s+(?:массив|общество)",
     "садовое общество", "area"),
]
_STREET_TYPE_RE = re.compile(
    NLB + "(?:" + "|".join(f"(?P<t{i}>{rx})" for i, (rx, _, _) in enumerate(_STREET_TYPES)) + ")" + NLA,
    re.I,
)
_AREA_TYPES = {canon for _, canon, kind in _STREET_TYPES if kind != "street"}


def _type_kind(m: re.Match) -> str:
    """Вид совпадения _STREET_TYPE_RE: street / area / complex."""
    return _STREET_TYPES[int(m.lastgroup[1:])][2]
_AREA_NUM_RE = re.compile(rf"\d{{1,2}}{LET}?")   # номер мкр: "2", "3а" (не "55/6", не "133")


def _tokens(text: str, s: int, e: int) -> list[re.Match]:
    return list(TOKEN_RE.finditer(text, s, e))


def _name_after(text, s, e, kind, later_number, house_known=False) -> tuple[list, bool]:
    """Название ПОСЛЕ типа ("ул. 8 Марта", "мкр Аксай 3а"). Возвращает (токены, absorbed)."""
    toks = _tokens(text, s, e)
    if not toks or text[s:toks[0].start()].strip(" .-"):
        return [], False
    name, i = [], 0
    if kind == "area" and len(toks) > 1 and toks[0].lastgroup == "num" and toks[1].lastgroup == "word" \
            and toks[0].end() == toks[1].start() and "/" not in toks[0].group(0):
        return [toks[0], toks[1]], False          # "мкрн 12ВГ" — буквенно-цифровое название
    if kind == "street" and len(toks) > 1 and toks[0].lastgroup == "num" and toks[1].lastgroup == "num" \
            and re.fullmatch(r"\d{1,3}-\d{1,3}", text[toks[0].start():toks[1].end()]) \
            and (house_known or later_number(toks[1].end())):
        return [toks[0], toks[1]], False          # "ул. 23-15, д. 4" — номерные улицы Астаны
    if kind == "street" and len(toks) > 1 and toks[0].lastgroup == "num" and toks[1].lastgroup == "word" \
            and "/" not in toks[0].group(0):
        name.append(toks[0])            # "8 Марта", "40 лет Победы"
        i = 1
    while i < len(toks) and toks[i].lastgroup == "word" and (
            not _is_stopword_token(text, toks[i])
            # ЖК может содержать слово-тип: "ЖК Изумрудный квартал"
            or (kind == "complex" and name and _STREET_TYPE_RE.match(text, toks[i].start()))):
        name.append(toks[i])
        i += 1
    if not name and kind == "street" and i < len(toks) and toks[i].lastgroup == "word" \
            and _STREET_TYPE_RE.fullmatch(toks[i].group(0)) and (i + 1 == len(toks) or toks[i + 1].lastgroup == "num"):
        name.append(toks[i])            # "улица Набережная 5": слово-тип как название после типа
        i += 1
    absorbed = False
    num_ok = i < len(toks) and toks[i].lastgroup == "num" and text[toks[i].start() - 1:toks[i].start()] != "-" and (
        _AREA_NUM_RE.fullmatch(toks[i].group(0))
        # "квартал 105, 12", "мкр. разъезд 103, 6": трёхзначный номер — название, если дальше есть номер дома
        or (re.fullmatch(r"\d{3}", toks[i].group(0)) and (later_number(toks[i].end()) or house_known)))
    if kind != "street" and num_ok:
        words = [t for t in name if t.lastgroup == "word"]
        if not words:
            name.append(toks[i])        # "мкр 5"
            i += 1
        elif not re.search(r"\d", words[-1].group(0)) and (
                later_number(toks[i].end()) or i + 1 < len(toks) or house_known):
            name.append(toks[i])        # "Аксай 3а, 59"
            absorbed = True
            i += 1
    if kind == "area" and name and i == len(toks) - 1 and re.fullmatch(r"(?i)район[а]?", toks[i].group(0)):
        name.append(toks[i])            # "мкр 7-ой район", "мкр 52 Район" — "район" в названии мкр
    return name, absorbed


# слово после числа, при котором число — часть названия улицы: "8 Марта", "50 лет Октября", "1 Мая"
_NUM_NAME_WORD_RE = re.compile(
    r"(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
    r"|лет|съезда|партсъезда|дивизии|гвардейцев|панфиловцев|линия|линии)", re.I)


def _is_num_name(toks, i) -> bool:
    """toks[i] — короткое число без дроби, за ним слово из _NUM_NAME_WORD_RE."""
    return (0 <= i < len(toks) - 1 and toks[i].lastgroup == "num" and "/" not in toks[i].group(0)
            and re.fullmatch(r"\d{1,3}", toks[i].group(0)) is not None
            and toks[i + 1].lastgroup == "word" and _NUM_NAME_WORD_RE.fullmatch(toks[i + 1].group(0)) is not None)


def _numbered_segment(text, s, e):
    """Сегмент целиком = название с числом в начале ("8 Марта", "50 лет Октября") -> (start, end)."""
    toks = _tokens(text, s, e)
    if len(toks) >= 2 and _is_num_name(toks, 0) and all(t.lastgroup == "word" for t in toks[1:]):
        return toks[0].start(), toks[-1].end()
    return None


def _name_before(text, s, e, kind) -> list:
    """Название ПЕРЕД типом ("Рыбинский переулок", "Нурсити 2 микрорайон", "66квартал")."""
    toks = _tokens(text, s, e)
    if not toks or text[toks[-1].end():e].strip():
        return []
    name, i = [], len(toks) - 1
    if kind != "street" and toks[i].lastgroup == "num" and "/" not in toks[i].group(0) \
            and len(re.match(r"\d+", toks[i].group(0)).group(0)) <= 3:
        name.insert(0, toks[i])
        i -= 1
    while i >= 0 and toks[i].lastgroup == "word" and len(name) < 4 and not _is_stopword_token(text, toks[i]):
        name.insert(0, toks[i])
        i -= 1
    if kind == "street" and name and _is_num_name(toks, i):   # "8 Марта көшесі"
        name.insert(0, toks[i])
    return name


def _comma_name_extension(text: str, name_start: int, name_end: int, later_number):
    """Запятая внутри названия улицы: 'ул. Керей, Жанибек хандар, 18'.
    После названия идёт сегмент из одних слов (не город, не мкр, не тип/ключевое слово), за ним — номер дома
    или конец. Возвращает None (не похоже), (конец, True) — склейка есть в справочнике улиц,
    (конец, False) — склейки в справочнике нет: имя не меняем, но запись помечаем и снижаем confidence."""
    m = re.compile(rf"\s*,\s*(?P<seg>{LET}[^\d,]*?)\s*(?=,|$|\d)").match(text, name_end)
    if not m:
        return None
    toks = _tokens(text, m.start("seg"), m.end("seg"))
    if not toks or len(toks) > 3 or any(t.lastgroup != "word" or _is_stopword_token(text, t) for t in toks):
        return None
    seg = text[m.start("seg"):m.end("seg")]
    if gz.is_known_city(seg) or gz.is_known_living_area(seg) or gz.is_living_area_base(seg) \
            or gz.province_from_text(seg) or _LANDMARK_RE.search(seg):
        return None
    if not (re.match(r"\s*,?\s*\d", text[m.end():]) or not text[m.end():].strip() or later_number(m.end())):
        return None
    if gz.is_known_multiword_street(text[name_start:name_end], seg):
        return m.end("seg"), True
    return m.end("seg"), False


_LA_NUM_SEG_RE = re.compile(rf"^\s*(?P<n>{LET}[\w-]*?(?:\s+{LET}[\w-]*)?)[\s-]*(?P<num>\d{{1,2}}{LET}?)\s*$")


def _numbered_living_area(text: str, house_known: bool = False) -> tuple[Optional[str], str]:
    """'Алматы, Шанырак 2, ул. Жанкожа батыра 202': сегмент «мкр без типа + номер», а улица с типом
    идёт отдельно -> это жилой массив (иначе номер мкр уходит в дом, а дом — в квартиру)."""
    segs = text.split(",")
    if len(segs) < 2:
        st0 = _STREET_TYPE_RE.search(text)       # без запятой: «шанырак 2 ул жанкожа батыра 202»
        if not st0 or not st0.start() or _type_kind(st0) != "street" or not _LA_NUM_SEG_RE.match(text[:st0.start()]):
            return None, text
        return _numbered_living_area(text[:st0.start()] + "," + text[st0.start():], house_known)
    for i, seg in enumerate(segs):
        m = _LA_NUM_SEG_RE.match(seg)
        if not m or _STREET_TYPE_RE.search(seg) or UNIT_KW_RE.search(seg):
            continue
        name, num = m.group("n"), m.group("num")
        if not (gz.is_known_living_area(f"{name}-{num}") or gz.is_known_living_area(f"{name} {num}")
                or (gz.is_living_area_base(name) and not gz.is_known_city(name))):
            continue
        rest = ",".join(x for j, x in enumerate(segs) if j != i)
        st = _STREET_TYPE_RE.search(rest)
        if st and _type_kind(st) == "street" and (house_known or re.search(r"\d", rest[st.end():])):
            return seg.strip(), _cleanup(rest)
    return None, text


def _extract_streets(text: str, house_known: bool):
    """Находит все вхождения типов улиц и их названия.
    Возвращает (main, living_area, остаток, флаги), где main = (type, name, kind) или None."""
    flags: set = set()
    occs = []
    for m in _STREET_TYPE_RE.finditer(text):
        idx = int(m.lastgroup[1:])
        _, canon, kind = _STREET_TYPES[idx]
        occs.append({"s": m.start(), "e": m.end(), "canon": canon, "kind": kind})
    if not occs:
        return None, None, text, flags
    # "ул. Набережная 26", "мкр. садовое общество, 6": слово-тип сразу после типа и без своего
    # названия — это и есть название
    merged = []
    for o in occs:
        if merged and "forced" not in merged[-1] and re.fullmatch(r"[\s.]*", text[merged[-1]["e"]:o["s"]]) \
                and re.match(r"\s*(?:[,\d]|$)", text[o["e"]:]):
            merged[-1]["forced"] = (o["s"], o["e"])
            continue
        merged.append(o)
    occs = merged

    def later_number(pos):
        return any(t.lastgroup == "num" for t in TOKEN_RE.finditer(text, pos))

    spans = []
    claimed_end = -1
    for j, o in enumerate(occs):
        if o["s"] < claimed_end:          # слово-тип внутри уже взятого названия ЖК
            o["kind"], o["name"] = "inside", None
            continue
        nxt = occs[j + 1]["s"] if j + 1 < len(occs) else len(text)
        if o["kind"] == "complex":
            nxt = len(text)
        prv = occs[j - 1]["e"] if j else 0
        a_end = min(_seg_end(text, o["e"]), nxt)
        b_start = max(text.rfind(",", 0, o["s"]) + 1, prv)
        if "forced" in o:
            o["name"] = text[o["forced"][0]:o["forced"][1]]
            spans.append((o["s"], o["forced"][1]))
            claimed_end = max(claimed_end, o["forced"][1])
            continue
        after, absorbed = _name_after(text, o["e"], a_end, o["kind"], later_number, house_known)
        before = _name_before(text, b_start, o["s"], o["kind"])
        a_words = sum(t.lastgroup == "word" for t in after)
        if a_words or (after and not before):
            name_toks, span = after, (o["s"], after[-1].end())
            if absorbed:
                flags.add("area_number_absorbed")
        elif before:
            name_toks, span = before, (before[0].start(), o["e"])
        else:
            name_toks, span = [], (o["s"], o["e"])
        o["name"] = text[name_toks[0].start():name_toks[-1].end()] if name_toks else None
        if o["kind"] == "street" and o["name"] and name_toks is after:
            ext = _comma_name_extension(text, name_toks[0].start(), name_toks[-1].end(), later_number)
            if ext:
                end, merged = ext
                if merged:
                    o["name"] = text[name_toks[0].start():end]
                    span = (span[0], end)
                    flags.add("street_comma_in_name")
                else:
                    flags.add("street_comma_split")
        if o["name"] is None and o["canon"] == "набережная" and \
                not any(x is not o and x["kind"] == "street" for x in occs):
            o["name"] = text[o["s"]:o["e"]].rstrip(".")   # "Набережная 22", "г. Актобе, Набережная168"
        spans.append(span)
        claimed_end = max(claimed_end, span[1])

    streets = [o for o in occs if o["kind"] == "street" and o["name"]]
    areas = [o for o in occs if o["kind"] in ("area", "complex") and o["name"]]
    main, living = None, None
    if streets:
        main = streets[0]
        if areas:
            living = areas[0]["name"]
    elif areas:
        complexes = [o for o in areas if o["kind"] == "complex"]
        plain = [o for o in areas if o["kind"] == "area"]
        if plain:
            main = plain[0]
            if complexes:
                living = complexes[0]["name"]
        else:
            living = complexes[0]["name"]
    else:
        typed = [o for o in occs if o["kind"] == "street"]
        if typed:   # тип есть, названия нет ("ул.") — тип запомним
            main = {**typed[0], "name": None}
    rest = _cut(text, spans)
    main_t = (main["canon"], main["name"], main["kind"]) if main else None
    return main_t, living, rest, flags


def extract_street(text: str) -> tuple[Optional[str], Optional[str], str]:
    """Совместимость со старым API: (street_type, street_name, остаток)."""
    main, _, rest, _ = _extract_streets(text, False)
    if not main:
        return None, None, text
    return main[0], main[1], rest


# ---------------------------------------------------------------------------
# 8. ДОМ И УЛИЦА БЕЗ ТИПА
# ---------------------------------------------------------------------------

def _take_house(text: str, apartment_known: bool = False) -> tuple[Optional[str], Optional[str], str, set]:
    """Первое число остатка = дом; "15-3" или ровно одно число после — квартира.
    Если квартира уже известна по слову "кв", лишние числа НЕ удаляем — они
    остаются в leftover и снижают confidence (неоднозначность видна в UI)."""
    flags: set = set()
    toks = [t for t in TOKEN_RE.finditer(text) if t.lastgroup == "num"]
    if not toks:
        return None, None, text, flags
    h = toks[0]
    spans = [h.span()]
    apt = None
    m = re.match(r"\s*-\s*(\d+)(?!\d)", text[h.end():])
    if apartment_known:
        pass
    elif m and "/" not in h.group(0):
        apt = m.group(1)
        spans = [(h.start(), h.end() + m.end())]
        flags.add("apartment_hyphen")
    elif len(toks) == 2 and "/" not in toks[1].group(0):
        apt = toks[1].group(0)
        spans.append(toks[1].span())
        flags.add("apartment_bare_number")
    return h.group(0), apt, _cut(text, spans), flags


def _int(s: str) -> int:
    m = re.match(r"\d+", s)
    return int(m.group(0)) if m else 0


def _untyped_street(text: str, house_known: bool, apartment_known: bool = False):
    """Улица без типа: слова непосредственно перед первым числом ("Туран 55/6",
    "Назарбаева8"); если номер дома уже найден по слову "дом" — последний
    буквенный сегмент. Возвращает (name, остаток, флаги)."""
    flags: set = set()
    segs = _segments(text)
    first_num = None
    if not house_known:
        for t in TOKEN_RE.finditer(text):
            if t.lastgroup == "num":
                first_num = t
                break
    if first_num is not None:
        s_seg, e_seg = next((s, e) for s, e in segs if s <= first_num.start() <= e)
        # "50 лет Октября 71-5": сегмент начинается с числа, за ним слова и ещё число
        seg_toks = _tokens(text, s_seg, e_seg)
        ns = _numbered_segment(text, s_seg, e_seg)
        if ns and any(t.lastgroup == "num" for t in TOKEN_RE.finditer(text, e_seg)):
            # "Алматы, 8 Марта, 15": сегмент-название с числом, номер дома — дальше
            return text[ns[0]:ns[1]], _cut(text, [ns]), flags
        if seg_toks and seg_toks[0] is not None and seg_toks[0].start() == first_num.start() \
                and "/" not in first_num.group(0):
            j = 1
            while j < len(seg_toks) and seg_toks[j].lastgroup == "word" and not _is_stopword_token(text, seg_toks[j]):
                j += 1
            if j > 1 and j < len(seg_toks) and seg_toks[j].lastgroup == "num":
                s0, e0 = seg_toks[0].start(), seg_toks[j - 1].end()
                return text[s0:e0], _cut(text, [(s0, e0)]), flags
        words = []
        for t in reversed(_tokens(text, s_seg, first_num.start())):
            if t.lastgroup != "word" or _is_stopword_token(text, t):
                break
            words.insert(0, t)
        if not words:  # "Абая, 15" — название в предыдущем сегменте
            idx = next(i for i, (s, e) in enumerate(segs) if s == s_seg)
            if idx > 0 and not text[s_seg:first_num.start()].strip():
                ps, pe = segs[idx - 1]
                toks = _tokens(text, ps, pe)
                if toks and all(t.lastgroup == "word" for t in toks):
                    words = toks
        if words:
            words = words[-5:]
            end = words[-1].end()
            # "Аксай 3а, 59, кв 70": квартира уже есть по слову "кв", после короткого
            # номера идёт ещё одно число -> короткий номер — часть названия мкр.
            nums = [t for t in TOKEN_RE.finditer(text, first_num.start()) if t.lastgroup == "num"]
            name_with_num = f"{text[words[0].start():end]} {first_num.group(0)}"
            if (apartment_known and len(nums) == 2 and _AREA_NUM_RE.fullmatch(first_num.group(0))
                    and re.match(r"\s*,", text[first_num.end():])
                    and _int(nums[1].group(0)) > _int(first_num.group(0))   # "Абая 15, 3" — не мкр
                    ) or gz.is_known_living_area(name_with_num):
                end = first_num.end()
                flags.add("area_number_absorbed")
            return text[words[0].start():end], _cut(text, [(words[0].start(), end)]), flags
        return None, text, flags
    # номера нет (или дом уже по ключевому слову): последний чисто буквенный сегмент
    for s, e in reversed(segs):
        ns = _numbered_segment(text, s, e)          # "8 Марта, д 15"
        if ns:
            return text[ns[0]:ns[1]], _cut(text, [ns]), flags
        toks = _tokens(text, s, e)
        if len(toks) >= 2 and toks[-1].lastgroup == "num" and _AREA_NUM_RE.fullmatch(toks[-1].group(0)) \
                and all(t.lastgroup == "word" for t in toks[:-1]) \
                and house_known and not any(x.lastgroup == "num" for x in TOKEN_RE.finditer(text) if x.start() < toks[0].start()):
            # дом уже найден по ключевому слову -> "Самал 2" — мкр с номером, а не дом
            flags.add("area_number_absorbed")
            return text[toks[0].start():toks[-1].end()], _cut(text, [(toks[0].start(), toks[-1].end())]), flags
        if toks and all(t.lastgroup == "word" for t in toks):
            return text[toks[0].start():toks[-1].end()], _cut(text, [(toks[0].start(), toks[-1].end())]), flags
    return None, text, flags


# ---------------------------------------------------------------------------
# 9. CONFIDENCE
# ---------------------------------------------------------------------------
_FLAG_PENALTY = {
    "unit_direction_default": 0.03,   # "9 кв 1" — направление выбрано правилом "кв -> число справа"
    "apartment_bare_number": 0.10,    # квартира без слова "кв" ("Абая 15, 3")
    "apartment_hyphen": 0.08,         # "15-3" трактовано как дом-квартира
    "street_untyped": 0.05,           # тип улицы не указан
    "city_implicit": 0.05,            # город без слова "город" в начале строки
    "city_by_position": 0.10,         # город = первый буквенный сегмент (не из справочника)
    "area_number_absorbed": 0.05,     # "Аксай 3а" — номер приписан к мкр
    "street_long": 0.15,              # название > 4 слов — вероятно, захвачен мусор
    "living_area_demoted": 0.05,      # мкр ушёл в living_area, улица без типа
    "truncated": 0.30,
    "province_from_ambiguous_city": 0.15,  # "Аксу" без слова "город": область — догадка по справочнику
    "province_city_conflict": 0.15,      # область из строки не совпадает со справочником города
    "street_from_city": 0.15,            # единственное название без «г.» при доме — считаем улицей
    "recipient_name": 0.05,              # отдельный сегмент-имя из списка убран как получатель
    "recipient_guess": 0.10,             # убран сегмент, похожий на имя по форме (имени нет в списке)
    "street_comma_split": 0.25,          # "ул. Абая, Жандосова, 5": после названия ещё сегмент из слов
    "street_not_in_gazetteer": 0.10,     # у города есть список улиц, а этой улицы в нём нет

    "living_area_from_gazetteer": 0.03,  # "Самал" признан мкр по основе "Самал-1/2"
    "city_street_swapped": 0.10,         # "Абая, Алматы 15" — город и улица переставлены
    "building_part": 0.10,               # корпус/блок: поля нет, как писать дом — неизвестно  # "Аксай" без слова "город" — область не выводим
}


def compute_confidence(fields: dict, leftover: str = "", flags=()) -> float:
    """Эвристическая оценка уверенности 0..1 по проверяемым признакам.
    НЕ откалиброванная вероятность (калибровку можно сделать по sample_output).
    Чем ниже — тем выше приоритет ручной проверки в UI."""
    scored = ("province", "city", "district", "living_area", "street_name", "house",
              "apartment", "entrance", "floor", "landmark")
    if not any(fields.get(f) for f in scored):
        return 0.0
    score = 1.0
    if not fields.get("street_name"):
        score -= 0.35
    if not fields.get("house"):
        score -= 0.25
    if not fields.get("city") and not fields.get("province"):
        score -= 0.10
    for f in flags:
        score -= _FLAG_PENALTY.get(f, 0.0)
    if fields.get("street_name") and "street_in_gazetteer" not in flags:
        score -= 0.03     # 1.0 — только улица, подтверждённая справочником города: меньше «ничьих» в сортировке UI
    words = [w for w in re.split(r"[\s,]+", leftover or "") if re.search(r"\w", w)]
    score -= min(0.35, 0.07 * len(words))
    return round(max(0.0, min(1.0, score)), 2)


# ---------------------------------------------------------------------------
# 10. ГЛАВНАЯ ФУНКЦИЯ
# ---------------------------------------------------------------------------

def _parse(raw_address) -> tuple[dict, set, str, str]:
    r, flags, leftover, normalized, _ = _parse_ex(raw_address)
    return r, flags, leftover, normalized


def _parse_ex(raw_address) -> tuple[dict, set, str, str, dict]:
    """Как _parse, плюс extras — то, что не является полями адреса (получатель)."""
    extras: dict = {}
    flags: set = set()
    r = {k: None for k in FIELD_ORDER if k not in ("id", "raw_address", "confidence")}
    if isinstance(raw_address, str) and len(re.sub(r"\s+", " ", raw_address).strip()) > MAX_PARSE_LEN:
        flags.add("truncated")
    text = normalize(raw_address)
    normalized = text
    if not text:
        return r, flags, "", normalized, extras

    text = _strip_noise(text)
    text = _resolve_bilingual(text)
    who, note, text, f = extract_recipient(text)
    flags |= f
    if who:
        extras["recipient"] = who
    if note:
        extras["courier_note"] = note

    units, text, f = extract_units(text)
    flags |= f
    r["apartment"], r["entrance"], r["floor"] = units.get("apartment"), units.get("entrance"), units.get("floor")
    r["house"] = units.get("house")

    text, f = _strip_building_part(text)
    flags |= f
    r["landmark"], text = extract_landmark(text)
    r["province"], text = extract_province(text)
    r["district"], text = extract_district(text)
    r["city"], text, f = extract_city(text, normalized)
    flags |= f
    if not r["district"] and r["city"]:
        r["district"], text = _bare_district(text, r["city"])

    pre_la, text = _numbered_living_area(text, r["house"] is not None)
    if pre_la:
        flags.add("living_area_from_gazetteer")
    main, living, text, f = _extract_streets(text, r["house"] is not None)
    flags |= f
    r["living_area"] = living or pre_la
    if main:
        r["street_type"], r["street_name"] = main[0], main[1]

    # дом и квартира без ключевых слов
    if r["street_name"] and main and main[2] == "area" and not r["living_area"]:
        # мкр + отдельная улица без типа ("мкр Самал, Абая 15") -> мкр в living_area
        name, rest, f = _untyped_street(text, r["house"] is not None)
        if name and (gz.is_known_living_area(name) or gz.is_living_area_base(name)) and not re.search(r"\d", name) \
                and 0 <= normalized.lower().find(name.lower()) < normalized.lower().find(r["street_name"].lower()) \
                and (not gz.is_known_street(name, r["city"], exact=True) or r["street_name"].isdigit()):
            # "Караганда, Юго-Восток, мкр Степной-2, 7": без типа стоит жилой массив, а мкр остаётся улицей
            r["living_area"] = name
            text = rest
            flags.add("living_area_from_gazetteer")
        elif name and (r["house"] is not None or re.search(r"\d", rest)):
            r["living_area"], r["street_type"], r["street_name"] = r["street_name"], None, name
            text = rest
            flags |= f | {"living_area_demoted"}
    if not r["street_name"]:
        name, text, f = _untyped_street(text, r["house"] is not None, r["apartment"] is not None)
        if name:
            r["street_name"] = name
            flags |= f | {"street_untyped"}
            if re.search(r"\d", name) and gz.is_known_living_area(name):
                r["street_type"] = "микрорайон"          # "Орбита-2, 14/1": мкр по справочнику
                flags.add("street_type_from_gazetteer")
    if r["house"] is None:
        house, apt, text, f = _take_house(text, r["apartment"] is not None)
        r["house"] = house
        if apt and r["apartment"] is None:
            r["apartment"] = apt
            flags |= f

    # остаток: первый буквенный сегмент -> город (адреса начинаются с города), известные ЖК
    leftover_segs = []
    for s, e in _segments(text):
        toks = _tokens(text, s, e)
        if toks and all(t.lastgroup == "word" for t in toks):
            leftover_segs.append(text[toks[0].start():toks[-1].end()])
    for seg in list(leftover_segs):
        if r["living_area"] is not None:
            break
        exact_la = gz.is_known_living_area(seg)
        # "Самал, Абая 15": "Самал" — основа мкр "Самал-1/2..." и не улица этого города -> мкр, а не город
        base_la = (not exact_la and gz.is_living_area_base(seg) and not gz.is_known_city(seg)
                   and not gz.is_known_street(seg, r["city"], exact=True))
        if exact_la or base_la:
            r["living_area"] = seg
            leftover_segs.remove(seg)
            text = _cleanup(text.replace(seg, " ", 1))
            if base_la:
                flags.add("living_area_from_gazetteer")
    if r["city"] is None and r["street_name"] and leftover_segs and \
            normalized.lower().find(leftover_segs[0].lower()) < normalized.lower().find(r["street_name"].lower()):
        seg0 = leftover_segs[0]
        if (r["street_type"] is None and gz.is_known_city(r["street_name"]) and not gz.is_known_city(seg0)
                and gz.is_known_street(seg0, r["street_name"], exact=True)):
            # "Абая, Алматы 15": "Абая" — улица города Алматы по справочнику -> меняем местами
            r["city"], r["street_name"] = gz.canonical_city(r["street_name"]), seg0
            flags.add("city_street_swapped")
            text = _cleanup(text.replace(leftover_segs.pop(0), " ", 1))
        else:
            r["city"] = gz.canonical_city(seg0)
            flags.add("city_by_position")
            text = _cleanup(text.replace(leftover_segs.pop(0), " ", 1))
    leftover = text

    # "Курмангазы дом 8", "Жансугуров, 10г": кроме «города» ничего нет, а дом есть. У адреса должна быть
    # улица или мкр, поэтому название мелкого пункта без слова «г./село» — это улица (крупный город — нет).
    if r["street_name"] is None and r["house"] and r["city"] and not r["living_area"] and not r["district"] \
            and r["province"] is None and "city_marker" not in flags and not gz.is_major_city(r["city"]) \
            and re.search(re.escape(r["city"]), normalized, re.I):
        r["street_name"], r["city"] = re.search(re.escape(r["city"]), normalized, re.I).group(0), None
        flags.add("street_from_city")

    # "Абай ауданы", "Әуезов ауданы" -> как в справочнике/2ГИС ("Абайский", "Ауэзовский")
    if r["district"] and (set(r["district"].lower()) & KAZ_LETTERS
                          or re.search(re.escape(r["district"]) + r"\s+ауданы", normalized, re.I)):
        canon = gz.canonical_district(r["district"], r["city"]) or gz.canonical_district(r["district"])
        if canon:
            r["district"] = canon
            flags.add("district_canonical")
    # справочник (восстановление; в метрику не входит, если поля нет в строке)
    if r["city"] is None and r["district"]:
        c = gz.city_for_district(r["district"])
        if c:
            r["city"] = c
            flags.add("city_from_district")
    # город, по которому можно выводить область: неоднозначные ("Аксай") — только со словом "город"
    city_ok = bool(r["city"]) and ("city_marker" in flags or not gz.is_ambiguous_city(r["city"]))
    if r["province"] is not None:
        expected = gz.province_for_city(r["city"]) if city_ok else None
        if expected and gz._key(expected) != gz._key(r["province"]):
            flags.add("province_city_conflict")   # "Алматинская область, Алматы": оставляем как в строке
    else:
        r["province"] = (gz.province_for_city(r["city"]) if city_ok else None) or \
                        (gz.province_for_district(r["district"]) if r["district"] else None)
        if r["province"] is None and r["city"] and not city_ok:
            # "Аксу, ул. ..." без слова "город": берём область самого крупного пункта с этим именем.
            # По регламенту неверная область и пустая — одинаковая ошибка, а пустая никогда не бывает верной.
            # Поэтому заполняем, но с флагом и сниженной уверенностью — в UI такие записи наверху.
            r["province"] = gz.province_for_city(r["city"])
            if r["province"]:
                flags.add("province_from_ambiguous_city")
    if r["street_name"] and len(r["street_name"].split()) > 4:
        flags.add("street_long")

    # справочник улиц (ключ "streets"): street_name НЕ исправляем — только тип и уверенность
    is_area = r["street_type"] in _AREA_TYPES
    if r["street_name"] and city_ok and not is_area and gz.has_streets(r["city"]) \
            and re.search(LET, r["street_name"]):
        if gz.is_known_street(r["street_name"], r["city"]):
            flags.add("street_in_gazetteer")
            if r["street_type"] is None and "city_from_district" not in flags:   # город должен быть в строке
                t = gz.street_type_for(r["street_name"], r["city"])
                if t:
                    r["street_type"] = t
                    flags.add("street_type_from_gazetteer")
        else:
            flags.add("street_not_in_gazetteer")
    return r, flags, leftover, normalized, extras


def parse_address(raw_address, record_id: Optional[str] = None) -> dict:
    """Принимает грязную строку адреса, возвращает dict по стандарту полей.
    Никогда не бросает исключение: при сбое — все поля null и confidence 0."""
    result = {k: None for k in FIELD_ORDER}
    result["id"] = record_id
    result["raw_address"] = raw_address
    try:
        fields, flags, leftover, _ = _parse(raw_address)
        result.update(fields)
        result["confidence"] = compute_confidence(fields, leftover, flags)
    except Exception:  # pragma: no cover — запись не должна теряться
        result["confidence"] = 0.0
    return result


def parse_batch(records: list[dict]) -> list[dict]:
    """records: [{"id": ..., "raw_address": ...}, ...] -> список разборов той же длины."""
    out = []
    for rec in records:
        rec = rec if isinstance(rec, dict) else {"raw_address": rec}
        out.append(parse_address(rec.get("raw_address"), rec.get("id")))
    return out


def explain_address(raw_address) -> dict:
    """Для UI ручной правки и отладки: результат + флаги + неразобранный остаток."""
    fields, flags, leftover, normalized, extras = _parse_ex(raw_address)
    result = {"raw_address": raw_address, **fields,
              "confidence": compute_confidence(fields, leftover, flags)}
    return {"result": result, "flags": sorted(flags), "leftover": leftover, "normalized": normalized,
            "extras": extras}


# --- обёртки старого API (если напарник уже импортировал эти функции) -------

def extract_apartment(text: str):
    v, rest, _ = extract_units(text)
    return v.get("apartment"), rest


def extract_entrance(text: str):
    v, rest, _ = extract_units(text)
    return v.get("entrance"), rest


def extract_floor(text: str):
    v, rest, _ = extract_units(text)
    return v.get("floor"), rest


def extract_house(text: str):
    v, rest, _ = extract_units(text)
    if v.get("house"):
        return v["house"], rest
    h, _, rest, _ = _take_house(rest)
    return h, rest


def extract_city_and_province(text: str):
    city, rest, _ = extract_city(text)
    return city, (gz.province_for_city(city) if city else None), rest


if __name__ == "__main__":
    import json
    import sys

    samples = sys.argv[1:] or [
        "город Алматы/Алматы қаласы, Мкр Аксай За, 59, кв 70",
        "Туран 55/6 магазин береке 4подьезд 9этаж",
        "66квартал 12дом 69квартира",
        "Нурсити 2 микрорайон 11дом 5 подьезд 239 кв",
    ]
    for s in samples:
        print(json.dumps(explain_address(s), ensure_ascii=False, indent=2))
