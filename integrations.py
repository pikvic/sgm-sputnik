"""User-initiated geocoding and Yandex authorization."""

import json
import secrets
from base64 import urlsafe_b64encode
from hashlib import sha256
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import settings as cfg
import storage
import gazetteer
from psycopg.types.json import Jsonb


def remote_json(request, timeout=10):
    with urlopen(request, timeout=timeout) as result:
        payload = result.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("Response too large")
        return json.loads(payload)


def authorize(session):
    if not cfg.YANDEX_CLIENT_ID or not cfg.YANDEX_CLIENT_SECRET:
        raise ValueError("Вход через Яндекс ещё не настроен.")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with storage.database() as db:
        db.execute("DELETE FROM oauth_states WHERE session_id=%s OR expires<now()", (session["id"],))
        db.execute(
            "INSERT INTO oauth_states(state_hash,session_id,expires,verifier) VALUES (%s,%s,now()+interval '10 minutes',%s)",
            (storage.digest(state), session["id"], verifier),
        )
    return "https://oauth.yandex.ru/authorize?" + urlencode({
        "response_type": "code",
        "client_id": cfg.YANDEX_CLIENT_ID,
        "redirect_uri": cfg.APP_URL + "/auth/callback",
        "scope": "login:info",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def callback(session, params):
    state = params.get("state", "")
    with storage.database() as db:
        saved = db.execute(
            "DELETE FROM oauth_states WHERE state_hash=%s AND session_id=%s AND expires>now() RETURNING verifier",
            (storage.digest(state), session["id"]),
        ).fetchone()
    if not saved:
        raise ValueError("Ссылка входа истекла. Начните вход заново.")
    if params.get("error") or not params.get("code"):
        raise ValueError("Вход через Яндекс отменён.")
    body = urlencode({
        "grant_type": "authorization_code", "code": params["code"],
        "client_id": cfg.YANDEX_CLIENT_ID, "client_secret": cfg.YANDEX_CLIENT_SECRET,
        "code_verifier": saved["verifier"],
    }).encode()
    token = remote_json(Request("https://oauth.yandex.ru/token", data=body))
    profile = remote_json(Request("https://login.yandex.ru/info?format=json", headers={
        "Authorization": "OAuth " + token["access_token"],
    }))
    identifier = profile.get("id")
    if not isinstance(identifier, str) or not identifier.isdigit():
        raise ValueError("Яндекс не вернул идентификатор пользователя.")
    login = profile.get("login")
    if not isinstance(login, str) or not 1 <= len(login) <= 200:
        login = None
    with storage.database() as db:
        db.execute("UPDATE sessions SET uid=%s,yandex_login=%s WHERE id=%s", (identifier, login, session["id"]))
    storage.record(session["id"], "login", {"admin": storage.is_admin({"uid": identifier, "yandex_login": login})})


def osm_geocode(session, params):
    if not cfg.GEOCODER_USER_AGENT:
        raise ValueError("Геокодирование ещё не настроено администратором. Укажите область на карте.")
    mode = params.get("mode", "search")
    query = {"format": "jsonv2", "accept-language": "ru", "addressdetails": 0}
    if mode == "search":
        text = params.get("q", "").strip()
        if not 2 <= len(text) <= 180:
            raise ValueError("Введите название длиной от 2 до 180 символов.")
        query.update(q=text, limit=5)
    elif mode == "reverse":
        lat, lon = float(params.get("lat", "nan")), float(params.get("lon", "nan"))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Некорректные координаты точки.")
        query.update(lat=lat, lon=lon, zoom=16)
    else:
        raise ValueError("Неизвестный режим геокодирования.")
    key = storage.digest("nominatim-candidates-v2|" + cfg.GEOCODER_URL + mode + json.dumps(query, sort_keys=True))
    with storage.database() as db:
        calls = db.execute(
            "SELECT count(*) n FROM events WHERE session_id=%s AND kind='geocode' AND created>now()-interval '1 minute'",
            (session["id"],),
        ).fetchone()["n"]
        if calls >= 8:
            raise storage.LimitError("Слишком частые обращения к геокодированию. Повторите через минуту.")
        db.execute("SELECT pg_advisory_xact_lock(%s)", (storage.LOCK_ID + 1,))
        cached = db.execute(
            "SELECT data FROM geocode_cache WHERE key=%s AND created>now()-interval '7 days'", (key,),
        ).fetchone()
        if cached:
            data = cached["data"]
        else:
            # The gate is shared by every worker and container using this database.
            db.execute("SELECT pg_sleep(greatest(0,1.1-extract(epoch FROM(now()-last_call)))) FROM geocode_gate WHERE id=1")
            url = cfg.GEOCODER_URL + "/" + mode + "?" + urlencode(query)
            try:
                payload = remote_json(Request(url, headers={"User-Agent": cfg.GEOCODER_USER_AGENT}), timeout=8)
                rows = payload if isinstance(payload, list) else ([] if payload.get("error") else [payload])
                data = []
                for row in rows[:5]:
                    south, north, west, east = map(float, row["boundingbox"])
                    data.append({
                        "id": "osm:" + str(row.get("osm_type", "place")) + ":" + str(row.get("osm_id", storage.digest(str(row))[:20])),
                        "name": str(row["display_name"])[:600],
                        "bbox": [west, south, east, north],
                        "point": [float(row["lon"]), float(row["lat"])],
                        "type": str(row.get("type", "")),
                        "source": "osm", "source_name": "OpenStreetMap / Nominatim",
                        "source_url": "https://www.openstreetmap.org/" + str(row.get("osm_type", "search")) + "/" + str(row.get("osm_id", "")),
                        "coordinate_note": "Точка из OpenStreetMap. Прямоугольный охват геокодера не является точной границей геологического объекта.",
                    })
            except (OSError, ValueError, KeyError, TypeError) as error:
                # Commit the request time even if the provider failed.
                db.execute("UPDATE geocode_gate SET last_call=clock_timestamp() WHERE id=1")
                db.commit()
                raise ValueError("Геокодер не ответил. Можно выбрать участок на карте.") from error
            db.execute("UPDATE geocode_gate SET last_call=clock_timestamp() WHERE id=1")
            db.execute(
                "INSERT INTO geocode_cache(key,data) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET created=now(),data=excluded.data",
                (key, Jsonb(data)),
            )
    return {"items": data, "attribution": cfg.GEOCODER_ATTRIBUTION}


def wikidata_search(session, query):
    """Two bounded, cached API requests. No coordinates are generated by the model."""
    key = storage.digest("wikidata-v1|" + query.casefold())
    with storage.database() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (storage.LOCK_ID + 1,))
        cached = db.execute("SELECT data FROM geocode_cache WHERE key=%s AND created>now()-interval '7 days'", (key,)).fetchone()
        if cached:
            return cached["data"]

        def get(params):
            db.execute("SELECT pg_advisory_xact_lock(%s)", (storage.LOCK_ID + 1,))
            blocked = db.execute("SELECT blocked_until>now() blocked FROM geocode_gate WHERE id=1").fetchone()["blocked"]
            if blocked:
                raise ValueError("Wikidata просит временно сократить обращения.")
            db.execute("SELECT pg_sleep(greatest(0,1.1-extract(epoch FROM(now()-last_call)))) FROM geocode_gate WHERE id=1")
            try:
                return remote_json(Request("https://www.wikidata.org/w/api.php?" + urlencode({"format": "json", **params}),
                                           headers={"User-Agent": cfg.GEOCODER_USER_AGENT}), timeout=8)
            except HTTPError as error:
                if error.code in (429, 503):
                    delay = error.headers.get("Retry-After", "60")
                    delay = min(3600, max(60, int(delay))) if delay.isdecimal() else 60
                    db.execute("UPDATE geocode_gate SET blocked_until=clock_timestamp()+%s*interval '1 second' WHERE id=1", (delay,))
                raise
            finally:
                db.execute("UPDATE geocode_gate SET last_call=clock_timestamp() WHERE id=1")
                db.commit()

        matches = get({"action": "wbsearchentities", "search": query, "language": "ru", "uselang": "ru", "limit": 5})
        if "error" in matches:
            raise ValueError("Wikidata вернула ошибку поиска.")
        ids = [row["id"] for row in matches.get("search", []) if str(row.get("id", "")).startswith("Q")][:5]
        rows = get({"action": "wbgetentities", "ids": "|".join(ids), "props": "labels|descriptions|claims", "languages": "ru|en"}) if ids else {}
        if "error" in rows:
            raise ValueError("Wikidata вернула ошибку координат.")
        data = []
        for identifier in ids:
            entity = rows.get("entities", {}).get(identifier, {})
            coordinates = entity.get("claims", {}).get("P625", [])
            if not coordinates:
                continue
            preferred = sorted(coordinates, key=lambda row: row.get("rank") != "preferred")
            coordinate = next((row.get("mainsnak", {}).get("datavalue", {}).get("value", {}) for row in preferred
                               if row.get("rank") != "deprecated" and row.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("globe", "").endswith("/Q2")), {})
            lon, lat = coordinate.get("longitude"), coordinate.get("latitude")
            if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                continue
            label = entity.get("labels", {}).get("ru", entity.get("labels", {}).get("en", {})).get("value", identifier)
            description = entity.get("descriptions", {}).get("ru", entity.get("descriptions", {}).get("en", {})).get("value", "Объект Wikidata")
            data.append({"id": "wd:" + identifier, "name": label[:600], "type": description[:300], "point": [lon, lat],
                         "source": "wikidata", "source_name": "Wikidata · CC0", "source_url": "https://www.wikidata.org/wiki/" + identifier,
                         "coordinate_note": "Ориентировочная точка Wikidata. Точность и соответствие объекту нужно проверить; это не граница месторождения."})
        db.execute("INSERT INTO geocode_cache(key,data) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET created=now(),data=excluded.data", (key, Jsonb(data)))
    return data


def geocode(session, params):
    mode = params.get("mode", "search")
    if mode == "reverse":
        result = osm_geocode(session, params)
        gazetteer.remember(session["id"], result["items"])
        storage.record(session["id"], "geocode", {"mode": mode, "results": len(result["items"])})
        return result
    if mode != "search":
        raise ValueError("Неизвестный режим геокодирования.")
    query = params.get("q", "").strip()
    if not 2 <= len(query) <= 180:
        raise ValueError("Введите название длиной от 2 до 180 символов.")
    with storage.database() as db:
        calls = db.execute("SELECT count(*) n FROM events WHERE session_id=%s AND kind='geocode' AND created>now()-interval '1 minute'", (session["id"],)).fetchone()["n"]
    if calls >= 8:
        raise storage.LimitError("Слишком частые обращения к геокодированию. Повторите через минуту.")
    items = gazetteer.search(query)
    warning = ""
    attribution = "Локальный справочник геологических объектов · Wikidata CC0"
    if not items and cfg.GEOCODER_USER_AGENT:
        try:
            result = osm_geocode(session, params)
            items, attribution = result["items"], result["attribution"]
        except ValueError:
            warning = "OpenStreetMap не ответил. "
        if not items:
            try:
                items = wikidata_search(session, query)
                attribution = "Wikidata · CC0"
            except (OSError, ValueError, KeyError, TypeError):
                warning += "Wikidata временно недоступна. "
    elif not items:
        warning = "Внешний поиск не настроен; проверен локальный справочник. "
    gazetteer.remember(session["id"], items)
    storage.record(session["id"], "geocode", {"mode": "search", "results": len(items)})
    return {"items": items, "attribution": attribution, "warning": warning.strip()}
