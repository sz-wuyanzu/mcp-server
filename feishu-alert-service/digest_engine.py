"""Alert Digest Engine — periodic summarization of group messages.

Architecture:
    DigestEngine  (scheduler loop)
      └── ChatWorker[]  (one per monitored chat)
            ├── _process_segment()  — fetch recent msgs → LLM summarize → append to digest file
            └── _send_report()      — read digest file → LLM summarize → send to chat → clear

Storage layout (data_dir/):
    {chat_id}.last_ts   — epoch-ms of last processed message
    {chat_id}.md        — accumulated segment summaries (cleared after report)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default prompts
# ---------------------------------------------------------------------------

SEGMENT_PROMPT = ""
REPORT_PROMPT = ""


# ---------------------------------------------------------------------------
# Protocols (for loose coupling — no direct import of concrete classes)
# ---------------------------------------------------------------------------

class MessageFetcher(Protocol):
    """Anything that can fetch and send Feishu messages."""

    def fetch_messages(
        self, chat_id: str, since_ts: Optional[str] = None, max_chars: int = 30000,
    ) -> tuple[list[str], Optional[str]]: ...

    def send_message(self, chat_id: str, text: str, mention_all: bool = False) -> bool: ...


class Summarizer(Protocol):
    """Anything that can summarize text."""

    def summarize(self, text: str, system_prompt: str) -> str: ...


# ---------------------------------------------------------------------------
# Config & Storage
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatConfig:
    chat_id: str
    name: str = ""
    enabled: bool = True
    mention_all: bool = False
    segment_interval: int = 10   # minutes — 每隔多少分钟做一次分段摘要
    report_cycle: int = 6        # 每 N 次分段摘要后发一次归总报告（归总间隔 = segment_interval * report_cycle）
    max_chars_per_fetch: int = 30000

    def __post_init__(self) -> None:
        if self.segment_interval < 1:
            object.__setattr__(self, "segment_interval", 1)
        if self.report_cycle < 1:
            object.__setattr__(self, "report_cycle", 1)


class Storage:
    """File-based storage for digest state.

    All I/O is wrapped with exception handling — a disk error will not crash the engine.
    """

    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            logger.info("Storage 目录: %s", self._dir.resolve())
        except OSError as exc:
            logger.error("创建 Storage 目录失败: %s — %s", self._dir, exc)
            raise

    def _path(self, chat_id: str, suffix: str) -> Path:
        safe = chat_id.replace("/", "_")
        return self._dir / f"{safe}{suffix}"

    def read_last_ts(self, chat_id: str) -> Optional[str]:
        p = self._path(chat_id, ".last_ts")
        try:
            if p.exists():
                val = p.read_text(encoding="utf-8").strip()
                return val if val else None
        except OSError as exc:
            logger.warning("读取 last_ts 失败 [%s]: %s", chat_id, exc)
        return None

    def write_last_ts(self, chat_id: str, ts: str) -> None:
        target = self._path(chat_id, ".last_ts")
        try:
            # Atomic write: write to temp file then rename
            tmp = target.with_suffix(".tmp")
            tmp.write_text(ts, encoding="utf-8")
            tmp.rename(target)
        except OSError as exc:
            logger.error("写入 last_ts 失败 [%s]: %s", chat_id, exc)

    def read_digest(self, chat_id: str) -> str:
        p = self._path(chat_id, ".md")
        try:
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("读取 digest 失败 [%s]: %s", chat_id, exc)
        return ""

    def append_digest(self, chat_id: str, entry: str) -> None:
        try:
            with open(self._path(chat_id, ".md"), "a", encoding="utf-8") as f:
                f.write(entry)
        except OSError as exc:
            logger.error("追加 digest 失败 [%s]: %s", chat_id, exc)

    def clear_digest(self, chat_id: str) -> None:
        target = self._path(chat_id, ".md")
        try:
            tmp = target.with_suffix(".tmp")
            tmp.write_text("", encoding="utf-8")
            tmp.rename(target)
        except OSError as exc:
            logger.error("清空 digest 失败 [%s]: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# ChatWorker
# ---------------------------------------------------------------------------

class ChatWorker:
    """Manages the digest cycle for a single monitored chat.

    Lifecycle per tick:
        1. Check if segment is due → fetch messages → summarize → append to digest
        2. Check if report is due  → read digest → summarize → send to chat → clear
    """

    def __init__(
        self,
        cfg: ChatConfig,
        feishu: MessageFetcher,
        llm: Summarizer,
        storage: Storage,
        segment_prompt: str = SEGMENT_PROMPT,
        report_prompt: str = REPORT_PROMPT,
    ) -> None:
        self.cfg = cfg
        self._feishu = feishu
        self._llm = llm
        self._storage = storage
        self._segment_prompt = segment_prompt
        self._report_prompt = report_prompt
        self._last_segment_time: float = 0.0
        self._segment_count: int = 0

    @property
    def label(self) -> str:
        return self.cfg.name or self.cfg.chat_id

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    @property
    def report_interval_minutes(self) -> int:
        """Actual report interval in minutes (for display)."""
        return self.cfg.segment_interval * self.cfg.report_cycle

    def tick(self) -> None:
        """Called every engine tick. Skipped if disabled."""
        if not self.cfg.enabled:
            return
        now = time.time()

        # --- Segment ---
        segment_due = (now - self._last_segment_time) >= self.cfg.segment_interval * 60
        if not segment_due:
            return

        self._last_segment_time = now  # Set BEFORE processing to maintain cadence
        try:
            self._process_segment()
        except Exception:
            logger.error("[%s] 分段摘要异常", self.label, exc_info=True)
        self._segment_count += 1

        # --- Report (every N segments) ---
        if self._segment_count >= self.cfg.report_cycle:
            try:
                sent = self._send_report()
                if sent:
                    self._segment_count = 0  # Reset only on success
                # On failure, keep counting — will retry next segment
            except Exception:
                logger.error("[%s] 归总报告异常", self.label, exc_info=True)

    # ---- Segment logic ----

    def _process_segment(self) -> None:
        last_ts = self._storage.read_last_ts(self.cfg.chat_id)

        # Guard: if last_ts is corrupt, reset it
        if last_ts is not None:
            try:
                int(last_ts)
            except ValueError:
                logger.warning("[%s] last_ts 格式异常 (%s), 重置", self.label, last_ts)
                last_ts = None

        # First run only: initialize last_ts to recent window
        if not last_ts:
            last_ts = str(int(time.time() * 1000) - self.cfg.segment_interval * 60 * 1000)
            logger.info("[%s] 初始化 last_ts (首次启动)", self.label)
            self._storage.write_last_ts(self.cfg.chat_id, last_ts)

        lines, new_ts = self._feishu.fetch_messages(
            self.cfg.chat_id,
            since_ts=last_ts,
            max_chars=self.cfg.max_chars_per_fetch,
        )
        if new_ts and new_ts != last_ts:
            self._storage.write_last_ts(self.cfg.chat_id, new_ts)

        if not lines:
            logger.debug("[%s] 本轮无新消息", self.label)
            return

        raw_text = "\n".join(lines)
        logger.info("[%s] 拉取 %d 条消息 (%d 字符), 开始摘要", self.label, len(lines), len(raw_text))

        summary = self._llm.summarize(raw_text, self._segment_prompt)
        if not summary:
            logger.warning("[%s] LLM 摘要返回空, 跳过本轮", self.label)
            return

        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._storage.append_digest(self.cfg.chat_id, f"\n## {ts_str}\n{summary}\n")
        logger.info("[%s] 分段摘要完成: %d 条消息 -> %d 字符", self.label, len(lines), len(summary))

    # ---- Report logic ----

    def _send_report(self) -> bool:
        """Generate and send the aggregated report. Returns True on success."""
        digest_text = self._storage.read_digest(self.cfg.chat_id)
        if not digest_text:
            logger.debug("[%s] 无摘要数据, 跳过报告", self.label)
            return True  # Nothing to report is not a failure

        # Hard truncate to avoid LLM proxy 504
        original_len = len(digest_text)
        if original_len > 1500:
            digest_text = digest_text[-1500:]
            logger.info("[%s] 截断摘要: %d -> 1500 字符", self.label, original_len)

        logger.info("[%s] 开始生成归总报告...", self.label)
        report = self._llm.summarize(digest_text, self._report_prompt)

        if not report:
            logger.warning("[%s] 归总 LLM 返回空, 保留摘要待下次重试", self.label)
            return False  # Will retry next cycle

        if report.strip() in ("无告警", "本周期内无告警"):
            self._storage.clear_digest(self.cfg.chat_id)
            logger.info("[%s] 本周期无告警, 已清空摘要", self.label)
            return True

        minutes = self.report_interval_minutes
        if minutes >= 120:
            period = f"过去 {minutes // 60} 小时"
        elif minutes >= 60:
            period = f"过去 1 小时"
        else:
            period = f"过去 {minutes} 分钟"
        header = f"📊 告警汇总报告 — {self.label}（{period}）\n\n"

        sent = self._feishu.send_message(self.cfg.chat_id, header + report, mention_all=self.cfg.mention_all)
        if sent:
            logger.info("[%s] 归总报告已发送 (mention_all=%s)", self.label, self.cfg.mention_all)
            self._storage.clear_digest(self.cfg.chat_id)
            return True
        else:
            logger.warning("[%s] 归总报告发送失败, 保留摘要待下次重试", self.label)
            return False


# ---------------------------------------------------------------------------
# DigestEngine (scheduler)
# ---------------------------------------------------------------------------

class DigestEngine:
    """Runs all ChatWorkers in a synchronous loop with graceful shutdown."""

    def __init__(self, workers: List[ChatWorker], tick_interval: int = 60) -> None:
        self._workers = workers
        self._tick_interval = max(tick_interval, 10)

    def run_forever(self, stop_check: Optional[Callable[[], bool]] = None) -> None:
        """Block and run the digest loop until *stop_check* returns True."""
        active = [w for w in self._workers if w.enabled]
        logger.info(
            "Digest 引擎启动: %d 个群 (%d 个启用), tick=%ds",
            len(self._workers), len(active), self._tick_interval,
        )
        for w in self._workers:
            status = "启用" if w.enabled else "禁用"
            logger.info(
                "  - [%s] segment=%dmin, report_cycle=%d (每%dmin), %s",
                w.label, w.cfg.segment_interval, w.cfg.report_cycle,
                w.report_interval_minutes, status,
            )

        while True:
            if stop_check and stop_check():
                logger.info("Digest 引擎停止 (收到退出信号)")
                break

            tick_start = time.monotonic()
            for worker in self._workers:
                try:
                    worker.tick()
                except Exception:
                    logger.error("[%s] Worker tick 异常", worker.label, exc_info=True)
            tick_elapsed = time.monotonic() - tick_start

            remaining = max(0, self._tick_interval - tick_elapsed)
            if tick_elapsed > self._tick_interval:
                logger.warning(
                    "Tick 耗时 %.1fs 超过间隔 %ds, 跳过等待",
                    tick_elapsed, self._tick_interval,
                )

            slept = 0.0
            while slept < remaining:
                if stop_check and stop_check():
                    break
                time.sleep(min(1.0, remaining - slept))
                slept += 1.0
