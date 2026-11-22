"""GEOINT Buddy FastAPI 入口：API + 离线瓦片服务 + 前端静态托管。"""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import store
from .agent import run_agent
from .config import settings, BASE_DIR, EDGE_ENGINES, REGION
from .tools.tiles import read_tile, tiles_info

FRONTEND_DIR = BASE_DIR.parent / "frontend"

app = FastAPI(title=settings.app_name, version=settings.version)


@app.middleware("http")
async def cache_control(request, call_next):
    """HTML/JS/CSS 禁缓存（避免前端更新后浏览器拿旧版）；瓦片长缓存。"""
    resp = await call_next(request)
    if not request.url.path.startswith("/tiles/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


# ---------------- 基础 ----------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": settings.version,
            "network_state": settings.network_state}


class NetworkState(BaseModel):
    state: str  # ONLINE / DEGRADED / OFFLINE


@app.post("/api/network")
def set_network(body: NetworkState) -> dict:
    if body.state not in ("ONLINE", "DEGRADED", "OFFLINE"):
        raise HTTPException(400, "state 必须为 ONLINE/DEGRADED/OFFLINE")
    settings.network_state = body.state
    return {"network_state": settings.network_state}


@app.get("/api/region")
def region() -> dict:
    return REGION


# ---------------- 端侧引擎切换 ----------------

@app.get("/api/engines")
def engines() -> dict:
    return {"engines": list(EDGE_ENGINES.values()), "current": settings.edge_engine}


class EngineIn(BaseModel):
    engine: str  # qwen3vl / gemma4


@app.post("/api/engine")
def set_engine(body: EngineIn) -> dict:
    if body.engine not in EDGE_ENGINES:
        raise HTTPException(400, f"未知引擎 {body.engine}，可选：{list(EDGE_ENGINES)}")
    settings.edge_engine = body.engine
    return {"current": settings.edge_engine, "engine": EDGE_ENGINES[body.engine]}


class CompareIn(BaseModel):
    task_id: str
    focus: str = "general"
    bbox: list[float] | None = None   # 缺省用任务 AOI


def _compare_brief(engine_id: str, envelope: dict) -> dict:
    r = envelope.get("data") or {}
    u = r.get("usage", {})
    return {"engine": engine_id, "engine_name": EDGE_ENGINES[engine_id]["name"],
            "parse_ok": r.get("parse_ok", False), "count": r.get("count", 0),
            "summary": r.get("summary", ""), "findings": r.get("findings", []),
            "elapsed_s": u.get("elapsed_s"), "completion_tokens": u.get("completion_tokens"),
            "layer_ids": envelope.get("artifacts") or r.get("layer_ids", [])}


@app.post("/api/engines/compare")
def engines_compare(body: CompareIn) -> dict:
    """同一 AOI 双端侧引擎并行判读，结果分别落图（图层名带引擎标识）。"""
    from concurrent.futures import ThreadPoolExecutor
    from .tools import call as tool_call

    task = store.get_task(body.task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    bbox = body.bbox
    if not bbox:
        gj = task.get("aoi_geojson")
        if not gj:
            raise HTTPException(400, "任务无 AOI 且未提供 bbox")
        ring = (json.loads(gj) if isinstance(gj, str) else gj)["coordinates"][0]
        lons = [p[0] for p in ring]; lats = [p[1] for p in ring]
        bbox = [min(lons), min(lats), max(lons), max(lats)]

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {eid: ex.submit(tool_call, "vlm_inspect",
                               {"bbox": bbox, "focus": body.focus,
                                "task_id": body.task_id, "engine": eid})
                for eid in ("qwen3vl", "gemma4")}
        results = {eid: f.result() for eid, f in futs.items()}
    return {"bbox": bbox, "focus": body.focus,
            **{eid: _compare_brief(eid, r) for eid, r in results.items()}}


# ---------------- 态势报告导出 ----------------

def _bbox_area_km2(bbox: list[float]) -> float:
    from math import cos, radians
    lon1, lat1, lon2, lat2 = bbox
    mid = (lat1 + lat2) / 2
    return (lon2 - lon1) * 111.32 * cos(radians(mid)) * (lat2 - lat1) * 110.57


@app.get("/api/tasks/{task_id}/report")
def task_report(task_id: str) -> Response:
    """任务态势研判报告（Markdown，用户视角）。"""
    import datetime

    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    msgs = store.get_messages(task_id)
    layers = store.list_layers(task_id)

    ts = datetime.datetime.fromtimestamp(task["created_at"]).strftime("%Y-%m-%d %H:%M")
    lines = ["# 态势研判报告 · " + task["title"], "",
             f"- 任务编号：`{task_id}`", f"- 创建时间：{ts}", f"- 任务状态：{task['status']}",
             f"- 报告生成：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    gj = json.loads(task["aoi_geojson"]) if task.get("aoi_geojson") else None
    if gj:
        ring = gj["coordinates"][0]
        lons = [p[0] for p in ring]; lats = [p[1] for p in ring]
        bbox = [min(lons), min(lats), max(lons), max(lats)]
        lines += ["## 一、任务区域（AOI）", "",
                  f"- 范围：经度 {bbox[0]:.4f}~{bbox[2]:.4f}，纬度 {bbox[1]:.4f}~{bbox[3]:.4f}",
                  f"- 幅员：约 {_bbox_area_km2(bbox):.1f} km²", ""]

    if layers:
        lines += ["## 二、判读与落图结果", ""]
        for l in layers:
            lines.append(f"### {l['name']}（{l['kind']}）")
            props = l.get("props") or {}
            g = l.get("geojson") or {}
            feats = g.get("features") if g.get("type") == "FeatureCollection" else (
                [g] if g.get("type") == "Feature" else [])
            for f in feats:
                p = f.get("properties") or {}
                geom = f.get("geometry") or {}
                if geom.get("type") == "Point":
                    lon, lat = geom["coordinates"][:2]
                    conf = p.get("confidence")
                    desc = p.get("description", "")
                    lines.append(f"- 目标（{p.get('ftype', 'target')}）：({lon:.4f}, {lat:.4f})"
                                 + (f"，置信度 {conf:.2f}" if isinstance(conf, (int, float)) else "")
                                 + (f"——{desc}" if desc else ""))
                elif p:
                    for k, v in p.items():
                        lines.append(f"- {k}：{v}")
            if props:
                src = props.get("source")
                if src:
                    lines.append(f"- 数据来源：{src}")
            lines.append("")

    final = next((m for m in reversed(msgs) if m["role"] == "assistant"), None)
    if final:
        lines += ["## 三、研判结论", "", final["content"].strip(), ""]

    if msgs:
        lines += ["## 附：研判过程摘录", ""]
        heads = {"user": "**指令**", "assistant": "**推理**", "tool": "**工具观察**"}
        for m in msgs:
            c = (m["content"] or "").strip()
            if not c or (m["role"] == "user" and c.startswith("〔")):
                continue
            if len(c) > 600:
                c = c[:600] + "…"
            lines += [heads.get(m["role"], m["role"]), "", c, ""]

    md = "\n".join(lines)
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=report_{task_id}.md"})


# ---------------- 任务与对话 ----------------

class TaskCreate(BaseModel):
    title: str
    aoi_geojson: dict | None = None


@app.post("/api/tasks")
def create_task(body: TaskCreate) -> dict:
    aoi = json.dumps(body.aoi_geojson, ensure_ascii=False) if body.aoi_geojson else None
    return store.create_task(body.title, aoi)


@app.get("/api/tasks")
def list_tasks() -> list[dict]:
    return store.list_tasks()


@app.get("/api/tasks/{task_id}/messages")
def task_messages(task_id: str) -> list[dict]:
    if not store.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return store.get_messages(task_id)


class ChatIn(BaseModel):
    task_id: str
    message: str


@app.post("/api/chat")
def chat_sse(body: ChatIn) -> StreamingResponse:
    """SSE 推送 Agent 事件流。"""
    def gen():
        try:
            for ev in run_agent(body.task_id, body.message):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'kind':'error','message':str(e)}, ensure_ascii=False)}\n\n"
        yield "data: {\"kind\":\"done\"}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------- 图层 ----------------

@app.get("/api/layers")
def layers(task_id: str | None = None) -> list[dict]:
    return store.list_layers(task_id)


@app.delete("/api/layers/{layer_id}")
def del_layer(layer_id: str) -> dict:
    if not store.delete_layer(layer_id):
        raise HTTPException(404, "图层不存在")
    return {"deleted": layer_id}


# ---------------- 离线瓦片 ----------------

@app.get("/api/tiles/info")
def tile_info() -> dict:
    return tiles_info()


@app.get("/tiles/{z}/{x}/{y}.png")
def tile(z: int, x: int, y: int) -> Response:
    data = read_tile(z, x, y)
    if data is None:
        raise HTTPException(404, "瓦片不存在")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})


# ---------------- Token 用量 ----------------

@app.get("/api/cloud/ping")
def cloud_ping() -> dict:
    """探测云端可达性（3s 超时）。前端 ONLINE 时提示是否需降级。"""
    from .llm import _client
    if settings.network_state == "OFFLINE":
        return {"reachable": False, "reason": "OFFLINE 模式已强制端侧"}
    try:
        _client(settings.cloud_base_url, settings.cloud_api_key, 3.0).models.list()
        return {"reachable": True, "model": settings.cloud_model}
    except Exception as e:
        return {"reachable": False, "reason": f"{type(e).__name__}: {e}"[:200]}


@app.get("/api/usage")
def usage() -> dict:
    from .llm import USAGE_LOG
    records = []
    if USAGE_LOG.exists():
        for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    summary: dict = {}
    for r in records:
        side = r.get("side", "?")
        s = summary.setdefault(side, {"calls": 0, "prompt_tokens": 0,
                                      "completion_tokens": 0, "errors": 0})
        s["calls"] += 1
        s["prompt_tokens"] += r.get("prompt_tokens", 0)
        s["completion_tokens"] += r.get("completion_tokens", 0)
        if r.get("error"):
            s["errors"] += 1
    return {"summary": summary, "recent": records[-30:]}


# ---------------- 前端静态托管 ----------------

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
