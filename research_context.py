"""Validated research searches. Coordinates come from the user or trusted candidates."""

import math
import re
import hashlib
from datetime import date

from pyproj import Geod

from catalog import PRODUCTS
import gazetteer

GEOD = Geod(ellps="WGS84")
MAX_SEARCHES = 24
MAX_OBJECTS = 8


def text(value, key, maximum=600):
    result = value.get(key, "")
    if not isinstance(result, str) or len(result) > maximum:
        raise ValueError("Слишком длинное или некорректное поле карточки исследования.")
    return result.strip()


def rectangle(bbox):
    west, south, east, north = bbox
    if east < west:
        east += 360
    return {"type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}


def clean_geometry(value):
    from shapely.geometry import shape
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise ValueError("Некорректный полигон области поиска.")
    rings = value.get("coordinates")
    if not isinstance(rings, list) or len(rings) != 1 or not isinstance(rings[0], list) or not 4 <= len(rings[0]) <= 150:
        raise ValueError("Слишком сложный полигон области поиска.")
    for point in rings[0]:
        if not isinstance(point, list) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point) or not -540 <= point[0] <= 540 or not -90 <= point[1] <= 90:
            raise ValueError("Некорректные координаты полигона.")
    geometry = shape(value)
    if rings[0][0] != rings[0][-1] or not geometry.is_valid or geometry.is_empty or not geometry.area:
        raise ValueError("Некорректные границы полигона.")
    return value


def circle(point, radius):
    lon, lat = point
    if GEOD.inv(lon, lat, lon, 90 if lat >= 0 else -90)[2] <= radius * 1000:
        raise ValueError("Круг охватывает полюс. Задайте область прямоугольником на карте.")
    ring = []
    for angle in range(0, 361, 5):
        x, y, _ = GEOD.fwd(lon, lat, angle, radius * 1000)
        x += round((lon - x) / 360) * 360
        ring.append([round(x, 6), round(y, 6)])
    west, east = min(p[0] for p in ring), max(p[0] for p in ring)
    # STAC bbox uses wrapped longitudes; GeoJSON remains continuous for display.
    wrap = lambda x: (x + 180) % 360 - 180
    bbox = [wrap(west), min(p[1] for p in ring), wrap(east), max(p[1] for p in ring)]
    return bbox, {"type": "Polygon", "coordinates": [ring]}


def clean_search(value, session_id=None):
    from app import bbox_for

    if not isinstance(value, dict):
        raise ValueError("Некорректная карточка исследования.")
    result = {key: text(value, key, 2000 if key in ("product_reason", "region_reason", "date_reason", "limitations") else 600)
              for key in ("goal", "target", "place_query", "product_reason", "region_reason", "date_reason", "limitations")}
    identifier = value.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,60}", identifier):
        raise ValueError("Некорректный идентификатор поиска.")
    result["id"] = identifier
    object_id = value.get("object_id", "")
    if not isinstance(object_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,60}", object_id):
        raise ValueError("Некорректный идентификатор объекта исследования.")
    result["object_id"] = object_id or "object_" + hashlib.sha256(str(value.get("object_ref") or value.get("target") or identifier).encode()).hexdigest()[:16]
    product = value.get("product", "")
    if not isinstance(product, str) or product and product not in PRODUCTS:
        raise ValueError("Неизвестный продукт.")
    result["product"] = product
    for key in ("start", "end"):
        raw = text(value, key, 10)
        result[key] = date.fromisoformat(raw).isoformat() if raw else ""
    if result["start"] and result["end"] and result["start"] > result["end"]:
        raise ValueError("Проверьте порядок дат.")
    if product and PRODUCTS[product]["start"] is None:
        result["start"] = result["end"] = ""
    bbox = value.get("bbox")
    if bbox is not None:
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("Некорректная область поиска.")
        result["bbox"] = list(map(float, bbox_for(",".join(map(str, bbox))).split(",")))
        result["geometry"] = rectangle(result["bbox"])
    result["confirmed"] = value.get("confirmed") is True
    reference = text(value, "object_ref", 120)
    result["object_ref"] = reference
    radius = value.get("radius_km")
    if radius not in (None, ""):
        if type(radius) not in (int, float) or not math.isfinite(radius) or not .1 <= radius <= 500:
            raise ValueError("Радиус области должен быть от 0,1 до 500 км.")
        result["radius_km"] = float(radius)
    if reference:
        item = gazetteer.resolve(reference, session_id)
        result["object"] = item
        if result.get("radius_km"):
            result["bbox"], result["geometry"] = circle(item["point"], result["radius_km"])
        elif "bbox" not in result and item.get("bbox"):
            result["bbox"] = item["bbox"]
            result["geometry"] = rectangle(item["bbox"])
        if not result["target"]:
            result["target"] = item["name"]
    elif result.get("radius_km"):
        # Keep the requested distance while the user is choosing a named object.
        result.pop("bbox", None)
        result.pop("geometry", None)
    return result


def clean(value, session_id=None):
    result = clean_search(value, session_id)
    searches = value.get("searches", [])
    if not isinstance(searches, list) or len(searches) > MAX_SEARCHES:
        raise ValueError("В одном диалоге можно вести до 24 поисков данных.")
    result["searches"] = [clean_search(row, session_id) for row in searches]
    ids = [row["id"] for row in result["searches"]]
    if any(not key for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("Поиски должны иметь разные идентификаторы.")
    groups = {}
    for row in result["searches"]:
        row["goal"] = result["goal"]
        identity = (row["target"], row["object_ref"])
        if row["object_id"] in groups and groups[row["object_id"]] != identity:
            raise ValueError("Название и положение одного объекта должны совпадать во всех его поисках.")
        groups[row["object_id"]] = identity
    if len(groups) > MAX_OBJECTS:
        raise ValueError("В одном чате можно вести до 8 объектов исследования.")
    return result


def compact(value):
    """No polygon vertices or geocoder prose in the model's token budget."""
    keys = ("id", "object_id", "goal", "target", "product", "start", "end", "bbox", "object_ref", "radius_km", "confirmed")
    result = {key: value[key] for key in keys if key in value}
    if value.get("object"):
        result["object_name"] = value["object"]["name"]
    result["searches"] = [{key: row[key] for key in keys if key in row and key != "goal"} for row in value.get("searches", [])]
    return result
