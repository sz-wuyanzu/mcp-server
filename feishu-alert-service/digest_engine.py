"""Alert Digest Engine — periodic summarization of group messages."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from feishu_client import FeishuClient
from llm_client import LLMClient

logger = logging.getLogger(__name__)

SEGMENT_PROMPT = (
    "你是一个运维告警分析助手。以下是最近一段时间的飞书群消息（包含告警和普通对话）。\n"
    "请提取并总结其中的告警信息，按服务/模块分类，标注严重程度和出现次数。\n"
    "忽略普通闲聊。如果没有告警消息，回复'无告警'。\n"
    "用简洁的中文输出，不要超过 500 字。"
)

REPORT_PROMPT = (
    "你是一个运维告警分析助手。以下是过去一段时间内多个时间段的告警摘要。\n"
    "请将它们归总为一份完整的告警报告：\n"
    "1. 按服务/模块分类汇总\n"
    "2. 标注每类告警的总出现次数和时间范围\n"
    "3. 高亮最严重或最频繁的问题\n"
    "4. 如果所有摘要都是'无告警'，回复'本周期内无告警'\n"
    "用简洁的中文输出。"
)


@dataclass
class ChatConfig:
    chat_id: str
    name: str = ""
    segment_interval: int = 10   # minutes
    report_interval: int = 240   # minutes
    max_chars_per_fetch: int = 30000


class _Storage:
    """File-based storage for digest state."""

    def __init__(self, data_dir: Path):
        self._dir = data_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _safe_name(self, chat_id: str) -> str:
        return chat_id.replace("/", "_")

    def read_last_ts(self, chat_id: str) -> Optional[str]:
        p = self._dir / f"{self._safe_name(chat_id)}.last_ts"
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or None
        return None

    def write_last_ts(self, chat_id: str, ts: str) -> None:
        (self._dir / f"{self._safe_name(chat_id)}.last_ts").write_text(ts, encoding="utf-8")

    def read_digest(self, chat_id: str) -> str:
        p = self._dir / f"{self._safe_name(chat_id)}.md"
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        return ""

    def append_digest(self, chat_id: str, entry: str) -> None:
        with open(self._dir / f"{self._safe_name(chat_id)}.md", "a", encoding="utf-8") as f:
            f.write(entry)

    def clear_digest(self, chat_id: str) -> None:
        p = self._dir / f"{self._safe_name(chat_id)}.md"
        p.write_text("", encoding="utf-8")


class ChatWorker:
    """Manages the digest cycle for a single chat."""

    def __init__(
        self,
        cfg: ChatConfig,
        feishu: FeishuClient,
        llm: LLMClient,
        storage: _Storage,
        segment_prompt: str = SEGMENT_PROMPT,
        report_prompt: str = REPORT_PROMPT,
    ):
        self.cfg = cfg
        self._feishu = feishu
        self._llm = llm
        self._storage = storage
        self._segment_prompt = segment_prompt
        self._report_prompt = report_prompt
        self._last_segment_time: float = 0
        self._last_report_time: float = 0
        self._report_timer_started = False

    @property
    def label(self) -> str:
        return self.cfg.name or self.cfg.chat_id

    def tick(self) -> None:
        """Called every tick. Runs segment and/or report if due."""
        now = time.time()

        # Segment check
        if now - self._last_segment_time >= self.cfg.segment_interval * 60:
            try:
                self._process_segment()
            except Exception:
                logger.warning("[%s] Segment error", self.label, exc_info=True)
            self._last_segment_time = time.time()

        # Report check
        if not self._report_timer_started:
            self._last_report_time = now
            self._report_timer_started = True
            logger.info("[%s] Report timer started, first report in %d min", self.label, self.cfg.report_interval)
            return

        if now - self._last_report_time >= self.cfg.report_interval * 60:
            try:
                self._send_report()
            except Exception:
                logger.warning("[%s] Report error", self.label, exc_info=True)
            self._last_report_time = time.time()

    def _process_segment(self) -> None:
        last_ts = self._storage.read_last_ts(self.cfg.chat_id)

        # Reset if too old or missing
        now_ms = str(int(time.time() * 1000))
        max_age_ms = self.cfg.segment_interval * 60 * 1000 * 3
        if not last_ts or (int(now_ms) - int(last_ts)) > max_age_ms:
            fresh_ts = str(int(time.time() * 1000) - self.cfg.segment_interval * 60 * 1000)
            logger.info("[%s] Resetting last_ts to recent window", self.label)
            last_ts = fresh_ts
            self._storage.write_last_ts(self.cfg.chat_id, last_ts)

        lines, new_ts = self._feishu.fetch_messages(
            self.cfg.chat_id, since_ts=last_ts, max_chars=self.cfg.max_chars_per_fetch,
        )
        if new_ts and new_ts != last_ts:
            self._storage.write_last_ts(self.cfg.chat_id, new_ts)

        if not lines:
            return

        raw_text = "\n".join(lines)
        logger.info("[%s] Fetched %d messages (%d chars)", self.label, len(lines), len(raw_text))

        summary = self._llm.summarize(raw_text, self._segment_prompt)
        if not summary:
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._storage.append_digest(self.cfg.chat_id, f"\n## {ts}\n{summary}\n")
        logger.info("[%s] Segment done: %d msgs -> %d chars", self.label, len(lines), len(summary))

    def _send_report(self) -> None:
        digest_text = self._storage.read_digest(self.cfg.chat_id)
        if not digest_text:
            return

        # Truncate to avoid LLM proxy 504
        if len(digest_text) > 1500:
            digest_text = digest_text[-1500:]

        logger.info("[%s] Generating report...", self.label)
        report = self._llm.summarize(digest_text, self._report_prompt)

        if not report or report.strip() in ("无告警", "本周期内无告警"):
            self._storage.clear_digest(self.cfg.chat_id)
            logger.info("[%s] No alerts, skipping report", self.label)
            return

        minutes = self.cfg.report_interval
        period = f"过去 {minutes // 60} 小时" if minutes >= 60 else f"过去 {minutes} 分钟"
        header = f"📊 告警汇总报告 — {self.label}（{period}）\n\n"

        if self._feishu.send_message(self.cfg.chat_id, header + report):
            logger.info("[%s] Report sent", self.label)
        else:
            logger.warning("[%s] Report send failed", self.label)

        self._storage.clear_digest(self.cfg.chat_id)


class DigestEngine:
    """Runs all chat workers in a loop."""

    def __init__(
        self,
        workers: List[ChatWorker],
        tick_interval: int = 60,
    ):
        self._workers = workers
        self._tick_interval = tick_interval

    def run_forever(self) -> None:
        logger.info("Digest engine started: %d chat(s)", len(self._workers))
        while True:
            for worker in self._workers:
                worker.tick()
            time.sleep(self._tick_interval)
