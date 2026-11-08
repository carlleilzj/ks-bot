"""验证 4 个 IG 账号切换到专业号后的发布权限。"""
import sys
sys.path.insert(0, "/opt/ks-bot")

from bot.config import load_settings
from bot.publish import instagram as ig

load_settings()

for key in ("ig_pets", "ig_satisfying", "ig_anim", "ig_home"):
    acct = ig.load_account(key)
    print("\n--- " + key + " ---")
    try:
        d = ig._request("GET", acct.ig_user_id, acct, params={"fields": "username"})
        name = d.get("username")
        print("  @" + str(name) + "  通道=" + acct.channel)
    except Exception as e:
        print("  ✗ 基础查询失败：" + str(e)[:120])

    try:
        d = ig._request("GET", acct.ig_user_id + "/content_publishing_limit", acct,
                        params={"fields": "quota_usage,config"})
        it = (d.get("data") or [{}])[0]
        used = it.get("quota_usage")
        total = (it.get("config") or {}).get("quota_total")
        print("  发布配额 " + str(used) + "/" + str(total) + "  ✓ 有 content_publish 权限")
    except Exception as e:
        print("  ✗ 配额查询失败：" + str(e)[:120])

    try:
        d = ig._request("GET", acct.ig_user_id + "/media", acct,
                        params={"fields": "id", "limit": 1})
        print("  media 接口可访问 ✓（专业号特征）")
    except Exception as e:
        print("  ✗ media 接口不可用：" + str(e)[:120])
