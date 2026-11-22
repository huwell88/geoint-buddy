"""指定区域 DEM 下载：AWS Terrain Tiles (Terrarium) → mbtiles。

Terrarium 编码：高程(米) = R*256 + G + B/256 - 32768，全球 SRTM/ASTER 30m 级，免 key。
用法：python3 download_dem.py [--bbox 98.66,31.05,98.74,31.11] [--zmin 12] [--zmax 14]
"""
import argparse
import sqlite3
import time
from pathlib import Path

import requests

BBOX = (98.66, 31.05, 98.74, 31.11)   # 白格滑坡区域（与影像瓦片一致）
URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
HEADERS = {"User-Agent": "guanlan-dem/0.1 (disaster emergency research)"}


def deg2tile(lon, lat, z):
    import math
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    lat_r = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * n)
    return x, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=",".join(str(v) for v in BBOX))
    ap.add_argument("--zmin", type=int, default=12)
    ap.add_argument("--zmax", type=int, default=14)
    ap.add_argument("--out", default="dem.mbtiles")
    ap.add_argument("--name", default="offline-dem")
    ap.add_argument("--description", default="AWS Terrain Tiles Terrarium DEM")
    args = ap.parse_args()
    bbox = [float(v) for v in args.bbox.split(",")]
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        ap.error("--bbox 必须为 minLon,minLat,maxLon,maxLat")

    out = Path(args.out)
    conn = sqlite3.connect(out)
    conn.execute("CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS tile_index ON tiles (zoom_level, tile_column, tile_row)")
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    conn.execute("DELETE FROM metadata")
    metadata = {
        "format": "png", "name": args.name, "type": "baselayer",
        "description": args.description, "bounds": ",".join(str(v) for v in bbox),
        "minzoom": str(args.zmin), "maxzoom": str(args.zmax),
    }
    conn.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())

    total = 0
    for z in range(args.zmin, args.zmax + 1):
        x0, y1 = deg2tile(bbox[0], bbox[1], z)
        x1, y0 = deg2tile(bbox[2], bbox[3], z)
        n_missing = 0
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                tms_y = (2 ** z - 1) - y          # mbtiles TMS 翻转
                r = requests.get(URL.format(z=z, x=x, y=y), headers=HEADERS, timeout=30)
                if r.status_code != 200:
                    n_missing += 1
                    continue
                conn.execute("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", (z, x, tms_y, r.content))
                total += 1
                time.sleep(0.1)
        conn.commit()
        print(f"z{z}: 完成 ({x1 - x0 + 1}x{y1 - y0 + 1} 网格), 缺失 {n_missing}")
    print(f"共 {total} 片 -> {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
