"""配置加载：.env（敏感信息）+ config.yaml（行为配置）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
CONFIG_PATH = BASE_DIR / "config.yaml"

DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
MEDIA_DIR = BASE_DIR / "media"
RAW_DIR = MEDIA_DIR / "raw"
WORK_DIR = MEDIA_DIR / "work"
FINAL_DIR = MEDIA_DIR / "final"

DB_PATH = DATA_DIR / "bot.db"
KS_STATE_PATH = DATA_DIR / "ks_state.json"   # 快手 Playwright 登录态
SESSION_FILE = DATA_DIR / "ig_session"  # instaloader 会话缓存

DEFAULT_CATEGORIES = [
    "搞笑", "美食", "生活记录", "萌宠", "影视娱乐", "音乐", "舞蹈", "游戏",
    "科技数码", "知识", "汽车", "运动", "时尚穿搭", "亲子", "情感", "教育",
    "旅游", "健康", "二次元", "三农",
]


@dataclass
class SubtitleConfig:
    enabled: bool = True
    font: str = "PingFang SC"
    font_size_ratio: float = 0.045
    outline_ratio: float = 0.006
    margin_v_ratio: float = 0.07
    bold: bool = True


@dataclass
class PublishConfig:
    headless: bool = True
    daily_limit: int = 5
    min_gap_hours: float = 2.0
    window: tuple[str, str] = ("10:00", "22:00")


@dataclass
class PlatformConfig:
    enabled: bool = False
    categories: list[str] = field(default_factory=list)  # 该平台的分区列表（无分区概念的平台留空）
    spark_task: bool = False          # 快手：发布时挂星火「关联变现任务」
    spark_task_title: str = ""        # 可选，收藏任务标题包含该字符串则优先；空则轮询


@dataclass
class DiscoveryConfig:
    """发现层配置：主动去各大平台采集热门动画短视频候选，发 TG 审核。"""
    enabled: bool = False
    interval_min: int = 60            # 每轮发现的间隔（分钟）
    limit_per_source: int = 20        # 每个适配器每轮最多取多少条
    max_pending_review: int = 10      # 待审核堆积上限（避免 TG 刷屏）
    sources: list[dict] = field(default_factory=list)  # [{type, ...params}]
    # 过滤规则（扁平化，方便 FilterRules.from_dict 读取）
    filters_min_duration: float = 5
    filters_max_duration: float = 120
    filters_min_score: float = 0
    filters: dict = field(default_factory=dict)   # 原始 filters 段，给 FilterRules 用


@dataclass
class Settings:
    # Instagram 监控（instaloader，监控别人公开账号）
    ig_targets: list[str] = field(default_factory=list)  # 目标账号列表
    ig_login_user: str = ""   # 小号用户名
    ig_login_pass: str = ""   # 小号密码
    # AI 文案（OpenAI 兼容）
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    # 语音识别
    asr_provider: str = "api"
    asr_model: str = "whisper-1"
    asr_language: str = "zh"
    asr_local_model: str = "small"
    asr_local_device: str = "auto"
    asr_local_compute: str = "int8"
    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_proxy: str = ""
    # 行为
    poll_interval_min: int = 5
    subtitle: SubtitleConfig = field(default_factory=SubtitleConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))  # 快手分区（兼容旧配置）
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, RAW_DIR, WORK_DIR, FINAL_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _load_yaml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # 配置写错不要硬崩，用默认值并提示
        log.warning("config.yaml 解析失败，使用默认配置: %s", e)
        return {}


def _build_subtitle(raw: dict) -> SubtitleConfig:
    cfg = SubtitleConfig()
    for key in ("enabled", "font", "font_size_ratio", "outline_ratio", "margin_v_ratio", "bold"):
        if key in raw:
            setattr(cfg, key, raw[key])
    return cfg


def _build_publish(raw: dict) -> PublishConfig:
    cfg = PublishConfig()
    for key in ("headless", "daily_limit", "min_gap_hours"):
        if key in raw:
            setattr(cfg, key, raw[key])
    window = raw.get("window")
    if isinstance(window, (list, tuple)) and len(window) == 2:
        cfg.window = (str(window[0]), str(window[1]))
    return cfg


def _build_discovery(raw: dict | None) -> DiscoveryConfig:
    """解析 discovery 段。raw 为 config.yaml 的 discovery 子树。"""
    raw = raw or {}
    cfg = DiscoveryConfig(
        enabled=bool(raw.get("enabled", False)),
        interval_min=int(raw.get("interval_min", 60) or 60),
        limit_per_source=int(raw.get("limit_per_source", 20) or 20),
        max_pending_review=int(raw.get("max_pending_review", 10) or 10),
        sources=list(raw.get("sources") or []) if isinstance(raw.get("sources"), list) else [],
        filters=dict(raw.get("filters") or {}),
    )
    # 扁平化过滤规则，方便 discovery.py 的 build_adapters 直接读
    f = cfg.filters
    cfg.filters_min_duration = float(f.get("min_duration", 5))
    cfg.filters_max_duration = float(f.get("max_duration", 120))
    cfg.filters_min_score = float(f.get("min_score", 0))
    return cfg


def load_settings() -> Settings:
    load_dotenv(ENV_PATH)
    raw = _load_yaml()

    targets = [t.strip() for t in os.getenv("IG_TARGETS", "").split(",") if t.strip()]
    s = Settings(
        ig_targets=targets,
        ig_login_user=os.getenv("IG_LOGIN_USER", "").strip(),
        ig_login_pass=os.getenv("IG_LOGIN_PASS", "").strip(),
        ai_base_url=os.getenv("AI_BASE_URL", "").strip() or "https://api.openai.com/v1",
        ai_api_key=os.getenv("AI_API_KEY", "").strip(),
        ai_model=os.getenv("AI_MODEL", "").strip() or "gpt-4o-mini",
        asr_provider=os.getenv("ASR_PROVIDER", "api").strip().lower(),
        asr_model=os.getenv("ASR_MODEL", "whisper-1").strip(),
        asr_language=os.getenv("ASR_LANGUAGE", "zh").strip(),
        asr_local_model=os.getenv("ASR_LOCAL_MODEL", "small").strip(),
        asr_local_device=os.getenv("ASR_LOCAL_DEVICE", "").strip(),
        asr_local_compute=os.getenv("ASR_LOCAL_COMPUTE", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").strip().rstrip("/"),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
        poll_interval_min=int(raw.get("poll_interval_min", 5) or 5),
    )
    if s.asr_language.lower() in ("auto", "none", ""):
        s.asr_language = ""
    asr_raw = raw.get("asr") or {}
    if not s.asr_local_device:
        s.asr_local_device = str(asr_raw.get("local_device") or "auto").strip() or "auto"
    if not s.asr_local_compute:
        s.asr_local_compute = str(asr_raw.get("local_compute") or "int8").strip() or "int8"

    s.subtitle = _build_subtitle(raw.get("subtitle") or {})
    s.publish = _build_publish(raw.get("publish") or {})
    s.discovery = _build_discovery(raw.get("discovery"))
    cats = raw.get("categories")
    if isinstance(cats, list) and cats:
        s.categories = [str(c).strip() for c in cats if str(c).strip()]

    # 发布平台：platforms.<name>.enabled；分区来自 platform_categories.<name>，
    # 未配置时 kuaishou 回退到顶层 categories（兼容旧配置）
    plat_raw = raw.get("platforms")
    plat_cats_raw = raw.get("platform_categories") or {}
    if isinstance(plat_raw, dict) and plat_raw:
        for name, conf in plat_raw.items():
            conf = conf if isinstance(conf, dict) else {}
            pc = PlatformConfig(
                enabled=bool(conf.get("enabled", False)),
                spark_task=bool(conf.get("spark_task", False)),
                spark_task_title=str(conf.get("spark_task_title") or "").strip(),
            )
            pcats = plat_cats_raw.get(name)
            if isinstance(pcats, list) and pcats:
                pc.categories = [str(c).strip() for c in pcats if str(c).strip()]
            elif name == "kuaishou":
                pc.categories = list(s.categories)
            s.platforms[str(name)] = pc
    else:
        # 旧配置（无 platforms 段）：只启用快手，保证行为不变
        s.platforms = {"kuaishou": PlatformConfig(enabled=True, categories=list(s.categories))}
    return s
