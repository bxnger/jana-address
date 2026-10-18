"""Синтетические адреса с эталоном — замена train.json, которого не будет.

    python scripts/gen_synthetic.py --n 3000 --seed 42
    -> data/synthetic/synth_input.json   (формат sample_input.json)
       data/synthetic/synth_gold.json    (формат sample_output.json)
       data/synthetic/synth_styles.json  (id -> какие приёмы «порчи» применены, для разбора ошибок)

Основа — реальные улицы, микрорайоны и районы из data/gazetteer.sqlite (OSM).
Поверх — приёмы из ТЗ: слитное написание, казахская половина через слэш, опечатки,
латиница, ориентиры, дубль города, район вместо города, невидимые символы.

Эталон размечен по правилам жюри: поле заполнено, если оно есть в строке
(province — по городу, как в sample_output.json). district в эталоне всегда
истинный из OSM: evaluate.py сам не считает его там, где района нет в строке,
а наш пайплайн по нему проверяется на восстановление.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "gazetteer.sqlite"
SEED = ROOT / "data" / "seed" / "kz_admin.json"
OUT = ROOT / "data" / "synthetic"
FULL_BUILDINGS = ROOT / "data" / "raw" / "buildings_full.sqlite"

FIELDS = ["id", "raw_address", "province", "city", "district", "living_area", "street_type",
          "street_name", "house", "apartment", "entrance", "floor", "landmark", "confidence"]
REPUBLICAN = {"Алматы", "Астана", "Шымкент"}
# Названия OSM, в которых уже сидит тип/служебное слово, дают неверный эталон — не берём
TYPE_WORDS = re.compile(r"(?i)(^|\s|-)(көшесі|даңғылы|шағын|ауданы|орамы|тұйығы|аллея|квартал|микрорайон\w*|"
                        r"мкр|улица|проспект|переулок|тупик|проезд|бульвар|шоссе|трасса|площадь|"
                        r"учетный|промзона|промышленная|зона|линия|участок|массив|жк|"
                        r"улицу|садовое|садоводческое|общество|товарищество|кооператив|дачный|снт|ст)(\s|$)"
                        r"|^ул\.")

TYPE_TOKENS = {
    "улица": ["ул. ", "ул ", "улица ", "Ул .", "ул.", "Ул. "],
    "проспект": ["пр. ", "пр-т ", "проспект ", "пр "],
    "переулок": ["пер. ", "переулок "],
    "бульвар": ["б-р ", "бульвар "],
    "шоссе": ["шоссе "],
    "проезд": ["проезд "],
}
MKR_TOKENS = ["мкр ", "мкр. ", "Мкр ", "микрорайон ", "мкрн "]
APT = ["кв {}", "кв.{}", "кв. {}", "{}кв", "квартира {}", "kv {}", "flat {}", "{}квартира", "кв-{}"]
ENT = ["{}подьезд", "подъезд {}", "{} подъезд", "под. {}", "п-д {}", "{}-й подъезд", "{} подьезд"]
FLOOR = ["{}этаж", "этаж {}", "эт. {}", "{} этаж", "{}-этаж", "{} эт"]
HOUSE = ["{}", "{}", "{}", "д. {}", "дом {}", "д.{}", "{}дом"]
LANDMARKS = ["магазин береке", "магазин Магнум", "ТЦ Достык", "Qazpost Солнечный", "возле аптеки",
             "напротив школы", "рядом с мечетью", "маг. Анар"]


def load(con):
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    kk = {c["name"]: c["kk"] for c in seed["cities"]}
    prov = {c["name"]: c["province"] for c in seed["cities"]}
    for name, name_kk, p in con.execute(
            "SELECT name, name_kk, province FROM places WHERE kind IN ('city','town') AND province IS NOT NULL"):
        prov.setdefault(name, p)
        if name_kk:
            kk.setdefault(name, name_kk)
    streets = defaultdict(list)
    for name, stype, city, district in con.execute(
            "SELECT name, street_type, city, district FROM streets WHERE segments >= 2"):
        if city in prov and re.fullmatch(r"[А-ЯЁа-яёӘәӨөҮүҰұҚқҒғҢңІі][А-ЯЁа-яёӘәӨөҮүҰұҚқҒғҢңІі .\-]{2,30}", name) \
                and (stype in TYPE_TOKENS or stype is None) and not TYPE_WORDS.search(name):
            streets[city].append((name, stype, district))
    # только города, где улиц достаточно; вес — число улиц (Алматы чаще, чем райцентр)
    streets = {c: s for c, s in streets.items() if len(s) >= 30}
    # районы-«дубли» по имени улицы делают эталон района ненадёжным — убираем
    for c, s in streets.items():
        dists = defaultdict(set)
        for n, _, d in s:
            dists[n].add(d)
        streets[c] = [(n, t, d if len(dists[n]) == 1 else None) for n, t, d in s]
    mkrs = defaultdict(list)
    for name, city, district in con.execute(
            "SELECT name, city, district FROM living_areas WHERE kind IN ('suburb','neighbourhood','quarter')"):
        n = re.sub(r"(?i)^(микрорайон|мкр\.?|мкрн\.?|жилой массив|ж/м)\s+", "", name).strip()
        if city in streets and re.fullmatch(r"[А-ЯЁа-яёӘәӨөҮүҰұҚқҒғҢңІі0-9][А-ЯЁа-яёӘәӨөҮүҰұҚқҒғҢңІі0-9 \-]{2,25}", n) \
                and not TYPE_WORDS.search(n) and not re.fullmatch(r"(?i)микрорайоны|\d+-?[йя]?", n):
            mkrs[city].append((n, district))
    # Реальные здания (улица + номер дома) — для проверки района по дому
    buildings = defaultdict(list)
    if FULL_BUILDINGS.exists():  # полная таблица зданий (scripts/compact_buildings.py)
        bcon = sqlite3.connect(FULL_BUILDINGS)
        for street, stype, house, city, district in bcon.execute(
                "SELECT street, street_type, house, city, district FROM buildings WHERE district IS NOT NULL"):
            if city in streets and (stype in TYPE_TOKENS or stype is None) and not TYPE_WORDS.search(street) \
                    and re.fullmatch(r"[А-ЯЁа-яёӘәӨөҮүҰұҚқҒғҢңІі][А-ЯЁа-яёӘәӨөҮүҰұҚқҒғҢңІі .\-]{2,30}", street):
                buildings[city].append((street, stype, house, district))
    return prov, kk, streets, mkrs, buildings


class Gen:
    def __init__(self, rnd, prov, kk, streets, mkrs, buildings, hard=False):
        self.r, self.prov, self.kk, self.streets, self.mkrs = rnd, prov, kk, streets, mkrs
        self.buildings = buildings
        self.hard = hard
        self.cities = list(streets)
        self.weights = [len(streets[c]) ** 0.7 for c in self.cities]

    def p(self, x):
        return self.r.random() < x

    def house(self):
        n = self.r.choice([self.r.randint(1, 30), self.r.randint(1, 250)])
        roll = self.r.random()
        if roll < 0.15:
            return f"{n}{self.r.choice('абвг')}"
        if roll < 0.30:
            return f"{n}/{self.r.randint(1, 9)}"
        return str(n)

    def city_part(self, city, g, tags):
        k = self.kk.get(city, city)
        opts = [("slash_kk", f"город {city}/{k} қаласы, "), ("g_dot", f"г. {city}, "), ("g_glued", f"г.{city}, "),
                ("bare", f"{city}, "), ("bare_space", f"{city} "), ("gorod", f"город {city}, "),
                ("dup_city", f"г. {city}, {city}, "), ("no_city", "")]
        if city not in REPUBLICAN:
            opts.append(("with_province", f"{self.prov[city]}, г. {city}, "))
        tag, text = self.r.choice(opts)
        tags.append("city:" + tag)
        if tag != "no_city":
            g["city"], g["province"] = city, (city if city in REPUBLICAN else self.prov[city])
        return text

    def details(self, g, tags):
        parts = []
        if self.p(0.7):
            g["apartment"] = str(self.r.randint(1, 300))
            f = self.r.choice(APT); parts.append(f.format(g["apartment"])); tags.append("apt:" + f)
        if self.p(0.3):
            g["entrance"] = str(self.r.randint(1, 12))
            f = self.r.choice(ENT); parts.append(f.format(g["entrance"])); tags.append("ent:" + f)
        if self.p(0.3):
            g["floor"] = str(self.r.randint(1, 16))
            f = self.r.choice(FLOOR); parts.append(f.format(g["floor"])); tags.append("floor:" + f)
        self.r.shuffle(parts)
        sep = self.r.choice([", ", " ", ", "])
        return "".join(sep + x for x in parts)

    def record(self):
        city = self.r.choices(self.cities, self.weights)[0]
        g = {f: None for f in FIELDS}
        tags = []
        head = self.city_part(city, g, tags)
        kind = self.r.choices(["street", "mkr", "la_street", "kvartal"], [0.70, 0.15, 0.08, 0.07])[0]
        tags.append("kind:" + kind)

        if kind == "kvartal":
            q, h = str(self.r.randint(1, 120)), str(self.r.randint(1, 40))
            g.update(street_type="квартал", street_name=q, house=h)
            body = self.r.choice([f"{q}квартал {h}дом", f"{q} квартал, д. {h}", f"квартал {q}, {h}"])
        elif kind == "mkr" and self.mkrs.get(city):
            name, dist = self.r.choice(self.mkrs[city])
            h = self.house()
            g.update(street_type="микрорайон", street_name=name, house=h, district=dist)
            if self.p(0.2) and re.search(r"\d$", name):
                body = f"{name} микрорайон {h}дом"; tags.append("mkr_postfix")
            else:
                body = f"{self.r.choice(MKR_TOKENS)}{name}, {h}"
        else:
            if self.buildings.get(city) and self.p(0.5):
                # настоящий дом из OSM: эталон района — район этого здания
                name, stype, h, dist = self.r.choice(self.buildings[city])
                tags.append("real_building")
            else:
                name, stype, dist = self.r.choice(self.streets[city])
                h = self.house()
            g.update(street_name=name, house=h, district=dist)
            if kind == "la_street" and self.mkrs.get(city):
                la, _ = self.r.choice(self.mkrs[city])
                g["living_area"] = la
                head += f"{self.r.choice(MKR_TOKENS)}{la}, "
            if stype and self.p(0.75):
                tok = self.r.choice(TYPE_TOKENS[stype])
                g["street_type"] = stype
                tags.append("type:" + tok.strip())
            else:
                tok = ""
                tags.append("type:none")
            shown = name.lower() if self.p(0.15) else name
            g["street_name"] = shown
            if not tok and re.fullmatch(r"\d+", h) and self.p(0.25):
                body = f"{shown}{h}"; tags.append("glued_house")
            else:
                hf = self.r.choice(HOUSE if re.fullmatch(r"\d+", h) else HOUSE[:-1])
                body = f"{tok}{shown}{self.r.choice([' ', ', '])}{hf.format(h)}"
                tags.append("house:" + hf)
            if g["city"] in REPUBLICAN and dist and self.p(0.15):
                head = head + f"{dist} район, "; tags.append("district_in_string")

        text = head + body + self.details(g, tags)
        if self.p(0.10):
            g["landmark"] = self.r.choice(LANDMARKS)
            text += f" {g['landmark']}"; tags.append("landmark")
        if self.p(0.08):
            i = self.r.randrange(len(text)); text = text[:i] + "​" + text[i:]; tags.append("invisible")
        if self.p(0.08):
            text = text.replace(", ", ",  ", 1); tags.append("double_space")
        if self.hard:
            text = self.real_world_noise(text, tags)
        return text, g, tags

    # --- «грязь» из реальных заказов (--hard): эталон не меняется — это не поля адреса ---
    PHONES = ["87071234567", "8 701 123 45 67", "+7 777 123 4567", "тел 87012223344", "т.8-702-555-12-12"]
    NAMES = ["Айгерим", "получатель Ержан", "для Асель", "Мадина", "звонить Нурлану"]
    INTERCOM = ["код домофона 12К", "домофон 45", "код 1234#", "домофон не работает"]
    TYPOS = [(r"квартира", "квартра"), (r"подъезд", "падъезд"), (r"этаж", "етаж"), (r"подьезд", "подезд")]

    def real_world_noise(self, text, tags):
        if self.p(0.2):
            ph = self.r.choice(self.PHONES)
            text = f"{text} {ph}" if self.p(0.7) else f"{ph}, {text}"; tags.append("hard:phone")
        if self.p(0.15):
            nm = self.r.choice(self.NAMES)
            text = f"{text}, {nm}" if self.p(0.6) else f"{nm}, {text}"; tags.append("hard:name")
        if self.p(0.15):
            text = f"{text}, {self.r.choice(self.INTERCOM)}"; tags.append("hard:intercom")
        if self.p(0.25):
            src, dst = self.r.choice(self.TYPOS)
            if re.search(src, text):
                text = re.sub(src, dst, text, count=1); tags.append("hard:typo_" + dst)
        if self.p(0.15) and re.search(r"(?i)\b(кв|дом|д)\.? ?\d", text):
            text = re.sub(r"(?i)\b(кв|дом|д)(\.?) ?(\d)", r"\1\2 №\3", text, count=1); tags.append("hard:number_sign")
        if self.p(0.15):
            text = text.replace(", ", ","); tags.append("hard:no_space_after_comma")
        if self.p(0.08):
            text = text.upper(); tags.append("hard:caps")
        return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--hard", action="store_true",
                    help="добавить «грязь» реальных заказов: телефоны, имена, домофон, опечатки, №, капс")
    args = ap.parse_args()
    con = sqlite3.connect(DB)
    gen = Gen(random.Random(args.seed), *load(con), hard=args.hard)
    inputs, gold, styles = [], [], {}
    for i in range(args.n):
        text, g, tags = gen.record()
        rid = f"syn_{i:05d}"
        g["id"], g["raw_address"] = rid, text
        inputs.append({"id": rid, "raw_address": text})
        gold.append(g)
        styles[rid] = tags
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in (("synth_input", inputs), ("synth_gold", gold), ("synth_styles", styles)):
        (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[done] {args.n} записей -> {OUT}  (городов: {len(gen.cities)})")


if __name__ == "__main__":
    main()
