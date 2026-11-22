"""VLM 遥感影像判读工具：拼接 AOI 瓦片 → VLM 研判 → 目标落图。

判读结论带置信度说明：VLM 为主观研判，非精确测量（提案要求可溯源）。
"""
import base64
import io
import json
import re

from . import register
from .tiles import stitch_bbox
from .. import store
from ..llm import chat_with_image
from ..config import settings

_FOCUS = {
    "landslide": "滑坡体（裸露土石、纹理紊乱区、沟谷堆积扇、与周边植被的色调反差）",
    "building": "建筑损毁（屋顶塌陷、墙体倒塌废墟纹理、与灾前格局差异）",
    "road": "道路阻断（路面被堆积物覆盖、路基冲毁导致的线状中断）",
    "flood": "洪水淹没（水体异常扩张、淹没边界、浑浊水体、被淹农田与建筑）",
    "barrier_lake": "堰塞湖（河道阻断、回水区沿河谷异常扩张、坝体松散堆积体）",
    "general": "滑坡体、建筑损毁、道路阻断、洪水淹没、堰塞湖五类灾害目标",
}

_PROMPT = """\
你是遥感影像判读专家。这是一张待核查区域的卫星影像，地理范围：经度 {lon1:.4f}~{lon2:.4f}，纬度 {lat1:.4f}~{lat2:.4f}。
请先扫视全图，依次核对影像中是否存在：{focus}。

严格输出 JSON（不要输出任何其他文字）：
{{"findings":[{{"type":"landslide|building_damage|road_blockage|flood|barrier_lake","description":"一句中文描述","rel_x":0.0,"rel_y":0.0,"confidence":0.0}}],"summary":"先一句影像整体地貌特征，再一句研判结论"}}

rel_x、rel_y 为目标中心相对图像的位置（左上 0,0，右下 1,1）。confidence 取 0~1。
判读要求：先逐一核对上述特征再下结论；裸露土石、纹理紊乱等可疑迹象即使把握不大也须标注（confidence 如实调低），由用户结合实地情况核实；仅当全图核对后确无相关迹象，findings 才为空数组。图像模糊或无价值时如实说明。"""


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("VLM 输出未包含 JSON")
    return json.loads(m.group(0))


@register("vlm_inspect",
          "对 AOI 离线卫星影像做 VLM 灾害迹象判读（滑坡/建筑损毁/道路阻断/洪水/堰塞湖，无迹象则如实报告），目标自动落图",
          'bbox: [minLon,minLat,maxLon,maxLat]；focus: landslide/building/road/flood/barrier_lake/general 之一；question: 可选，自定义判读问题；engine: 可选，指定端侧引擎 qwen3vl/gemma4（默认当前引擎）',
          effect="write")
def vlm_inspect(bbox: list[float], focus: str = "general",
                question: str | None = None, task_id: str | None = None,
                engine: str | None = None, idempotency_key: str | None = None) -> dict:
    image, meta = stitch_bbox(bbox)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    lon1, lat1, lon2, lat2 = meta["extent"]
    prompt = question or _PROMPT.format(
        lon1=lon1, lon2=lon2, lat1=lat1, lat2=lat2,
        focus=_FOCUS.get(focus, _FOCUS["general"]))

    # 解析失败自动重试一次（小模型偶发不按 JSON 协议输出）
    parsed = None
    for attempt in (1, 2):
        raw, usage = chat_with_image(prompt, b64, purpose=f"vlm_inspect:{focus}", engine=engine)
        try:
            parsed = _extract_json(raw)
            break
        except (ValueError, json.JSONDecodeError):
            parsed = None
            if attempt == 1:
                continue

    result: dict = {"zoom": meta["zoom"], "extent": meta["extent"],
                    "tiles": meta["tiles"], "missing_tiles": meta["missing"],
                    "side": usage["side"], "usage": usage}
    if parsed is None:
        result["parse_ok"] = False
        result["error"] = ("判读输出解析失败（模型未按 JSON 协议输出）。"
                           "注意：这表示本次判读失败，并非区域内无灾害迹象；"
                           "请再次调用本工具重试，或改用 engine 参数换引擎判读。")
        result["raw"] = raw[: settings.detection_summary_limit]
        return result

    result["parse_ok"] = True
    result["summary"] = parsed.get("summary", "")
    findings = []
    for f in parsed.get("findings", [])[:50]:
        try:
            rx = min(1.0, max(0.0, float(f.get("rel_x", 0.5))))
            ry = min(1.0, max(0.0, float(f.get("rel_y", 0.5))))
        except (TypeError, ValueError):
            continue
        lon = lon1 + rx * (lon2 - lon1)
        lat = lat2 - ry * (lat2 - lat1)  # 图像 y 向下
        findings.append({
            "type": f.get("type", "unknown"),
            "description": str(f.get("description", ""))[:200],
            "lon": round(lon, 6), "lat": round(lat, 6),
            "confidence": round(min(1.0, max(0.0, float(f.get("confidence", 0.5)))), 2),
        })
    result["findings"] = findings
    result["count"] = len(findings)

    # 目标落图：每类目标一个 FeatureCollection 图层（图层名带引擎，便于双引擎判读对比）
    eng_name = usage.get("engine_name") or usage["side"]
    if findings and task_id:
        by_type: dict[str, list] = {}
        for f in findings:
            by_type.setdefault(f["type"], []).append(f)
        layer_ids = []
        for ftype, items in by_type.items():
            fc = {"type": "FeatureCollection", "features": [
                {"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [it["lon"], it["lat"]]},
                 "properties": {"description": it["description"],
                                "confidence": it["confidence"]}}
                for it in items]}
            layer = store.save_layer(
                f"VLM判读-{ftype}·{eng_name}", "detection", fc, task_id=task_id,
                props={"focus": focus, "count": len(items), "side": usage["side"],
                       "engine": eng_name, "source": "vlm"},
                idempotency_key=(f"{idempotency_key}:{ftype}"
                                 if idempotency_key else None))
            layer_ids.append(layer["id"])
        result["layer_ids"] = layer_ids

    # 注入上下文摘要限长（原项目经验）
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > settings.detection_summary_limit:
        result["findings"] = findings[:20]
        result["truncated"] = True
    return result
