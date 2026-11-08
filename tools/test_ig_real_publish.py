"""真实发布测试：用一条已有成品视频发到指定 IG 账号。

⚠️ 会真实发布到 Instagram，产生一条公开 Reels。
用法：
    python tools/test_ig_real_publish.py ig_pets
    python tools/test_ig_real_publish.py ig_pets --dry-run   # 只验证到「建容器」为止
"""
import sys
sys.path.insert(0, "/opt/ks-bot")

from pathlib import Path

from bot.config import FINAL_DIR, load_settings
from bot.publish import instagram as ig
from bot.publish.media_upload import build_public_url

load_settings()

args = [a for a in sys.argv[1:] if not a.startswith("--")]
dry_run = "--dry-run" in sys.argv

if not args:
    print("用法：python tools/test_ig_real_publish.py <account_key> [--dry-run]")
    print("可用：ig_pets / ig_satisfying / ig_anim / ig_home")
    sys.exit(2)

account = args[0]
acct = ig.load_account(account)
if not acct:
    print(f"✗ 未配置凭据：{account}")
    sys.exit(1)

videos = sorted(FINAL_DIR.glob("*_final.mp4"),
                key=lambda p: p.stat().st_mtime, reverse=True)
if not videos:
    print("✗ media/final 里没有成品视频")
    sys.exit(1)

video = videos[0]
print(f"账号：{account}（@{acct.ig_user_id}，通道 {acct.channel}）")
print(f"视频：{video.name}（{video.stat().st_size // 1024} KB）")

url = build_public_url(video)
print(f"媒体URL：{url.split('?')[0][:100]}...")

caption = ig.compose_caption(
    "test post — please ignore 🧪",
    "this is an automated publish test from the ks-bot pipeline",
    ["test", "pipeline"])
print(f"caption：{caption!r}")

if dry_run:
    print("\n[DRY-RUN] 只验证到建容器，不真正发布")
    cid = ig.create_reel_container(acct, url, caption, share_to_feed=False)
    print(f"✓ 容器已创建：{cid}")
    print("（容器 24 小时后自动过期，不会产生公开作品）")
    sys.exit(0)

print("\n开始真实发布...")
permalink = ig.publish(
    video=video, title="test post — please ignore 🧪",
    description="this is an automated publish test from the ks-bot pipeline",
    tags=["test", "pipeline"], account_key=account, media_url=url)

print(f"\n✅ 发布成功：{permalink}")
print("请到 IG 上确认，并手动删除这条测试作品")
