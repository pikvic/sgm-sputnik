"""Small, reviewable geology gazetteer and session-bound geocoder candidates."""

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from psycopg.types.json import Jsonb

import storage

DATA_FILE = Path(__file__).with_name("geological-objects.json")


def objects():
    return json.loads(DATA_FILE.read_text(encoding="utf-8")) if DATA_FILE.exists() else []


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    text = re.sub(r"[^\w\s-]", " ", text)
    words = [word for word in text.replace("-", " ").split() if word not in {
        "месторождение", "месторождения", "рудник", "рудника", "шахта", "карьер", "вулкан",
        "изучить", "изучение", "район", "область", "около", "вокруг", "данные", "снимки",
    }]
    return " ".join(words)


def search(query, limit=5):
    needle = normalized(query)
    if len(needle) < 3:
        return []
    ranked = []
    for entry in objects():
        names = [normalized(value) for value in [entry["name"], *entry.get("aliases", [])]]
        score = 0
        for name in names:
            if not name:
                continue
            if needle == name:
                score = max(score, 1)
            elif re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", needle):
                score = max(score, .96)
            elif len(needle) >= 4 and needle in name:
                score = max(score, .88)
            elif len(needle) <= 70:
                score = max(score, SequenceMatcher(None, needle, name).ratio() * .85)
        if score >= .66:
            ranked.append((score, candidate(entry)))
    ranked.sort(key=lambda item: (-item[0], item[1]["name"]))
    return [row for _, row in ranked[:limit]]


def candidate(entry):
    return {**entry, "id": "geo:" + entry["id"], "type": entry.get("kind", "Геологический объект"),
            "source": "gazetteer", "coordinate_note": entry.get("coordinate_note", "Ориентировочная точка, не граница объекта.")}


def remember(session_id, items):
    if not items:
        return
    with storage.database() as db:
        for item in items:
            db.execute(
                """INSERT INTO object_candidates(session_id,id,data) VALUES (%s,%s,%s)
                ON CONFLICT(session_id,id) DO UPDATE SET data=excluded.data,created=now()""",
                (session_id, item["id"], Jsonb(item)),
            )


def resolve(reference, session_id=None):
    if not isinstance(reference, str) or len(reference) > 120:
        raise ValueError("Некорректная ссылка на объект.")
    if reference.startswith("geo:"):
        for entry in objects():
            if "geo:" + entry["id"] == reference:
                return candidate(entry)
    elif session_id:
        with storage.database() as db:
            row = db.execute(
                "SELECT data FROM object_candidates WHERE session_id=%s AND id=%s AND created>now()-interval '7 days'",
                (session_id, reference),
            ).fetchone()
        if row:
            return row["data"]
    raise ValueError("Объект не найден среди проверенных вариантов. Найдите его по названию заново.")
