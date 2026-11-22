"""混合式 Agent：任务级 CaseState + 证据缺口状态机 + custom 文本协议补救。

messages 只用于对话与工具审计；模型推理只读取紧凑 CaseState，不回放历史 OBS。
"""
import json
import re
import time
from typing import Any, Generator

from . import store, case as case_model
from .config import settings, edge_engine_conf
from .llm import chat, count_message_tokens, LLMError
from .prompts import build_planner_prompt, build_summary_prompt, build_system_prompt
from .tools import call as tool_call, catalog_text, cacheable as tool_cacheable

_TOOL_RE = re.compile(r"〔TOOL〕\s*(\S+?)\s+(.*?)\s*〔/TOOL〕", re.S)
_FINAL_RE = re.compile(r"〔FINAL〕\s*(.*?)\s*〔/FINAL〕", re.S)
_INTENTS = {"chat", "area", "terrain", "imagery", "risk", "buffer", "list", "custom"}


def _event(kind: str, **data) -> dict:
    return {"kind": kind, "ts": time.time(), **data}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…(摘要截断)"


def _slim_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _slim_value(v) for k, v in value.items()
                if k not in ("usage", "tiles", "geojson", "raw")}
    if isinstance(value, list):
        return [_slim_value(v) for v in value[:20]]
    if isinstance(value, str):
        return _clip(value, 400)
    return value


def _compact_observation(result: Any) -> str:
    """生成紧凑合法 JSON，保留质量、结论、坐标、统计和图层引用。"""
    if isinstance(result, dict) and "data" in result and "status" in result:
        raw = result.get("data")
        data = dict(raw) if isinstance(raw, dict) else {"items": raw}
        for key in ("status", "quality", "artifacts", "error"):
            if result.get(key) not in (None, {}, []):
                data[key] = result[key]
        result = data
    if isinstance(result, dict):
        result = dict(result)
        usage = result.pop("usage", None)
        if isinstance(usage, dict) and usage.get("engine_name"):
            result["engine"] = usage["engine_name"]
        if isinstance(result.get("findings"), list):
            result["findings"] = result["findings"][:8]
    data = _slim_value(result)
    if isinstance(data, dict):
        priority = ("status", "quality", "error", "parse_ok", "summary", "count",
                    "findings", "coverage", "artifacts", "layer_ids", "layer_id")
        data = {**{k: data[k] for k in priority if k in data},
                **{k: v for k, v in data.items() if k not in priority}}
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    limit = settings.agent_observation_limit
    while len(text) > limit and isinstance(data, dict) and data.get("findings"):
        data["findings"].pop()
        data["truncated"] = True
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text) > limit and isinstance(data, dict):
        essential = (
            "status", "quality", "error", "parse_ok", "summary", "count", "findings",
            "coverage", "artifacts", "layer_ids", "layer_id", "area_m2", "area_km2",
            "distance_m", "distance_km", "elev_min", "elev_max", "elev_mean", "relief",
            "slope_mean", "slope_max", "steep_pct_over25", "dem_tiles_missing",
            "dem_tiles_total", "valid_samples", "found", "display_name", "lon", "lat",
            "bbox", "id", "name", "kind", "items",
        )
        data = {k: data[k] for k in essential if k in data}
        data["truncated"] = True
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(text) > limit:
        text = json.dumps({"truncated": True, "preview": _clip(text, limit - 60)},
                          ensure_ascii=False, separators=(",", ":"))
    return text


def _token_count(messages: list[dict]) -> int:
    exact = count_message_tokens(messages)
    if exact is not None:
        return exact
    return sum(len(str(m.get("content", ""))) + 8 for m in messages) + 64


def _fit_prompt_budget(messages: list[dict], protected: int) -> list[dict]:
    """custom 路径保留任务前缀，按完整问答对滚动裁减并预留输出。"""
    work = [dict(m) for m in messages]
    output_reserve = edge_engine_conf()["max_tokens"] + 512
    budget = max(1024, min(settings.agent_prompt_budget, 8192 - output_reserve))
    total = _token_count(work)
    dropped = 0
    while total > budget and len(work) > protected + 2:
        # 始终删除最早 assistant+OBS 一对，避免留下连续 user 或孤立工具观察。
        del work[protected:protected + 2]
        dropped += 2
        total = _token_count(work)
    if dropped:
        note = f"〔滚动摘要〕已省略最早 {dropped} 条低价值消息，合格证据仍在 CaseState 中。\n"
        for message in reversed(work):
            if message["role"] == "user":
                message["content"] = note + message["content"]
                break
    return work


def _compact_old_observations(messages: list[dict], protected: int) -> None:
    """custom 兼容路径只保留最新 OBS 全文，标准路径不调用此函数。"""
    obs_indices = [i for i in range(protected, len(messages))
                   if messages[i]["role"] == "user"
                   and messages[i]["content"].startswith("〔OBS〕")]
    for i in obs_indices[:-1]:
        content = messages[i]["content"]
        if len(content) > 460 and "历史证据摘要" not in content:
            messages[i]["content"] = "〔OBS〕历史证据摘要：" + _clip(content[5:-6], 420) + "〔/OBS〕"


def _parse_json_object(text: str) -> dict | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _fallback_plan(user_text: str) -> dict:
    """规划 JSON 失败时的有限规则映射，不让小模型阻塞标准任务。"""
    text = user_text.lower()
    if "缓冲" in text:
        intent = "buffer"
    elif "图层" in text and any(k in text for k in ("列表", "哪些", "查询", "已有")):
        intent = "list"
    elif any(k in text for k in ("面积", "幅员")):
        intent = "area"
    elif any(k in text for k in ("地形", "高程", "坡度", "dem")):
        intent = "terrain"
    elif any(k in text for k in ("综合", "全面", "风险", "研判", "灾害")):
        intent = "risk"
    elif any(k in text for k in ("影像", "判读", "滑坡", "识别")):
        intent = "imagery"
    else:
        intent = "chat"
    focus = "landslide" if "滑坡" in text else "general"
    radius = None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(公里|千米|km|米|m)", text)
    if match:
        radius = float(match.group(1))
        if match.group(2) in ("公里", "千米", "km"):
            radius *= 1000
    return {"intent": intent, "focus": focus, "radius_m": radius,
            "save": any(k in text for k in ("落图", "保存")), "answer": ""}


def _parse_plan(content: str, user_text: str) -> dict:
    plan = _parse_json_object(content)
    if not plan or plan.get("intent") not in _INTENTS:
        return _fallback_plan(user_text)
    fallback = _fallback_plan(user_text)
    return {
        "intent": plan["intent"],
        "focus": plan.get("focus") if plan.get("focus") in
                 {"general", "landslide", "building", "road", "flood", "barrier_lake"}
                 else fallback["focus"],
        "radius_m": plan.get("radius_m") if isinstance(plan.get("radius_m"), (int, float))
                    else fallback["radius_m"],
        "save": bool(plan.get("save", fallback["save"])),
        "answer": str(plan.get("answer") or ""),
    }


def _positions(value: Any):
    if isinstance(value, list) and len(value) >= 2 and all(
            isinstance(v, (int, float)) for v in value[:2]):
        yield value[0], value[1]
    elif isinstance(value, list):
        for item in value:
            yield from _positions(item)


def _task_aoi(task: dict) -> tuple[list[float] | None, dict | None]:
    raw = task.get("aoi_geojson")
    if not raw:
        return None, None
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None, None
    if isinstance(obj, list) and len(obj) == 4:
        bbox = [float(v) for v in obj]
        return bbox, _bbox_polygon(bbox)
    if isinstance(obj, dict):
        if isinstance(obj.get("bbox"), list) and len(obj["bbox"]) == 4:
            return [float(v) for v in obj["bbox"]], obj
        geometry = obj.get("geometry", obj)
        points = list(_positions(geometry.get("coordinates", []))) if isinstance(geometry, dict) else []
        if points:
            lons, lats = [p[0] for p in points], [p[1] for p in points]
            return [min(lons), min(lats), max(lons), max(lats)], obj
    return None, None


def _bbox_polygon(bbox: list[float]) -> dict:
    x1, y1, x2, y2 = bbox
    return {"type": "Polygon", "coordinates": [[
        [x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]]}


def _strip_final(content: str) -> str:
    match = _FINAL_RE.search(content)
    if match:
        return match.group(1).strip()
    return _TOOL_RE.sub("", content).replace("〔FINAL〕", "").replace("〔/FINAL〕", "").strip()


def _fallback_final(state: dict) -> str:
    parts = []
    for kind, item in state.get("evidence", {}).items():
        status, data = item.get("status"), item.get("data") or {}
        if status not in {"ok", "degraded"}:
            continue
        if kind == "area" and isinstance(data, dict) and "area_km2" in data:
            parts.append(f"AOI 面积 {data['area_km2']} 平方公里。")
        elif kind == "imagery" and isinstance(data, dict):
            parts.append(str(data.get("summary") or f"影像判读发现 {data.get('count', 0)} 处目标。"))
        elif kind == "terrain" and isinstance(data, dict):
            if status == "degraded":
                parts.append(f"DEM 数据降级，覆盖率 {float(item.get('quality', {}).get('coverage', 0)):.1%}，地形结论仅供有限参考。")
            else:
                parts.append(f"平均坡度 {data.get('slope_mean')}°，高程范围 {data.get('elev_min')}~{data.get('elev_max')}m。")
        elif kind == "geometry" and isinstance(data, dict):
            parts.append(f"缓冲区面积 {data.get('area_km2')} 平方公里。")
        elif kind == "review" and isinstance(data, dict):
            parts.append("第二引擎独立复核：" + str(data.get("summary") or "未形成有效复核结论。"))
        elif kind == "layers" and isinstance(data, list):
            parts.append(f"当前任务共有 {len(data)} 个已保存图层。")
    errors = [x.get("error") for x in state.get("executions", []) if x.get("error")]
    if errors:
        parts.append("数据或工具限制：" + str(errors[-1]))
    return "".join(parts) or "未能获得足够证据，暂无法形成可靠结论。"


def _evidence_gap(state: dict) -> dict | None:
    """地形证据不合格时生成机器可检查的缺口，禁止继续定量总结。"""
    item = (state.get("evidence") or {}).get("terrain")
    if not item:
        return None
    quality = item.get("quality") or {}
    status = item.get("status")
    if status not in {"insufficient", "error"} and quality.get("status") != "insufficient":
        return None
    data = item.get("data") or {}
    coverage = quality.get("coverage", data.get("coverage"))
    return {"kind": "terrain", "status": quality.get("status") or status,
            "coverage": coverage, "original_intent": state.get("intent") or "risk"}


def _gap_guidance(gap: dict) -> str:
    coverage = gap.get("coverage")
    detail = (f"DEM 覆盖率 {float(coverage):.1%}" if isinstance(coverage, (int, float))
              else "DEM 未返回合格数据")
    return (f"地形证据不足（{detail}），本次不据此输出区域整体定量风险结论。可选："
            "① 补充 DEM 后回复“重试地形研判”，复用已有影像证据；"
            "② 重新圈选有数据覆盖的 AOI；"
            "③ 回复“仅定性观察”，跳过 DEM，仅依据影像给出低证据等级观察。")


def _apply_gap_choice(state: dict, user_text: str, plan: dict) -> tuple[dict, str, bool, bool]:
    """将用户对证据缺口的选择映射为一次有限重规划；新目标自动清除旧缺口。"""
    pending = state.pop("pending_evidence_gap", None)
    if not pending:
        return plan, "", False, False
    text = re.sub(r"\s+", "", user_text)
    original = pending.get("original_intent") if pending.get("original_intent") in {"risk", "terrain"} else "risk"
    if "仅定性" in text:
        return {"intent": "risk", "focus": plan.get("focus") or "landslide",
                "radius_m": None, "save": False, "answer": ""}, "", True, True
    if "重试" in text or ("补充" in text and "DEM" in text.upper()):
        return {**plan, "intent": original, "answer": ""}, "", False, False
    if "重新圈选" in text or ("更换" in text and "AOI" in text.upper()):
        return plan, "请清除当前圈选并重新绘制 AOI，然后再次提交研判；已有图层仍保留用于对照。", False, False
    return plan, "", False, False


def _needs_review(state: dict, user_text: str) -> bool:
    """仅低置信、解析失败或显式复核时增加一次第二引擎调用。"""
    if case_model.usable_evidence(state, "review"):
        return False
    item = case_model.usable_evidence(state, "imagery")
    if not item:
        return False
    if any(word in user_text for word in ("复核", "双引擎", "交叉验证")):
        return True
    data = item.get("data") or {}
    if data.get("parse_ok") is False:
        return True
    findings = data.get("findings") or []
    confidences = []
    for finding in findings:
        try:
            confidences.append(float(finding.get("confidence", 0)))
        except (TypeError, ValueError):
            pass
    return bool(confidences) and max(confidences) < 0.65


def _review_engine() -> str:
    return "qwen3vl_ft" if settings.edge_engine == "qwen3vl" else "qwen3vl"


def _perform_tool(task_id: str, state: dict, name: str, args: dict,
                  run_cache: dict, evidence_name: str | None = None) -> Generator[dict, None, dict]:
    """执行一次真实工具并归约 CaseState；缓存只用于允许缓存的相同签名。"""
    actual = dict(args)
    actual["task_id"] = task_id  # 服务端任务上下文不可被模型覆盖
    signature = case_model.execution_signature(name, actual)
    actual["idempotency_key"] = f"{task_id}:{signature}"
    public_args = {k: v for k, v in actual.items() if k not in ("task_id", "idempotency_key")}
    yield _event("tool_call", tool=name, args=public_args, signature=signature)
    if tool_cacheable(name) and signature in run_cache:
        envelope = {**run_cache[signature], "_cached": True}
        obs_text = _compact_observation(envelope)
        yield _event("tool_result", tool=name, elapsed_s=0, cached=True,
                     quality=envelope.get("quality"), artifacts=envelope.get("artifacts", []),
                     preview=obs_text[:300])
        return envelope

    started = time.time()
    try:
        envelope = tool_call(name, actual)
    except Exception as exc:
        envelope = {"status": "error", "data": {}, "quality": {}, "artifacts": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "idempotency_key": actual["idempotency_key"]}
    else:
        if tool_cacheable(name) and envelope.get("status") in {"ok", "degraded"}:
            run_cache[signature] = envelope
    case_model.reduce_result(state, evidence_name or name, actual, envelope)
    store.save_case_state(task_id, state)
    obs_text = _compact_observation(envelope)
    store.add_message(task_id, "tool", f"〔OBS〕工具 {name} 返回：{obs_text}〔/OBS〕",
                      meta={"tool": name, "status": envelope.get("status")})
    if envelope.get("status") == "error":
        yield _event("tool_error", tool=name,
                     message=str(envelope.get("error") or "工具执行失败"))
    else:
        yield _event("tool_result", tool=name, elapsed_s=round(time.time() - started, 2),
                     cached=False, quality=envelope.get("quality"),
                     artifacts=envelope.get("artifacts", []), preview=obs_text[:300])
    return envelope


def _standard_actions(plan: dict, bbox: list[float]) -> list[tuple[str, dict, str | None]]:
    intent, focus = plan["intent"], plan["focus"]
    if intent == "area":
        return [("geo_area", {"bbox": bbox}, "area")]
    if intent == "terrain":
        return [("dem_stats", {"bbox": bbox}, "terrain")]
    if intent == "imagery":
        return [("vlm_inspect", {"bbox": bbox, "focus": focus}, "imagery")]
    if intent == "risk":
        return [("vlm_inspect", {"bbox": bbox, "focus": focus}, "imagery"),
                ("dem_stats", {"bbox": bbox}, "terrain")]
    return []


def run_agent(task_id: str, user_text: str) -> Generator[dict, None, None]:
    """规划→按证据缺口执行→总结；custom 保留受预算约束的文本协议。"""
    task = store.get_task(task_id)
    if not task:
        yield _event("error", message="任务不存在")
        return
    store.add_message(task_id, "user", user_text)
    yield _event("start", task_id=task_id)

    aoi_bbox, aoi_geojson = _task_aoi(task)
    state = store.get_case_state(task_id) or case_model.new_case(user_text, aoi_bbox)
    state["goal"] = user_text
    state["aoi_bbox"] = aoi_bbox
    steps = llm_calls = tool_calls = 0
    run_cache: dict[str, dict] = {}
    final_text = ""
    plan: dict = {}
    skip_terrain = qualitative_only = False
    gap: dict | None = None

    # 1. 规划：唯一允许输出意图 JSON 的节点。
    steps += 1
    yield _event("step", step=steps, phase="plan")
    planner_messages = [
        {"role": "system", "content": build_planner_prompt()},
        {"role": "user", "content": user_text},
    ]
    try:
        planner_content, usage = chat(planner_messages, purpose="agent_plan")
        llm_calls += 1
    except LLMError as exc:
        yield _event("error", message=f"LLM 规划失败：{exc}")
        store.update_task_status(task_id, "failed")
        return
    yield _event("round", round=llm_calls, phase="plan", side=usage["side"], usage=usage)
    plan = _parse_plan(planner_content, user_text)
    plan, choice_final, skip_terrain, qualitative_only = _apply_gap_choice(state, user_text, plan)
    final_text = choice_final
    state["intent"] = plan["intent"]
    store.save_case_state(task_id, state)

    if plan["intent"] == "chat" and not final_text:
        final_text = plan["answer"] or ("" if _parse_json_object(planner_content) else planner_content.strip())

    # 2. 标准路径只补齐缺失证据，不跑固定全工具链。
    needs_aoi = plan["intent"] in {"area", "terrain", "imagery", "risk", "buffer"}
    if not final_text and needs_aoi and not aoi_bbox:
        final_text = "请先在地图上圈选 AOI，再执行这项分析。"

    if not final_text and plan["intent"] in {"area", "terrain", "imagery", "risk"}:
        actions = _standard_actions(plan, aoi_bbox)
        if skip_terrain:
            actions = [action for action in actions if action[2] != "terrain"]
        for name, args, evidence_kind in actions:
            signature = case_model.execution_signature(name, args)
            if evidence_kind and case_model.usable_evidence(state, evidence_kind, signature):
                if steps >= settings.agent_max_steps:
                    break
                steps += 1
                yield _event("step", step=steps, phase="reuse_evidence", evidence=evidence_kind)
                continue
            if steps >= settings.agent_max_steps or tool_calls >= settings.agent_max_tools:
                break
            steps += 1
            tool_calls += 1
            yield _event("step", step=steps, phase="tool", tool=name)
            yield from _perform_tool(task_id, state, name, args, run_cache)

    elif not final_text and plan["intent"] == "list":
        if steps < settings.agent_max_steps and tool_calls < settings.agent_max_tools:
            steps += 1
            tool_calls += 1
            yield _event("step", step=steps, phase="tool", tool="list_layers")
            yield from _perform_tool(task_id, state, "list_layers", {}, run_cache)

    elif not final_text and plan["intent"] == "buffer":
        radius = plan.get("radius_m")
        if not radius or radius <= 0:
            final_text = "请提供有效的缓冲半径，例如“对 AOI 做 500 米缓冲区并落图”。"
        else:
            args = {"geojson": aoi_geojson, "radius_m": radius}
            signature = case_model.execution_signature("geo_buffer", args)
            buffered = case_model.usable_evidence(state, "geometry", signature)
            envelope = None
            if buffered and not plan["save"] and steps < settings.agent_max_steps:
                steps += 1
                yield _event("step", step=steps, phase="reuse_evidence", evidence="geometry")
                envelope = buffered
            elif steps < settings.agent_max_steps and tool_calls < settings.agent_max_tools:
                # CaseState 不存大 GeoJSON；要求落图时重算轻量几何，再由 save_layer 幂等写入。
                steps += 1
                tool_calls += 1
                yield _event("step", step=steps, phase="tool", tool="geo_buffer")
                envelope = yield from _perform_tool(task_id, state, "geo_buffer", args, run_cache)
            if envelope and plan["save"] and envelope.get("status") in {"ok", "degraded"} \
                    and steps < settings.agent_max_steps and tool_calls < settings.agent_max_tools:
                geojson = (envelope.get("data") or {}).get("geojson")
                if geojson:
                    steps += 1
                    tool_calls += 1
                    save_args = {"name": f"AOI缓冲区{radius:g}m", "kind": "buffer",
                                 "geojson": geojson, "props": {"radius_m": radius}}
                    yield _event("step", step=steps, phase="tool", tool="save_layer")
                    yield from _perform_tool(task_id, state, "save_layer", save_args, run_cache)

    # 3. custom 路径保留文本协议，但与标准路径共享 CaseState、预算和工具契约。
    elif not final_text and plan["intent"] == "custom":
        messages = [
            {"role": "system", "content": build_system_prompt(
                catalog_text(), settings.agent_max_steps, case_model.compact_case(state, 3200))},
            {"role": "user", "content": user_text},
        ]
        while steps < settings.agent_max_steps and llm_calls < settings.agent_max_llm_calls:
            steps += 1
            yield _event("step", step=steps, phase="custom_reason")
            _compact_old_observations(messages, 2)
            try:
                content, usage = chat(_fit_prompt_budget(messages, 2),
                                      purpose=f"agent_custom_{llm_calls + 1}")
                llm_calls += 1
            except LLMError:
                break
            yield _event("round", round=llm_calls, phase="custom", side=usage["side"], usage=usage)
            tool_m, final_m = _TOOL_RE.search(content), _FINAL_RE.search(content)
            if not tool_m or final_m:
                final_text = _strip_final(content)
                break
            name = tool_m.group(1)
            try:
                args = json.loads(tool_m.group(2))
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):
                obs = "〔OBS〕工具参数必须是合法 JSON 对象，请重新输出工具块。〔/OBS〕"
                messages.append({"role": "assistant", "content": tool_m.group(0)})
                messages.append({"role": "user", "content": obs})
                yield _event("tool_error", tool=name, message="工具参数必须是 JSON 对象")
                continue
            signature = case_model.execution_signature(name, {**args, "task_id": task_id})
            cached = tool_cacheable(name) and signature in run_cache
            if steps >= settings.agent_max_steps or \
                    (not cached and tool_calls >= settings.agent_max_tools):
                break
            steps += 1
            if not cached:
                tool_calls += 1
            yield _event("step", step=steps, phase="tool", tool=name)
            envelope = yield from _perform_tool(task_id, state, name, args, run_cache)
            obs = f"〔OBS〕工具 {name} 返回：{_compact_observation(envelope)}〔/OBS〕"
            messages.append({"role": "assistant", "content": tool_m.group(0)})
            messages.append({"role": "user", "content": obs})

    # 条件复核：原判读与第二引擎证据分开归约，避免复核覆盖原始结论。
    if not final_text and plan["intent"] in {"imagery", "risk"} and not qualitative_only \
            and not _evidence_gap(state) and _needs_review(state, user_text) \
            and steps < settings.agent_max_steps and tool_calls < settings.agent_max_tools:
        steps += 1
        tool_calls += 1
        reviewer = _review_engine()
        yield _event("step", step=steps, phase="review", engine=reviewer)
        yield from _perform_tool(task_id, state, "vlm_inspect", {
            "bbox": aoi_bbox, "focus": plan["focus"], "engine": reviewer,
        }, run_cache, evidence_name="vlm_review")

    # 证据不足由状态机强制门控：不追加总结 LLM，直接输出有限结论和可选路径。
    if not final_text and plan["intent"] in {"risk", "terrain"} and not qualitative_only:
        gap = _evidence_gap(state)
        if gap:
            state["pending_evidence_gap"] = gap
            store.save_case_state(task_id, state)
            yield _event("evidence_gap", gap=gap)
    store.save_case_state(task_id, state)

    # 4. 总结只读 CaseState；预算耗尽不再追加额外 LLM。
    if not final_text and not gap and llm_calls < settings.agent_max_llm_calls and steps < settings.agent_max_steps:
        steps += 1
        yield _event("step", step=steps, phase="summary")
        summary_messages = [
            {"role": "system", "content": build_summary_prompt()},
            {"role": "user", "content": "用户问题：" + user_text +
             "\nCaseState：\n" + case_model.compact_case(state, 3200)},
        ]
        try:
            summary, usage = chat(summary_messages, purpose="agent_summary")
            llm_calls += 1
        except LLMError:
            pass
        else:
            yield _event("round", round=llm_calls, phase="summary", side=usage["side"], usage=usage)
            final_text = _strip_final(summary)
    final_text = final_text or _fallback_final(state)
    if gap:
        final_text += "\n\n" + _gap_guidance(gap)
    elif qualitative_only:
        final_text += "\n\n本次按你的选择仅作影像定性观察，未使用 DEM 进行区域整体定量风险判断，证据等级较低。"
    if settings.network_state == "OFFLINE":
        final_text += "（当前离线模式，端侧模型研判）"
    store.add_message(task_id, "assistant", final_text,
                      meta={"steps": steps, "tool_calls": tool_calls, "llm_calls": llm_calls,
                            "evidence_gap": gap, "qualitative_only": qualitative_only})
    store.update_task_status(task_id, "done")
    yield _event("final", content=final_text, evidence_gap=gap,
                 budget={"steps": steps, "tool_calls": tool_calls, "llm_calls": llm_calls})
