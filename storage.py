"""Shared sessions, atomic usage accounting and administrator telemetry."""

import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import settings as cfg

COOKIE = "sputnik_session"
LOCK_ID = 78543211
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id text PRIMARY KEY,
    expires timestamptz NOT NULL,
    uid text,
    yandex_login text,
    consent boolean NOT NULL DEFAULT false,
    consent_scope text NOT NULL DEFAULT '',
    context jsonb NOT NULL DEFAULT '{}',
    messages jsonb NOT NULL DEFAULT '[]',
    revision integer NOT NULL DEFAULT 0,
    tokens bigint NOT NULL DEFAULT 0,
    requests integer NOT NULL DEFAULT 0,
    busy_until timestamptz,
    last_request timestamptz
);
CREATE TABLE IF NOT EXISTS daily_usage (
    day date PRIMARY KEY,
    tokens bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_requests (
    id text PRIMARY KEY,
    session_id text NOT NULL,
    ip_hash text NOT NULL,
    day date NOT NULL,
    created timestamptz NOT NULL DEFAULT now(),
    reserved bigint NOT NULL,
    charged bigint NOT NULL,
    is_admin boolean NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    prompt_tokens bigint,
    completion_tokens bigint,
    elapsed_ms integer,
    error text
);
CREATE INDEX IF NOT EXISTS ai_requests_day_ip ON ai_requests(day, ip_hash);
CREATE TABLE IF NOT EXISTS events (
    id bigserial PRIMARY KEY,
    created timestamptz NOT NULL DEFAULT now(),
    session_id text,
    kind text NOT NULL,
    data jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_created ON events(created);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash text PRIMARY KEY,
    session_id text NOT NULL,
    expires timestamptz NOT NULL,
    verifier text NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_cache (
    key text PRIMARY KEY,
    created timestamptz NOT NULL DEFAULT now(),
    data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS geocode_gate (
    id integer PRIMARY KEY CHECK (id=1),
    last_call timestamptz NOT NULL DEFAULT 'epoch'
);
INSERT INTO geocode_gate(id) VALUES (1) ON CONFLICT DO NOTHING;
ALTER TABLE geocode_gate ADD COLUMN IF NOT EXISTS blocked_until timestamptz NOT NULL DEFAULT 'epoch';
CREATE TABLE IF NOT EXISTS maintenance (
    id integer PRIMARY KEY CHECK (id=1),
    last_cleanup date NOT NULL DEFAULT '1970-01-01'
);
INSERT INTO maintenance(id) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS object_candidates (
    session_id text NOT NULL,
    id text NOT NULL,
    created timestamptz NOT NULL DEFAULT now(),
    data jsonb NOT NULL,
    PRIMARY KEY(session_id,id)
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS consent_scope text NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS yandex_login text;
"""


class LimitError(Exception):
    pass


@contextmanager
def database():
    with psycopg.connect(cfg.DATABASE_URL, row_factory=dict_row, connect_timeout=4) as connection:
        yield connection


def initialize():
    with database() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        db.execute(SCHEMA)
        cleanup(db)


def cleanup(db):
    needed = db.execute(
        """UPDATE maintenance SET last_cleanup=(now() AT TIME ZONE 'UTC')::date
        WHERE id=1 AND last_cleanup<(now() AT TIME ZONE 'UTC')::date RETURNING id""",
    ).fetchone()
    if not needed:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.TELEMETRY_DAYS)
    db.execute("DELETE FROM events WHERE created<%s", (cutoff,))
    db.execute("DELETE FROM ai_requests WHERE created<%s", (cutoff,))
    db.execute("DELETE FROM oauth_states WHERE expires<now()")
    db.execute("DELETE FROM sessions WHERE expires<now() AND (busy_until IS NULL OR busy_until<now())")
    db.execute("DELETE FROM geocode_cache WHERE created<now()-interval '7 days'")
    db.execute("DELETE FROM object_candidates WHERE created<now()-interval '7 days' OR session_id NOT IN (SELECT id FROM sessions)")
    db.execute("DELETE FROM daily_usage WHERE day<%s", (cutoff.date(),))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def is_admin(session):
    if not session or not session.get("uid"):
        return False
    # A login is trusted only when obtained by the server from Yandex's OAuth API.
    login = (session.get("yandex_login") or "").casefold()
    return session["uid"] in cfg.ADMIN_YANDEX_IDS or bool(login and login in {
        value.casefold() for value in cfg.ADMIN_YANDEX_IDS if not value.isdecimal()
    })

def has_consent(session):
    return bool(session["consent"] and session["consent_scope"] == cfg.AI_CONSENT_SCOPE)


def get_session(environ, create=False):
    cookies = SimpleCookie()
    try:
        cookies.load(environ.get("HTTP_COOKIE", ""))
    except CookieError:
        pass
    raw = cookies[COOKIE].value if COOKIE in cookies else ""
    key = digest(raw) if 20 <= len(raw) <= 200 else ""
    with database() as db:
        session = db.execute("SELECT * FROM sessions WHERE id=%s AND expires>now()", (key,)).fetchone()
        if session or not create:
            return session, None
        raw = secrets.token_urlsafe(32)
        session = db.execute(
            "INSERT INTO sessions(id,expires) VALUES (%s,%s) RETURNING *",
            (digest(raw), datetime.now(timezone.utc) + timedelta(days=cfg.SESSION_DAYS)),
        ).fetchone()
    cookie = f"{COOKIE}={raw}; Path=/; HttpOnly; SameSite=Lax; Max-Age={cfg.SESSION_DAYS * 86400}"
    if cfg.APP_URL.startswith("https://"):
        cookie += "; Secure"
    return session, cookie


def status(session):
    with database() as db:
        global_usage = db.execute("SELECT tokens FROM daily_usage WHERE day=(now() AT TIME ZONE 'UTC')::date").fetchone()
    admin = is_admin(session)
    return {
        "authenticated": bool(session["uid"]),
        "admin": admin,
        "admin_relogin_needed": bool(session["uid"] and not session.get("yandex_login")
                                     and any(not value.isdecimal() for value in cfg.ADMIN_YANDEX_IDS)),
        "enabled": bool(cfg.AI_API_KEY),
        "login_enabled": bool(cfg.YANDEX_CLIENT_ID and cfg.YANDEX_CLIENT_SECRET),
        "consent": has_consent(session),
        "local_model": cfg.AI_LOCAL_MODE,
        "context": session["context"],
        "messages": session["messages"],
        "remaining_tokens": None if admin else max(0, cfg.AI_SESSION_TOKENS - session["tokens"]),
        "remaining_requests": None if admin else max(0, cfg.AI_SESSION_REQUESTS - session["requests"]),
        "global_remaining_tokens": None if admin else max(0, cfg.AI_ANON_DAILY_TOKENS - (global_usage or {}).get("tokens", 0)),
        "geocoding": True,
        "external_geocoding": bool(cfg.GEOCODER_USER_AGENT),
        "geocoder_attribution": cfg.GEOCODER_ATTRIBUTION,
    }


def reserve(session_id, ip, tokens, revision):
    request_id = secrets.token_urlsafe(24)
    with database() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        session = db.execute("SELECT * FROM sessions WHERE id=%s AND expires>now() FOR UPDATE", (session_id,)).fetchone()
        if not session or not has_consent(session):
            raise LimitError("Подтвердите условия передачи данных ИИ-помощнику.")
        if session["revision"] != revision:
            raise LimitError("Диалог изменился в другой вкладке. Обновите панель помощника.")
        now = datetime.now(timezone.utc)
        if session["busy_until"] and session["busy_until"] > now:
            raise LimitError("Предыдущий запрос ещё выполняется.")
        if session["last_request"] and (now - session["last_request"]).total_seconds() < 3:
            raise LimitError("Подождите несколько секунд перед следующим запросом.")
        admin = is_admin(session)
        day = now.date()
        ip_hash = digest(str(day) + "|" + ip)
        db.execute("INSERT INTO daily_usage(day) VALUES (%s) ON CONFLICT DO NOTHING", (day,))
        usage = db.execute("SELECT tokens FROM daily_usage WHERE day=%s FOR UPDATE", (day,)).fetchone()["tokens"]
        ip_count = db.execute("SELECT count(*) n FROM ai_requests WHERE day=%s AND ip_hash=%s AND NOT is_admin", (day, ip_hash)).fetchone()["n"]
        if not admin and (
            session["tokens"] + tokens > cfg.AI_SESSION_TOKENS
            or usage + tokens > cfg.AI_ANON_DAILY_TOKENS
            or session["requests"] >= cfg.AI_SESSION_REQUESTS
            or ip_count >= cfg.AI_IP_DAILY_REQUESTS
        ):
            raise LimitError("Недостаточно лимита для запроса. Попробуйте более короткий диалог или обратитесь к администратору.")
        db.execute(
            "UPDATE sessions SET tokens=tokens+%s, requests=requests+%s, last_request=now(), busy_until=%s WHERE id=%s",
            (0 if admin else tokens, 0 if admin else 1, now + timedelta(seconds=cfg.AI_TIMEOUT_SECONDS + 30), session_id),
        )
        if not admin:
            db.execute("UPDATE daily_usage SET tokens=tokens+%s WHERE day=%s", (tokens, day))
        db.execute(
            "INSERT INTO ai_requests(id,session_id,ip_hash,day,reserved,charged,is_admin) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (request_id, session_id, ip_hash, day, tokens, tokens, admin),
        )
    return request_id


def finish(request_id, *, usage=None, elapsed_ms=0, error=None, result=None):
    with database() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        row = db.execute("SELECT * FROM ai_requests WHERE id=%s FOR UPDATE", (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return
        prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
        known = type(prompt) is int and type(completion) is int and min(prompt, completion) >= 0
        charged = prompt + completion if known else row["reserved"]
        delta = charged - row["charged"]
        db.execute(
            "UPDATE ai_requests SET charged=%s,prompt_tokens=%s,completion_tokens=%s,elapsed_ms=%s,status=%s,error=%s WHERE id=%s",
            (charged, prompt if known else None, completion if known else None, elapsed_ms,
             "error" if error else "complete", error, request_id),
        )
        if not row["is_admin"]:
            db.execute("UPDATE daily_usage SET tokens=tokens+%s WHERE day=%s", (delta, row["day"]))
        quota_delta = 0 if row["is_admin"] else delta
        if result:
            db.execute(
                "UPDATE sessions SET tokens=tokens+%s,busy_until=NULL,context=%s,messages=%s,revision=revision+1 WHERE id=%s",
                (quota_delta, Jsonb(result["context"]), Jsonb(result["messages"]), row["session_id"]),
            )
        else:
            db.execute("UPDATE sessions SET tokens=tokens+%s,busy_until=NULL WHERE id=%s", (quota_delta, row["session_id"]))


def record(session_id, kind, data=None):
    with database() as db:
        cleanup(db)
        db.execute(
            "INSERT INTO events(session_id,kind,data) VALUES (%s,%s,%s)",
            (session_id, kind, Jsonb(data or {})),
        )


def telemetry():
    with database() as db:
        cleanup(db)
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.TELEMETRY_DAYS)
        events = db.execute(
            "SELECT kind,count(*) count FROM events WHERE created>=%s GROUP BY kind ORDER BY count DESC", (cutoff,),
        ).fetchall()
        ai = db.execute(
            """SELECT count(*) requests,coalesce(sum(charged),0)::bigint charged_tokens,
            coalesce(sum(prompt_tokens),0)::bigint prompt_tokens,coalesce(sum(completion_tokens),0)::bigint completion_tokens,
            count(*) FILTER (WHERE status='error') errors,
            count(*) FILTER (WHERE prompt_tokens IS NULL) reserved_without_usage
            FROM ai_requests WHERE created>=%s""", (cutoff,),
        ).fetchone()
        recent = db.execute(
            """SELECT created,status,charged,prompt_tokens,completion_tokens,elapsed_ms,is_admin,error
            FROM ai_requests ORDER BY created DESC LIMIT 100""",
        ).fetchall()
        service = db.execute(
            """SELECT data->>'path' path,data->>'product' product,count(*) requests,
            count(*) FILTER (WHERE (data->>'status')::int>=400) errors
            FROM events WHERE kind='service_request' AND created>=%s
            GROUP BY 1,2 ORDER BY requests DESC""", (cutoff,),
        ).fetchall()
    for row in recent:
        row["created"] = row["created"].isoformat()
    return {"days": cfg.TELEMETRY_DAYS, "events": events, "service": service, "assistant": ai, "recent": recent}
