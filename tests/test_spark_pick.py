"""星火任务选择：收益加权轮换 + 黑名单剔除（2026-10-15）。

背景：原先 pick_spark_title 是均匀轮询（cursor % len），
实测各任务单条产出差 140 倍（狐缘山间 0.285 元/条 vs 千万际遇 0.002 元/条），
均匀轮换等于把大部分曝光浪费在几乎不产出的任务上。
"""

from bot.publish.kuaishou import (
    SPARK_TASK_BLOCKLIST,
    _spark_weight,
    pick_spark_title,
)


def test_blocklist_tasks_never_picked():
    """已停/无产出的任务不参与轮换。"""
    titles = [
        "山猎诡事：1977任务时间：2026.08.01-2027.08.01",
        "千万际遇辨亲情任务时间：2026.09.03-2027.09.30",
        "扫墓丫头，替祖宗横扫天下任务时间：2026.05.14-2027.05.31",
    ]
    picked = {pick_spark_title(titles, cursor=i)[0] for i in range(30)}
    assert not any("山猎诡事" in p for p in picked)
    assert not any("千万际遇" in p for p in picked)
    assert all("扫墓丫头" in p for p in picked)


def test_blocklist_only_returns_empty():
    """候选全是黑名单 → 返回空串，调用方按未挂载处理。"""
    titles = ["山猎诡事：1977任务时间：x", "千万际遇辨亲情任务时间：y"]
    assert pick_spark_title(titles) == ("", 0)


def test_prefer_still_wins():
    """人工指定优先于加权。"""
    titles = ["扫墓丫头，替祖宗横扫天下任务时间：a",
              "我花五万买破船，里面藏着万吨黄金任务时间：b"]
    picked, _ = pick_spark_title(titles, prefer="破船")
    assert "破船" in picked


def test_high_weight_task_gets_more_slots():
    """高权重任务在轮换中占比更高（不是 1:1 均匀）。"""
    titles = ["扫墓丫头，替祖宗横扫天下任务时间：a",   # 0.078
              "我花五万买破船，里面藏着万吨黄金任务时间：b"]  # 0.05
    counts = {"扫墓": 0, "破船": 0}
    cur = 0
    for _ in range(200):
        name, cur = pick_spark_title(titles, cursor=cur)
        if "扫墓" in name:
            counts["扫墓"] += 1
        elif "破船" in name:
            counts["破船"] += 1
    # 权重比 0.078:0.05 ≈ 1.56:1，扫墓应明显多于破船
    assert counts["扫墓"] > counts["破船"], counts
    # 且两者都应有曝光（不是完全排除）
    assert counts["破船"] > 0, counts


def test_unknown_task_gets_conservative_weight():
    """未知任务给保守默认值，不为 0（仍有机会曝光）。"""
    w = _spark_weight("某个新任务任务时间：2026.10.01-2027.10.01")
    assert 0 < w <= 0.05


def test_cursor_advances():
    """每次选择 cursor 递增，保证轮换推进。"""
    titles = ["扫墓丫头，替祖宗横扫天下任务时间：a"]
    _, cur1 = pick_spark_title(titles, cursor=0)
    _, cur2 = pick_spark_title(titles, cursor=cur1)
    assert cur2 > cur1
