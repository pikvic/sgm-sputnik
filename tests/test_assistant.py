"""Run only against an isolated PostgreSQL database, as documented in README."""

import io
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults

import app
import assistant
import assistant_routes
import integrations
import settings as cfg
import storage
import gazetteer
import research_context


@unittest.skipUnless(os.getenv("SPUTNIK_TEST_DATABASE") == "1", "An isolated test database is required")
class AssistantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        storage.initialize()

    def setUp(self):
        with storage.database() as db:
            db.execute("TRUNCATE sessions,daily_usage,ai_requests,events,oauth_states,geocode_cache,object_candidates")
            db.execute("UPDATE geocode_gate SET blocked_until='epoch',last_call='epoch'")

    def session(self, uid=None):
        session, cookie = storage.get_session({}, create=True)
        with storage.database() as db:
            db.execute("UPDATE sessions SET consent=true,consent_scope=%s,uid=%s WHERE id=%s", (cfg.AI_CONSENT_SCOPE, uid, session["id"]))
        session["consent"] = True
        session["consent_scope"] = cfg.AI_CONSENT_SCOPE
        session["uid"] = uid
        return session, cookie

    def invoke(self, path, cookie, body=None, origin=None):
        environ = {}
        setup_testing_defaults(environ)
        encoded = json.dumps(body or {}).encode()
        environ.update(
            PATH_INFO=path, HTTP_COOKIE=cookie or "", HTTP_ORIGIN=origin or "",
            REQUEST_METHOD="POST" if body is not None else "GET",
            CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(encoded)),
            **{"wsgi.input": io.BytesIO(encoded)},
        )
        seen = {}
        def start(status, headers, exc_info=None):
            seen.update(status=int(status.split()[0]), headers=dict(headers))
        data = b"".join(app.application(environ, start))
        return seen["status"], json.loads(data)

    def test_quota_is_atomic_across_sessions(self):
        sessions = [self.session()[0] for _ in range(2)]
        def attempt(session):
            try:
                return storage.reserve(session["id"], "test", 40, session["revision"])
            except storage.LimitError:
                return None
        with patch.object(cfg, "AI_ANON_DAILY_TOKENS", 60):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(attempt, sessions))
        self.assertEqual(sum(bool(value) for value in outcomes), 1)
        with storage.database() as db:
            self.assertEqual(db.execute("SELECT tokens FROM daily_usage").fetchone()["tokens"], 40)

    def test_same_session_cannot_send_two_parallel_calls(self):
        session, _ = self.session()
        storage.reserve(session["id"], "test", 40, 0)
        with self.assertRaises(storage.LimitError):
            storage.reserve(session["id"], "test", 40, 0)

    def test_unknown_usage_retains_reservation_and_reset_retains_limit(self):
        session, cookie = self.session()
        request_id = storage.reserve(session["id"], "test", 80, 0)
        storage.finish(request_id, error="timeout")
        status, _ = self.invoke("/api/assistant/reset", cookie, {}, cfg.APP_URL)
        self.assertEqual(status, 200)
        state = self.invoke("/api/assistant/session", cookie)[1]
        self.assertEqual(state["remaining_tokens"], cfg.AI_SESSION_TOKENS - 80)
        self.assertEqual(storage.telemetry()["assistant"]["reserved_without_usage"], 1)

    def test_usage_is_reconciled_once(self):
        session, _ = self.session()
        request_id = storage.reserve(session["id"], "test", 100, 0)
        storage.finish(request_id, usage={"prompt_tokens": 30, "completion_tokens": 10})
        storage.finish(request_id, usage={"prompt_tokens": 1, "completion_tokens": 1})
        with storage.database() as db:
            current = db.execute("SELECT * FROM sessions WHERE id=%s", (session["id"],)).fetchone()
        self.assertEqual(current["tokens"], 40)

    def test_stale_dialog_revision_is_rejected(self):
        session, _ = self.session()
        with storage.database() as db:
            db.execute("UPDATE sessions SET revision=revision+1 WHERE id=%s", (session["id"],))
        with self.assertRaises(storage.LimitError):
            storage.reserve(session["id"], "test", 50, session["revision"])

    def test_admin_bypasses_budgets_but_anonymous_cannot_read_telemetry(self):
        session, cookie = self.session("12345")
        with patch.object(cfg, "ADMIN_YANDEX_IDS", {"12345"}), patch.object(cfg, "AI_SESSION_TOKENS", 1), patch.object(cfg, "AI_ANON_DAILY_TOKENS", 1):
            request_id = storage.reserve(session["id"], "test", 1000, 0)
            self.assertEqual(self.invoke("/api/admin/telemetry", cookie)[0], 200)
            storage.finish(request_id, usage={"prompt_tokens": 100, "completion_tokens": 10})
            with storage.database() as db:
                current = db.execute("SELECT tokens,requests FROM sessions WHERE id=%s", (session["id"],)).fetchone()
                anonymous = db.execute("SELECT tokens FROM daily_usage").fetchone()
            self.assertEqual(current["tokens"], 0, "Administrator usage must not consume the session quota")
            self.assertEqual(current["requests"], 0)
            self.assertEqual(anonymous["tokens"], 0)
            self.assertEqual(storage.telemetry()["assistant"]["charged_tokens"], 110)
        other, other_cookie = self.session()
        self.assertEqual(self.invoke("/api/admin/telemetry", other_cookie)[0], 403)

    def test_csrf_and_consent(self):
        session, cookie = self.session()
        self.assertEqual(self.invoke("/api/assistant/reset", cookie, {}, "https://elsewhere.example")[0], 403)
        self.assertEqual(self.invoke("/api/assistant/reset", cookie, {})[0], 403)
        with storage.database() as db:
            db.execute("UPDATE sessions SET consent=false WHERE id=%s", (session["id"],))
        with self.assertRaises(storage.LimitError):
            storage.reserve(session["id"], "test", 40, 0)

    def test_provider_change_requires_new_consent(self):
        session, _ = self.session()
        with patch.object(cfg, "AI_CONSENT_SCOPE", "different-provider"):
            self.assertFalse(storage.has_consent(session))
            with self.assertRaises(storage.LimitError):
                storage.reserve(session["id"], "test", 40, 0)

    def test_ip_limit_does_not_trust_spoofed_forwarding_headers(self):
        from ipaddress import ip_network
        environ = {"REMOTE_ADDR": "192.0.2.10", "HTTP_X_FORWARDED_FOR": "198.51.100.200"}
        with patch.object(cfg, "TRUSTED_PROXY_NETWORKS", []):
            self.assertEqual(assistant_routes.client_address(environ), "192.0.2.10")
        with patch.object(cfg, "TRUSTED_PROXY_NETWORKS", [ip_network("192.0.2.10/32")]):
            self.assertEqual(assistant_routes.client_address(environ), "198.51.100.200")
            environ["HTTP_X_FORWARDED_FOR"] = "203.0.113.1, 198.51.100.200"
            self.assertEqual(assistant_routes.client_address(environ), "198.51.100.200")

    def test_oauth_state_is_bound_to_session_and_single_use(self):
        session, _ = self.session()
        other, _ = self.session()
        with patch.object(cfg, "YANDEX_CLIENT_ID", "test-client"), patch.object(cfg, "YANDEX_CLIENT_SECRET", "test-secret"):
            url = integrations.authorize(session)
        state = parse_qs(urlsplit(url).query)["state"][0]
        self.assertEqual(parse_qs(urlsplit(url).query)["code_challenge_method"], ["S256"])
        with self.assertRaises(ValueError):
            integrations.callback(other, {"state": state, "code": "test"})
        with patch.object(integrations, "remote_json", side_effect=[{"access_token": "test"}, {"id": "12345"}]):
            integrations.callback(session, {"state": state, "code": "test"})
        with self.assertRaises(ValueError):
            integrations.callback(session, {"state": state, "code": "test"})
        with storage.database() as db:
            self.assertEqual(db.execute("SELECT uid FROM sessions WHERE id=%s", (session["id"],)).fetchone()["uid"], "12345")

    def test_model_cannot_invent_collections_or_scenes(self):
        with self.assertRaises(ValueError):
            assistant.validated_answer({"reply": "test", "plan": [{"product": "invented"}]}, {}, {})
        with self.assertRaises(ValueError):
            assistant.validated_answer({"reply": "test", "scene_recommendations": [{"id": "invented"}]}, {}, {})

    def test_yandex_verified_login_can_be_administrator(self):
        session, cookie = self.session()
        with patch.object(cfg, "YANDEX_CLIENT_ID", "test"), patch.object(cfg, "YANDEX_CLIENT_SECRET", "test"), patch.object(cfg, "ADMIN_YANDEX_IDS", {"geologist"}):
            url = integrations.authorize(session)
            state = parse_qs(urlsplit(url).query)["state"][0]
            with patch.object(integrations, "remote_json", side_effect=[{"access_token": "test"}, {"id": "12345", "login": "geologist"}]):
                integrations.callback(session, {"state": state, "code": "test"})
            self.assertTrue(self.invoke("/api/assistant/session", cookie)[1]["admin"])
            self.assertEqual(self.invoke("/api/admin/telemetry", cookie)[0], 200)
            self.invoke("/auth/logout", cookie, {}, cfg.APP_URL)
            self.assertEqual(self.invoke("/api/admin/telemetry", cookie)[0], 403)

    def test_local_gazetteer_finds_abandoned_quarry_without_external_provider(self):
        session, _ = self.session()
        with patch.object(cfg, "GEOCODER_USER_AGENT", ""), patch.object(integrations, "remote_json") as remote:
            result = integrations.geocode(session, {"q": "карьер Старая Линза"})
        self.assertEqual(result["items"][0]["name"], "Старая Линза")
        self.assertEqual(result["items"][0]["source"], "gazetteer")
        remote.assert_not_called()
        self.assertTrue(gazetteer.search("Staraya Linza Quarry"))

    def test_empty_osm_uses_wikidata_and_keeps_candidate_provenance(self):
        session, _ = self.session()
        entities = {"entities": {"Q123": {"labels": {"ru": {"value": "Тестовый рудник"}}, "claims": {"P625": [
            {"rank": "normal", "mainsnak": {"datavalue": {"value": {"longitude": 135, "latitude": 45, "globe": "http://www.wikidata.org/entity/Q2"}}}}
        ]}}}}
        with patch.object(cfg, "GEOCODER_USER_AGENT", "Sputnik test"), patch.object(integrations, "remote_json", side_effect=[[], {"search": [{"id": "Q123"}]}, entities]) as remote:
            result = integrations.geocode(session, {"q": "Неизвестный тестовый рудник"})
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(result["items"][0]["point"], [135, 45])
        resolved = gazetteer.resolve("wd:Q123", session["id"])
        self.assertEqual(resolved["source_url"], "https://www.wikidata.org/wiki/Q123")
        other, _ = self.session()
        with self.assertRaises(ValueError):
            gazetteer.resolve("wd:Q123", other["id"])

    def test_one_task_has_multiple_objects_and_independent_product_regions(self):
        sample = gazetteer.search("Старая Линза")[0]
        other = gazetteer.search("Тальков Камень")[0]
        rows = [
            {"id": "s1", "object_id": "o1", "target": sample["name"], "object_ref": sample["id"], "radius_km": 20, "product": "sentinel-2", "start": "2025-06-01", "end": "2025-08-31"},
            {"id": "s2", "object_id": "o1", "target": sample["name"], "object_ref": sample["id"], "radius_km": 50, "product": "landsat", "start": "2000-06-01", "end": "2000-08-31"},
            {"id": "s3", "object_id": "o2", "target": other["name"], "object_ref": other["id"], "radius_km": 10, "product": "copernicus-dem"},
        ]
        value = research_context.clean({**rows[0], "goal": "Сравнить изменения карьеров", "searches": rows})
        self.assertEqual(len({r["object_id"] for r in value["searches"]}), 2)
        self.assertEqual(len(value["searches"]), 3)
        self.assertNotEqual(value["searches"][0]["bbox"], value["searches"][1]["bbox"])
        self.assertEqual(value["searches"][1]["start"], "2000-06-01")
        self.assertEqual({r["goal"] for r in value["searches"]}, {value["goal"]})
        payload = research_context.compact(value)
        self.assertNotIn("geometry", payload)
        self.assertNotIn("object", payload)

    def test_radius_region_uses_geodesic_distance_and_actual_polygon(self):
        from shapely.geometry import box, mapping
        point = [135, 45]
        bbox, region = research_context.circle(point, 20)
        for vertex in region["coordinates"][0]:
            self.assertAlmostEqual(research_context.GEOD.inv(*point, *vertex)[2], 20000, delta=.2)
        corner = mapping(box(bbox[0], bbox[1], bbox[0] + .01, bbox[1] + .01))
        self.assertEqual(assistant.overlap(corner, bbox, region), 0)
        self.assertGreater(assistant.overlap(corner, bbox), 0)
        self.assertEqual(assistant.overlap(region, bbox, region), 100)
        from urllib.parse import urlencode
        self.assertLess(len(urlencode({"product": "sentinel-2", "bbox": ",".join(map(str, bbox)), "region": json.dumps(region)})), 3900)

    def test_model_cannot_invent_object_coordinates_or_refs(self):
        with self.assertRaises(ValueError):
            assistant.validated_answer({"reply": "test", "task": {"object_ref": "geo:invented"}}, {}, {})
        row = research_context.clean_search({"id": "s1", "object_ref": gazetteer.search("Старая Линза")[0]["id"], "radius_km": 20,
                                             "object": {"point": [0, 0]}, "geometry": {"type": "Point", "coordinates": [0, 0]}})
        self.assertNotEqual(row["object"]["point"], [0, 0])
        self.assertEqual(row["geometry"]["type"], "Polygon")

    def test_context_saved_without_calling_model_and_restored(self):
        session, cookie = self.session()
        body = {"id": "s1", "object_id": "o1", "goal": "Одна задача", "target": "участок", "bbox": [0, 0, 2, 1],
                "searches": [{"id": "s1", "object_id": "o1", "target": "участок", "bbox": [0, 0, 2, 1]}]}
        with patch.object(assistant, "call_model") as model:
            self.assertEqual(self.invoke("/api/assistant/context", cookie, body, cfg.APP_URL)[0], 200)
        model.assert_not_called()
        restored = self.invoke("/api/assistant/session", cookie)[1]["context"]
        self.assertEqual(restored["searches"][0]["object_id"], "o1")
        self.assertEqual(restored["goal"], "Одна задача")

    def test_product_proposals_group_under_same_object_and_preserve_other_objects(self):
        item = gazetteer.search("Старая Линза")[0]
        current = research_context.clean({"id": "s1", "object_id": "o1", "target": item["name"], "goal": "Изменения карьеров", "object_ref": item["id"], "searches": []})
        raw = {"reply": "Два продукта", "task": {"goal": current["goal"]}, "plan": [
            {"product": "sentinel-2", "target": item["name"], "object_ref": item["id"], "radius_km": 20, "start": "2025-06-01", "end": "2025-08-31", "reason": "Детализация", "region_reason": "Окрестности", "date_reason": "Летний сезон"},
            {"product": "landsat", "target": item["name"], "object_ref": item["id"], "radius_km": 50, "start": "2000-06-01", "end": "2000-08-31", "reason": "Ретроспектива", "region_reason": "Региональный контекст", "date_reason": "Ранний срез"},
        ]}
        answer = assistant.validated_answer(raw, current, {}, [item])
        value = assistant.update_workspace(current, answer, None)
        self.assertEqual(len(value["searches"]), 2)
        self.assertEqual(len({r["object_id"] for r in value["searches"]}), 1)
        self.assertEqual({r["radius_km"] for r in value["searches"]}, {20, 50})
        self.assertTrue(all(not r["confirmed"] for r in value["searches"]))

    def test_a_new_object_without_a_product_does_not_replace_existing_searches(self):
        row = {"id": "s1", "object_id": "o1", "target": "Первый карьер", "product": "sentinel-2", "start": "2025-06-01", "end": "2025-08-31", "bbox": [0, 0, 2, 1]}
        ctx = research_context.clean({**row, "goal": "Сравнение карьеров", "searches": [row]})
        answer = assistant.validated_answer({"reply": "Уточним положение второго объекта", "task": {"target": "Второй карьер"}}, ctx, {})
        result = assistant.update_workspace(ctx, answer, None)
        self.assertEqual(len(result["searches"]), 2)
        self.assertEqual(result["target"], "Второй карьер")
        old = next(row for row in result["searches"] if row["id"] == "s1")
        self.assertEqual(old["target"], "Первый карьер")
        self.assertEqual(old["bbox"], [0, 0, 2, 1])
        self.assertNotIn("bbox", result)

    def test_explicit_user_radius_survives_model_omission_and_point_is_sourced(self):
        session, _ = self.session()
        output = {"reply": "**Предварительный подбор**", "task": {"target": "Старая Линза", "goal": "Изменения карьера"},
                  "plan": [{"product": "sentinel-2", "target": "Старая Линза", "start": "2025-06-01", "end": "2025-08-31", "radius_km": 5,
                            "reason": "Наблюдение поверхности", "date_reason": "Летний сезон"}]}
        fake = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
        with patch.object(cfg, "AI_API_KEY", "test"), patch.object(assistant, "call_model", return_value=fake):
            result = assistant.chat(session, {"message": "Изучить карьер Старая Линза. Радиус поиска 20 км.", "context": {}}, "test")
        query = result["context"]["searches"][0]
        self.assertEqual(query["radius_km"], 20)
        self.assertEqual(query["object"]["name"], "Старая Линза")
        self.assertIn("пользователем", query["region_reason"])
        self.assertEqual(query["date_reason"], "Летний сезон")

    def test_output_limit_error_explains_setting_and_records_actual_usage(self):
        session, _ = self.session()
        fake = {"choices": [{"finish_reason": "length", "message": {"content": "{\"reply\":"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 1400}}
        with patch.object(cfg, "AI_API_KEY", "test"), patch.object(assistant, "call_model", return_value=fake) as model:
            with self.assertRaisesRegex(ValueError, "AI_MAX_OUTPUT_TOKENS"):
                assistant.chat(session, {"message": "Подбери данные по нескольким объектам", "context": {}}, "test")
        self.assertEqual(model.call_args.args[0]["max_tokens"], cfg.AI_MAX_OUTPUT_TOKENS)
        self.assertEqual(storage.telemetry()["assistant"]["charged_tokens"], 2400)

    def test_search_filters_bbox_corner_against_actual_radius_polygon(self):
        from shapely.geometry import box, mapping
        bbox, region = research_context.circle([135, 45], 20)
        source = {"features": [
            {"id": "inside", "bbox": bbox, "geometry": region, "properties": {}, "assets": {}},
            {"id": "corner", "bbox": bbox, "geometry": mapping(box(bbox[0], bbox[1], bbox[0]+.01, bbox[1]+.01)), "properties": {}, "assets": {}},
        ]}
        with patch.object(app, "read_json", return_value=source):
            result = app.search({"product": "sentinel-2", "bbox": ",".join(map(str, bbox)), "start": "2025-06-01", "end": "2025-08-31", "region": json.dumps(region)})
        self.assertEqual([r["id"] for r in result["items"]], ["inside"])
        self.assertEqual(result["items"][0]["overlap_percent"], 100)

    def test_unsupported_goal_does_not_offer_a_search(self):
        answer = assistant.validated_answer({
            "reply": "RTC не содержит фазу, нужного SLC нет.",
            "task_supported": False,
            "plan": [{"product": "sentinel-1", "reason": "Ближайший радарный продукт"}],
        }, {"product": "sentinel-1"}, {})
        self.assertEqual(answer["plan"], [])
        self.assertFalse(answer["task_supported"])
        self.assertTrue(answer["sources"])

    def test_scene_reference_resolves_to_original_stac_identifier(self):
        identifier = "S2B_MSIL2A_20250821T020659_R103_T53TLJ_20250821T035126"
        answer = assistant.validated_answer({
            "reply": "Предварительный подбор", "task_supported": True,
            "scene_recommendations": [{"ref": "scene_1", "reason": "Пересечение", "limitations": "Облака"}],
        }, {}, {"items": [{"id": identifier, "overlap_percent": 36.24, "cloud": 15.6, "date": "2025-08-21"}]})
        self.assertEqual(answer["scene_recommendations"][0]["id"], identifier)

    def test_exhausted_budget_never_calls_provider(self):
        session, _ = self.session()
        with patch.object(cfg, "AI_API_KEY", "test"), patch.object(cfg, "AI_SESSION_TOKENS", 1), patch.object(assistant, "call_model") as model:
            with self.assertRaises(storage.LimitError):
                assistant.chat(session, {"message": "Нужны снимки", "context": {}}, "test")
        model.assert_not_called()

    def test_polygon_coverage_and_dateline(self):
        from shapely.geometry import box, mapping
        self.assertAlmostEqual(assistant.overlap(mapping(box(0, 0, 1, 1)), [0, 0, 2, 1]), 50, delta=.05)
        self.assertEqual(assistant.overlap(mapping(box(4, 4, 5, 5)), [0, 0, 2, 1]), 0)
        dateline = {"type": "Polygon", "coordinates": [[[179, 0], [-179, 0], [-179, 1], [179, 1], [179, 0]]]}
        self.assertEqual(assistant.overlap(dateline, [179, 0, -179, 1]), 100)

    def test_geocoding_uses_cache(self):
        session, _ = self.session()
        sample = [{"display_name": "Шивелуч", "boundingbox": ["56.6", "56.7", "161.3", "161.5"], "lat": "56.65", "lon": "161.4"}]
        with patch.object(cfg, "GEOCODER_USER_AGENT", "Sputnik test"), patch.object(integrations, "remote_json", return_value=sample) as remote:
            first = integrations.geocode(session, {"q": "Шивелуч"})
            second = integrations.geocode(session, {"q": "Шивелуч"})
        self.assertEqual(first, second)
        self.assertEqual(remote.call_count, 1)

    def test_chat_uses_verified_scene_metadata_and_records_usage(self):
        session, _ = self.session()
        task = {"goal": "Наблюдать изменения", "target": "участок", "bbox": [0, 0, 2, 1],
                "product": "sentinel-2", "start": "2025-08-01", "end": "2025-08-31"}
        scene = {"id": "real-scene", "date": "2025-08-21T00:00:00Z", "cloud": 15,
                 "platform": "Sentinel-2B", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                 "bbox": [0, 0, 1, 1], "assets": ["B04", "B08"]}
        output = {"reply": "Предварительный подбор по метаданным.", "task": {"goal": task["goal"]}, "plan": [],
                  "scene_recommendations": [{"id": "real-scene", "reason": "Частичное пересечение", "limitations": "Качество участка не проверено"}]}
        fake = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
        with patch.object(cfg, "AI_API_KEY", "test"), patch.object(app, "search", return_value={"items": [scene], "next": None}), patch.object(assistant, "call_model", return_value=fake) as model:
            result = assistant.chat(session, {"message": "Подбери сцены", "mode": "scenes", "context": task}, "test")
        self.assertEqual(result["scene_recommendations"][0]["id"], "real-scene")
        self.assertAlmostEqual(result["scene_recommendations"][0]["overlap_percent"], 50, delta=.05)
        prompt = json.loads(model.call_args.args[0]["messages"][-1]["content"])
        self.assertEqual(prompt["scenes"][0]["assets"], ["B04", "B08"])
        self.assertEqual(storage.telemetry()["assistant"]["charged_tokens"], 1100)


if __name__ == "__main__":
    unittest.main()
