#!/usr/bin/env python3
"""YouTube OAuth2 授权小助手：获取 YOUTUBE_REFRESH_TOKEN 并输出配置项。

用法：
    python tools/youtube_auth.py
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "http://localhost:8080/"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]


class _OAuthHandler(BaseHTTPRequestHandler):
    code: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "code" in qs:
            _OAuthHandler.code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h1>授权成功！您可以关闭此页面，返回终端查看 Refresh Token。</h1>".encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Failed: No authorization code received.")

    def log_message(self, fmt, *args):
        pass


def main():
    print("=" * 60)
    print(" YouTube Data API v3 快速授权小工具")
    print("=" * 60)
    print("提示：请先在 Google Cloud Console 创建 OAuth 2.0 客户端 ID（类型选择 Web 应用），")
    print(f"并在「已获授权的重定向 URI」中添加：{REDIRECT_URI}\n")

    client_id = os.getenv("YOUTUBE_CLIENT_ID") or input("请输入 Client ID: ").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET") or input("请输入 Client Secret: ").strip()

    if not client_id or not client_secret:
        print("❌ Client ID 和 Client Secret 不能为空！")
        sys.exit(1)

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_ENDPOINT}?{urlencode(params)}"

    print("\n👉 请在浏览器中打开以下链接完成 Google 账号授权：\n")
    print(auth_url)
    print("\n正在本地 8080 端口等待回调（若无法自动回调，也可直接把浏览器地址栏的完整跳转 URL 贴在下面）...")

    server = HTTPServer(("127.0.0.1", 8080), _OAuthHandler)
    server.timeout = 180

    try:
        server.handle_request()
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(0)

    code = _OAuthHandler.code
    if not code:
        manual = input("\n未能自动接收回调，请直接粘贴跳转后的完整 URL 或 code: ").strip()
        if "code=" in manual:
            qs = parse_qs(urlparse(manual).query)
            code = qs.get("code", [manual])[0]
        else:
            code = manual

    if not code:
        print("❌ 未获取到授权 code")
        sys.exit(1)

    print("\n正在向 Google 交换 Refresh Token...")
    token_data = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(TOKEN_ENDPOINT, data=token_data)
        if resp.status_code != 200:
            print(f"❌ 交换 Token 失败 (HTTP {resp.status_code}): {resp.text}")
            sys.exit(1)
        res = resp.json()

    refresh_token = res.get("refresh_token")
    if not refresh_token:
        print("⚠️ Google 未返回 refresh_token（可能此前已授权）。请在授权链接重新带上 prompt=consent，或在 Google 账号中移除应用权限后重试。")
        print("完整返回：", res)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("🎉 授权成功！请将以下配置写入项目 .env 文件：")
    print("=" * 60)
    print(f"YOUTUBE_CLIENT_ID={client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={refresh_token}")
    print("=" * 60)


if __name__ == "__main__":
    main()
