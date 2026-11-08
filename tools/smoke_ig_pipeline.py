"""端到端链路验证：媒体托管 → API 发布流程（mock Meta）→ worker 集成。

这个脚本不是单测，是「上线前手工跑一次」的冒烟脚本：
1. 真起媒体服务，用真实签名 URL 做一次 curl，确认 nginx 之外的部分通；
2. mock 掉 Meta 的三个端点，走通 publish() 的完整分支；
3. 验证 worker 传给 publish() 的参数对不对（account_key / media_url）。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("IG_MEDIA_SECRET", "smoke-test-secret")

from bot.publish import instagram as ig          # noqa: E402
from bot.publish import media_upload as mu       # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def step1_media_server() -> bool:
    """起媒体服务，验证签名 URL 能下载、坏 token 被拒。"""
    print("\n[1/3] 媒体托管冒烟")
    final = Path("/tmp/ig_smoke_final")
    final.mkdir(exist_ok=True)
    video = final / "smoke_final.mp4"
    video.write_bytes(b"\x00" * 4096)

    import bot.publish.media_upload as mod
    original = mod.FINAL_DIR
    mod.FINAL_DIR = final

    port = _free_port()
    mu.start_media_server("127.0.0.1", port)
    time.sleep(0.5)

    base = f"http://127.0.0.1:{port}"
    url = mu.build_public_url(video, base_url=base)

    # 正常 URL 应该能下载
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = r.read()
            ct = r.headers.get("Content-Type", "")
        assert len(data) == 4096, f"长度不对：{len(data)}"
        assert "video/mp4" in ct, f"Content-Type 不对：{ct}"
        print(f"    ✓ 签名 URL 可下载（{len(data)} bytes, {ct}）")
    except Exception as e:
        print(f"    ✗ 下载失败：{e}")
        mod.FINAL_DIR = original
        return False

    # 篡改 token 必须被拒
    bad = url.replace("/ig-media/", "/ig-media/DEADBEEF.")
    try:
        urllib.request.urlopen(bad, timeout=5)
        print("    ✗ 篡改 token 竟然通过了！")
        mod.FINAL_DIR = original
        return False
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("    ✓ 篡改 token 被拒绝（403）")
        else:
            print(f"    ✗ 篡改 token 返回 {e.code}，期望 403")
            mod.FINAL_DIR = original
            return False

    # 路径穿越必须被拒（签名有效但路径跳出 final 目录）
    escape_rel = "../../etc/passwd"
    esc_token = mu.build_media_token(escape_rel)
    esc_url = f"{base}/ig-media/{esc_token}/{escape_rel}"
    try:
        urllib.request.urlopen(esc_url, timeout=5)
        print("    ✗ 路径穿越竟然通过了！")
        mod.FINAL_DIR = original
        return False
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            print(f"    ✓ 路径穿越被拒绝（{e.code}）")
        else:
            print(f"    ✗ 路径穿越返回 {e.code}")
            mod.FINAL_DIR = original
            return False

    mod.FINAL_DIR = original
    return True


def step2_publish_flow() -> bool:
    """mock Meta 三端点，走通 publish()。"""
    print("\n[2/3] API 发布流程冒烟（mock Meta）")
    calls: list[str] = []

    acct = ig.IgAccount(key="ig_pets", ig_user_id="17841400000000000",
                        access_token="IGAAxxxx")

    def fake_create(a, video_url, caption, **kw):
        calls.append(f"create:{video_url.split('/')[-1]}")
        assert caption.startswith("hook"), f"caption 首行不是钩子：{caption[:40]}"
        return "cid_smoke"

    def fake_wait(a, cid, **kw):
        calls.append("wait")
        return "FINISHED"

    def fake_publish(a, cid):
        calls.append("publish")
        return "mid_smoke"

    def fake_permalink(a, mid):
        calls.append("permalink")
        return "https://www.instagram.com/reel/SMOKE/"

    with patch.object(ig, "load_account", return_value=acct), \
         patch.object(ig, "check_publishing_quota", return_value=2), \
         patch.object(ig, "create_reel_container", side_effect=fake_create), \
         patch.object(ig, "wait_container_ready", side_effect=fake_wait), \
         patch.object(ig, "publish_container", side_effect=fake_publish), \
         patch.object(ig, "fetch_permalink", side_effect=fake_permalink):
        url = ig.publish(
            Path("/tmp/smoke_final.mp4"),
            "hook line here", "supporting desc",
            ["animation", "funnyvideos", "fyp"],
            account_key="ig_pets",
            media_url="https://media.example.com/ig-media/abc/smoke_final.mp4",
        )

    expected = ["create:smoke_final.mp4", "wait", "publish", "permalink"]
    if calls != expected:
        print(f"    ✗ 调用顺序不对：{calls}")
        return False
    own = [c for c in calls if c.startswith("create:smoke_final.mp4")]
    if not own:
        print("    ✗ 没有把 media_url 传给 create_reel_container")
        return False
    print(f"    ✓ 流程顺序正确：{' → '.join(calls)}")
    print(f"    ✓ 返回 permalink：{url}")
    return True


def step3_worker_params() -> bool:
    """确认 worker 会把 account_key 和 media_url 传给 IG publish()。"""
    print("\n[3/3] Worker 参数传递冒烟")
    import inspect
    from bot import publish_worker as pw
    src = inspect.getsource(pw.process_platform)

    checks = [
        ('extra["account_key"] = platform', "account_key 未透传"),
        ('extra["media_url"] = ig_media_url', "media_url 未透传"),
        ("fetch_media_url(base, s, task_id)", "未调用协调端取媒体 URL"),
        ("is_ig = platform.startswith(\"ig_\")", "缺少 IG 分支判定"),
    ]
    ok = True
    for needle, err in checks:
        if needle not in src:
            print(f"    ✗ {err}（找不到 {needle!r}）")
            ok = False
    if ok:
        print("    ✓ worker 分支与参数传递完整")
    return ok


def main() -> int:
    results = [step1_media_server(), step2_publish_flow(), step3_worker_params()]
    print("\n" + "=" * 56)
    if all(results):
        print("✅ 冒烟全通过")
        print("=" * 56)
        return 0
    print("❌ 有步骤失败，见上面 ✗ 项")
    print("=" * 56)
    return 1


if __name__ == "__main__":
    sys.exit(main())
