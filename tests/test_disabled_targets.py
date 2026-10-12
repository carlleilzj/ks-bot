"""停用平台不可被显式指定绕过（2026-10-10）。

背景：视频号因上传 IP 风控停用（platforms.weixin.enabled=false）后，
TG 投链里带 @视频号 的指定仍在建 job 并发布（每条 15 分钟超时刷屏），
因为 _task_platforms 的「显式指定优先于 config 开关」语义。修复后：
停用平台在指定路径被硬性跳过；指定平台全部停用时不发任何平台。
"""

from bot.config import Settings
import bot.main as main_mod
from bot.source import telegram_listener as tl


class _Spec:
    def __init__(self, name):
        self.name = name


_ALL = ("kuaishou", "douyin", "xhs", "weixin", "toutiao")
_ENABLED = ("kuaishou", "toutiao")


def _patch(monkeypatch):
    """注入固定的启用/全量平台表，避免依赖真实 config 与 publisher 模块。"""
    monkeypatch.setattr(main_mod, "enabled_publishers",
                        lambda s: [_Spec(n) for n in _ENABLED])
    monkeypatch.setattr(main_mod, "all_publishers",
                        lambda: {n: _Spec(n) for n in _ALL})
    monkeypatch.setattr(tl, "enabled_publishers",
                        lambda s: [_Spec(n) for n in _ENABLED])


def test_disabled_target_gets_no_job(monkeypatch):
    """指定已停用平台 → 不发任何平台（不建 job）。"""
    _patch(monkeypatch)
    task = {"shortcode": "x", "target_platforms": "weixin"}
    assert main_mod._task_platforms(Settings(), task) == []


def test_mixed_target_keeps_only_enabled(monkeypatch):
    """启用+停用混合指定 → 只保留启用平台。"""
    _patch(monkeypatch)
    task = {"shortcode": "x", "target_platforms": "kuaishou,weixin"}
    assert [p.name for p in main_mod._task_platforms(Settings(), task)] == ["kuaishou"]


def test_no_target_uses_all_enabled(monkeypatch):
    """未指定 → 全部启用平台（默认行为不变）。"""
    _patch(monkeypatch)
    task = {"shortcode": "x", "target_platforms": None}
    assert [p.name for p in main_mod._task_platforms(Settings(), task)] == list(_ENABLED)


def test_unknown_name_still_falls_back(monkeypatch):
    """全未识别（拼错）→ 仍回退全部启用平台（原有行为不变）。"""
    _patch(monkeypatch)
    task = {"shortcode": "x", "target_platforms": "foobar"}
    assert [p.name for p in main_mod._task_platforms(Settings(), task)] == list(_ENABLED)


def test_split_disabled_targets(monkeypatch):
    """TG 入口拆分：(可用, 已停用)。"""
    _patch(monkeypatch)
    s = Settings()
    assert tl._split_disabled_targets(s, ["weixin"]) == (None, ["weixin"])
    assert tl._split_disabled_targets(s, ["kuaishou", "weixin"]) == (["kuaishou"], ["weixin"])
    assert tl._split_disabled_targets(s, ["kuaishou"]) == (["kuaishou"], [])
    assert tl._split_disabled_targets(s, None) == (None, [])


def test_publish_gate_blocks_disabled_platform():
    """停用平台被发布闸门拦截：存量 PENDING job 不再被派发（2026-10-12）。

    此前停用只挡新建 job，已在队列里的 job 仍会被发布端领取并每条烧
    15 分钟上传超时。enabled 检查在窗口/限额之前短路，不需要 db。
    """
    from bot.config import PlatformConfig
    from bot.main import publish_gate

    s = Settings()
    s.platforms = {"weixin": PlatformConfig(enabled=False)}
    s.publish.window = []
    s.publish.min_gap_hours = 0
    s.publish.daily_limit = 999
    reason = publish_gate(s, None, "weixin")
    assert reason is not None and "已停用" in reason
