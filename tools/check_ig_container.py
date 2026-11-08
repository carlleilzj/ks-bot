"""检查一个已创建容器的处理状态，验证 Meta 确实能拉到并处理我们的视频。"""
import sys
sys.path.insert(0, "/opt/ks-bot")

from bot.config import load_settings
from bot.publish import instagram as ig

load_settings()

if len(sys.argv) < 3:
    print("用法：python check_ig_container.py <account_key> <container_id>")
    sys.exit(2)

account, cid = sys.argv[1], sys.argv[2]
acct = ig.load_account(account)
if not acct:
    print(f"✗ 未配置凭据：{account}")
    sys.exit(1)

print(f"账号 {account}（{acct.channel}），容器 {cid}")
try:
    status = ig.wait_container_ready(acct, cid, max_wait_sec=300)
    print(f"\n✅ 容器已就绪：{status}")
    print("Meta 已成功拉取并处理我们的公网视频，真实发布链路可用")
except Exception as e:
    print(f"\n❌ 容器未就绪：{e}")
    sys.exit(1)
