import json
from ipaddress import ip_address
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import psycopg
from psycopg.types.json import Jsonb

import assistant
import integrations
import settings as cfg
import storage

from app import ApiError, json_response, response


def client_address(environ):
    raw_peer = environ.get("REMOTE_ADDR", "")
    try:
        peer = ip_address(raw_peer)
        forwarded = environ.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if len(forwarded) > 20:
            return str(peer)
        for value in reversed(forwarded):
            if not any(peer in network for network in cfg.TRUSTED_PROXY_NETWORKS):
                break
            peer = ip_address(value.strip())
        return str(peer)
    except ValueError:
        return raw_peer


def body_json(environ):
    if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
        raise ApiError(415, "Нужен запрос в формате JSON.")
    length = int(environ.get("CONTENT_LENGTH") or "0")
    if not 0 < length <= 40000:
        raise ApiError(413, "Слишком большой или пустой запрос.")
    data = json.loads(environ["wsgi.input"].read(length))
    if not isinstance(data, dict):
        raise ApiError(400, "Некорректное тело запроса.")
    return data


def require_post(environ):
    if environ["REQUEST_METHOD"] != "POST":
        raise ApiError(405, "Для этого действия нужен POST-запрос.")
    if environ.get("HTTP_ORIGIN", "") != cfg.APP_URL:
        raise ApiError(403, "Источник запроса не совпадает с адресом сервиса.")


def route(environ, start):
    path = environ["PATH_INFO"]
    if not cfg.DATABASE_URL:
        return json_response(start, {"error": "Помощник ещё не настроен администратором."}, 503)
    try:
        session, cookie = storage.get_session(environ, create=path in ("/api/assistant/session", "/auth/login"))
        if not session:
            raise ApiError(401, "Откройте панель помощника, чтобы начать сессию.")

        def with_cookie(status, headers, exc_info=None):
            if cookie:
                headers = headers + [("Set-Cookie", cookie)]
            return start(status, headers, exc_info)

        method = environ["REQUEST_METHOD"]
        if path == "/api/assistant/session" and method == "GET":
            state = storage.status(session)
            state["yandex_id"] = session["uid"]
            return json_response(with_cookie, state)
        if path == "/auth/login" and method == "GET":
            return response(with_cookie, 302, headers=(("Location", integrations.authorize(session)),))
        if path == "/auth/callback" and method == "GET":
            params = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=10).items()}
            integrations.callback(session, params)
            return response(with_cookie, 302, headers=(("Location", cfg.APP_URL + "/#assistant"),))
        if path == "/api/admin/telemetry" and method == "GET":
            if not storage.is_admin(session):
                raise ApiError(403, "Телеметрия доступна администратору.")
            return json_response(with_cookie, storage.telemetry())
        if path == "/api/geocode" and method == "GET":
            params = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), max_num_fields=6).items()}
            return json_response(with_cookie, integrations.geocode(session, params))
        require_post(environ)
        body = body_json(environ)
        if path == "/api/assistant/consent":
            if body.get("accepted") is not True:
                raise ApiError(400, "Подтвердите условия передачи данных.")
            with storage.database() as db:
                db.execute("UPDATE sessions SET consent=true,consent_scope=%s WHERE id=%s", (cfg.AI_CONSENT_SCOPE, session["id"]))
            return json_response(with_cookie, {"ok": True})
        if path == "/api/assistant/reset":
            with storage.database() as db:
                changed = db.execute(
                    """UPDATE sessions SET messages='[]',context='{}',revision=revision+1
                    WHERE id=%s AND (busy_until IS NULL OR busy_until<now()) RETURNING id""", (session["id"],),
                ).fetchone()
                if not changed:
                    raise ApiError(409, "Дождитесь завершения текущего запроса.")
            return json_response(with_cookie, {"ok": True})
        if path == "/api/assistant/context":
            context = assistant.clean_context(body, session["id"])
            with storage.database() as db:
                changed = db.execute(
                    """UPDATE sessions SET context=%s,revision=revision+1 WHERE id=%s
                    AND (busy_until IS NULL OR busy_until<now()) RETURNING id""", (Jsonb(context), session["id"]),
                ).fetchone()
                if not changed:
                    raise ApiError(409, "Дождитесь завершения текущего запроса.")
            return json_response(with_cookie, {"context": context})
        if path == "/auth/logout":
            with storage.database() as db:
                db.execute("UPDATE sessions SET uid=NULL,yandex_login=NULL WHERE id=%s", (session["id"],))
            return json_response(with_cookie, {"ok": True})
        if path == "/api/assistant/chat":
            return json_response(with_cookie, assistant.chat(session, body, client_address(environ)))
        if path == "/api/events":
            if body.get("kind") not in ("assistant_open", "plan_apply", "object_confirm", "scene_open"):
                raise ApiError(400, "Неизвестный тип события.")
            # Only allow product identifiers. User messages and coordinates are not telemetry.
            data = {"product": body["product"]} if body.get("product") in assistant.PRODUCTS else {}
            storage.record(session["id"], body["kind"], data)
            return json_response(with_cookie, {"ok": True})
        raise ApiError(404, "Страница не найдена.")
    except storage.LimitError as error:
        return json_response(start, {"error": str(error)}, 429)
    except ApiError as error:
        return json_response(start, {"error": str(error)}, error.status)
    except (ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, ValueError) else "Получен неподдерживаемый ответ внешнего сервиса."
        return json_response(start, {"error": message}, 400)
    except (OSError, HTTPError, URLError):
        return json_response(start, {"error": "Сервис не ответил. Повторите запрос позже. Повторный запрос учитывается в лимите."}, 502)
    except psycopg.Error:
        return json_response(start, {"error": "Учёт лимитов временно недоступен. Попробуйте позже."}, 503)


def log_service(environ, status, elapsed_ms):
    if not cfg.DATABASE_URL or not environ["PATH_INFO"].startswith("/api/") or environ["PATH_INFO"].startswith(("/api/assistant", "/api/admin", "/api/events", "/api/geocode")):
        return
    try:
        session, _ = storage.get_session(environ)
        data = {"path": environ["PATH_INFO"], "status": status, "elapsed_ms": elapsed_ms}
        params = parse_qs(environ.get("QUERY_STRING", ""))
        product = params.get("product", [""])[0]
        if product in assistant.PRODUCTS:
            data["product"] = product
        storage.record(session["id"] if session else None, "service_request", data)
    except (psycopg.Error, OSError, ValueError):
        pass
