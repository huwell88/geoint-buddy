"""轻量 CaseState：保存可复用证据，并压缩传给 LLM 的执行上下文。"""
import hashlib
import json
import time
from typing import Any

_RUNTIME_FIELDS = {"task_id", "idempotency_key", "run_id", "request_id", "trace_id"}
_OMIT_FIELDS = {"geojson", "tiles", "usage", "raw"}
_EVIDENCE_KINDS = {
    "geo_area": "area",
    "aoi_stats": "area",
    "vlm_inspect": "imagery",
    "vlm_review": "review",
    "dem_stats": "terrain",
    "geo_buffer": "geometry",
    "list_layers": "layers",  # 仅用于本次总结；工具元数据禁止跨调用缓存
}
_EXECUTION_FIELDS = ("name", "signature", "status", "quality", "artifacts", "error", "ts")


def new_case(goal: str, aoi_bbox: list[float] | None = None) -> dict:
    return {
        "version": 1,
        "goal": goal,
        "intent": "custom",
        "aoi_bbox": aoi_bbox,
        "evidence": {},
        "executions": [],
    }


def _signature_data(value: Any) -> Any:
    """递归剔除运行时字段，避免同一计算因任务上下文产生不同签名。"""
    if isinstance(value, dict):
        return {k: _signature_data(v) for k, v in value.items() if k not in _RUNTIME_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_signature_data(v) for v in value]
    return value


def execution_signature(name: str, args: dict | None) -> str:
    payload = {"name": name, "args": _signature_data(args if args is not None else {})}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_kind(name: str) -> str | None:
    return _EVIDENCE_KINDS.get(name)


def _compact(value: Any) -> Any:
    """递归移除大字段，并限制列表长度。"""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if k not in _OMIT_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_compact(v) for v in value[:20]]
    return value


def reduce_result(state: dict, name: str, args: dict | None, envelope: dict) -> dict:
    """将统一工具结果归约为最新证据和紧凑执行记录。"""
    signature = execution_signature(name, args)
    entry = {"signature": signature}
    for key in ("status", "data", "quality", "artifacts", "error", "idempotency_key"):
        entry[key] = _compact(envelope.get(key))

    kind = evidence_kind(name)
    if kind:
        evidence = state.setdefault("evidence", {})
        # 新失败不能抹掉此前可用的同类证据；执行日志仍保留本次失败。
        if entry["status"] in {"ok", "degraded"} or kind not in evidence:
            evidence[kind] = entry
    state.setdefault("executions", []).append({
        "name": name,
        "signature": signature,
        "status": entry["status"],
        "quality": entry["quality"],
        "artifacts": entry["artifacts"],
        "error": entry["error"],
        "ts": time.time(),
    })
    return state


def usable_evidence(state: dict, kind: str, signature: str | None = None) -> dict | None:
    item = state.get("evidence", {}).get(kind)
    if not item or item.get("status") not in {"ok", "degraded"}:
        return None
    if signature is not None and item.get("signature") != signature:
        return None
    return item


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def compact_case(state: dict, limit: int = 6000) -> str:
    """优先保留目标与证据；空间不足时先丢弃较早执行记录。"""
    executions = [
        {key: _compact(item[key]) for key in _EXECUTION_FIELDS if key in item}
        for item in state.get("executions", [])[-10:]
    ]
    slim = {
        "version": state.get("version", 1),
        "goal": state.get("goal", ""),
        "intent": state.get("intent", "custom"),
        "aoi_bbox": state.get("aoi_bbox"),
        "evidence": _compact(state.get("evidence", {})),
        "executions": executions,
    }
    text = _dumps(slim)
    while len(text) > limit and slim["executions"]:
        slim["executions"].pop(0)
        text = _dumps(slim)
    for key in ("executions", "version"):
        if len(text) > limit:
            slim.pop(key, None)
            text = _dumps(slim)
    while len(text) > limit and slim.get("evidence"):
        oldest = next(iter(slim["evidence"]))
        del slim["evidence"][oldest]
        text = _dumps(slim)
    for key in ("evidence", "aoi_bbox", "intent", "goal"):
        if len(text) > limit:
            slim.pop(key, None)
            text = _dumps(slim)
    return text if len(text) <= limit else ""
