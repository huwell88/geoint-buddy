"""工具注册表：注册、调用、生成工具清单文本。"""
import inspect
from typing import Any, Callable

_REGISTRY: dict[str, dict[str, Any]] = {}


def register(name: str, description: str, params_doc: str,
             *, effect: str = "read", cache: bool = True):
    """装饰器：注册工具函数，并保存读写效果与缓存元数据。"""
    def deco(fn: Callable[..., Any]):
        _REGISTRY[name] = {
            "fn": fn, "description": description, "params_doc": params_doc,
            "effect": effect, "cache": cache,
        }
        return fn
    return deco


def cacheable(name: str) -> bool:
    """返回工具结果是否允许缓存；未注册工具交给 call 统一报错。"""
    return _REGISTRY.get(name, {}).get("cache", False)


def effect(name: str) -> str:
    """返回工具的读写效果。"""
    return _REGISTRY.get(name, {}).get("effect", "read")


def call(name: str, args: dict) -> dict[str, Any]:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的工具: {name}")
    fn = _REGISTRY[name]["fn"]
    idempotency_key = args.get("idempotency_key")
    # 按函数签名过滤多余参数（如 agent 注入的 task_id），无 **kwargs 时丢弃未声明参数
    sig = inspect.signature(fn)
    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        args = {k: v for k, v in args.items() if k in sig.parameters}
    raw = fn(**args)

    quality = raw.get("quality", {}) if isinstance(raw, dict) else {}
    error = raw.get("error") if isinstance(raw, dict) else None
    artifacts = []
    if isinstance(raw, dict):
        artifacts.extend(raw.get("layer_ids") or [])
        if raw.get("layer_id"):
            artifacts.append(raw["layer_id"])
    status = quality.get("status") or ("error" if error else "ok")
    return {
        "status": status,
        "data": raw,
        "quality": quality,
        "artifacts": artifacts,
        "error": error,
        "idempotency_key": idempotency_key,
    }


def catalog_text() -> str:
    """生成注入系统提示词的工具清单。"""
    lines = []
    for name, meta in _REGISTRY.items():
        lines.append(f"- {name}：{meta['description']}\n  参数：{meta['params_doc']}")
    return "\n".join(lines)


def tool_names() -> list[str]:
    return list(_REGISTRY)


# 导入工具模块以触发注册
from . import geo, geocode, tiles, vlm, dem  # noqa: E402,F401
