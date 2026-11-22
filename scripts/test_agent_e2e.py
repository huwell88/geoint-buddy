"""混合 Agent 真实端侧回归：同 AOI、跨追问复用、幂等落图与 DEM 质量。"""
import json
import sqlite3
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
DB_PATH = "/home/orangepi/geoint-buddy/backend/data/geoint_buddy.db"
AOI = [103.2, 31.5, 103.3, 31.6]
failures = []
metrics = {"requests": [], "max_prompt_tokens": 0}


def post(path, body, timeout=3600):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check(name, condition, detail=""):
    print(f"[CHECK] {'PASS' if condition else 'FAIL'} {name} {detail}", flush=True)
    if not condition:
        failures.append(name)


def chat(task_id, label, message):
    started = time.time()
    events = []
    tools = []
    print(f"\n[RUN] {label}: {message}", flush=True)
    with post("/api/chat", {"task_id": task_id, "message": message}) as resp:
        while True:
            line = resp.readline()
            if not line:
                break
            if not line.startswith(b"data: "):
                continue
            ev = json.loads(line[6:].decode())
            events.append(ev)
            kind = ev.get("kind")
            if kind == "round":
                usage = ev.get("usage") or {}
                prompt = usage.get("prompt_tokens", 0) or 0
                metrics["max_prompt_tokens"] = max(metrics["max_prompt_tokens"], prompt)
                print(f"  [LLM {ev.get('round')}] {ev.get('phase')} prompt={prompt} "
                      f"completion={usage.get('completion_tokens')} elapsed={usage.get('elapsed_s')}s", flush=True)
            elif kind == "tool_call":
                tools.append(ev["tool"])
                print(f"  [TOOL] {ev['tool']} {ev.get('args')}", flush=True)
            elif kind == "tool_result":
                print(f"  [RESULT] {ev['tool']} cached={ev.get('cached')} "
                      f"quality={ev.get('quality')} artifacts={ev.get('artifacts')}", flush=True)
            elif kind == "tool_error":
                print(f"  [TOOL_ERROR] {ev.get('tool')}: {ev.get('message')}", flush=True)
            elif kind == "error":
                print(f"  [ERROR] {ev.get('message')}", flush=True)
            elif kind == "final":
                print(f"  [FINAL] budget={ev.get('budget')} content={ev.get('content', '')[:220]}", flush=True)
    result = {"label": label, "elapsed_s": round(time.time() - started, 2),
              "tools": tools, "events": events}
    metrics["requests"].append(result)
    return result


def layers(task_id):
    with urllib.request.urlopen(BASE + "/api/layers?task_id=" + task_id, timeout=30) as resp:
        return json.load(resp)


def cleanup(task_id):
    if not task_id:
        return
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("DELETE FROM layers WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM messages WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    print(f"\n[CLEANUP] task={task_id} 自检任务、消息与图层已清理", flush=True)


def main():
    task_id = None
    try:
        feature = {"type": "Feature", "properties": {"name": "Agent混合编排回归"},
                   "geometry": {"type": "Polygon", "coordinates": [[
                       [AOI[0], AOI[1]], [AOI[2], AOI[1]], [AOI[2], AOI[3]],
                       [AOI[0], AOI[3]], [AOI[0], AOI[1]]]]}}
        with post("/api/tasks", {"title": "Agent混合编排自检", "aoi_geojson": feature}, 30) as resp:
            task_id = json.load(resp)["id"]
        print(f"[TASK] {task_id} AOI={AOI}", flush=True)

        chat_run = chat(task_id, "chat", "你好，请简要介绍你能做什么。")
        check("闲聊零工具", chat_run["tools"] == [], str(chat_run["tools"]))
        final = next((e for e in chat_run["events"] if e["kind"] == "final"), {})
        check("闲聊仅规划一次 LLM", final.get("budget", {}).get("llm_calls") == 1)

        area = chat(task_id, "area", "测算圈选区域面积。")
        check("面积只执行 geo_area", area["tools"] == ["geo_area"], str(area["tools"]))
        area_reuse = chat(task_id, "area_reuse", "再次说明这个区域的面积。")
        check("面积追问复用证据且不执行工具", area_reuse["tools"] == [], str(area_reuse["tools"]))
        check("面积追问产生复用状态", any(e.get("phase") == "reuse_evidence"
                                      for e in area_reuse["events"]))

        list_run = chat(task_id, "list", "查询当前任务已有图层。")
        check("图层查询只执行实时 list_layers", list_run["tools"] == ["list_layers"],
              str(list_run["tools"]))

        buffer1 = chat(task_id, "buffer_save", "对 AOI 做 500 米缓冲区并落图。")
        check("缓冲区计算与显式落图分离", buffer1["tools"] == ["geo_buffer", "save_layer"],
              str(buffer1["tools"]))
        count1 = len(layers(task_id))
        buffer2 = chat(task_id, "buffer_repeat", "再次对 AOI 做 500 米缓冲区并落图。")
        count2 = len(layers(task_id))
        check("重复缓冲区不新增图层", count1 == 1 and count2 == 1,
              f"before={count1} after={count2}")
        artifacts1 = [a for e in buffer1["events"] if e["kind"] == "tool_result"
                      for a in e.get("artifacts", [])]
        artifacts2 = [a for e in buffer2["events"] if e["kind"] == "tool_result"
                      for a in e.get("artifacts", [])]
        check("重复落图复用同一 artifact", artifacts1 == artifacts2 and len(artifacts1) == 1,
              f"{artifacts1} vs {artifacts2}")

        risk = chat(task_id, "risk", "综合研判该区域滑坡灾害风险，需要影像和地形证据。")
        check("综合风险仅补齐 VLM 与 DEM", risk["tools"] == ["vlm_inspect", "dem_stats"],
              str(risk["tools"]))
        dem_results = [e for e in risk["events"] if e["kind"] == "tool_result"
                       and e.get("tool") == "dem_stats"]
        check("DEM 返回质量状态而非伪统计", bool(dem_results and dem_results[0].get("quality", {}).get("status")),
              str(dem_results[0].get("quality") if dem_results else "missing"))
        if dem_results:
            quality = dem_results[0].get("quality", {})
            status, coverage = quality.get("status"), quality.get("coverage")
            check("DEM 覆盖率与质量等级一致",
                  (status == "ok" and coverage == 1) or
                  (status == "degraded" and 0.8 <= coverage < 1) or
                  (status == "insufficient" and coverage < 0.8),
                  f"status={status} coverage={coverage}")
        final = next((e for e in risk["events"] if e["kind"] == "final"), {})
        check("标准风险任务不超过规划加总结两次 LLM",
              final.get("budget", {}).get("llm_calls", 99) <= 2,
              str(final.get("budget")))

        for item in metrics["requests"]:
            check(f"{item['label']} 有最终答复", any(e["kind"] == "final" for e in item["events"]))
            check(f"{item['label']} 无全局错误", not any(e["kind"] == "error" for e in item["events"]))
        check("所有真实 prompt 低于 5120 tokens", metrics["max_prompt_tokens"] < 5120,
              f"peak={metrics['max_prompt_tokens']}")

        print("\n[METRICS] " + json.dumps({
            "task_id": task_id,
            "max_prompt_tokens": metrics["max_prompt_tokens"],
            "requests": [{"label": r["label"], "elapsed_s": r["elapsed_s"], "tools": r["tools"]}
                         for r in metrics["requests"]],
            "final_layers": [(x["id"], x["name"], x["kind"]) for x in layers(task_id)],
        }, ensure_ascii=False), flush=True)
    finally:
        cleanup(task_id)
    if failures:
        raise SystemExit("回归失败项: " + ", ".join(failures))
    print("\n[PASS] 全部真实端侧回归通过", flush=True)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--cleanup":
        cleanup(sys.argv[2])
    else:
        main()
