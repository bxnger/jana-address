"""Нормализация текста — общая для парсера и справочника."""
import re
import unicodedata

# Невидимые служебные символы, которые встречаются внутри строк
_INVISIBLE = re.compile("[­​-‏ - ⁠-⁯﻿]")

# Казахские буквы -> ближайшие русские (только для сравнения, не для вывода)
_KK2RU = str.maketrans({
    "ә": "а", "ө": "о", "ү": "у", "ұ": "у", "қ": "к", "ғ": "г",
    "ң": "н", "һ": "х", "і": "и", "ё": "е",
})


def clean(s: str) -> str:
    """Убирает невидимые символы и лишние пробелы, сохраняя написание."""
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = _INVISIBLE.sub("", s)
    s = s.replace(" ", " ").replace("\t", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm(s: str) -> str:
    """Ключ для сравнения: нижний регистр, без казахских букв, пунктуации и типов."""
    s = clean(s).lower().translate(_KK2RU)
    s = re.sub(r"[^\w\s-]", " ", s)
    s = s.replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


_LA_PREFIX = re.compile(r"(?i)^(жк|ж\.к\.|мкр\.?|мкрн\.?|микрорайон|жилой комплекс|жилой массив|ж/м)\s+")


def clean_living_area(name: str) -> str:
    """'Микрорайон Тастыбулак' -> 'Тастыбулак', '"ЖК Hayat Park"' -> 'Hayat Park'."""
    name = name.strip().strip('"«»\'')
    return re.sub(r"\s+", " ", _LA_PREFIX.sub("", name).strip().strip('"«»\''))


def house_key(h: str | None) -> str | None:
    """'12 А' -> '12а', '33/1' -> '33/1'; иначе None. Та же нормализация — в scripts/build_gazetteer.py."""
    if not h:
        return None
    h = re.sub(r"\s+", "", str(h).lower().replace("ё", "е"))
    return h if re.fullmatch(r"\d{1,4}[а-яa-z]?(/\d{1,4}[а-яa-z]?)?", h) else None


def has_kazakh(s: str) -> bool:
    return bool(re.search("[әөүұқғңһі]", s.lower()))
