"""账号环境隔离诊断：判断多个账号是否被平台视为「同一环境」。

为什么需要它
------------
2026 年的平台风控早已不只看内容，还会算**账号之间的技术关联**：
同设备指纹、同登录态、同出口 IP 都会让多个号被归成一簇，
一个号出事连坐其他号。

我们手上是 2 个快手号 + 4 个 IG 号，天然共享一台机器。
本模块把「关联程度」量化出来，让人一眼看出风险点在哪，
而不是等封号后猜原因。

诊断维度（与 CreatorHub 的 transport_matrix 同思路，按需精简）
--------------------------------------------------------------
    profile_isolated    浏览器 Profile 目录是否唯一
    credential_isolated 登录态指纹（SHA256）是否与其他账号重复
    network_isolated    出口 IP 是否与其他账号相同
    environment_id      综合环境指纹（前缀），相同即高风险

注意：IG 走的是 Meta 官方 API，没有浏览器 Profile；
      它的「登录态」是 token，我们按 token 指纹去重。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 单个账号的环境快照
# ---------------------------------------------------------------------------

@dataclass
class AccountEnv:
    """一个账号的环境快照。"""
    platform: str
    display_name: str = ""
    # 浏览器 Profile / 登录态文件路径（IG 为空——它走 API 无状态文件）
    profile_path: str = ""
    profile_exists: bool = False
    # 登录态指纹（不含原始凭据，只存摘要）
    credential_fp: str = ""
    # 出口 IP（探测失败留空）
    exit_ip: str = ""
    # 综合环境指纹
    environment_id: str = ""

    def fingerprint(self) -> str:
        """环境指纹：把身份相关因素揉成短哈希，相同即关联。"""
        seed = "|".join((
            self.platform,
            self.credential_fp,
            self.exit_ip,
        ))
        return hashlib.sha256(seed.encode()).hexdigest()[:12]


@dataclass
class IsolationReport:
    """诊断报告。"""
    accounts: list[AccountEnv] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def same_network_groups(self) -> dict[str, list[str]]:
        """按出口 IP 分组，返回 {ip: [platform, ...]}（只返回有 2+ 账号的组）。"""
        groups: dict[str, list[str]] = {}
        for a in self.accounts:
            if a.exit_ip:
                groups.setdefault(a.exit_ip, []).append(a.platform)
        return {ip: plats for ip, plats in groups.items() if len(plats) > 1}

    def duplicate_credentials(self) -> dict[str, list[str]]:
        """按登录态指纹分组，返回重复使用同一凭据的账号（严重问题）。"""
        groups: dict[str, list[str]] = {}
        for a in self.accounts:
            if a.credential_fp:
                groups.setdefault(a.credential_fp, []).append(a.platform)
        return {fp: plats for fp, plats in groups.items() if len(plats) > 1}


# ---------------------------------------------------------------------------
# 凭据指纹（只算摘要，不读明文内容进日志）
# ---------------------------------------------------------------------------

def _fp_file(path: Path) -> str:
    """文件内容指纹（SHA256 前 16 位）。文件不存在返回空。"""
    try:
        if not path.is_file():
            return ""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return ""


def _fp_storage_state(path: Path) -> str:
    """storage_state JSON 的语义指纹：只取 cookies 的 name+value，忽略格式差异。

    直接哈希整文件会被「键顺序 / 空白」干扰，导致同一份登录态算出不同指纹
    （从而漏报重复）。这里归一化后再哈希。
    """
    try:
        if not path.is_file():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        norm = sorted(
            f"{c.get('name')}={c.get('value')}@{c.get('domain','')}"
            for c in cookies if isinstance(c, dict)
        )
        if not norm:
            return ""
        return hashlib.sha256("\n".join(norm).encode()).hexdigest()[:16]
    except Exception:
        return _fp_file(path)


def _fp_token(token: str) -> str:
    """token 指纹：绝不打明文，只取摘要前缀。"""
    token = (token or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 出口 IP 探测
# ---------------------------------------------------------------------------

IP_PROBE_URLS = (
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/ip",
)


def probe_exit_ip(timeout: float = 8.0) -> str:
    """探测本机出口 IP。失败返回空串（不阻断诊断）。"""
    for url in IP_PROBE_URLS:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as c:
                r = c.get(url)
            if r.status_code != 200:
                continue
            text = r.text.strip()
            if text.startswith("{"):
                try:
                    return str(json.loads(text).get("ip") or "")
                except Exception:
                    continue
            if re.fullmatch(r"[0-9a-fA-F:.]{7,45}", text):
                return text
        except Exception as e:
            log.debug("出口 IP 探测失败（%s）：%s", url, str(e)[:80])
    return ""


# ---------------------------------------------------------------------------
# 采集各账号环境
# ---------------------------------------------------------------------------

# 浏览器型平台的登录态文件名（与 publish/__init__.py 的 state_path_for 一致）
_BROWSER_STATE_FILES = {
    "kuaishou": "ks_state.json",
    "kuaishou2": "ks2_state.json",
    "douyin": "douyin_state.json",
    "xhs": "xhs_state.json",
    "weixin": "weixin_state.json",
    "toutiao": "toutiao_state.json",
}

# IG 矩阵号：走 Meta API，凭据在环境变量
_IG_ENV_KEYS = {
    "ig_pets": "IG_PETS",
    "ig_satisfying": "IG_SATISFYING",
    "ig_anim": "IG_ANIM",
    "ig_home": "IG_HOME",
}


def collect_envs(data_dir: Path, platforms: dict | None = None,
                 probe_ip: bool = True) -> list[AccountEnv]:
    """采集所有启用平台的环境快照。

    platforms: Settings.platforms（用来判断启用状态与显示名）。
    probe_ip: 是否探测出口 IP（离线诊断时关掉，省时间）。
    """
    envs: list[AccountEnv] = []
    exit_ip = probe_exit_ip() if probe_ip else ""

    # 浏览器型平台
    for platform, filename in _BROWSER_STATE_FILES.items():
        cfg = (platforms or {}).get(platform)
        if cfg is not None and not getattr(cfg, "enabled", False):
            continue
        state_path = data_dir / filename
        env = AccountEnv(
            platform=platform,
            display_name=getattr(cfg, "display_name", "") or platform,
            profile_path=str(state_path),
            profile_exists=state_path.is_file(),
            credential_fp=_fp_storage_state(state_path),
            exit_ip=exit_ip,
        )
        env.environment_id = env.fingerprint()
        envs.append(env)

    # IG 矩阵号（token 指纹）
    for platform, key in _IG_ENV_KEYS.items():
        cfg = (platforms or {}).get(platform)
        if cfg is not None and not getattr(cfg, "enabled", False):
            continue
        token = (os.getenv(f"IG_TOKEN_{key}") or "").strip()
        user_id = (os.getenv(f"IG_USERID_{key}") or "").strip()
        env = AccountEnv(
            platform=platform,
            display_name=getattr(cfg, "display_name", "") or platform,
            profile_path="",          # API 通道无 Profile
            profile_exists=bool(token and user_id),
            # 用 user_id + token 摘要一起算：同一 token 换绑不同号也要能区分
            credential_fp=_fp_token(f"{user_id}:{token}") if token else "",
            exit_ip=exit_ip,
        )
        env.environment_id = env.fingerprint()
        envs.append(env)

    return envs


def analyze(envs: list[AccountEnv]) -> IsolationReport:
    """生成诊断结论。"""
    report = IsolationReport(accounts=list(envs))

    # 1) 同出口 IP（最多账号共享同一 IP 是最主要的风险源）
    net_groups = report.same_network_groups()
    for ip, plats in net_groups.items():
        report.warnings.append(
            f"⚠️ {len(plats)} 个账号共享出口 IP {ip}：{', '.join(plats)}"
            f" — 平台可能将其判定为同一操作者（连坐风险）"
        )

    # 2) 凭据重复（同一份登录态被两个账号使用，几乎必被封）
    dup_creds = report.duplicate_credentials()
    for fp, plats in dup_creds.items():
        report.warnings.append(
            f"🚨 {len(plats)} 个账号使用了**同一份登录态**：{', '.join(plats)}"
            f"（指纹 {fp}）— 这是最高危配置，必须立即修复"
        )

    # 3) 登录态缺失（配置错或过期，发布必然失败）
    missing = [a.platform for a in envs if not a.profile_exists]
    if missing:
        report.warnings.append(
            f"⚠️ 以下账号缺少登录态/凭据：{', '.join(missing)}"
        )

    # 4) IG 矩阵号全部同 IP（提示但不算错误——官方 API 通道本就不挑 IP）
    ig_plats = [a.platform for a in envs if a.platform.startswith("ig_") and a.exit_ip]
    net_groups = report.same_network_groups()
    if len(ig_plats) >= 2:
        ig_set = set(ig_plats)
        if any(ig_set <= set(v) for v in net_groups.values()):
            report.warnings.append(
                "ℹ️ IG 矩阵号共享同一出口 IP —— 走 Meta 官方 API 时这**不是**封号主因，"
                "但账号注册/养号阶段仍建议错开网络"
            )

    return report
