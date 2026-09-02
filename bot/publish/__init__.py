"""发布平台注册表：平台名 -> PublisherSpec。

新增平台只需三步：
1. 建 bot/publish/<name>.py，实现 login_interactive(state_path) 和 publish(...)
2. 在下方 _SPECS 注册（异常统一用 base.PublishError / base.LoginExpired）
3. config.yaml 的 platforms 里加一行并启用
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import DATA_DIR, Settings


@dataclass(frozen=True)
class PublisherSpec:
    name: str                      # 平台标识（DB/config 用的 key）
    display_name: str              # 显示名（TG 通知用）
    state_path: Path               # Playwright 登录态文件
    supports_category: bool        # 发布页是否有分区选择
    login: object                  # login_interactive(state_path=...) -> bool
    publish: object                # publish(video, title, description, tags, category, cover, headless, state_path) -> str | None


def state_path_for(name: str) -> Path:
    """各平台登录态文件路径；kuaishou 沿用历史文件名 ks_state.json。"""
    return DATA_DIR / ("ks_state.json" if name == "kuaishou" else f"{name}_state.json")


def _build_specs() -> dict[str, PublisherSpec]:
    from . import douyin, kuaishou, weixin, xhs

    specs = [
        PublisherSpec(
            name="kuaishou", display_name="快手",
            state_path=state_path_for("kuaishou"), supports_category=True,
            login=kuaishou.login_interactive, publish=kuaishou.publish,
        ),
        PublisherSpec(
            name="douyin", display_name="抖音",
            state_path=state_path_for("douyin"), supports_category=False,
            login=douyin.login_interactive, publish=douyin.publish,
        ),
        PublisherSpec(
            name="xhs", display_name="小红书",
            state_path=state_path_for("xhs"), supports_category=False,
            login=xhs.login_interactive, publish=xhs.publish,
        ),
        PublisherSpec(
            name="weixin", display_name="微信视频号",
            state_path=state_path_for("weixin"), supports_category=False,
            login=weixin.login_interactive, publish=weixin.publish,
        ),
    ]
    return {sp.name: sp for sp in specs}


_SPECS: dict[str, PublisherSpec] | None = None


def all_publishers() -> dict[str, PublisherSpec]:
    global _SPECS
    if _SPECS is None:
        _SPECS = _build_specs()
    return _SPECS


def get_publisher(name: str) -> PublisherSpec:
    specs = all_publishers()
    if name not in specs:
        raise KeyError(f"未知发布平台 '{name}'，可选：{', '.join(specs)}")
    return specs[name]


def enabled_publishers(s: Settings) -> list[PublisherSpec]:
    """按 config.yaml platforms.<name>.enabled 返回启用的平台（有序：快手/抖音/小红书）。"""
    return [sp for name, sp in all_publishers().items()
            if s.platforms.get(name) is not None and s.platforms[name].enabled]
