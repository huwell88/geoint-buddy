"""在线地理编码：Nominatim。断网时返回明确错误供 Agent 反思。"""
import requests

from . import register
from ..config import settings


@register("geocode", "地名解析为经纬度与包围盒（在线服务，断网不可用）",
          'name: 地名，如"西藏江达县波罗乡"')
def geocode(name: str) -> dict:
    resp = requests.get(
        f"{settings.nominatim_url}/search",
        params={"q": name, "format": "json", "limit": 3, "accept-language": "zh"},
        headers={"User-Agent": "geoint-buddy/0.2"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return {"found": False, "name": name}
    r = results[0]
    return {
        "found": True,
        "display_name": r["display_name"],
        "lon": float(r["lon"]),
        "lat": float(r["lat"]),
        "bbox": [float(r["boundingbox"][2]), float(r["boundingbox"][0]),
                 float(r["boundingbox"][3]), float(r["boundingbox"][1])],  # [minLon,minLat,maxLon,maxLat]
    }
