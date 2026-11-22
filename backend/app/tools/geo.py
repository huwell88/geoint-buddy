"""地理计算工具：距离、面积、缓冲区、AOI 统计。基于 pyproj + shapely。"""
import json

from pyproj import Geod
from shapely.geometry import shape, mapping, box
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from . import register
from .. import store

_GEOD = Geod(ellps="WGS84")
_TO_MERC = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
_TO_WGS = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform


def _area_m2(geom) -> float:
    area, _ = _GEOD.geometry_area_perimeter(geom)
    return abs(area)


def _bbox_geom(bbox: list[float]):
    return box(bbox[0], bbox[1], bbox[2], bbox[3])


@register("geo_distance", "计算两点间大地线距离",
          'p1: [经度,纬度]；p2: [经度,纬度]')
def geo_distance(p1: list[float], p2: list[float]) -> dict:
    _, _, dist = _GEOD.inv(p1[0], p1[1], p2[0], p2[1])
    return {"distance_m": round(dist, 1), "distance_km": round(dist / 1000, 3)}


@register("geo_area", "计算 GeoJSON 多边形或 bbox 的面积",
          'bbox: [minLon,minLat,maxLon,maxLat]，与 geojson 二选一；geojson: Polygon/Feature 对象')
def geo_area(bbox: list[float] | None = None, geojson: dict | None = None) -> dict:
    if geojson:
        geom = shape(geojson["geometry"] if geojson.get("type") == "Feature" else geojson)
    elif bbox:
        geom = _bbox_geom(bbox)
    else:
        raise ValueError("必须提供 bbox 或 geojson")
    a = _area_m2(geom)
    return {"area_m2": round(a, 1), "area_km2": round(a / 1e6, 4)}


@register("geo_buffer", "对 GeoJSON 要素做圆形缓冲区（米），返回缓冲后 GeoJSON 与面积",
          'geojson: Point/LineString/Polygon 或 Feature；radius_m: 缓冲半径（米）')
def geo_buffer(geojson: dict, radius_m: float) -> dict:
    geom = shape(geojson["geometry"] if geojson.get("type") == "Feature" else geojson)
    buffered = shp_transform(_TO_WGS, shp_transform(_TO_MERC, geom).buffer(radius_m))
    gj = mapping(buffered)
    a = _area_m2(buffered)
    return {"geojson": gj, "area_m2": round(a, 1),
            "area_km2": round(a / 1e6, 4)}


@register("aoi_stats", "统计圈选区域（AOI）的面积、周长、中心点与对角距离",
          'bbox: [minLon,minLat,maxLon,maxLat]')
def aoi_stats(bbox: list[float]) -> dict:
    geom = _bbox_geom(bbox)
    area, perim = _GEOD.geometry_area_perimeter(geom)
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    _, _, diag = _GEOD.inv(bbox[0], bbox[1], bbox[2], bbox[3])
    return {
        "center": [round(cx, 6), round(cy, 6)],
        "area_m2": round(abs(area), 1),
        "area_km2": round(abs(area) / 1e6, 4),
        "perimeter_km": round(abs(perim) / 1000, 2),
        "diagonal_km": round(diag / 1000, 2),
        "bbox": bbox,
    }


@register("save_layer", "将任意 GeoJSON 要素落图持久化（前端地图自动渲染）",
          'name: 图层名；kind: aoi/detection/buffer/marker；geojson: GeoJSON 几何或 Feature；props: 可选附加属性对象',
          effect="write")
def save_layer_tool(name: str, kind: str, geojson: dict, props: dict | None = None,
                    task_id: str | None = None, idempotency_key: str | None = None) -> dict:
    layer = store.save_layer(name, kind, geojson, task_id=task_id, props=props,
                             idempotency_key=idempotency_key)
    return {**layer, "layer_id": layer["id"]}


@register("list_layers", "列出当前已保存的图层（含检测与 AOI）",
          'task_id: 可选，按任务过滤', cache=False)
def list_layers_tool(task_id: str | None = None) -> list[dict]:
    layers = store.list_layers(task_id)
    # 摘要不回传完整 geojson，避免爆上下文
    return [{"id": l["id"], "name": l["name"], "kind": l["kind"],
             "props": l.get("props")} for l in layers]
