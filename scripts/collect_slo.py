#!/usr/bin/env python3
"""板端任务级 SLO、资源占用与重复运行稳定性采集。

只调用真实 HTTP API，不注入 mock 结果。每轮新建任务并使用区域 demo_aoi，
保存完整 SSE 事件、/proc 资源样本和描述性汇总；小样本不用于宣称稳定 SLO。

板端用法：
python3 scripts/collect_slo.py --rounds 3 --scenario risk --engine qwen3vl
python3 scripts/collect_slo.py --rounds 3 --scenario all
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.request

SCENARIOS = {
    "area": "测算圈选区域面积。",
    "imagery": "判读圈选区域影像中的滑坡迹象。",
    "risk": "综合研判该区域滑坡灾害风险，需要影像和地形证据。",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def api_request(base, path, body=None, timeout=30):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def api_json(base, path, body=None, timeout=30):
    with api_request(base, path, body, timeout) as response:
        return json.load(response)


def percentile(values, q):
    """最近秩百分位，不把小样本插值成看似精确的结果。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def latency_stats(values):
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 3) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def summarize_runs(runs):
    n = len(runs)
    completed = [r for r in runs if r.get("completed")]
    clean = [r for r in runs if r.get("clean")]
    gaps = [r for r in runs if r.get("evidence_gap")]
    llm_rounds = [u for r in runs for u in r.get("llm_rounds", [])]
    completion_tokens = sum((u.get("completion_tokens") or 0) for u in llm_rounds)
    prompt_tokens = sum((u.get("prompt_tokens") or 0) for u in llm_rounds)
    llm_elapsed = sum((u.get("elapsed_s") or 0) for u in llm_rounds)
    return {
        "n": n,
        "completed": len(completed),
        "completion_rate": len(completed) / n if n else None,
        "clean_completed": len(clean),
        "clean_completion_rate": len(clean) / n if n else None,
        "evidence_gap_runs": len(gaps),
        "evidence_gap_rate": len(gaps) / n if n else None,
        "e2e_latency_s": latency_stats([r["elapsed_s"] for r in runs
                                        if isinstance(r.get("elapsed_s"), (int, float))]),
        "completed_e2e_latency_s": latency_stats([r["elapsed_s"] for r in completed]),
        "llm": {
            "calls": len(llm_rounds), "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "elapsed_s": round(llm_elapsed, 3),
            "completion_tokens_per_s": round(completion_tokens / llm_elapsed, 3)
            if llm_elapsed else None,
        },
        "definitions": {
            "completed": "收到 final 且没有全局 error；不代表结论正确。",
            "clean_completed": "completed，且没有 tool_error 或 evidence_gap。",
            "latency": "客户端从创建任务前计时到 SSE 流结束；百分位采用最近秩。",
        },
        "interpretation": "小样本仅描述本次运行，不用于宣称统计显著或长期稳定 SLO。"
        if n < 20 else "仅描述本次实测窗口，不外推其他负载与硬件。",
    }


def _stats(values, digits=2):
    if not values:
        return {"mean": None, "max": None}
    return {"mean": round(sum(values) / len(values), digits),
            "max": round(max(values), digits)}


def summarize_resources(samples):
    valid = [s for s in samples if s.get("supported")]
    memory = [s["memory"]["used_mb"] for s in valid if s.get("memory")]
    cpu = [s["system_cpu_pct"] for s in valid
           if isinstance(s.get("system_cpu_pct"), (int, float))]
    processes = {}
    for sample in valid:
        for proc in sample.get("processes", []):
            item = processes.setdefault(proc["key"], {"cmdline": proc["cmdline"],
                                                      "rss_mb": [], "cpu_pct": []})
            item["rss_mb"].append(proc["rss_mb"])
            if isinstance(proc.get("cpu_pct"), (int, float)):
                item["cpu_pct"].append(proc["cpu_pct"])
    return {
        "supported": bool(valid), "sample_count": len(samples),
        "system_cpu_pct": _stats(cpu), "system_memory_used_mb": _stats(memory),
        "processes": {key: {"cmdline": value["cmdline"],
                            "rss_mb": _stats(value["rss_mb"]),
                            "cpu_pct": _stats(value["cpu_pct"])}
                      for key, value in processes.items()},
        "note": "CPU 首样本为 None；进程 CPU 以单核 100% 为基准，RSS 为常驻内存。",
    }


class ResourceSampler:
    """Linux /proc 采样，无需新增依赖；非 Linux 会明确记录 unsupported。"""

    def __init__(self, interval=1.0, process_regex=r"llama-server|uvicorn"):
        self.interval = interval
        self.pattern = re.compile(process_regex)
        self.samples = []
        self.previous_system = None
        self.previous_processes = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def _cpu_times():
        fields = [float(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        return sum(fields), idle

    @staticmethod
    def _memory():
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = float(value.strip().split()[0]) / 1024
        total, available = values["MemTotal"], values["MemAvailable"]
        return {"total_mb": round(total, 2), "available_mb": round(available, 2),
                "used_mb": round(total - available, 2)}

    def snapshot(self):
        if not Path("/proc/stat").exists():
            return {"ts": time.time(), "supported": False}
        sample = {"ts": time.time(), "supported": True, "memory": self._memory(),
                  "load1": float(Path("/proc/loadavg").read_text().split()[0]),
                  "system_cpu_pct": None, "processes": []}
        total, idle = self._cpu_times()
        if self.previous_system:
            old_total, old_idle = self.previous_system
            delta, idle_delta = total - old_total, idle - old_idle
            if delta > 0:
                sample["system_cpu_pct"] = round(100 * (1 - idle_delta / delta), 2)
        current = {}
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            try:
                cmdline = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
                if not self.pattern.search(cmdline):
                    continue
                stat = (proc_dir / "stat").read_text().rsplit(") ", 1)[1].split()
                proc_ticks = int(stat[11]) + int(stat[12])
                rss_mb = int(stat[21]) * os.sysconf("SC_PAGE_SIZE") / 1024 ** 2
                key = f"{proc_dir.name}:{cmdline}"
                current[key] = (total, proc_ticks)
                cpu_pct = None
                if key in self.previous_processes:
                    old_total, old_ticks = self.previous_processes[key]
                    if total > old_total:
                        cpu_pct = round((proc_ticks - old_ticks) / (total - old_total)
                                        * (os.cpu_count() or 1) * 100, 2)
                sample["processes"].append({"key": key, "pid": int(proc_dir.name),
                                            "cmdline": cmdline, "rss_mb": round(rss_mb, 2),
                                            "cpu_pct": cpu_pct})
            except (OSError, ValueError, IndexError):
                continue
        self.previous_system = (total, idle)
        self.previous_processes = current
        return sample

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.samples.append(self.snapshot())
            except OSError as exc:
                self.samples.append({"ts": time.time(), "supported": False,
                                     "error": f"{type(exc).__name__}: {exc}"})
            self.stop_event.wait(self.interval)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()


def bbox_geometry(bbox):
    return {"type": "Polygon", "coordinates": [[
        [bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]],
        [bbox[0], bbox[3]], [bbox[0], bbox[1]]]]}


def run_task(base, scenario, message, aoi, run_index, timeout):
    started = time.perf_counter()
    record = {"run": run_index, "scenario": scenario, "message": message,
              "started_at": utc_now(), "task_id": None, "events": [],
              "completed": False, "clean": False, "error": None}
    try:
        task = api_json(base, "/api/tasks", {
            "title": f"SLO {scenario} #{run_index}",
            "aoi_geojson": bbox_geometry(aoi)}, timeout=30)
        record["task_id"] = task["id"]
        with api_request(base, "/api/chat", {"task_id": task["id"], "message": message},
                         timeout=timeout) as response:
            while True:
                line = response.readline()
                if not line:
                    break
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:].decode())
                record["events"].append(event)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["elapsed_s"] = round(time.perf_counter() - started, 3)
    events = record["events"]
    finals = [e for e in events if e.get("kind") == "final"]
    errors = [e for e in events if e.get("kind") == "error"]
    tool_errors = [e for e in events if e.get("kind") == "tool_error"]
    gaps = [e for e in events if e.get("kind") == "evidence_gap"]
    record["llm_rounds"] = [(e.get("usage") or {}) for e in events if e.get("kind") == "round"]
    record["tool_results"] = [{"tool": e.get("tool"), "elapsed_s": e.get("elapsed_s"),
                               "cached": e.get("cached"), "quality": e.get("quality")}
                              for e in events if e.get("kind") == "tool_result"]
    record["evidence_gap"] = gaps[0].get("gap") if gaps else None
    record["final"] = finals[-1] if finals else None
    record["completed"] = bool(finals and not errors and not record["error"])
    record["clean"] = bool(record["completed"] and not tool_errors and not gaps)
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--rounds", type=int, default=3, help="每个场景的独立任务数")
    ap.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="risk")
    ap.add_argument("--engine", help="可选：运行期间切换到指定端侧引擎，结束后恢复")
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--sample-interval", type=float, default=1.0)
    ap.add_argument("--process-regex", default=r"llama-server|uvicorn")
    ap.add_argument("--output-dir")
    args = ap.parse_args()
    if args.rounds < 1 or args.timeout <= 0 or args.sample_interval <= 0:
        ap.error("--rounds 必须 >= 1，--timeout 与 --sample-interval 必须 > 0")

    health = api_json(args.base, "/api/health")
    region = api_json(args.base, "/api/region")
    engines = api_json(args.base, "/api/engines")
    started = datetime.now(timezone.utc)
    run_id = started.strftime("slo_%Y%m%dT%H%M%S_%fZ")
    output_dir = Path(args.output_dir) if args.output_dir else Path("eval-results") / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"started_at": started.isoformat(), "script": str(Path(__file__).resolve()),
                "script_sha256": file_hash(__file__), "args": vars(args),
                "initial_health": health, "initial_engines": engines, "region": region,
                "offline_scope": "设置应用 OFFLINE；不据此宣称物理断网。"}
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    sampler = ResourceSampler(args.sample_interval, args.process_regex)
    scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    runs = []
    sampler.start()
    try:
        api_json(args.base, "/api/network", {"state": "OFFLINE"})
        if args.engine:
            api_json(args.base, "/api/engine", {"engine": args.engine})
        with (output_dir / "runs.jsonl").open("w", encoding="utf-8") as run_file:
            index = 0
            for scenario in scenarios:
                for _ in range(args.rounds):
                    index += 1
                    record = run_task(args.base, scenario, SCENARIOS[scenario],
                                      region["demo_aoi"], index, args.timeout)
                    runs.append(record)
                    run_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                    run_file.flush()
                    print(f"[{index}/{len(scenarios) * args.rounds}] {scenario} "
                          f"elapsed={record['elapsed_s']}s completed={record['completed']} "
                          f"clean={record['clean']}", flush=True)
    finally:
        sampler.stop()
        with (output_dir / "resources.jsonl").open("w", encoding="utf-8") as resource_file:
            for sample in sampler.samples:
                resource_file.write(json.dumps(sample, ensure_ascii=False) + "\n")
        try:
            api_json(args.base, "/api/network", {"state": health.get("network_state", "ONLINE")})
            if args.engine and engines.get("current"):
                api_json(args.base, "/api/engine", {"engine": engines["current"]})
        except Exception as exc:
            manifest["restore_error"] = f"{type(exc).__name__}: {exc}"
            (output_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {"finished_at": utc_now(), "region_id": region["id"],
               "requested_engine": args.engine or engines.get("current"),
               "runs": summarize_runs(runs), "resources": summarize_resources(sampler.samples)}
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"结果目录：{output_dir.resolve()}", flush=True)
    if runs and summary["runs"]["completion_rate"] < 1:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
