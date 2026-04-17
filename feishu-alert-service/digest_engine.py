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


# ---------------------------------------------------------------------------
# Protocols (for loose coupling — no direct import of concrete classes)
# ---------------------------------------------------------------------------

class MessageFetcher(Protocol):
    """Anything that can fetch and send Feishu messages."""

    def fetch_messages(
        self, chat_id: str, since_ts: Optional[str] = None, max_chars: int = 30000,
    ) -> tuple[list[str], Optional[str]]: ...

    def send_message(self, chat_id: str, text: str) -> bool: ...


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
    segment_interval: int = 10   # minutes
    report_interval: int = 240   # minutes
    max_chars_per_fetch: int = 30000


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
        self._last_report_time: float = 0.0
        self._report_timer_started = False

    @property
    def label(self) -> str:
        return self.cfg.name or self.cfg.chat_id

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def tick(self) -> None:
        """Called every engine tick. Skipped if disabled."""
        if not self.cfg.enabled:
            return
        now = time.time()

        # --- Segment ---
        segment_due = (now - self._last_segment_time) >= self.cfg.segment_interval * 60
        if segment_due:
            try:
                self._process_segment()
            except Exception:
                logger.error("[%s] 分段摘要异常", self.label, exc_info=True)
            self._last_segment_time = time.time()

        # --- Report ---
        if not self._report_timer_started:
            self._last_report_time = now
            self._report_timer_started = True
            logger.info(
                "[%s] 报告计时器启动, 首次报告将在 %d 分钟后生成",
                self.label, self.cfg.report_interval,
            )
            return

        report_due = (now - self._last_report_time) >= self.cfg.report_interval * 60
        if report_due:
            try:
                self._send_report()
            except Exception:
                logger.error("[%s] 归总报告异常", self.label, exc_info=True)
            self._last_report_time = time.time()

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

        # Reset if too old or missing — only fetch recent window
        now_ms = int(time.time() * 1000)
        max_age_ms = self.cfg.segment_interval * 60 * 1000 * 3
        if not last_ts or (now_ms - int(last_ts)) > max_age_ms:
            fresh_ts = str(now_ms - self.cfg.segment_interval * 60 * 1000)
            logger.info("[%s] 重置 last_ts 到最近时间窗口", self.label)
            last_ts = fresh_ts
            self._storage.write_last_ts(self.cfg.chat_id, last_ts)

        lines, new_ts = self._feishu.fetch_messages(
            self.cfg.chat_id,
            since_ts=last_ts,
            max_chars=self.cfg.max_chars_per_fetch,
        )
        if new_ts and new_ts != last_ts:
            self._storage.write_last_ts(self.cfg.chat_id, new_ts)
        elif not lines:
            # No new messages — advance last_ts to now to prevent repeated resets
            self._storage.write_last_ts(self.cfg.chat_id, str(int(time.time() * 1000)))

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

    def _send_report(self) -> None:
        digest_text = self._storage.read_digest(self.cfg.chat_id)
        if not digest_text:
            logger.debug("[%s] 无摘要数据, 跳过报告", self.label)
            return

        # Hard truncate to avoid LLM proxy 504
        original_len = len(digest_text)
        if original_len > 1500:
            digest_text = digest_text[-1500:]
            logger.info("[%s] 截断摘要: %d -> 1500 字符", self.label, original_len)

        logger.info("[%s] 开始生成归总报告...", self.label)
        report = self._llm.summarize(digest_text, self._report_prompt)

        if not report:
            logger.warning("[%s] 归总 LLM 返回空, 保留摘要待下次重试", self.label)
            return  # Do NOT clear digest — retry next cycle

        if report.strip() in ("无告警", "本周期内无告警"):
            self._storage.clear_digest(self.cfg.chat_id)
            logger.info("[%s] 本周期无告警, 已清空摘要", self.label)
            return

        minutes = self.cfg.report_interval
        if minutes >= 120:
            period = f"过去 {minutes // 60} 小时"
        elif minutes >= 60:
            period = f"过去 1 小时"
        else:
            period = f"过去 {minutes} 分钟"
        header = f"📊 告警汇总报告 — {self.label}（{period}）\n\n"

        sent = self._feishu.send_message(self.cfg.chat_id, header + report)
        if sent:
            logger.info("[%s] 归总报告已发送", self.label)
            self._storage.clear_digest(self.cfg.chat_id)
        else:
            logger.warning("[%s] 归总报告发送失败, 保留摘要待下次重试", self.label)
            # Do NOT clear digest — retry next cycle


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
                "  - [%s] segment=%dmin, report=%dmin, %s",
                w.label, w.cfg.segment_interval, w.cfg.report_interval, status,
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
