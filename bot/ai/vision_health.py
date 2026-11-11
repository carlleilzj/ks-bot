"""视觉链路健康哨兵：连续失败到阈值就告警，并支持恢复通知。

为什么需要（2026-11-12 事故）
----------------------------
把 AI_BASE_URL 从 cpa 换到 wb 时，vision 的 base_url 跟着抽走，但
VISION_API_KEY 还是 cpa 家的 key → 三处视觉调用全部 403。所有失败都只是
WARNING 级，任务照常跑完并发出去，**几小时**后才被「文案变得很泛」
这类间接现象发现。

代价是隐性的但很贵：
  · 画面探针失效 → 文案失去画面输入，退回泛化描述
  · 封面选优失效 → 退回盲截第 1 秒
  · 真人检测/水印质检失效 → 质检等于放行

设计要点
--------
* **按原因分链计数**，不同链互不影响：一条内容探针失败不该把封面选优的
  计数重置掉，反之亦然。
* **只在「刚达到阈值」时告警一次**，不是每次失败都发 —— 否则视觉挂一整夜
  会刷出上百条 TG，反而让人屏蔽通知。
* **恢复也通知一次**，让 operator 知道问题过去了，不用再盯。
* 计数是进程级内存状态：进程重启后归零。这是刻意的 —— 重启本身就是
  一次排障动作，从零开始观察更符合直觉。
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("vision.health")

# 连续失败多少次触发告警。3 次 ≈ 一条视频的三个视觉环节（探针/封面/质检）
# 或三条视频的同一环节，足以排除偶发抖动。
ALERT_THRESHOLD = 3

_lock = threading.Lock()
# {chain: {"failures": int, "alerted": bool}}
_state: dict[str, dict] = {}


def _get(chain: str) -> dict:
    return _state.setdefault(chain, {"failures": 0, "alerted": False})


def reset(chain: str) -> None:
    """成功一次就把该链的失败计数清零；若先前告警过，补一条恢复通知。"""
    with _lock:
        st = _get(chain)
        was_alerted = st["alerted"]
        st["failures"] = 0
        st["alerted"] = False
    if was_alerted:
        log.info("视觉链路 [%s] 已恢复", chain)
        _notify(f"✅ 视觉链路恢复：{chain}\n之前的视觉调用失败已不再发生。")


def record_failure(s, chain: str, error: str) -> None:
    """记一次失败。首次达到阈值时告警（含端点/key 诊断信息）。"""
    with _lock:
        st = _get(chain)
        st["failures"] += 1
        n = st["failures"]
        should_alert = n >= ALERT_THRESHOLD and not st["alerted"]
        if should_alert:
            st["alerted"] = True

    if not should_alert:
        return

    log.error("视觉链路 [%s] 连续失败 %d 次：%s", chain, n, str(error)[:200])
    text = (
        f"⚠️ 视觉链路异常：{chain}\n"
        f"连续失败 {n} 次（阈值 {ALERT_THRESHOLD}）\n"
        f"错误：{str(error)[:250]}\n"
        f"影响：画面探针/封面选优/真人检测会静默降级，"
        f"文案质量下降但任务仍会照常发出。\n"
        f"排查：确认 .env 里 VISION_BASE_URL / VISION_API_KEY 来自同一家中转站"
        f"（端点与 key 必须成对）"
    )
    # 端点信息有助于当场定位：最常见的就是端点/key 不同家
    try:
        from ..ai.vision import vision_endpoint

        base, key = vision_endpoint(s)
        text += f"\n当前视觉端点：{base}\nkey 前缀：{key[:8]}…"
    except Exception:
        pass
    _notify(text)


def _notify(text: str) -> None:
    """发 TG；失败不抛（告警本身不能把流水线带崩）。"""
    try:
        from ..config import load_settings
        from ..notify import telegram

        telegram.notify_info(load_settings(), text)
    except Exception as e:  # pragma: no cover —— 网络问题
        log.debug("视觉告警推送失败：%s", e)


def snapshot() -> dict[str, dict]:
    """当前计数快照（测试与诊断用）。"""
    with _lock:
        return {k: dict(v) for k, v in _state.items()}


def clear() -> None:
    """清空所有计数（测试用）。"""
    with _lock:
        _state.clear()
