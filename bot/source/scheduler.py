"""发现调度线程：周期性调用各 SourceAdapter，过滤后入库 CANDIDATE，
预下载封面，发 TG 审核卡片（PENDING_REVIEW）。

独立 daemon 线程，与主循环和 TG 监听线程共享同一个 Database 实例。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import httpx

from ..config import WORK_DIR, Settings
from ..db import Database, State
from ..notify import telegram
from .discovery import Candidate, SourceAdapter, build_adapters
from .downloader import extract_meta, parse_url
from .filter import FilterChain, FilterRules

log = logging.getLogger(__name__)


class DiscoveryScheduler(threading.Thread):
    """周期发现 + 过滤 + 入库 + 发审核卡片。"""

    daemon = True

    def __init__(self, s: Settings, db: Database, wakeup: threading.Event | None = None):
        super().__init__(name="discovery")
        self.s = s
        self.db = db
        self._stop = threading.Event()
        self._wakeup = wakeup
        self.adapters: list[SourceAdapter] = build_adapters(s)
        disc = getattr(s, "discovery", None)
        self.enabled = bool(disc and disc.enabled and self.adapters)
        rules = FilterRules.from_dict(getattr(disc, "filters", None) if disc else None)
        self.chain = FilterChain(rules)
        self.reject_real_person = rules.reject_real_person
        self.interval_sec = (disc.interval_min if disc else 60) * 60
        self.limit_per_source = disc.limit_per_source if disc else 20
        self.max_pending_review = disc.max_pending_review if disc else 10

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        if not self.enabled:
            log.info("发现层未启用（config.yaml discovery.enabled=false 或无 sources）")
            return
        log.info("发现层已启动：%d 个适配器，间隔 %d 分钟", len(self.adapters), self.interval_sec // 60)
        telegram.notify_info(self.s, f"🔎 发现层已启动\n"
                                     f"适配器：{len(self.adapters)} 个\n"
                                     f"间隔：{self.interval_sec // 60} 分钟\n"
                                     f"每轮每源最多取 {self.limit_per_source} 条\n"
                                     f"待审核堆积上限 {self.max_pending_review} 条")
        # 首次启动等 10 秒（让 TG 监听先起来），之后按间隔跑
        self._stop.wait(10)
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                log.exception("发现层异常，60s 后重试：%s", e)
                self._stop.wait(60)
            self._stop.wait(self.interval_sec)

    def _cycle(self) -> None:
        """一轮发现：跑各适配器 → 去重 → 过滤 → 入库 → 发卡片。"""
        # 堆积保护：待审核超过上限就暂停发现，避免刷屏
        pending = self.db.pending_review_count()
        if pending >= self.max_pending_review:
            log.info("待审核 %d 条已达上限 %d，本轮发现跳过", pending, self.max_pending_review)
            if pending == self.max_pending_review:
                telegram.notify_info(self.s, f"⏸️ 待审核已达 {pending} 条，发现层暂停\n"
                                             f"请在 TG 审核后再继续")
            return

        # 收集所有候选
        all_cands: list[Candidate] = []
        for ad in self.adapters:
            try:
                all_cands.extend(ad.discover(limit=self.limit_per_source))
            except Exception as e:
                log.warning("适配器 %s 失败：%s", ad.name, e)

        # 去重（内存层；DB 层 source_url UNIQUE 是最终防线）
        seen_ids: set[str] = set()
        sent = 0
        # 按 score 降序（YouTube search 的 view_count；RSS 的发布顺序近似）
        for c in sorted(all_cands, key=lambda x: -x.score):
            if c.video_id in seen_ids:
                continue
            seen_ids.add(c.video_id)

            ok, reason = self.chain.keep(c)
            if not ok:
                log.debug("[discovery] 丢弃 %s：%s（%s）", c.video_id, reason, c.title[:40])
                continue

            # DB 查重（source_url 已存在）
            clean_url = parse_url(c.url)
            if self.db.find_by_source_url(clean_url):
                continue

            # 拿标准元数据（caption/thumbnail 等）
            try:
                meta = extract_meta(clean_url)
            except Exception as e:
                log.warning("[discovery] 解析失败 %s：%s", clean_url, str(e)[:120])
                continue

            # 入库为 CANDIDATE
            tid = self.db.insert_video(meta, state=State.CANDIDATE, source_tag=c.source_tag)
            if tid is None:
                continue  # 竞态：已被插入

            # 预下载封面（审核卡片要用）
            cover = WORK_DIR / f"{meta.shortcode}_cover.jpg"
            if meta.thumbnail_url:
                try:
                    self._download_cover(meta.thumbnail_url, cover)
                except Exception as e:
                    log.debug("[discovery] 封面下载失败 %s：%s", meta.shortcode, e)

            self.db.update(tid,
                            cover_path=str(cover) if cover.exists() else None,
                            title=meta.title or c.title,
                            source_url=meta.source_url)

            # 封面 AI 质检（可选）：一次调用判 真人/水印/是否动画 三项。
            # 动物动画赛道：真人、水印、明确非动画都直接丢弃。
            # 必须在封面下载之后做（需要图片文件）。
            if self.reject_real_person and cover.exists():
                try:
                    from ..ai.vision import inspect_cover
                    verdict = inspect_cover(cover, self.s)
                    if not verdict.ok_for_animal_anime:
                        why = ("真人镜头" if verdict.has_real_person else
                               f"水印: {verdict.watermark_desc}" if verdict.has_watermark else
                               "非动画内容")
                        self.db.reject(tid, f"{why}（AI 封面检测）")
                        log.info("[discovery] 丢弃 %s：%s", meta.shortcode, why)
                        continue
                except Exception as e:
                    # 检测失败不误杀：放行进入审核，由人工把关
                    log.warning("[discovery] 封面质检异常（放行）：%s", e)

            # 转为 PENDING_REVIEW 并发审核卡片
            self.db.update(tid, state=State.PENDING_REVIEW)
            task = self.db.get(tid)
            if task:
                telegram.send_review_card(self.s, task)
                sent += 1

            # 每轮最多发 max_pending_review 张卡片，避免 TG 刷屏
            if sent >= self.max_pending_review:
                break

        log.info("发现层本轮：候选 %d，发审核 %d（仍待审核 %d）",
                 len(all_cands), sent, self.db.pending_review_count())

        # 待审核堆积到上限，通知一次
        if self.db.pending_review_count() >= self.max_pending_review and sent > 0:
            telegram.notify_info(self.s, f"📥 发现层已发 {sent} 条待审核\n"
                                         f"待审核堆积 {self.db.pending_review_count()} 条，"
                                         f"已达上限 {self.max_pending_review}，发现层暂停")

    @staticmethod
    def _download_cover(url: str, dest: Path) -> None:
        """下载缩略图到 dest。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code == 200 and resp.content:
                dest.write_bytes(resp.content)
