"""指定区域离线瓦片下载：Esri World Imagery → mbtiles。

用法：python3 download_tiles.py [--bbox 98.62,31.00,98.78,31.16] [--zmin 12] [--zmax 16]
Esri 瓦片无需 key；mbtiles 规范 y 为 TMS 行号（与 slippy 翻转）。
"""
import argparse
import math
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

# 金沙江白格滑坡（西藏江达县波罗乡，2018-10-11）周边区域
BAIGE_BBOX = [98.62, 31.00, 98.78, 31.16]  # [minLon,minLat,maxLon,maxLat]
TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
HEADERS = {"User-Agent": "geoint-buddy-tilefetch/0.2"}


def deg2tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat_r = math.radians(max(-85.0511, min(85.0511, lat)))
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * n)


def init_mbtiles(path: Path, bbox: list[float], zmin: int, zmax: int,
                 name: str, description: str) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    conn.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    meta = {
        "name": name, "type": "baselayer", "version": "1",
        "format": "jpg", "description": description,
        "bounds": ",".join(str(v) for v in bbox),
        "center": f"{cx},{cy},{zmin}",
        "minzoom": str(zmin), "maxzoom": str(zmax),
    }
    conn.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())
    return conn


def fetch(z: int, x: int, y: int, retries: int = 3) -> tuple[int, int, int, bytes | None]:
    for attempt in range(retries):
        try:
            r = requests.get(TILE_URL.format(z=z, y=y, x=x), headers=HEADERS, timeout=20)
            if r.status_code == 200 and len(r.content) > 1000:
                tms_y = (2 ** z - 1) - y  # TMS 翻转
                return z, x, tms_y, r.content
        except requests.RequestException:
            pass
        time.sleep(0.5 * (attempt + 1))
    return z, x, y, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=",".join(str(v) for v in BAIGE_BBOX))
    ap.add_argument("--zmin", type=int, default=12)
    ap.add_argument("--zmax", type=int, default=16)
    ap.add_argument("--out", default="baige.mbtiles")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--name", default="offline-imagery")
    ap.add_argument("--description", default="Esri World Imagery offline tiles")
    args = ap.parse_args()
    bbox = [float(v) for v in args.bbox.split(",")]
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        ap.error("--bbox 必须为 minLon,minLat,maxLon,maxLat")

    tiles = []
    for z in range(args.zmin, args.zmax + 1):
        x1, y2 = deg2tile(bbox[0], bbox[1], z)
        x2, y1 = deg2tile(bbox[2], bbox[3], z)
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for y in range(min(y1, y2), max(y1, y2) + 1):
                tiles.append((z, x, y))
    total = len(tiles)
    print(f"bbox={bbox} z{args.zmin}-{args.zmax} 共 {total} 瓦片", flush=True)

    out = Path(args.out)
    conn = init_mbtiles(out, bbox, args.zmin, args.zmax, args.name, args.description)
    done = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(fetch, z, x, y): (z, x, y) for z, x, y in tiles}
        batch = []
        for fut in as_completed(futs):
            z, x, tms_y, data = fut.result()
            if data:
                batch.append((z, x, tms_y, data))
            else:
                fail += 1
            done += 1
            if len(batch) >= 200:
                conn.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", batch)
                conn.commit()
                batch.clear()
            if done % 200 == 0 or done == total:
                rate = done / max(time.time() - t0, 0.1)
                print(f"\r{done}/{total} ({rate:.0f}/s, 失败 {fail})", end="", flush=True)
        if batch:
            conn.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", batch)
            conn.commit()
    conn.execute("VACUUM")
    conn.close()
    size_mb = out.stat().st_size / 1e6
    print(f"\n完成：{out} {size_mb:.1f}MB，成功 {total - fail}/{total}")
    sys.exit(1 if fail > total * 0.05 else 0)


if __name__ == "__main__":
    main()
