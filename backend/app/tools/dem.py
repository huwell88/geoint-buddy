"""DEM 地形分析工具：离线 Terrarium 高程瓦片 → 海拔/坡度统计。

数据源：AWS Terrain Tiles（SRTM/ASTER 30m 级），Terrarium 编码：
高程(米) = R*256 + G + B/256 - 32768。坡度由采样网格中心差分计算。
"""
import math
import sqlite3
from io import BytesIO

from PIL import Image

from ..config import DEM_MBTILES, REGION
from .. import store
from . import register

DEM_Z = REGION["dem_zoom"]         # 分析固定层级（接近 SRTM 30m 真实分辨率）


def _read_tile(z, x, y):
    if not DEM_MBTILES.exists():
        return None
    tms_y = (2 ** z - 1) - y
    conn = sqlite3.connect(f"file:{DEM_MBTILES}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, tms_y)).fetchone()
    conn.close()
    return row[0] if row else None


def _tile_bounds(bbox, z):
    n = 2 ** z
    x0 = int((bbox[0] + 180) / 360 * n)
    x1 = int((bbox[2] + 180) / 360 * n)
    lat2y = lambda lat: int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n)
    return x0, x1, lat2y(bbox[3]), lat2y(bbox[1])     # y0 北(小), y1 南(大)


def _y2lat(y_tile, z):
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y_tile / (2 ** z)))))


def _dem_grid(bbox):
    """读取 DEM 网格；缺失瓦片的样本为 None，并用有效掩码标记。"""
    x0, x1, y0, y1 = _tile_bounds(bbox, DEM_Z)
    W, H = (x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256
    mosaic = Image.new("RGB", (W, H))
    missing = 0
    total_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
    valid_tiles = set()
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            data = _read_tile(DEM_Z, tx, ty)
            if data:
                mosaic.paste(Image.open(BytesIO(data)).convert("RGB"), ((tx - x0) * 256, (ty - y0) * 256))
                valid_tiles.add((tx, ty))
            else:
                missing += 1
    step = max(2, W // 256)               # 自适应降采样，控制网格 ~256 列
    px = mosaic.load()
    n = 2 ** DEM_Z
    heights, lats, cell_x, valid_mask = [], [], [], []
    for j in range(0, H, step):
        lat = _y2lat(y0 + j / 256, DEM_Z)  # 该行中心纬度
        row, mask_row = [], []
        for i in range(0, W, step):
            tile_xy = (x0 + i // 256, y0 + j // 256)
            valid = tile_xy in valid_tiles
            mask_row.append(valid)
            if valid:
                r, g, b, *_ = px[i, j]
                row.append(r * 256 + g + b / 256 - 32768)
            else:
                row.append(None)
        heights.append(row)
        valid_mask.append(mask_row)
        lats.append(lat)
        deg_per_px = 360 / (n * 256)
        cell_x.append(deg_per_px * step * 111320 * math.cos(math.radians(lat)))
    cell_y = 360 / (n * 256) * step * 111320
    return heights, lats, cell_x, cell_y, missing, total_tiles, valid_mask


@register("dem_stats", "DEM 地形统计：海拔范围/相对高差/平均与最大坡度/陡坡(>25°)占比，滑坡研判基础参数",
          "bbox: [minLon,minLat,maxLon,maxLat]", effect="write")
def dem_stats(bbox: list, task_id: str | None = None,
              idempotency_key: str | None = None):
    """AOI 地形统计，并按完整采样网格的有效覆盖率标记质量。"""
    if not DEM_MBTILES.exists():
        return {"error": f"{DEM_MBTILES.name} 未就绪，请先运行 scripts/download_dem.py"}
    heights, _, cell_x, cell_y, missing, total_tiles, valid_mask = _dem_grid(bbox)
    total_samples = sum(len(row) for row in heights)
    flat = [h for row in heights for h in row if h is not None]
    valid_samples = len(flat)
    coverage = valid_samples / total_samples if total_samples else 0.0
    base = {
        "zoom": DEM_Z,
        "coverage": coverage,
        "dem_tiles_total": total_tiles,
        "dem_tiles_missing": missing,
        "total_samples": total_samples,
        "valid_samples": valid_samples,
    }
    if not flat or coverage < 0.8:
        reason = "AOI 内无有效 DEM 数据" if not flat else "DEM 有效采样覆盖率低于 80%，统计证据不足"
        return {**base, "error": reason,
                "quality": {"status": "insufficient", "coverage": coverage}}

    slopes = []
    for j in range(1, len(heights) - 1):
        for i in range(1, len(heights[j]) - 1):
            # 中心及四邻域均有有效高程时，才计算该样本坡度
            neighbors_valid = (
                valid_mask[j][i] and valid_mask[j][i - 1] and valid_mask[j][i + 1]
                and valid_mask[j - 1][i] and valid_mask[j + 1][i]
            )
            if not neighbors_valid or cell_x[j] == 0 or cell_y == 0:
                continue
            dzdx = (heights[j][i + 1] - heights[j][i - 1]) / (2 * cell_x[j])
            dzdy = (heights[j + 1][i] - heights[j - 1][i]) / (2 * cell_y)
            slopes.append(math.degrees(math.atan(math.hypot(dzdx, dzdy))))
    steep = sum(1 for s in slopes if s > 25) / len(slopes) * 100 if slopes else 0
    status = "ok" if coverage == 1 else "degraded"
    result = {
        "elev_min": round(min(flat), 1), "elev_max": round(max(flat), 1),
        "elev_mean": round(sum(flat) / len(flat), 1),
        "relief": round(max(flat) - min(flat), 1),
        "slope_mean": round(sum(slopes) / len(slopes), 1) if slopes else None,
        "slope_max": round(max(slopes), 1) if slopes else None,
        "steep_pct_over25": round(steep, 1),
        **base,
        "quality": {"status": status, "coverage": coverage},
    }
    # 覆盖率达标后落图；降级结果保留明确质量标记
    if task_id:
        feat = {"type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[
                    [bbox[0], bbox[1]], [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]], [bbox[0], bbox[3]], [bbox[0], bbox[1]]]]},
                "properties": {"ftype": "terrain", **result}}
        layer = store.save_layer(
            "DEM地形统计", "terrain", feat, task_id=task_id,
            props={"source": "dem"},
            idempotency_key=(f"{idempotency_key}:stats" if idempotency_key else None))
        result["layer_id"] = layer["id"]
    return result


@register("dem_elevation", "查询给定点列海拔(米)，用于为检测目标补充高程信息",
          "points: [[lon,lat],...] 最多20点", effect="write")
def dem_elevation(points: list, task_id: str | None = None,
                  idempotency_key: str | None = None):
    """查询给定点列海拔（米）。"""
    points = points[:20]
    if not DEM_MBTILES.exists():
        return {"error": f"{DEM_MBTILES.name} 未就绪"}
    n = 2 ** DEM_Z
    results = []
    for lon, lat in points:
        x_f = (lon + 180) / 360 * n
        lat_r = math.radians(lat)
        y_f = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n
        tx, ty = int(x_f), int(y_f)
        data = _read_tile(DEM_Z, tx, ty)
        if not data:
            results.append({"lon": lon, "lat": lat, "elev": None})
            continue
        img = Image.open(BytesIO(data)).convert("RGB")
        r, g, b, *_ = img.getpixel((int((x_f - tx) * 255), int((y_f - ty) * 255)))
        results.append({"lon": lon, "lat": lat, "elev": round(r * 256 + g + b / 256 - 32768, 1)})
    # 落图：高程点，点击弹出海拔
    if task_id:
        feats = [{"type": "Feature",
                  "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                  "properties": {"ftype": "terrain_point",
                                 "description": f"高程 {p['elev']} m" if p["elev"] is not None else "无 DEM 数据"}}
                 for p in results]
        layer = store.save_layer(
            "DEM高程点", "terrain_point",
            {"type": "FeatureCollection", "features": feats},
            task_id=task_id, props={"source": "dem"},
            idempotency_key=(f"{idempotency_key}:points" if idempotency_key else None))
        return {"points": results, "zoom": DEM_Z, "layer_id": layer["id"]}
    return {"points": results, "zoom": DEM_Z}
