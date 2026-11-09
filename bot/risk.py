"""平台风控冷却状态机：把「固定配额」升级为「渐进冷却 + 渐进恢复」。

为什么需要它（借鉴 CreatorHub 的 risk.py，按我们的规模做了简化）
------------------------------------------------------------------
现状只有三道闸：每日上限、最小间隔、发布窗口。它们回答的是
「今天还能不能发」，但回答不了「**刚才是不是已经被平台警告过**」。

真实事故形态：
    某平台返回「操作过于频繁」→ 我们 2 小时后照常发下一条 → 再次被拒
    → 触发更严的风控。日限/间隔对这种「已经进入风控视野」的状态完全无感。

本模块补上这条回路：
    命中风控 → risk_level += 1 → 冷却 30min / 2h / 6h / 24h（递增）
    连续成功 N 次 → risk_level -= 1（渐进恢复，不是一键归零）

设计取舍（刻意不照搬 CreatorHub）
--------------------------------
1. **只做平台级**，不做账号池 / 代理出口组：我们每个平台配置就是一个账号，
   没有「同出口多账号」的拓扑，那套网络组熔断在这里是空转。
2. **用 SQLite 单表**，不引入 SQLModel/SQLAlchemy：与现有 db.py 保持一致。
3. **只拦发布**：评论互动（kuaishou_reply）自己已有 dry-run/上限/fail-stop，
   不在这里重复拦截，避免两套限流彼此打架。
4. **状态持久化**：冷却截止时间写库，重启不清零 —— 否则「重启绕过冷却」
   等于风控措施失效。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# 冷却阶梯（秒）：level 1~4 依次对应。level 超过 4 时用最后一档。
DEFAULT_COOLDOWN_STEPS = (1800, 7200, 21600, 86400)   # 30min / 2h / 6h / 24h

# 连续多少次成功才降一级。取 3：太少（1~2 次）会让偶发失败就重置，
# 太多（5+）会让恢复慢到影响正常节奏。
DEFAULT_RECOVERY_SUCCESSES = 3

# 最大风险等级（对应冷却阶梯上限）
MAX_RISK_LEVEL = 4


# ---------------------------------------------------------------------------
# 风险信号识别
# ---------------------------------------------------------------------------

# 需要冷却的平台提示文案。分两类：
#   HARD  —— 明确的平台风控/限流，必须冷却（继续发只会加重）
#   SOFT  —— 可能只是偶发，但仍然值得退一步（计入但判级更保守）
RISK_SIGNALS_HARD = (
    "操作过于频繁", "访问频繁", "请求过于频繁", "频控",
    "安全验证", "人机验证", "拖动滑块", "滑块验证", "验证码",
    "环境异常", "设备异常", "账号异常",
    "rate limit", "too many requests", "too frequent",
    "verification_required", "captcha",
    "http 429",
)

RISK_SIGNALS_SOFT = (
    "发布失败，请稍后再试", "请稍后再试",
    "已被限制", "限制传播", "不予推荐",
)

# 明确不是风控的错误：登录态失效、网络问题、参数问题。
# 这些不该消耗风险等级（否则一次网络抖动就冷却 30 分钟，太蠢）。
AUTH_SIGNALS = (
    "登录态已失效", "登录已失效", "未登录", "请重新扫码", "需要重新登录",
    "logged_out", "login expired", "session expired", "cookie expired",
    "login required", "not logged in",
)

NETWORK_SIGNALS = (
    "connection timeout", "connect timeout", "connecttimeout",
    "read timeout", "readtimeout", "timed out", "timeout",
    "connection reset", "connection refused", "connectionerror",
    "proxyerror", "proxy error", "proxy_auth",
    "dns", "tls", "networkerror", "network error",
    "临时通信失败",
)


def classify_failure(error_text: str) -> tuple[str, str]:
    """把一次失败分类，返回 (category, signal)。

    category 取值：
        "auth"    —— 登录态问题（该走重登，不该冷却）
        "network" —— 网络/代理问题（该退避重试，不该冷却）
        "risk"    —— 平台风控（必须冷却）
        "business"—— 其他业务失败（不冷却，按原有重试逻辑）

    判定顺序很重要：**先排除 auth/network，再判风控**。
    因为「登录态已失效，请稍后再试」这类报文同时含两类关键词，
    如果先判风控就会让一次 session 过期无故冷却平台半小时。
    """
    text = (error_text or "").strip().lower()
    if not text:
        return "business", "empty"

    # 1. 登录态（最高优先级）
    for marker in AUTH_SIGNALS:
        if marker.lower() in text:
            return "auth", "auth_expired"

    # 2. 网络问题
    for marker in NETWORK_SIGNALS:
        if marker.lower() in text:
            return "network", "network_failure"

    # 3. 平台风控
    for marker in RISK_SIGNALS_HARD:
        if marker.lower() in text:
            return "risk", f"risk:{marker}"

    for marker in RISK_SIGNALS_SOFT:
        if marker.lower() in text:
            return "risk", f"soft:{marker}"

    return "business", "business_error"


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

@dataclass
class RiskState:
    """一个平台的当前风险状态（与 DB 行一一对应）。"""
    platform: str
    risk_level: int = 0
    cooldown_until: str = ""        # ISO 时间串；空 = 无冷却
    recovery_successes: int = 0
    last_risk_at: str = ""
    last_risk_reason: str = ""
    consecutive_failures: int = 0
    updated_at: str = ""

    def cooldown_remaining_sec(self, now: datetime | None = None) -> int:
        """还剩多少秒冷却；<=0 表示已过冷却期。"""
        if not self.cooldown_until:
            return 0
        now = now or datetime.now()
        try:
            until = datetime.fromisoformat(self.cooldown_until)
        except ValueError:
            return 0
        return max(0, int((until - now).total_seconds()))

    def is_cooling(self, now: datetime | None = None) -> bool:
        return self.cooldown_remaining_sec(now) > 0


def cooldown_for_level(level: int, steps: tuple[int, ...] = DEFAULT_COOLDOWN_STEPS) -> int:
    """风险等级 → 冷却秒数。level 从 1 开始；超界取最后一档。"""
    if level <= 0:
        return 0
    idx = min(level - 1, len(steps) - 1)
    return steps[idx]


def format_duration(seconds: int) -> str:
    """把秒数格式化成中文人类可读（用于 TG 消息与 gate 提示）。"""
    if seconds <= 0:
        return "0秒"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}分钟"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f}小时".replace(".0小时", "小时")
    return f"{hours / 24:.1f}天".replace(".0天", "天")


# ---------------------------------------------------------------------------
# 决策（纯函数，便于测试）
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """放行 / 拦截结果。"""
    allowed: bool
    reason: str = ""
    retry_after_sec: int = 0

    def __bool__(self) -> bool:
        return self.allowed


def evaluate_publish_gate(state: RiskState | None,
                          now: datetime | None = None) -> GateResult:
    """根据风险状态判断该平台当前能否发布。"""
    if state is None:
        return GateResult(True)
    now = now or datetime.now()
    remaining = state.cooldown_remaining_sec(now)
    if remaining > 0:
        return GateResult(
            False,
            f"{state.platform} 处于平台风控冷却期（还剩 {format_duration(remaining)}），"
            f"原因：{state.last_risk_reason or '平台风控'}",
            retry_after_sec=remaining,
        )
    return GateResult(True)


def apply_failure(state: RiskState, category: str, signal: str,
                  now: datetime | None = None,
                  steps: tuple[int, ...] = DEFAULT_COOLDOWN_STEPS) -> RiskState:
    """一次失败后的状态转移。

    只有 category == "risk" 才会提升风险等级并设置冷却；
    auth / network / business 只累加连续失败计数（用于日志与告警），
    不冷却 —— 否则一次网络抖动就停发半小时。
    """
    now = now or datetime.now()
    state.consecutive_failures += 1
    state.updated_at = now.isoformat(timespec="seconds")

    if category != "risk":
        # 非风控失败不改变风险等级；但如果本来就在冷却，不要缩短冷却
        return state

    state.risk_level = min(MAX_RISK_LEVEL, max(1, state.risk_level + 1))
    state.recovery_successes = 0
    cooldown = cooldown_for_level(state.risk_level, steps)
    state.cooldown_until = (now + timedelta(seconds=cooldown)).isoformat(timespec="seconds")
    state.last_risk_at = now.isoformat(timespec="seconds")
    state.last_risk_reason = str(signal or "平台风控")[:240]
    log.warning("平台 %s 命中风控（level=%d），冷却 %s 至 %s",
                state.platform, state.risk_level, format_duration(cooldown),
                state.cooldown_until)
    return state


def apply_success(state: RiskState, now: datetime | None = None,
                  recovery_successes: int = DEFAULT_RECOVERY_SUCCESSES) -> RiskState:
    """一次成功后的状态转移：渐进恢复，不是一键归零。

    归零的问题：刚冷却完发第一条成功就把等级清空，下一条再失败又回到
    30 分钟起步 —— 等于阶梯失效。渐进恢复让「连续 x 次成功」才降一级。
    """
    now = now or datetime.now()
    state.consecutive_failures = 0
    state.updated_at = now.isoformat(timespec="seconds")

    if state.risk_level <= 0:
        return state

    state.recovery_successes += 1
    if state.recovery_successes >= max(1, recovery_successes):
        state.risk_level = max(0, state.risk_level - 1)
        state.recovery_successes = 0
        if state.risk_level == 0:
            state.cooldown_until = ""
            log.info("平台 %s 风险等级已恢复为 0（可正常节奏发布）", state.platform)
        else:
            log.info("平台 %s 风险等级降为 %d", state.platform, state.risk_level)
    return state


def manual_clear(state: RiskState, now: datetime | None = None) -> RiskState:
    """人工解除风控（平台侧问题已处理完后使用）。"""
    now = now or datetime.now()
    state.risk_level = 0
    state.cooldown_until = ""
    state.recovery_successes = 0
    state.consecutive_failures = 0
    state.last_risk_reason = ""
    state.updated_at = now.isoformat(timespec="seconds")
    return state
