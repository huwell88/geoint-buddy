"""离线瓦片工具：mbtiles 读取、bbox 瓦片覆盖统计、区域拼图。

mbtiles 规范：tiles 表 y 为 TMS 行号（与 slippy y 翻转）。
"""
import io
import math
import sqlite3
from pathlib import Path

from PIL import Image

from . import register
from ..config import IMAGERY_MBTILES


def _mbtiles_files() -> list[Path]:
    return [IMAGERY_MBTILES] if IMAGERY_MBTILES.exists() else []


def deg2tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """slippy map 瓦片号。z 必须为整数（原项目经验）。"""
    z = int(z)
    lat_r = math.radians(max(-85.0511, min(85.0511, lat)))
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return x, y


def tile2deg(x: int, y: int, z: int) -> tuple[float, float]:
    """瓦片左上角经纬度。"""
    n = 2 ** int(z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def read_tile(z: int, x: int, y: int) -> bytes | None:
    """按 slippy 号读瓦片（内部转 TMS）。"""
    tms_y = (2 ** int(z) - 1) - y
    for f in _mbtiles_files():
        with _open(f) as conn:
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, tms_y)).fetchone()
        if row:
            return row[0]
    return None


def tiles_info() -> dict:
    out = []
    for f in _mbtiles_files():
        with _open(f) as conn:
            meta = dict(conn.execute("SELECT name,value FROM metadata").fetchall())
            cnt = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
            zooms = [r[0] for r in conn.execute(
                "SELECT DISTINCT zoom_level FROM tiles ORDER BY zoom_level").fetchall()]
        out.append({"file": f.name, "name": meta.get("name", f.stem),
                    "tiles": cnt, "zooms": zooms,
                    "bounds": meta.get("bounds", "")})
    return {"mbtiles": out}


@register("tile_coverage", "查询离线影像瓦片对指定 bbox 的覆盖情况（有哪些层级可用）",
          'bbox: [minLon,minLat,maxLon,maxLat]')
def tile_coverage(bbox: list[float]) -> dict:
    info = tiles_info()
    if not info["mbtiles"]:
        return {"available": False, "reason": "未找到 mbtiles 离线瓦片"}
    coverage = []
    for m in info["mbtiles"]:
        if not m["zooms"]:
            continue
        z = max(m["zooms"])
        x1, y2 = deg2tile(bbox[0], bbox[1], z)
        x2, y1 = deg2tile(bbox[2], bbox[3], z)
        n = (x2 - x1 + 1) * (y2 - y1 + 1)
        coverage.append({"file": m["file"], "max_zoom": z,
                         "tiles_needed_at_max_zoom": n})
    return {"available": bool(coverage), "coverage": coverage}


def stitch_bbox(bbox: list[float], z: int | None = None,
                max_size: int = 1024) -> tuple[Image.Image, dict]:
    """将 bbox 覆盖的瓦片拼成一张图，返回 (PIL 图像, 元信息)。

    元信息含实际像素地理范围，供 VLM 判读结果做地理配准。
    """
    info = tiles_info()
    zooms = [z for m in info["mbtiles"] for z in m["zooms"]]
    if not zooms:
        raise FileNotFoundError("无离线瓦片可用")
    z = int(z) if z else max(zooms)
    x1, y2 = deg2tile(bbox[0], bbox[1], z)
    x2, y1 = deg2tile(bbox[2], bbox[3], z)
    nx, ny = x2 - x1 + 1, y2 - y1 + 1
    if nx * ny > 64:
        # 瓦片太多则自动降层级，控制拼图规模
        z = max(min(zooms), z - 2)
        x1, y2 = deg2tile(bbox[0], bbox[1], z)
        x2, y1 = deg2tile(bbox[2], bbox[3], z)
        nx, ny = x2 - x1 + 1, y2 - y1 + 1

    canvas = Image.new("RGB", (nx * 256, ny * 256), (40, 40, 40))
    missing = 0
    for ix, tx in enumerate(range(x1, x2 + 1)):
        for iy, ty in enumerate(range(y1, y2 + 1)):
            data = read_tile(z, tx, ty)
            if data:
                canvas.paste(Image.open(io.BytesIO(data)).convert("RGB"),
                             (ix * 256, iy * 256))
            else:
                missing += 1
    if canvas.width > max_size or canvas.height > max_size:
        canvas.thumbnail((max_size, max_size), Image.LANCZOS)

    lon1, lat1 = tile2deg(x1, y1, z)
    lon2, lat2 = tile2deg(x2 + 1, y2 + 1, z)
    meta = {"zoom": z, "tiles": nx * ny, "missing": missing,
            "extent": [lon1, lat2, lon2, lat1],  # [minLon,minLat,maxLon,maxLat]
            "size": list(canvas.size)}
    return canvas, meta
