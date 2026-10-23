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
    """各平台登录态文件路径；kuaishou 沿用历史文件名 ks_state.json。

    kuaishou2 是第二个快手号（矩阵号）：登录态独立成 ks2_state.json，
    日限/文案/星火池按平台维度天然隔离，互不影响 A 号。
    """
    special = {"kuaishou": "ks_state.json", "kuaishou2": "ks2_state.json"}
    return DATA_DIR / special.get(name, f"{name}_state.json")


def _build_specs() -> dict[str, PublisherSpec]:
    from . import douyin, kuaishou, toutiao, weixin, xhs

    specs = [
        PublisherSpec(
            name="kuaishou", display_name="快手",
            state_path=state_path_for("kuaishou"), supports_category=True,
            login=kuaishou.login_interactive, publish=kuaishou.publish,
        ),
        # 第二个快手号：同一套发布实现，独立登录态/日限/文案/星火。
        PublisherSpec(
            name="kuaishou2", display_name="快手2号",
            state_path=state_path_for("kuaishou2"), supports_category=True,
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
        PublisherSpec(
            name="toutiao", display_name="今日头条",
            state_path=state_path_for("toutiao"), supports_category=False,
            login=toutiao.login_interactive, publish=toutiao.publish,
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
    """所有可用平台（platforms.<name>.enabled=true）。

    注意：这是「能被指定发布」的集合，不等于「默认就发」。
    默认发布集合见 default_publishers。
    """
    return [sp for name, sp in all_publishers().items()
            if s.platforms.get(name) is not None and s.platforms[name].enabled]


def default_publishers(s: Settings) -> list[PublisherSpec]:
    """默认发布平台（enabled 且 default）。

    不指定平台时发到这里。用于「可用但不默认发」的平台——
    如今日头条：链接里显式写 @头条 才发，不占用默认队列。
    """
    return [sp for name, sp in all_publishers().items()
            if s.platforms.get(name) is not None
            and s.platforms[name].enabled
            and s.platforms[name].default]
