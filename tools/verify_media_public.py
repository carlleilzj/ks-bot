"""验证 IG 媒体公网链路：签名 URL 能通过 nginx 从公网拉取。"""
import sys
sys.path.insert(0, "/opt/ks-bot")

import subprocess

from bot.config import FINAL_DIR, load_settings
from bot.publish.media_upload import build_public_url

# load_settings 内部会 load_dotenv(ENV_PATH)，把 .env 注入 os.environ
load_settings()

videos = sorted(FINAL_DIR.glob("*_final.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
if not videos:
    print("没有可用的成品视频")
    sys.exit(1)

video = videos[0]
print(f"用视频：{video.name}（{video.stat().st_size // 1024} KB）")

url = build_public_url(video)
print(f"签名 URL：{url[:110]}...")

host = url.split("//")[1].split("/")[0]
r = subprocess.run(
    ["curl", "-s", "--resolve", f"{host}:443:62.106.75.207", "-o", "/dev/null",
     "-w", "%{http_code} %{size_download} %{content_type}", url],
    capture_output=True, text=True, timeout=120)
print(f"公网拉取结果：HTTP {r.stdout}")

code = r.stdout.split()[0] if r.stdout else "?"
if code == "200":
    print("✅ 公网链路打通：Meta 可以从这个 URL 拉到视频")
else:
    print(f"❌ 公网拉取失败（HTTP {code}）")
    sys.exit(1)
