"""混合 Agent、任务级记忆、工具契约与 DEM 质量门控的离线测试。"""
import copy
import gc
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect_slo
from app import agent, case, config, main, store, tools
from app.tools import dem, tiles


AOI = [0.0, 0.0, 1.0, 1.0]
USAGE = {"side": "edge", "engine_name": "offline-test", "prompt_tokens": 100,
         "completion_tokens": 20}


def plan(intent, answer=""):
    return json.dumps({"intent": intent, "answer": answer}, ensure_ascii=False)


def ok(data, quality=None, artifacts=None):
    return {"status": (quality or {}).get("status", "ok"), "data": data,
            "quality": quality or {}, "artifacts": artifacts or [], "error": None,
            "idempotency_key": "test-key"}


def error(message):
    return {"status": "error", "data": {}, "quality": {}, "artifacts": [],
            "error": message, "idempotency_key": "test-key"}


class AgentTestMixin:
    def run_mocked(self, replies, tool_results=None, initial_state=None,
                   user_text="综合风险研判", **budgets):
        budgets.setdefault("agent_max_steps", agent.settings.agent_max_steps)
        saved = {"state": copy.deepcopy(initial_state)}

        def save_state(_task_id, state):
            saved["state"] = copy.deepcopy(state)

        task = {"id": "task-1", "aoi_geojson": json.dumps(AOI)}
        with patch.object(agent.store, "get_task", return_value=task), \
             patch.object(agent.store, "get_case_state",
                          side_effect=lambda _tid: copy.deepcopy(saved["state"])), \
             patch.object(agent.store, "save_case_state", side_effect=save_state), \
             patch.object(agent.store, "get_messages",
                          side_effect=AssertionError("审计消息不得用于模型推理")), \
             patch.object(agent.store, "add_message"), \
             patch.object(agent.store, "update_task_status"), \
             patch.object(agent, "chat",
                          side_effect=[(content, USAGE) for content in replies]) as chat_mock, \
             patch.object(agent, "tool_call", side_effect=tool_results or []) as tool_mock, \
             patch.multiple(agent.settings, **budgets):
            events = list(agent.run_agent("task-1", user_text))
        return events, saved["state"], chat_mock, tool_mock


class AgentContextTest(AgentTestMixin, unittest.TestCase):
    def test_observation_is_compact_json_and_keeps_evidence(self):
        result = {
            "zoom": 15,
            "tiles": list(range(100)),
            "usage": {"engine_name": "观澜·白格微调版", "prompt_tokens": 9000},
            "parse_ok": True,
            "summary": "发现滑坡迹象",
            "count": 12,
            "findings": [
                {"type": "landslide", "description": "裸露土石" * 30,
                 "lon": 103.2, "lat": 31.5, "confidence": 0.8}
                for _ in range(12)
            ],
            "layer_ids": ["layer-1"],
        }
        text = agent._compact_observation(result)
        payload = json.loads(text)
        self.assertLessEqual(len(text), agent.settings.agent_observation_limit)
        self.assertEqual(payload["summary"], "发现滑坡迹象")
        self.assertEqual(payload["layer_ids"], ["layer-1"])
        self.assertNotIn("usage", payload)
        self.assertNotIn("tiles", payload)
        self.assertLessEqual(len(payload["findings"]), 8)

    def test_old_observations_become_rolling_summaries(self):
        old_obs = "〔OBS〕工具 vlm_inspect 返回：" + "证据" * 500 + "〔/OBS〕"
        latest_obs = "〔OBS〕工具 dem_stats 返回：" + "最新证据" * 300 + "〔/OBS〕"
        messages = [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "任务"},
            {"role": "assistant", "content": "〔TOOL〕vlm_inspect {}〔/TOOL〕"},
            {"role": "user", "content": old_obs},
            {"role": "assistant", "content": "〔TOOL〕dem_stats {}〔/TOOL〕"},
            {"role": "user", "content": latest_obs},
        ]
        agent._compact_old_observations(messages, protected=2)
        self.assertIn("历史证据摘要", messages[3]["content"])
        self.assertLess(len(messages[3]["content"]), 500)
        self.assertEqual(messages[5]["content"], latest_obs)

    @patch("app.agent.count_message_tokens", return_value=None)
    def test_prompt_budget_preserves_task_and_complete_pairs(self, _tokenize):
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "原始任务"},
        ]
        for i in range(10):
            messages.append({"role": "assistant", "content": f"推理{i}"})
            messages.append({"role": "user", "content": "〔OBS〕" + "观察" * 150 + "〔/OBS〕"})
        with patch.object(agent.settings, "agent_prompt_budget", 600):
            fitted = agent._fit_prompt_budget(messages, protected=2)
        self.assertEqual(fitted[0]["content"], "系统提示")
        self.assertEqual(fitted[1]["content"], "原始任务")
        self.assertLess(len(fitted), len(messages))
        self.assertLessEqual(agent._token_count(fitted), 1024)
        self.assertEqual(fitted[2]["role"], "assistant")
        self.assertEqual(fitted[3]["role"], "user")
        self.assertTrue(any(m["role"] == "user" and
                            m["content"].startswith("〔滚动摘要〕") for m in fitted[2:]))

    def test_chat_needs_no_tool_or_summary_call(self):
        events, _, chat_mock, tool_mock = self.run_mocked([plan("chat", "可以直接回答")])
        self.assertEqual(events[-1]["content"], "可以直接回答")
        self.assertEqual(events[-1]["budget"],
                         {"steps": 1, "tool_calls": 0, "llm_calls": 1})
        self.assertEqual(chat_mock.call_count, 1)
        tool_mock.assert_not_called()

    def test_area_executes_only_area_then_reuses_persistent_evidence(self):
        first, state, first_chat, first_tool = self.run_mocked(
            [plan("area"), "〔FINAL〕面积为一平方公里。〔/FINAL〕"],
            [ok({"area_m2": 1000000, "area_km2": 1})])
        self.assertEqual(first_tool.call_count, 1)
        self.assertEqual(first_tool.call_args.args[0], "geo_area")
        self.assertEqual(first[-1]["budget"],
                         {"steps": 3, "tool_calls": 1, "llm_calls": 2})
        summary_messages = first_chat.call_args_list[1].args[0]
        self.assertIn("CaseState", summary_messages[1]["content"])
        self.assertNotIn("〔OBS〕工具", summary_messages[1]["content"])

        second, _, second_chat, second_tool = self.run_mocked(
            [plan("area"), "〔FINAL〕仍为一平方公里。〔/FINAL〕"], initial_state=state)
        second_tool.assert_not_called()
        self.assertTrue(any(ev.get("phase") == "reuse_evidence" for ev in second))
        self.assertEqual(second[-1]["budget"]["tool_calls"], 0)
        self.assertEqual(second_chat.call_count, 2)

    def test_risk_runs_vlm_and_dem_once_per_evidence_gap(self):
        imagery = ok({"summary": "发现一处疑似滑坡", "count": 1, "findings": []})
        terrain = ok({"elev_min": 100, "elev_max": 200, "slope_mean": 20})
        first, state, _, first_tool = self.run_mocked(
            [plan("risk"), "〔FINAL〕综合研判。〔/FINAL〕"], [imagery, terrain])
        self.assertEqual([call.args[0] for call in first_tool.call_args_list],
                         ["vlm_inspect", "dem_stats"])
        second, _, _, second_tool = self.run_mocked(
            [plan("risk"), "〔FINAL〕复用后研判。〔/FINAL〕"], initial_state=state)
        second_tool.assert_not_called()
        self.assertEqual(second[-1]["budget"]["tool_calls"], 0)

    def test_dem_gap_offers_replan_and_retries_with_imagery_reuse(self):
        imagery = ok({"summary": "发现一处疑似滑坡", "count": 1, "findings": []})
        insufficient = ok({"coverage": 0.0, "dem_tiles_missing": 4},
                          {"status": "insufficient", "coverage": 0.0})
        first, state, first_chat, _ = self.run_mocked(
            [plan("risk")], [imagery, insufficient])
        self.assertIn("pending_evidence_gap", state)
        self.assertEqual(state["pending_evidence_gap"]["coverage"], 0.0)
        self.assertIn("仅定性观察", first[-1]["content"])
        self.assertIn("重试地形研判", first[-1]["content"])
        self.assertTrue(any(ev["kind"] == "evidence_gap" for ev in first))
        self.assertEqual(first_chat.call_count, 1)  # 缺口时不再让总结模型补写定量结论

        second, second_state, _, second_tool = self.run_mocked(
            [plan("risk"), "〔FINAL〕补充地形后完成研判。〔/FINAL〕"],
            [ok({"elev_min": 100, "elev_max": 200, "slope_mean": 20})],
            initial_state=state, user_text="补充 DEM 后重试地形研判")
        self.assertEqual([call.args[0] for call in second_tool.call_args_list], ["dem_stats"])
        self.assertTrue(any(ev.get("phase") == "reuse_evidence" and
                            ev.get("evidence") == "imagery" for ev in second))
        self.assertNotIn("pending_evidence_gap", second_state)

    def test_qualitative_choice_skips_dem_and_reuses_imagery(self):
        imagery = ok({"summary": "发现一处疑似滑坡", "count": 1, "findings": []})
        insufficient = ok({"coverage": 0.0}, {"status": "insufficient", "coverage": 0.0})
        _, state, _, _ = self.run_mocked([plan("risk")], [imagery, insufficient])
        events, _, _, tool_mock = self.run_mocked(
            [plan("risk"), "〔FINAL〕仅见一处疑似裸露坡面。〔/FINAL〕"],
            initial_state=state, user_text="仅定性观察")
        tool_mock.assert_not_called()
        self.assertTrue(any(ev.get("phase") == "reuse_evidence" for ev in events))
        self.assertIn("证据等级较低", events[-1]["content"])
        self.assertIsNone(events[-1]["evidence_gap"])

    def test_low_confidence_triggers_one_review_without_overwriting_original(self):
        original = ok({"summary": "原引擎低置信判断", "count": 1, "parse_ok": True,
                       "findings": [{"type": "landslide", "confidence": 0.4}]})
        terrain = ok({"elev_min": 100, "elev_max": 200, "slope_mean": 20})
        review = ok({"summary": "复核发现另一处迹象", "count": 2, "parse_ok": True,
                     "findings": [{"type": "landslide", "confidence": 0.8}]})
        events, state, _, tool_mock = self.run_mocked(
            [plan("risk"), "〔FINAL〕原判读与复核存在分歧，需现场核实。〔/FINAL〕"],
            [original, terrain, review])
        self.assertEqual(tool_mock.call_count, 3)
        self.assertEqual(tool_mock.call_args_list[2].args[0], "vlm_inspect")
        self.assertEqual(tool_mock.call_args_list[2].args[1]["engine"], "qwen3vl_ft")
        self.assertEqual(state["evidence"]["imagery"]["data"]["summary"], "原引擎低置信判断")
        self.assertEqual(state["evidence"]["review"]["data"]["summary"], "复核发现另一处迹象")
        self.assertTrue(any(ev.get("phase") == "review" for ev in events))

    def test_buffer_repeat_recomputes_geometry_with_same_idempotency_key(self):
        geometry = ok({"geojson": {"type": "Polygon", "coordinates": []}, "area_km2": 1})
        saved_layer = ok({"id": "layer-1", "layer_id": "layer-1"}, artifacts=["layer-1"])
        request = "对 AOI 做 500 米缓冲区并落图"
        first, state, _, first_tool = self.run_mocked(
            [plan("buffer"), "〔FINAL〕已落图〔/FINAL〕"], [geometry, saved_layer],
            user_text=request)
        self.assertEqual([call.args[0] for call in first_tool.call_args_list],
                         ["geo_buffer", "save_layer"])
        self.assertNotIn("geojson", state["evidence"]["geometry"]["data"])
        second, _, _, second_tool = self.run_mocked(
            [plan("buffer"), "〔FINAL〕已复用落图〔/FINAL〕"], [geometry, saved_layer],
            initial_state=state, user_text=request)
        self.assertEqual([call.args[0] for call in second_tool.call_args_list],
                         ["geo_buffer", "save_layer"])
        first_key = first_tool.call_args_list[1].args[1]["idempotency_key"]
        second_key = second_tool.call_args_list[1].args[1]["idempotency_key"]
        self.assertEqual(first_key, second_key)

    def test_custom_cached_call_does_not_consume_real_tool_budget(self):
        tool_block = "〔TOOL〕geo_area {\"bbox\":[0,0,1,1]}〔/TOOL〕"
        events, _, _, tool_mock = self.run_mocked(
            [plan("custom"), tool_block, tool_block, "〔FINAL〕完成〔/FINAL〕"],
            [ok({"area_m2": 1000000, "area_km2": 1})],
            agent_max_tools=1, agent_max_llm_calls=4, agent_max_steps=8)
        self.assertEqual(tool_mock.call_count, 1)
        results = [ev for ev in events if ev["kind"] == "tool_result"]
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]["cached"])
        self.assertTrue(results[1]["cached"])
        self.assertEqual(events[-1]["budget"]["tool_calls"], 1)

    def test_custom_list_layers_is_always_live(self):
        tool_block = "〔TOOL〕list_layers {}〔/TOOL〕"
        events, _, _, tool_mock = self.run_mocked(
            [plan("custom"), tool_block, tool_block, "〔FINAL〕完成〔/FINAL〕"],
            [ok([{"id": "old"}]), ok([{"id": "old"}, {"id": "new"}])],
            agent_max_llm_calls=4, agent_max_steps=8)
        self.assertEqual(tool_mock.call_count, 2)
        self.assertTrue(all(not ev["cached"] for ev in events
                            if ev["kind"] == "tool_result"))

    def test_custom_tool_arguments_must_be_json_object(self):
        events, _, _, tool_mock = self.run_mocked(
            [plan("custom"), "〔TOOL〕geo_area []〔/TOOL〕", "〔FINAL〕参数无效〔/FINAL〕"])
        tool_mock.assert_not_called()
        self.assertTrue(any(ev["kind"] == "tool_error" and "JSON 对象" in ev["message"]
                            for ev in events))
        self.assertEqual(events[-1]["content"], "参数无效")

    def test_step_budget_stops_before_extra_summary_llm(self):
        events, _, chat_mock, tool_mock = self.run_mocked(
            [plan("area")], [ok({"area_m2": 1000000, "area_km2": 1})],
            agent_max_steps=2)
        self.assertEqual(tool_mock.call_count, 1)
        self.assertEqual(chat_mock.call_count, 1)
        self.assertEqual(events[-1]["budget"],
                         {"steps": 2, "tool_calls": 1, "llm_calls": 1})
        self.assertIn("面积", events[-1]["content"])

    def test_returned_tool_error_emits_tool_error_event(self):
        events, _, _, _ = self.run_mocked(
            [plan("area"), "〔FINAL〕数据不足〔/FINAL〕"], [error("读取失败")])
        self.assertTrue(any(ev["kind"] == "tool_error" for ev in events))
        self.assertFalse(any(ev["kind"] == "tool_result" for ev in events))


class CaseStateTest(unittest.TestCase):
    def test_reducer_omits_large_geometry_and_failed_retry_preserves_evidence(self):
        state = case.new_case("测算", AOI)
        args = {"bbox": AOI, "task_id": "server-task", "idempotency_key": "runtime"}
        case.reduce_result(state, "geo_area", args,
                           ok({"area_km2": 1, "geojson": {"type": "Polygon"}}))
        evidence = state["evidence"]["area"]
        self.assertNotIn("geojson", evidence["data"])
        case.reduce_result(state, "geo_area", args, error("瞬时失败"))
        self.assertEqual(state["evidence"]["area"]["status"], "ok")
        self.assertEqual(len(state["executions"]), 2)
        self.assertEqual(state["executions"][-1]["status"], "error")
        self.assertLessEqual(len(case.compact_case(state, 600)), 600)

    def test_sqlite_case_state_and_layer_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(store, "DB_PATH", Path(tmp) / "test.db"):
            store.init_db()
            task = store.create_task("离线测试")
            state = case.new_case("持久化", AOI)
            store.save_case_state(task["id"], state)
            self.assertEqual(store.get_case_state(task["id"])["aoi_bbox"], AOI)

            geom = {"type": "Point", "coordinates": [0, 0]}
            first = store.save_layer("测试", "marker", geom, task_id=task["id"],
                                     idempotency_key="same-write")
            second = store.save_layer("测试", "marker", geom, task_id=task["id"],
                                      idempotency_key="same-write")
            self.assertEqual(first["id"], second["id"])
            self.assertTrue(second["idempotent_reused"])
            self.assertEqual(len(store.list_layers(task["id"])), 1)
            store.save_layer("实时新增", "marker", geom, task_id=task["id"],
                             idempotency_key="other-write")
            self.assertEqual(len(store.list_layers(task["id"])), 2)
            gc.collect()  # Windows 下释放 sqlite 连接后再清理临时目录

    def test_save_layer_exposes_artifact_id_in_unified_envelope(self):
        with patch("app.tools.geo.store.save_layer",
                   return_value={"id": "layer-1", "name": "测试", "kind": "marker"}):
            envelope = tools.call("save_layer", {
                "name": "测试", "kind": "marker",
                "geojson": {"type": "Point", "coordinates": [0, 0]}})
        self.assertEqual(envelope["artifacts"], ["layer-1"])
        self.assertEqual(envelope["data"]["layer_id"], "layer-1")

    def test_compare_brief_accepts_unified_tool_envelope(self):
        brief = main._compare_brief("qwen3vl", ok(
            {"parse_ok": True, "count": 2, "summary": "两处", "findings": [],
             "usage": {"elapsed_s": 3, "completion_tokens": 20}},
            artifacts=["layer-1"]))
        self.assertEqual(brief["count"], 2)
        self.assertEqual(brief["layer_ids"], ["layer-1"])
        self.assertEqual(brief["completion_tokens"], 20)


class SLOCollectionTest(unittest.TestCase):
    def test_run_task_parses_real_sse_shape(self):
        class FakeResponse:
            def __init__(self):
                self.lines = iter([
                    b'data: {"kind":"start","task_id":"task-1"}\n',
                    b'data: {"kind":"final","content":"done","budget":{"llm_calls":1}}\n',
                    b'data: {"kind":"done"}\n', b''])

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def readline(self):
                return next(self.lines)

        with patch.object(collect_slo, "api_json", return_value={"id": "task-1"}), \
                patch.object(collect_slo, "api_request", return_value=FakeResponse()):
            result = collect_slo.run_task("http://test", "area", "面积", [0, 0, 1, 1], 1, 30)
        self.assertTrue(result["completed"])
        self.assertTrue(result["clean"])
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["final"]["content"], "done")

    def test_summary_separates_completion_clean_runs_and_gaps(self):
        runs = [
            {"elapsed_s": 10, "completed": True, "clean": True, "evidence_gap": None,
             "llm_rounds": [{"elapsed_s": 4, "prompt_tokens": 100, "completion_tokens": 40}]},
            {"elapsed_s": 20, "completed": True, "clean": False,
             "evidence_gap": {"kind": "terrain"}, "llm_rounds": []},
            {"elapsed_s": 30, "completed": False, "clean": False, "evidence_gap": None,
             "llm_rounds": []},
        ]
        result = collect_slo.summarize_runs(runs)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["clean_completed"], 1)
        self.assertAlmostEqual(result["completion_rate"], 2 / 3)
        self.assertEqual(result["e2e_latency_s"]["p50"], 20)
        self.assertEqual(result["e2e_latency_s"]["p95"], 30)
        self.assertEqual(result["llm"]["completion_tokens_per_s"], 10)
        self.assertIn("小样本", result["interpretation"])

    def test_resource_summary_uses_only_numeric_samples(self):
        samples = [
            {"supported": True, "system_cpu_pct": None,
             "memory": {"used_mb": 1000}, "processes": [
                 {"key": "1:llama-server", "cmdline": "llama-server", "rss_mb": 100,
                  "cpu_pct": None}]},
            {"supported": True, "system_cpu_pct": 25,
             "memory": {"used_mb": 1200}, "processes": [
                 {"key": "1:llama-server", "cmdline": "llama-server", "rss_mb": 200,
                  "cpu_pct": 50}]},
        ]
        result = collect_slo.summarize_resources(samples)
        self.assertEqual(result["system_cpu_pct"], {"mean": 25, "max": 25})
        self.assertEqual(result["system_memory_used_mb"], {"mean": 1100, "max": 1200})
        self.assertEqual(result["processes"]["1:llama-server"]["rss_mb"]["max"], 200)
        self.assertFalse(collect_slo.summarize_resources([{"supported": False}])["supported"])


class RegionProfileTest(unittest.TestCase):
    def test_profiles_are_complete_and_select_distinct_regions(self):
        baige = config.load_region("regions/baige.json")
        luding = config.load_region("regions/luding.json")
        self.assertEqual(baige["id"], "baige")
        self.assertEqual(luding["id"], "luding")
        self.assertNotEqual(baige["center"], luding["center"])
        self.assertNotEqual(baige["imagery_mbtiles"], luding["imagery_mbtiles"])
        self.assertNotEqual(baige["dem_mbtiles"], luding["dem_mbtiles"])

    def test_active_tools_use_the_same_profile(self):
        self.assertEqual(tiles.IMAGERY_MBTILES,
                         config.DATA_DIR / config.REGION["imagery_mbtiles"])
        self.assertEqual(dem.DEM_MBTILES, config.DATA_DIR / config.REGION["dem_mbtiles"])
        self.assertEqual(dem.DEM_Z, config.REGION["dem_zoom"])
        self.assertEqual(main.region(), config.REGION)

    def test_imagery_reader_excludes_dem_and_other_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            imagery = root / "luding.mbtiles"
            imagery.touch()
            (root / "luding_dem.mbtiles").touch()
            (root / "baige.mbtiles").touch()
            with patch.object(tiles, "IMAGERY_MBTILES", imagery):
                self.assertEqual(tiles._mbtiles_files(), [imagery])


class DemQualityTest(unittest.TestCase):
    def test_low_coverage_is_insufficient_and_does_not_write_layer(self):
        heights = [[100, None, 100], [None, None, None], [100, None, 100]]
        mask = [[value is not None for value in row] for row in heights]
        grid = (heights, [0, 0, 0], [10, 10, 10], 10, 2, 4, mask)
        with patch.object(dem, "DEM_MBTILES") as dem_path, \
             patch.object(dem, "_dem_grid", return_value=grid), \
             patch.object(dem.store, "save_layer") as save_layer:
            dem_path.exists.return_value = True
            result = dem.dem_stats(AOI, task_id="task-1")
        self.assertEqual(result["quality"]["status"], "insufficient")
        self.assertLess(result["coverage"], 0.8)
        self.assertNotIn("elev_min", result)
        self.assertNotIn("slope_mean", result)
        save_layer.assert_not_called()

    def test_degraded_statistics_exclude_missing_samples(self):
        heights = [[None, None, None, 100], [100] * 4, [100] * 4, [100] * 4]
        mask = [[value is not None for value in row] for row in heights]
        grid = (heights, [0] * 4, [10] * 4, 10, 1, 4, mask)
        with patch.object(dem, "DEM_MBTILES") as dem_path, \
             patch.object(dem, "_dem_grid", return_value=grid):
            dem_path.exists.return_value = True
            result = dem.dem_stats(AOI)
        self.assertEqual(result["quality"]["status"], "degraded")
        self.assertAlmostEqual(result["coverage"], 13 / 16)
        self.assertEqual(result["valid_samples"], 13)
        self.assertEqual(result["elev_min"], 100)
        self.assertEqual(result["elev_max"], 100)
        self.assertEqual(result["slope_mean"], 0)


if __name__ == "__main__":
    unittest.main()
