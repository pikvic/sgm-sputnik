import json
import logging
import math
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import cache
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from catalog import PRODUCTS, public_catalog

ROOT = Path(__file__).resolve().parent
HOST = "planetarycomputer.microsoft.com"
STAC = f"https://{HOST}/api/stac/v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,240}\Z")
BLOB = re.compile(r"([a-z0-9]{3,24})\.blob\.core\.windows\.net\Z")
STATIC = {
    "/": "index.html",
    "/app.css": "app.css",
    "/app.js": "app.js",
    "/vendor/maplibre-gl.js": "vendor/maplibre-gl.js",
    "/vendor/maplibre-gl.css": "vendor/maplibre-gl.css",
    "/vendor/LICENSE.txt": "vendor/LICENSE.txt",
}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def read_json(url, *, item_lookup=False, timeout=20):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "Sputnik-GeologyScience/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as error:
        if error.code == 404 and item_lookup:
            raise ApiError(404, "Сцена больше не доступна в каталоге.") from error
        if error.code == 429:
            raise ApiError(503, "Поставщик ограничил частоту запросов. Повторите поиск чуть позже.") from error
        raise ApiError(502, "Поставщик данных вернул ошибку.") from error
    except TimeoutError as error:
        raise ApiError(504, "Поставщик данных не ответил вовремя.") from error
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ApiError(504, "Поставщик данных не ответил вовремя.") from error
        raise ApiError(502, "Не удалось получить ответ от поставщика данных.") from error
    except (OSError, ValueError) as error:
        raise ApiError(502, "Не удалось получить ответ от поставщика данных.") from error
    if not isinstance(payload, dict):
        raise ApiError(502, "Поставщик вернул неожиданный формат данных.")
    return payload


def product_for(params):
    product = PRODUCTS.get(params.get("product"))
    if product is None:
        raise ApiError(400, "Выберите продукт из каталога.")
    return product


def bbox_for(raw):
    try:
        west, south, east, north = map(float, raw.split(","))
    except ValueError as error:
        raise ApiError(400, "Укажите четыре координаты области поиска.") from error
    if not (
        all(math.isfinite(value) for value in (west, south, east, north))
        and -180 <= west <= 180 and -180 <= east <= 180
        and -90 <= south < north <= 90 and west != east
        and not (west == 180 and east == -180)
    ):
        raise ApiError(400, "Проверьте границы области поиска и порядок координат.")
    return ",".join(map(str, (west, south, east, north)))


def search_dates(params, product):
    if product["start"] is None:
        return None
    try:
        start, end = (date.fromisoformat(params.get(key, "")) for key in ("start", "end"))
    except ValueError as error:
        raise ApiError(400, "Укажите начало и конец периода.") from error
    if start > end:
        raise ApiError(400, "Дата окончания должна быть не раньше даты начала.")
    if (product.get("end") and start.isoformat() > product["end"]) or end.isoformat() < product["start"]:
        return ""
    return f"{start}T00:00:00Z/{end}T23:59:59.999Z"


def supported_url(url):
    if not isinstance(url, str) or any(ord(char) < 32 for char in url):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and (
        parts.netloc == HOST or BLOB.fullmatch(parts.netloc)
    )


def summary(item):
    props = item.get("properties", {})
    return {
        "id": item["id"],
        "platform": props.get("platform"),
        "date": props.get("datetime") or props.get("start_datetime"),
        "cloud": props.get("eo:cloud_cover"),
        "bbox": item.get("bbox"),
        "geometry": item.get("geometry"),
    }


def search(params):
    product = product_for(params)
    query = {"collections": product["collection"], "bbox": bbox_for(params.get("bbox", "")), "limit": 20}
    interval = search_dates(params, product)
    if interval == "":
        return {"items": [], "next": None, "reason": "outside_archive"}
    if interval:
        query.update(datetime=interval, sortby="-datetime")
    if product.get("filter"):
        query["query"] = json.dumps(product["filter"], separators=(",", ":"))
    if params.get("token"):
        query["token"] = params["token"]
    payload = read_json(f"{STAC}/search?{urlencode(query)}")
    token = None
    for link in payload.get("links", []):
        if link.get("rel") == "next":
            token = parse_qs(urlsplit(link["href"]).query).get("token", [None])[0]
            break
    return {"items": [summary(item) for item in payload["features"]], "next": token}


def item_for(params):
    product = product_for(params)
    identifier = params.get("id", "")
    if not IDENTIFIER.fullmatch(identifier) or identifier in {".", ".."}:
        raise ApiError(400, "Некорректный идентификатор сцены.")
    url = f"{STAC}/collections/{product['collection']}/items/{quote(identifier, safe='')}"
    item = read_json(url, item_lookup=True)
    for key, condition in product.get("filter", {}).items():
        if item.get("properties", {}).get(key) != condition["eq"]:
            raise ApiError(404, "Сцена не относится к выбранному продукту.")
    return item, url


def downloadable(asset):
    url = asset.get("href", "")
    if not supported_url(url):
        return False
    roles = asset.get("roles", [])
    path = urlsplit(url).path.lower()
    return "data" in roles or path.endswith((".tif", ".tiff", ".nc", ".jp2"))


def file_size(asset):
    size = asset.get("file:size")
    return size if type(size) is int and size >= 0 else None


def scene_detail(item, source):
    assets = item.get("assets", {})
    preview = next((
        href for key in ("rendered_preview", "thumbnail")
        if (href := assets.get(key, {}).get("href")) and supported_url(href)
    ), None)
    tilejson = assets.get("tilejson", {}).get("href")
    if not supported_url(tilejson) or urlsplit(tilejson).netloc != HOST:
        tilejson = None
    files = [
        {"key": key, "title": asset.get("title") or key, "media": asset.get("type"), "size": file_size(asset)}
        for key, asset in assets.items() if downloadable(asset)
    ]
    return {
        "preview": preview,
        "tilejson": tilejson,
        "files": files,
        "source": source,
        "metadata": {
            "id": item["id"],
            "collection": item.get("collection"),
            "bbox": item.get("bbox"),
            "properties": item.get("properties", {}),
        },
    }


def storage_token(endpoint, timeout=20):
    token = read_json(endpoint, timeout=timeout).get("token")
    if not isinstance(token, str) or not parse_qs(token.lstrip("?")).get("sig") or "\r" in token or "\n" in token:
        raise ApiError(502, "Поставщик не выдал ссылку для скачивания.")
    return token.lstrip("?")


def sign_asset(url, get_token=storage_token):
    parts = urlsplit(url)
    match = BLOB.fullmatch(parts.netloc)
    if not match or "sig" in parse_qs(parts.query):
        return url
    container = parts.path.strip("/").partition("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9$-]+", container):
        raise ApiError(502, "Поставщик вернул неподдерживаемую ссылку на файл.")
    token = get_token(f"https://{HOST}/api/sas/v1/token/{match[1]}/{quote(container, safe='')}")
    query = "&".join(part for part in (parts.query, token) if part)
    return urlunsplit(parts._replace(query=query))


def head_size(url):
    try:
        request = Request(url, method="HEAD", headers={"Accept-Encoding": "identity"})
        with urlopen(request, timeout=5) as result:
            length = result.headers.get("Content-Length", "")
            return int(length) if length.isdecimal() else None
    except OSError:
        return None


def scene_file_sizes(item):
    @cache
    def lookup_token(endpoint):
        # Assets in one scene usually share a storage container.
        try:
            return storage_token(endpoint, timeout=5)
        except ApiError as error:
            return error

    def get_token(endpoint):
        token = lookup_token(endpoint)
        if isinstance(token, ApiError):
            raise token
        return token

    sizes = {}
    signed = {}
    for key, asset in item.get("assets", {}).items():
        if not downloadable(asset):
            continue
        sizes[key] = file_size(asset)
        if sizes[key] is None:
            try:
                signed[key] = sign_asset(asset["href"], get_token)
            except ApiError:
                pass
    if signed:
        with ThreadPoolExecutor(max_workers=4) as pool:
            sizes.update(zip(signed, pool.map(head_size, signed.values())))
    return sizes


def response(start, status, body=b"", content="application/json; charset=utf-8", headers=()):
    start(f"{status} {HTTPStatus(status).phrase}", [
        ("Content-Type", content), ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"), *headers,
    ])
    return [body]


def json_response(start, data, status=200):
    return response(start, status, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def application(environ, start):
    try:
        if environ["REQUEST_METHOD"] != "GET":
            raise ApiError(405, "Поддерживаются только GET-запросы.")
        path = environ["PATH_INFO"]
        if path in STATIC:
            file = ROOT / "static" / STATIC[path]
            if not file.is_file():
                raise ApiError(404, "Файл интерфейса не найден.")
            content = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return response(start, 200, file.read_bytes(), f"{content}; charset=utf-8")
        if path == "/healthz":
            return json_response(start, {"status": "ok"})
        if path == "/api/catalog":
            return json_response(start, public_catalog())
        raw = environ.get("QUERY_STRING", "")
        if len(raw) > 4096:
            raise ApiError(400, "Слишком длинный запрос.")
        try:
            params = {key: values[0] for key, values in parse_qs(raw, max_num_fields=12).items()}
        except ValueError as error:
            raise ApiError(400, "Некорректные параметры запроса.") from error
        if path == "/api/search":
            return json_response(start, search(params))
        if path == "/api/item":
            item, url = item_for(params)
            return json_response(start, scene_detail(item, url))
        if path == "/api/file-sizes":
            item, _ = item_for(params)
            return json_response(start, scene_file_sizes(item))
        if path == "/api/download":
            item, _ = item_for(params)
            asset = item.get("assets", {}).get(params.get("asset", ""), {})
            if not downloadable(asset):
                raise ApiError(404, "Файл не доступен для скачивания.")
            return response(start, 302, headers=(("Location", sign_asset(asset["href"])),))
        raise ApiError(404, "Страница не найдена.")
    except ApiError as error:
        return json_response(start, {"error": str(error)}, error.status)
    except (KeyError, TypeError, ValueError) as error:
        logging.warning("Unexpected catalogue response: %s", type(error).__name__)
        return json_response(start, {"error": "Не удалось обработать ответ каталога."}, 502)


if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    with make_server("127.0.0.1", 8080, application) as server:
        print("Sputnik · http://127.0.0.1:8080", flush=True)
        server.serve_forever()
