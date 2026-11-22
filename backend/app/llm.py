"""端云协同 LLM 路由：云端 AnyInt 优先，失败自动回退端侧 llama.cpp。

所有调用记录 token 用量到 data/llm_usage.jsonl（用户要求记录 Token 消耗）。
"""
import json
import time
from pathlib import Path
from typing import Any

from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError
import requests

from .config import settings, DATA_DIR, edge_engine_conf

USAGE_LOG = DATA_DIR / "llm_usage.jsonl"


def _client(base_url: str, api_key: str, timeout: float) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)


def _log_usage(record: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with USAGE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class LLMError(Exception):
    """双链路均失败时抛出。"""


def count_message_tokens(messages: list[dict], engine: str | None = None) -> int | None:
    """用端侧 /tokenize 估算聊天提示词；不可用时由调用方安全降级。"""
    eng = edge_engine_conf(engine)
    parts = []
    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        parts.append(f"{message.get('role', 'user')}: {content}")
    url = eng["base_url"].removesuffix("/v1") + "/tokenize"
    try:
        resp = requests.post(url, json={"content": "\n".join(parts)}, timeout=5)
        resp.raise_for_status()
        tokens = resp.json().get("tokens")
        if isinstance(tokens, list):
            # 文本拼接不含聊天模板，为每条消息及生成提示预留开销。
            return len(tokens) + 8 * len(messages) + 64
    except (requests.RequestException, ValueError, TypeError):
        pass
    return None


def chat(
    messages: list[dict],
    *,
    prefer: str = "cloud",
    max_tokens: int | None = None,
    temperature: float = 0.3,
    purpose: str = "chat",
    engine: str | None = None,
) -> tuple[str, dict]:
    """调用聊天补全，返回 (内容, 用量信息)。

    prefer: "cloud" 云端优先失败回退端侧；"edge" 强制端侧。
    engine: 指定端侧引擎 id（qwen3vl/gemma4），空则用当前全局引擎。
    network_state=OFFLINE 时强制端侧。
    """
    if settings.network_state == "OFFLINE":
        prefer = "edge"

    eng = edge_engine_conf(engine)
    attempts: list[tuple[str, OpenAI, str, float, int]] = []
    if prefer == "cloud":
        attempts.append(("cloud", _client(settings.cloud_base_url, settings.cloud_api_key,
                                          settings.cloud_timeout), settings.cloud_model,
                         settings.cloud_timeout, max_tokens or settings.llm_max_tokens))
    attempts.append(("edge", _client(eng["base_url"], settings.edge_api_key,
                                     eng["timeout"]), eng["model"],
                     eng["timeout"], eng["max_tokens"]))

    errors: list[str] = []
    for side, client, model, _timeout, mtok in attempts:
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=mtok, temperature=temperature,
            )
            content = resp.choices[0].message.content or ""
            usage = {
                "side": side, "model": model, "purpose": purpose,
                **({"engine": eng["id"], "engine_name": eng["name"]} if side == "edge" else {}),
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "elapsed_s": round(time.time() - t0, 2),
            }
            _log_usage(usage)
            return content, usage
        except (APIConnectionError, APITimeoutError, APIStatusError) as e:
            errors.append(f"{side}: {type(e).__name__} {e}")
            _log_usage({"side": side, "model": model, "purpose": purpose,
                        "error": f"{type(e).__name__}: {e}",
                        "elapsed_s": round(time.time() - t0, 2)})
            continue
    raise LLMError("；".join(errors))


def chat_with_image(
    prompt: str,
    image_b64: str,
    *,
    prefer: str = "cloud",
    max_tokens: int = 1200,
    purpose: str = "vlm",
    engine: str | None = None,
) -> tuple[str, dict]:
    """多模态判读：图像 base64 + 文本问题。engine 可指定端侧引擎。"""
    if settings.network_state == "OFFLINE" or engine:
        prefer = "edge"   # 指定引擎即为端侧判读（如双引擎对比）

    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ],
    }]

    eng = edge_engine_conf(engine)
    attempts: list[tuple[str, OpenAI, str, int]] = []
    if prefer == "cloud":
        vlm_model = settings.cloud_vlm_model or settings.cloud_model
        attempts.append(("cloud", _client(settings.cloud_base_url, settings.cloud_api_key,
                                          settings.cloud_timeout), vlm_model, max_tokens))
    # 端侧按当前引擎路由；Gemma4 思考耗 token，预算取引擎自身值而非调用方上限
    attempts.append(("edge", _client(eng["base_url"], settings.edge_api_key,
                                     eng["timeout"]), eng["model"],
                     eng["max_tokens"]))

    errors: list[str] = []
    for side, client, model, mtok in attempts:
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, max_tokens=mtok, temperature=0.2,
            )
            content = resp.choices[0].message.content or ""
            usage = {
                "side": side, "model": model, "purpose": purpose,
                **({"engine": eng["id"], "engine_name": eng["name"]} if side == "edge" else {}),
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "elapsed_s": round(time.time() - t0, 2),
            }
            _log_usage(usage)
            return content, usage
        except (APIConnectionError, APITimeoutError, APIStatusError) as e:
            errors.append(f"{side}: {type(e).__name__} {e}")
            _log_usage({"side": side, "model": model, "purpose": purpose,
                        "error": f"{type(e).__name__}: {e}",
                        "elapsed_s": round(time.time() - t0, 2)})
            continue
    raise LLMError("；".join(errors))
