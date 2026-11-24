#!/bin/bash
# 观澜全栈启动：双 llama-server（Qwen3VL 8081 / Gemma4 8082）+ uvicorn 8000。
# uvicorn 默认直连云端；需要代理时在启动前设置 HTTPS_PROXY。
# 用法：bash ~/geoint-buddy/scripts/start_all.sh
set -e

BIN="$HOME/local-llm-test/llama.cpp/build-cpu/bin/llama-server"
PROXY="${HTTPS_PROXY:-}"

up() { curl -s -m 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1 || curl -s -m 2 "http://127.0.0.1:$1/api/health" >/dev/null 2>&1; }

# 1) Qwen3VL 8081
if ! up 8081; then
  LD_LIBRARY_PATH=/usr/local/gcc-13/lib64 setsid nohup "$BIN" \
    -m "$HOME/models/Qwen3VL-4B-Instruct-Q4_K_M.gguf" \
    --mmproj "$HOME/models/mmproj-Qwen3VL-4B-Instruct-F16.gguf" \
    -t 10 -c 8192 --port 8081 > "$HOME/llama-server-qwen3vl.log" 2>&1 &
  echo "qwen3vl(8081) 启动中（模型加载约 30-60s）…"
else
  echo "✓ qwen3vl(8081) 已在运行"
fi

# 2) Gemma4 8082
if ! up 8082; then
  LD_LIBRARY_PATH=/usr/local/gcc-13/lib64 setsid nohup "$BIN" \
    -m "$HOME/models/gemma-4-E4B-it-IQ4_NL.gguf" \
    --mmproj "$HOME/models/mmproj-F32.gguf" \
    -t 10 -c 8192 --port 8082 > "$HOME/llama-server-gemma4.log" 2>&1 &
  echo "gemma4(8082) 启动中（模型加载约 30-60s）…"
else
  echo "✓ gemma4(8082) 已在运行"
fi

# 3) 后端（默认直连；HTTPS_PROXY 非空时使用上行代理）
if ! up 8000; then
  ( cd "$HOME/geoint-buddy/backend" &&
    env HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" NO_PROXY=127.0.0.1,localhost \
      setsid nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 \
      > "$HOME/geoint-buddy-uvicorn.log" 2>&1 & )
  echo "uvicorn(8000) 启动中（${PROXY:+代理 $PROXY}${PROXY:-直连}）…"
else
  echo "✓ uvicorn(8000) 已在运行"
fi

echo "--- 等待 10s 后复查 ---"
sleep 10
for p in 8000 8081 8082; do
  if up "$p"; then echo "✓ :$p 就绪"; else echo "… :$p 未就绪（稍后可重跑本脚本复查）"; fi
done
