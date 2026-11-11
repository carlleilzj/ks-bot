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
class WatermarkConfig:
    """水印擦除配置：在转码阶段用 ffmpeg delogo 擦除固定位置的烧录水印。"""

    enabled: bool = False
    # 区域列表：每项 [x, y, w, h]，数值全 <=1 视为相对比例，否则为绝对像素
    regions: list = field(default_factory=list)
    box: int = 1          # delogo 采样边框宽度
    # 常见预设名（config.yaml 里写 name 即可，坐标按比例自动适配任意分辨率）
    # 见 PRESET_REGIONS


# 预置水印区域（相对比例，自动适配分辨率）：
# 坐标按 [x, y, w, h] 相对比例给出，已用 1276x718 实测素材逐像素标定
# （白色文字/Logo 包围盒 + 6px 余量）。实测水印位置全片稳定，不漂移。
PRESET_REGIONS: dict[str, list] = {
    # 左上角圆形头像/频道 Logo（实测包围盒 x24-93, y16-85，含弧形文字）
    "top_left": [[0.0141, 0.0139, 0.0643, 0.1142]],
    # 右下角账号名文字水印（实测包围盒 x939-1254, y650-700）
    "bottom_right": [[0.7312, 0.8969, 0.2571, 0.0877]],
    # 常见「左上 Logo + 右下账号名」组合（IMILASHA 这类素材的典型布局）
    "corner_pair": [[0.0141, 0.0139, 0.0643, 0.1142], [0.7312, 0.8969, 0.2571, 0.0877]],
    # 抖音/TikTok 右下角音波 logo（比账号名更靠右、更方正）
    "douyin": [[0.780, 0.880, 0.200, 0.100]],
}


@dataclass
class TransformConfig:
    """二创变换：抖音「原创性不足」扣分对策。

    平台查重比对画面帧特征 + 音频波形 + 关键帧哈希。只转码 + 去元数据时
    成片逐像素等于源片，判搬运是一眼的。开启后每条片子按区间随机取值，
    参数独立，避免固定模板反而触发同质化判定。
    """

    enabled: bool = False
    crop_pct: tuple = (0.02, 0.05)     # 四边裁切比例区间
    zoom_pct: tuple = (1.02, 1.06)     # 放大后裁回原尺寸
    hflip_prob: float = 0.5            # 水平镜像概率
    hue_deg: tuple = (-4.0, 4.0)
    sat: tuple = (0.96, 1.06)
    bright: tuple = (-0.03, 0.03)
    contrast: tuple = (0.97, 1.05)
    fps: tuple = (29.5, 30.0)
    tempo: tuple = (0.97, 1.03)
    crf: tuple = (19, 24)


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
    # 是否加入「默认发布平台」（不指定平台时发到哪些）。
    # 与 enabled 的区别（2026-10-15 引入）：
    #   enabled=true, default=true  → 默认就发，也能被指定
    #   enabled=true, default=false → 不默认发，只有链接里显式指定才发
    #                                 （今日头条用这个：按需投，不占默认队列）
    #   enabled=false               → 完全停用，指定也不发（硬性，见 publish_gate）
    default: bool = True
    categories: list[str] = field(default_factory=list)  # 该平台的分区列表（无分区概念的平台留空）
    spark_task: bool = False          # 快手：发布时挂星火「关联变现任务」
    spark_task_title: str = ""        # 可选，收藏任务标题包含该字符串则优先；空则轮询
    # 快手「作者服务」通用挂载（2026-11-02 起，星火的替代路线）：
    #   attach_service  —— 服务类型，如「关联推广任务」「关联商品」「关联变现任务」
    #                      （发布页下拉实测值；空 = 不挂）
    #   attach_prefer   —— 二级条目关键词优先匹配（如「短剧」；空 = 轮换）
    #   attach_fallback —— 主挂失败（池空/未找到）时的兜底服务类型（如「关联商品」）
    # 与 spark_task 的区别：这些任务池在发布页实时读取，无需 App 收藏；
    # 挂载失败**软降级**（记日志继续发布），不像星火那样硬阻断。
    attach_service: str = ""
    attach_prefer: str = ""
    attach_fallback: str = ""
    # 快手：封面上叠标题大字。2026-11-05 起**全局取消**：
    # A/B 号都已关，且平台「智能推荐封面」是原图无法叠字，
    # 两套封面来源混用时叠字会造成风格不一致。
    # 默认 False —— 新平台/漏配也不会突然开始叠大字。
    cover_title: bool = False
    anchors: dict = field(default_factory=dict)  # 抖音：挂载标签 {类型: 搜索词}
    hot_topic: str = ""               # 抖音：关联热点词
    # Instagram Reels（Meta Graph API）：
    share_to_feed: bool = True        # 是否同时分享到 Feed（false = 只在 Reels 标签页）
    is_ai_generated: bool = False     # AI 生成内容自我披露（Meta 要求，AI 动画类账号建议开）
    # 每平台覆盖 publish 段的全局节奏（None = 用全局值）。
    # 矩阵号冷启动期常用：kuaishou2 日限 3、间隔放宽。
    daily_limit: int | None = None
    min_gap_hours: float | None = None
    # 每平台覆盖发布窗口（None = 用全局 window）。
    # Instagram 用得上：欧美受众的黄金时段是北京时间凌晨，
    # 沿用国内的 10:00~22:00 等于把内容发在他们的深夜。
    window: tuple[str, str] | None = None
    # 自动素材发现（2026-11-09）：替代人工投链。
    # 结构见 config.yaml 的 platforms.<name>.discover；空 dict = 该平台不自动发现。
    discover: dict = field(default_factory=dict)


@dataclass
class VisionConfig:
    """AI 画面质检开关。

    2026-09-16 起改为手动发链接模式：素材由人工筛选，真人检测不再需要，
    故 real_person_check 默认 False。水印与动画判定不受影响。
    """
    real_person_check: bool = False   # False = 完全停用真人检测（转码层）


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
    # 真人检测（vision）：留空回退 ai_api_key/ai_model
    vision_api_key: str = ""
    vision_model: str = ""
    # vision 专用端点：留空 = 回退 ai_base_url。
    # 2026-11-12 踩到：把 AI_BASE_URL 从 cpa 换到 wb（换文案供应商）时，
    # vision 的 base_url 跟着抽走，但 VISION_API_KEY 还是 cpa 家的 key，
    # 两边指向不同中转站 → 全部 403 Invalid API key，画面探针/封面选优
    # /真人检测全部静默瘫痪（只打 WARNING）。文案供应商与视觉供应商可以
    # 不是同一家，所以两者必须能分开配。
    vision_base_url: str = ""
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
    # 远程发布（分体部署）：VPS 跑 API，家庭端跑发布 worker
    remote_api_token: str = ""          # Bearer token；空 = 不起 API 服务
    remote_api_bind: str = "127.0.0.1"  # 绑定地址：Tailscale IP（100.x）或 127.0.0.1
    remote_api_port: int = 8765
    remote_api_url: str = ""            # 家庭端填：http://<VPS的Tailscale IP>:8765
    # 行为
    poll_interval_min: int = 5
    subtitle: SubtitleConfig = field(default_factory=SubtitleConfig)
    transform: TransformConfig = field(default_factory=TransformConfig)
    watermark: WatermarkConfig = field(default_factory=WatermarkConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    categories: list[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))  # 快手分区（兼容旧配置）
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    # 顶层 discovery 段：目前只放跨 lane 的全局参数（待发水位闸门）。
    # 各平台的寻源配置仍在 platforms.<name>.discover。
    discovery: dict = field(default_factory=dict)


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR, RAW_DIR, WORK_DIR, FINAL_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _opt_int(v) -> int | None:
    """可选 int：None/空串 → None，非法值 → None（不抛）。"""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_yaml() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # 配置写错不要硬崩，用默认值并提示
        log.warning("config.yaml 解析失败，使用默认配置: %s", e)
        return {}


def _build_vision(raw: dict | None) -> VisionConfig:
    """解析 vision 段。

    real_person_check:
      False（默认，2026-09-16 起）= 完全停用真人检测；
      True = 恢复真人检测（转码层跳过真人镜头）。
    注意：无论开关如何，水印擦除（watermark 段）与动画判定都照常工作。
    """
    d = raw or {}
    return VisionConfig(real_person_check=bool(d.get("real_person_check", False)))


def _build_watermark(raw: dict | None) -> WatermarkConfig:
    """解析 watermark 段。

    支持两种区域写法：
      regions: [[0.02, 0.02, 0.16, 0.10], ...]   # 显式坐标（比例或像素）
      presets: ["corner_pair"]                    # 预置名（见 PRESET_REGIONS）
    """
    raw = raw or {}
    cfg = WatermarkConfig(
        enabled=bool(raw.get("enabled", False)),
        box=int(raw.get("box", 1) or 1),
    )
    regions: list = []

    for preset in (raw.get("presets") or []):
        key = str(preset).strip()
        if key in PRESET_REGIONS:
            regions.extend(PRESET_REGIONS[key])
        else:
            log.warning("未知水印预设 %r（可选：%s）", key, ", ".join(PRESET_REGIONS))

    for item in (raw.get("regions") or []):
        if isinstance(item, (list, tuple)) and len(item) == 4:
            try:
                regions.append([float(v) for v in item])
            except (TypeError, ValueError):
                log.warning("水印区域格式错误，已跳过：%r", item)
        else:
            log.warning("水印区域需为 [x, y, w, h]，已跳过：%r", item)

    cfg.regions = regions
    if cfg.enabled and not regions:
        log.warning("watermark.enabled=true 但未配置任何区域，水印擦除将空转")
    return cfg


def _build_subtitle(raw: dict) -> SubtitleConfig:
    cfg = SubtitleConfig()
    for key in ("enabled", "font", "font_size_ratio", "outline_ratio", "margin_v_ratio", "bold"):
        if key in raw:
            setattr(cfg, key, raw[key])
    return cfg


def _build_transform(raw: dict | None) -> TransformConfig:
    """解析 transform 段。区间项写成 [lo, hi]，单值项直接给数。"""
    raw = raw or {}
    cfg = TransformConfig(enabled=bool(raw.get("enabled", False)))
    ranges = ("crop_pct", "zoom_pct", "hue_deg", "sat", "bright", "contrast",
              "fps", "tempo", "crf")
    for key in ranges:
        val = raw.get(key)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            try:
                setattr(cfg, key, (float(val[0]), float(val[1])))
            except (TypeError, ValueError):
                log.warning("transform.%s 区间格式错误，用默认值：%r", key, val)
    if "hflip_prob" in raw:
        try:
            cfg.hflip_prob = max(0.0, min(1.0, float(raw["hflip_prob"])))
        except (TypeError, ValueError):
            pass
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
        vision_api_key=os.getenv("VISION_API_KEY", "").strip(),
        vision_model=os.getenv("VISION_MODEL", "").strip(),
        vision_base_url=os.getenv("VISION_BASE_URL", "").strip(),
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
        remote_api_token=os.getenv("REMOTE_API_TOKEN", "").strip(),
        remote_api_bind=os.getenv("REMOTE_API_BIND", "127.0.0.1").strip(),
        remote_api_port=int(os.getenv("REMOTE_API_PORT", "8765").strip() or "8765"),
        remote_api_url=os.getenv("REMOTE_API_URL", "").strip(),
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
    s.transform = _build_transform(raw.get("transform"))
    s.watermark = _build_watermark(raw.get("watermark"))
    s.vision = _build_vision(raw.get("vision"))
    s.publish = _build_publish(raw.get("publish") or {})
    # 顶层 discovery 段：目前只有待发水位闸门（backlog_watermark）。
    # 0 / 缺省 = 不启用闸门；正整数 = 待发量达到该值就暂停自动发现。
    disc_top = raw.get("discovery")
    if isinstance(disc_top, dict):
        s.discovery = dict(disc_top)
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
            _anchors = conf.get("anchors") or {}
            if not isinstance(_anchors, dict):
                _anchors = {}
            pc = PlatformConfig(
                enabled=bool(conf.get("enabled", False)),
                # default 缺省 = true，保证旧配置行为不变（启用即默认发）
                default=bool(conf.get("default", True)),
                spark_task=bool(conf.get("spark_task", False)),
                spark_task_title=str(conf.get("spark_task_title") or "").strip(),
                attach_service=str(conf.get("attach_service") or "").strip(),
                attach_prefer=str(conf.get("attach_prefer") or "").strip(),
                attach_fallback=str(conf.get("attach_fallback") or "").strip(),
                # 2026-11-05 全局取消封面大字，缺省 False（显式写 true 仍可开启）
                cover_title=bool(conf.get("cover_title", False)),
                anchors={str(k): str(v) for k, v in _anchors.items() if v},
                hot_topic=str(conf.get("hot_topic") or "").strip(),
                share_to_feed=bool(conf.get("share_to_feed", True)),
                is_ai_generated=bool(conf.get("is_ai_generated", False)),
                daily_limit=_opt_int(conf.get("daily_limit")),
                min_gap_hours=_opt_float(conf.get("min_gap_hours")),
                # 自动素材发现配置：原样透传，由 bot.source.discover 解释。
                # 结构校验放在消费端（那里能看到实际字段含义），
                # 这里只保证是 dict，避免 YAML 写错类型时下游崩在别处。
                discover=(conf.get("discover")
                          if isinstance(conf.get("discover"), dict) else {}),
            )
            # 每平台发布窗口（列表形式 ["HH:MM","HH:MM"]，支持跨夜）
            win_raw = conf.get("window")
            if isinstance(win_raw, (list, tuple)) and len(win_raw) == 2:
                pc.window = (str(win_raw[0]), str(win_raw[1]))
            pcats = plat_cats_raw.get(name)
            if isinstance(pcats, list) and pcats:
                pc.categories = [str(c).strip() for c in pcats if str(c).strip()]
            elif name.startswith("kuaishou"):
                pc.categories = list(s.categories)
            s.platforms[str(name)] = pc
    else:
        # 旧配置（无 platforms 段）：只启用快手，保证行为不变
        s.platforms = {"kuaishou": PlatformConfig(enabled=True, categories=list(s.categories))}
    return s
