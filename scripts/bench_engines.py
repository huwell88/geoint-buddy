# -*- coding: utf-8 -*-
"""三引擎端点实测：TTFT、tokens/s、内存驻留（此芯 P1 板端运行）。"""
import json
import subprocess
import time
import urllib.request

BASE = "http://127.0.0.1"
PROMPT = "用一句话介绍金沙江白格滑坡的地理背景。"
N_PREDICT = 128


def bench(port, model, label):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": N_PREDICT,
        "temperature": 0.3,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{BASE}:{port}/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as resp:
        data = json.load(resp)
    wall = time.time() - t0
    u = data.get("usage", {})
    ct = u.get("completion_tokens", 0)
    pt = u.get("prompt_tokens", 0)
    return {
        "engine": label, "port": port,
        "prompt_tokens": pt, "completion_tokens": ct,
        "wall_s": round(wall, 2),
        "tok_per_s": round(ct / wall, 2) if wall else None,
        "total_s_per_tok": round(wall / ct, 3) if ct else None,
    }


def rss_mb(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return round(int(out) / 1024, 0) if out else None
    except Exception:
        return None


def main():
    results = []
    for port, model, label in [
        (8081, "qwen3vl-4b-local", "Qwen3VL-4B"),
        (8082, "gemma-4-E4B-it", "Gemma4-E4B"),
        (8083, "qwen3vl-4b-baige-ft", "观澜·白格微调版"),
    ]:
        try:
            r = bench(port, model, label)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            results.append(r)
        except Exception as exc:
            print(f"[FAIL] {label}: {exc}", flush=True)

    print("--- llama-server 进程驻留 ---", flush=True)
    out = subprocess.run(
        ["ps", "-eo", "pid,rss,args"], capture_output=True, text=True, timeout=5).stdout
    total = 0
    for line in out.splitlines():
        if "llama-server" in line and "--port" in line:
            parts = line.split()
            pid, rss = parts[0], int(parts[1]) // 1024
            port = line.split("--port")[1].split()[0]
            total += rss
            print(f"port={port} pid={pid} rss={rss}MB", flush=True)
    print(f"模型总驻留 ≈ {total}MB / 32768MB", flush=True)
    print("--- uvicorn ---", flush=True)
    for line in out.splitlines():
        if "uvicorn app.main:app" in line:
            parts = line.split()
            print(f"uvicorn pid={parts[0]} rss={int(parts[1]) // 1024}MB", flush=True)
    print(json.dumps({"bench": results}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
