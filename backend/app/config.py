"""GEOINT Buddy 复赛版配置：端云协同（云端 AnyInt / 端侧 llama.cpp）"""
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
DATA_DIR = BASE_DIR / "data"
TILES_DIR = DATA_DIR / "tiles"                              # mbtiles 存放目录
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "geoint_buddy.db"


class RegionProfile(BaseModel):
    id: str
    name: str
    center: list[float]
    zoom: int
    download_bbox: list[float]
    demo_aoi: list[float]
    imagery_mbtiles: str
    dem_mbtiles: str
    dem_zoom: int


def load_region(profile: str) -> dict:
    """加载相对 backend/ 的区域配置，并校验地图范围与层级。"""
    path = Path(profile)
    if not path.is_absolute():
        path = BASE_DIR / path
    region = RegionProfile.model_validate_json(path.read_text(encoding="utf-8")).model_dump()
    if len(region["center"]) != 2:
        raise ValueError(f"{path}: center 必须为 [lon, lat]")
    for key in ("download_bbox", "demo_aoi"):
        bbox = region[key]
        if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError(f"{path}: {key} 必须为有效的 [minLon,minLat,maxLon,maxLat]")
    for key in ("zoom", "dem_zoom"):
        if not 0 <= region[key] <= 22:
            raise ValueError(f"{path}: {key} 必须位于 0-22")
    return region


class Settings(BaseSettings):
    """全局配置，通过 .env 覆盖。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "观澜 · 灾害应急遥感智能体"
    version: str = "0.2.0"
    region_profile: str = "regions/baige.json"

    # ---- 网络状态：ONLINE / DEGRADED / OFFLINE ----
    # OFFLINE 时跳过云端直连端侧；ONLINE 时云端优先、失败自动回退端侧
    network_state: str = "ONLINE"

    # ---- 云端 AnyInt（OpenAI 兼容）----
    cloud_base_url: str = "https://api.anyint.ai/openai/v1"
    cloud_api_key: str = "REPLACE_ME"
    cloud_model: str = "qwen-plus"
    cloud_vlm_model: str = ""                               # 云端多模态模型 ID，空则用 cloud_model
    cloud_timeout: float = 240.0

    # ---- 端侧 llama.cpp（OpenAI 兼容）----
    edge_base_url: str = "http://127.0.0.1:8081/v1"
    edge_api_key: str = "none"
    edge_model: str = "qwen3vl-4b-local"                    # llama-server 不校验模型名
    edge_timeout: float = 300.0                             # 端侧 4B 推理慢，放宽超时
    edge_max_tokens: int = 1600                             # 端侧上下文有限，防截断

    # ---- 端侧引擎 2：Gemma4（llama-server 8082，思考模型）----
    gemma_base_url: str = "http://127.0.0.1:8082/v1"
    gemma_model: str = "gemma-4-E4B-it"
    gemma_timeout: float = 900.0                            # 思考模型，判读可达数分钟
    gemma_max_tokens: int = 2560                            # 需容纳思考与最终答案

    # ---- 端侧引擎 3：观澜微调版（白格滑坡 LoRA，llama-server 8083）----
    ft_base_url: str = "http://127.0.0.1:8083/v1"
    ft_model: str = "qwen3vl-4b-baige-ft"
    ft_timeout: float = 1800.0                            # 长提示词预填充约 11 分钟，需同时覆盖后续长生成
    ft_max_tokens: int = 1600

    # ---- 当前端侧引擎（运行时可切换，与 network_state 同模式）----
    edge_engine: str = "qwen3vl"                            # qwen3vl / gemma4 / qwen3vl_ft

    llm_max_tokens: int = 4096

    # ---- 服务 ----
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- Agent：状态转移、真实工具执行与 LLM 调用分别计费 ----
    agent_max_steps: int = 6
    agent_max_tools: int = 4
    agent_max_llm_calls: int = 3
    detection_summary_limit: int = 4000                     # 工具原始输出限长
    agent_observation_limit: int = 1200                     # 单次注入 Agent 的紧凑观察字符上限
    agent_prompt_budget: int = 5120                         # 推理前提示词预算（另预留输出与模板空间）

    # ---- 外部服务 ----
    nominatim_url: str = "https://nominatim.openstreetmap.org"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
REGION = load_region(settings.region_profile)
IMAGERY_MBTILES = DATA_DIR / REGION["imagery_mbtiles"]
DEM_MBTILES = DATA_DIR / REGION["dem_mbtiles"]

# ---- 端侧引擎注册表：三 llama-server（Qwen3VL 8081 / Gemma4 8082 / 观澜微调版 8083），前端可切换 ----
EDGE_ENGINES: dict[str, dict] = {
    "qwen3vl": {
        "id": "qwen3vl", "name": "Qwen3VL-4B",
        "desc": "响应快（约 12 tok/s）",
        "base_url": settings.edge_base_url, "model": settings.edge_model,
        "timeout": settings.edge_timeout, "max_tokens": settings.edge_max_tokens,
    },
    "gemma4": {
        "id": "gemma4", "name": "Gemma4-E4B",
        "desc": "思考模型，判读更细但慢（单次约 2-4 分钟）",
        "base_url": settings.gemma_base_url, "model": settings.gemma_model,
        "timeout": settings.gemma_timeout, "max_tokens": settings.gemma_max_tokens,
    },
    "qwen3vl_ft": {
        "id": "qwen3vl_ft", "name": "观澜·白格微调版",
        "desc": "白格滑坡数据 LoRA 微调，灾害迹象检出显著提升",
        "base_url": settings.ft_base_url, "model": settings.ft_model,
        "timeout": settings.ft_timeout, "max_tokens": settings.ft_max_tokens,
    },
}


def edge_engine_conf(engine_id: str | None = None) -> dict:
    """端侧引擎配置；engine_id 为空时取当前全局引擎，未知 id 回退默认引擎。"""
    eid = engine_id or settings.edge_engine
    return EDGE_ENGINES.get(eid, EDGE_ENGINES["qwen3vl"])
