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
import threading
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

    def verify_chat(self, chat_id: str) -> str: ...


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
        if self.segment_interval > 1440:  # max 24 hours
            logger.warning("segment_interval=%d 过大, 限制为 1440 分钟", self.segment_interval)
            object.__setattr__(self, "segment_interval", 1440)
        if self.report_cycle < 1:
            object.__setattr__(self, "report_cycle", 1)
        if self.report_cycle > 100:
            logger.warning("report_cycle=%d 过大, 限制为 100", self.report_cycle)
            object.__setattr__(self, "report_cycle", 100)


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
        # Dynamic display name: config.name > fetched from API > chat_id
        self._display_name: str = cfg.name or ""

    @property
    def label(self) -> str:
        return self._display_name or self.cfg.chat_id

    def refresh_display_name(self) -> None:
        """Refresh display name from Feishu API (if not set in config)."""
        if self.cfg.name:
            return  # Config has explicit name, don't override
        try:
            name = self._feishu.verify_chat(self.cfg.chat_id)
            if name and name != self.cfg.chat_id:
                self._display_name = name
        except Exception:
            pass  # Keep existing name on failure

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    @property
    def report_interval_minutes(self) -> int:
        """Actual report interval in minutes (for display)."""
        return self.cfg.segment_interval * self.cfg.report_cycle

    def tick(self, llm_semaphore: Optional[threading.Semaphore] = None) -> None:
        """Called every interval. Skipped if disabled."""
        if not self.cfg.enabled:
            return
        now = time.time()

        # --- Segment ---
        segment_due = (now - self._last_segment_time) >= self.cfg.segment_interval * 60
        if not segment_due:
            return

        self._last_segment_time = now  # Set BEFORE processing to maintain cadence
        has_new_data = False
        try:
            has_new_data = self._process_segment(llm_semaphore)
        except Exception:
            logger.error("[%s] 分段摘要异常", self.label, exc_info=True)

        # Only count segments that produced new summaries
        if has_new_data:
            self._segment_count += 1

        # --- Report (every N segments) ---
        if self._segment_count >= self.cfg.report_cycle:
            try:
                sent = self._send_report(llm_semaphore)
                if sent:
                    self._segment_count = 0  # Reset only on success
            except Exception:
                logger.error("[%s] 归总报告异常", self.label, exc_info=True)

    # ---- Segment logic ----

    def _process_segment(self, llm_semaphore: Optional[threading.Semaphore] = None) -> bool:
        """Fetch and summarize recent messages. Returns True if new summary was written."""
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
            return False

        raw_text = "\n".join(lines)
        logger.info("[%s] 拉取 %d 条消息 (%d 字符), 开始摘要", self.label, len(lines), len(raw_text))

        if llm_semaphore:
            llm_semaphore.acquire()
        try:
            summary = self._llm.summarize(raw_text, self._segment_prompt)
        finally:
            if llm_semaphore:
                llm_semaphore.release()
        if not summary:
            logger.warning("[%s] LLM 摘要返回空, 跳过本轮", self.label)
            return False

        ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._storage.append_digest(self.cfg.chat_id, f"\n## {ts_str}\n{summary}\n")
        logger.info("[%s] 分段摘要完成: %d 条消息 -> %d 字符", self.label, len(lines), len(summary))
        return True

    # ---- Report logic ----

    def _send_report(self, llm_semaphore: Optional[threading.Semaphore] = None) -> bool:
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

        # Refresh display name before generating report
        self.refresh_display_name()

        logger.info("[%s] 开始生成归总报告...", self.label)

        if llm_semaphore:
            llm_semaphore.acquire()
        try:
            report = self._llm.summarize(digest_text, self._report_prompt)
        finally:
            if llm_semaphore:
                llm_semaphore.release()

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
# DigestEngine (scheduler — one thread per worker)
# ---------------------------------------------------------------------------

class DigestEngine:
    """Runs each ChatWorker in its own thread with independent timing.

    Workers share a semaphore to limit concurrent LLM calls.
    """

    def __init__(
        self,
        workers: List[ChatWorker],
        max_concurrent_llm: int = 3,
    ) -> None:
        self._workers = workers
        self._llm_semaphore = threading.Semaphore(max_concurrent_llm)
        self._threads: List[threading.Thread] = []

    def run_forever(self, stop_check: Optional[Callable[[], bool]] = None) -> None:
        """Start all worker threads and block until stop_check returns True."""
        active = [w for w in self._workers if w.enabled]
        logger.info(
            "Digest 引擎启动: %d 个群 (%d 个启用), 并发上限=%d",
            len(self._workers), len(active), self._llm_semaphore._value,
        )
        for w in self._workers:
            status = "启用" if w.enabled else "禁用"
            logger.info(
                "  - [%s] segment=%dmin, report_cycle=%d (每%dmin), %s",
                w.label, w.cfg.segment_interval, w.cfg.report_cycle,
                w.report_interval_minutes, status,
            )

        # Start one thread per enabled worker
        for worker in self._workers:
            if not worker.enabled:
                continue
            t = threading.Thread(
                target=self._worker_loop,
                args=(worker, stop_check),
                name=f"worker-{worker.cfg.chat_id[-8:]}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        # Block main thread until stop signal
        while True:
            if stop_check and stop_check():
                logger.info("Digest 引擎停止 (收到退出信号)")
                break
            time.sleep(1)

        # Wait for threads to finish (they check stop_check too)
        for t in self._threads:
            t.join(timeout=5)

    def _worker_loop(
        self,
        worker: ChatWorker,
        stop_check: Optional[Callable[[], bool]],
    ) -> None:
        """Run a single worker's tick loop in its own thread."""
        interval = worker.cfg.segment_interval * 60
        logger.info("[%s] Worker 线程启动 (interval=%ds)", worker.label, interval)

        while True:
            if stop_check and stop_check():
                break

            tick_start = time.monotonic()
            try:
                worker.tick(self._llm_semaphore)
            except Exception:
                logger.error("[%s] Worker tick 异常", worker.label, exc_info=True)
            tick_elapsed = time.monotonic() - tick_start

            # Sleep remaining time, check stop every second
            remaining = max(0, interval - tick_elapsed)
            slept = 0.0
            while slept < remaining:
                if stop_check and stop_check():
                    break
                time.sleep(min(1.0, remaining - slept))
                slept += 1.0

        logger.info("[%s] Worker 线程退出", worker.label)
