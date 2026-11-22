#!/bin/bash
# 观澜全栈停止。用法：bash ~/geoint-buddy/scripts/stop_all.sh
pkill -f 'uvicorn app.main' 2>/dev/null && echo "uvicorn 已停止" || echo "uvicorn 未运行"
pkill -f 'llama-server.*--port 8081' 2>/dev/null && echo "qwen3vl(8081) 已停止" || echo "qwen3vl 未运行"
pkill -f 'llama-server.*--port 8082' 2>/dev/null && echo "gemma4(8082) 已停止" || echo "gemma4 未运行"
