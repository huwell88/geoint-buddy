# 观澜 · 灾害应急遥感智能体（GEOINT Buddy）

离线优先的灾害应急遥感研判智能体，全程运行于**此芯 P1 开发板（CIX CD8180，12 核 CPU / 32GB RAM）**。
断网场景下，单板完成遥感影像判读、DEM 地形分析、量测缓冲与多轮研判，无需任何云端依赖。

- **赛道**：端侧本地推理智能体
- **演示区**：金沙江白格滑坡（2018-10-11，西藏江达县波罗乡）
- **三端侧引擎**（llama.cpp，OpenAI 兼容）：
  - Qwen3VL-4B（8081）：主力多模态判读，约 12 tok/s
  - Gemma4-E4B（8082）：思考模型，精细判读
  - 观澜·白格微调版（8083）：白格滑坡 LoRA 微调，16K 上下文；冻结对照中滑坡参照命中增加，无灾区误报也增加

## 架构

```
┌─ 前端 MapLibre 地图 + SSE 流式对话 ─────────────────┐
│  步骤(step)/工具调用/质量(quality)/图层(artifacts)事件 │
└─────────────────────┬───────────────────────────┘
┌─────────────────────┴───────────────────────────┐
│ FastAPI Agent（混合编排）                          │
│  · 规划 LLM → 意图识别（chat/area/terrain/…）      │
│  · 证据缺口状态机 → 仅执行缺失证据的标准工具         │
│  · CaseState 任务级记忆（跨追问复用，SQLite 持久化） │
│  · 自定义 ReAct 兜底（〔TOOL〕/〔OBS〕/〔FINAL〕）   │
│  · 三重预算：steps / tool_calls / llm_calls        │
│  · 总结 LLM 只读紧凑 CaseState，不读审计消息         │
├──────────────────────────────────────────────────┤
│ 工具层：vlm_inspect / dem_stats / geo_area /       │
│  geo_buffer / aoi_stats / save_layer / list_layers │
│  统一结果信封 {status, data, quality, artifacts}    │
│  幂等键落图，DEM 覆盖率质量门控（ok/degraded/…）     │
└──────────────────────────────────────────────────┘
```

## 部署与复现（此芯 P1 / Debian 系）

仓库不包含 GGUF 模型、遥感瓦片、DEM 或真实 `.env`。以下步骤分别准备推理运行时、模型、离线数据和后端；模型及数据须遵守各自原始许可。

### 1. 编译 llama.cpp CPU 后端

当前性能基准使用 llama.cpp `2a667032d`。已有可用 `llama-server` 时可跳过本步。

```bash
sudo apt update
sudo apt install -y git cmake build-essential python3-venv

git clone https://github.com/ggml-org/llama.cpp.git ~/local-llm-test/llama.cpp
git -C ~/local-llm-test/llama.cpp checkout 2a667032d
cmake -S ~/local-llm-test/llama.cpp \
      -B ~/local-llm-test/llama.cpp/build-cpu \
      -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON
cmake --build ~/local-llm-test/llama.cpp/build-cpu \
      -j"$(nproc)" --target llama-server llama-bench
```

### 2. 准备模型文件

从对应模型的官方发布渠道获取文件，并统一放到 `~/models/`：

| 文件 | 用途 |
|---|---|
| `Qwen3VL-4B-Instruct-Q4_K_M.gguf` | 8081 主力模型 |
| `mmproj-Qwen3VL-4B-Instruct-F16.gguf` | Qwen3VL 视觉投影 |
| `gemma-4-E4B-it-IQ4_NL.gguf` | 8082 备用模型 |
| `mmproj-F32.gguf` | Gemma 视觉投影 |
| `qwen3vl-4b-baige-Q4_K_M.gguf` | 8083 可选微调模型 |

模型体积较大，不进入 Git。部署后记录实际文件哈希，便于复核版本：

```bash
sha256sum ~/models/*.gguf
```

### 3. 安装后端并准备离线数据

```bash
cd ~/geoint-buddy
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example backend/.env   # 按需修改；OFFLINE 无需云端 key

# 数据下载需要外网；输出到 .gitignore 排除的运行时目录
backend/.venv/bin/python scripts/download_tiles.py \
  --out backend/data/tiles/baige.mbtiles
backend/.venv/bin/python scripts/download_dem.py \
  --out backend/data/tiles/dem.mbtiles
```

区域由 `REGION_PROFILE` 指定；默认使用 `regions/baige.json`。切换区域还需准备该配置引用的影像和 DEM 文件。

### 4. 一键启动与检查

```bash
# 启动 8081/8082 llama-server 和 8000 FastAPI
bash ~/geoint-buddy/scripts/start_all.sh

# 可选：启动 8083 微调引擎
LD_LIBRARY_PATH=/usr/local/gcc-13/lib64 setsid nohup \
  ~/local-llm-test/llama.cpp/build-cpu/bin/llama-server \
  -m ~/models/qwen3vl-4b-baige-Q4_K_M.gguf \
  --mmproj ~/models/mmproj-Qwen3VL-4B-Instruct-F16.gguf \
  -t 10 -c 16384 --port 8083 > ~/llama-server-baige.log 2>&1 &

curl http://127.0.0.1:8000/api/health
curl -X POST localhost:8000/api/network \
  -H 'Content-Type: application/json' -d '{"state":"OFFLINE"}'
# 可选：切换微调引擎，也可在页面右上角操作
curl -X POST localhost:8000/api/engine \
  -H 'Content-Type: application/json' -d '{"engine":"qwen3vl_ft"}'
```

浏览器访问 `http://<板卡IP>:8000`，圈选 AOI 后开始研判。`OFFLINE` 是应用层设置，不等同于物理断网；物理断网演练应手动断开外部网络。

停止：`bash ~/geoint-buddy/scripts/stop_all.sh`

## 测试

```bash
# 离线单测（记忆复用/幂等/DEM 质量门控/预算/协议，无需模型）
PYTHONPATH=backend backend/.venv/bin/python3 scripts/test_agent_context.py

# 板端真实回归（同 AOI 6 轮追问、跨追问证据复用、幂等落图、DEM 质量，自动清理）
backend/.venv/bin/python3 scripts/test_agent_e2e.py

# 真实任务级 SLO：面积/影像/风险各 3 轮，保存完整 SSE 与 /proc 资源样本
backend/.venv/bin/python3 scripts/collect_slo.py \
  --rounds 3 --scenario all --engine qwen3vl
```

## 实测结果

基础 llama.cpp 基准见 [docs/benchmark.md](docs/benchmark.md)。不同实验回答不同问题，不能用 tok/s、运行完成率或区域迁移替代模型精度评价。

| 指标 | 实测结果 |
|---|---|
| Qwen3VL-4B 解码速度（8 线程） | 12.65 tok/s |
| 上下文治理：prompt 峰值 | 8254 → 2334 tok（-72%） |
| 六轮多轮研判 | 6/6 完成，无 400/超时 |
| 真实任务级 SLO（面积/影像/风险各 3 轮） | 9/9 completed、9/9 clean、0 证据缺口 |
| 端到端时延 | 均值 76.214 s；P50 57.963 s；最近秩 P95/max 158.120 s |
| SLO 资源占用（665 个样本） | CPU 均值 74.53%、峰值 90.76%；内存 used 均值 15.77 GB、峰值 16.08 GB |
| 泸定区域迁移 | Agent 零改代码；影像/DEM 均 60/60，missing=0、coverage=1.0；白格/泸定各 27 项测试通过 |

### 基座 / 微调冻结对照

2026-11-22 冻结 3 个 AOI 的影像、提示词与生成参数，基座和微调引擎各运行 2 轮，共 12 次真实推理。滑坡区使用单点参照；无灾区以输出疑似目标的轮次统计误报；堰塞湖区没有独立真值，只描述输出。

| 区域 / 指标 | 基座 8081 | 微调 8083 |
|---|---:|---:|
| 滑坡区单点参照命中 | 0/2 | 2/2 |
| 无灾区误报轮次 | 0/2 | 2/2 |
| 无灾区 findings 总数 | 0 | 8 |
| JSON 解析成功 | 6/6 | 6/6 |

逐轮分类为 `base_better=2`、`ft_better=2`、`result_disagreement=2`，其余持平。结果同时体现检出敏感性收益和误报代价，不能概括为“精度显著提升”。

### 证据边界

- 每引擎每 AOI 仅 2 轮；疑似滑坡不等于现场确认，小样本不用于统计显著性推断。
- 训练集完全重复审计为 0，仅排除完全图像、提示词和范围重复，不等于空间隔离或独立泛化验证。
- SLO 的 P95 使用最近秩，仅描述本次 9 轮；`completed` 表示收到 final 且无全局错误，不代表结论正确。
- 泸定结果证明配置、数据路径和工具链可迁移，不证明跨区域模型判读精度。
- 当前实测使用 CPU 推理；NPU 适配不作为已启用能力声明。

## 目录

```
backend/app/     FastAPI + Agent（agent.py 混合编排 / case.py 任务记忆 / store.py SQLite）
backend/app/tools/  vlm / dem / geo 工具（统一结果信封 + 幂等）
frontend/        MapLibre 前端（SSE 步骤/质量/图层事件）
scripts/         一键启停、数据下载、单测与板端回归
docs/            性能基准
```
