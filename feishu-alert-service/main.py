#!/usr/bin/env python3
"""Feishu Alert Service — standalone alert digest service.

Entry point for the digest engine. Feishu credentials and LLM config
are read from the Hermes installation (~/.hermes).

Usage:
    python main.py [config.yaml]
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict

# Ensure sibling modules are importable regardless of working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from feishu_client import FeishuClient
from llm_client import LLMClient
from hermes_config import HermesConfigError, load_feishu_credentials, load_llm_config
from digest_engine import ChatConfig, ChatWorker, DigestEngine, Storage

# ---------------------------------------------------------------------------
# Logging: stdout + file
# ---------------------------------------------------------------------------

_LOG_PREFIX = "mcp-server/feishu-alert-service"
_LOG_FORMAT = f"%(asctime)s %(levelname)-7s [{_LOG_PREFIX}] [%(name)s] %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

def _setup_logging() -> None:
    _self_dir = Path(__file__).resolve().parent
    log_dir = _self_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = None

    root = logging.getLogger()
    if root.handlers:
        return  # Already configured (e.g. imported by mcp_server.py)
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT))
    root.addHandler(console)

    if log_dir:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(
            log_dir / "service.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT))
        root.addHandler(fh)

_setup_logging()
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    """Load and validate the service config file."""
    p = Path(path)
    if not p.exists():
        logger.error("配置文件不存在: %s", p.resolve())
        logger.error("请编辑 config.yaml 填入群 chat_id 后再启动。")
        sys.exit(1)

    try:
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        logger.error("配置文件 YAML 语法错误: %s\n%s", path, exc)
        sys.exit(1)
    except OSError as exc:
        logger.error("读取配置文件失败: %s — %s", path, exc)
        sys.exit(1)

    if not isinstance(cfg, dict):
        logger.error("配置文件格式错误: 顶层必须是字典，当前为 %s", type(cfg).__name__)
        sys.exit(1)

    # Validate chats
    chats = cfg.get("chats")
    if not chats or not isinstance(chats, list):
        logger.error("配置错误: 缺少 'chats' 列表。请在 config.yaml 中添加至少一个群配置。")
        logger.error("示例:")
        logger.error("  chats:")
        logger.error('    - chat_id: "oc_xxx"')
        logger.error('      name: "生产告警群"')
        sys.exit(1)

    # Check for placeholder chat_id
    valid_chats = []
    for i, entry in enumerate(chats):
        if not isinstance(entry, dict):
            logger.warning("chats[%d] 不是字典格式，已跳过", i)
            continue
        cid = str(entry.get("chat_id", "")).strip()
        if not cid:
            logger.warning("chats[%d] 缺少 chat_id，已跳过", i)
            continue
        if cid.startswith("oc_xxx") or cid.startswith("oc_yyy"):
            logger.error(
                "chats[%d] 的 chat_id 还是示例值 (%s)，请替换为真实的群 ID。\n"
                "获取方式: 飞书群设置 → 群信息 → 群号",
                i, cid,
            )
            sys.exit(1)
        valid_chats.append(entry)

    if not valid_chats:
        logger.error("没有有效的群配置。请检查 config.yaml 中的 chats 列表。")
        sys.exit(1)

    cfg["chats"] = valid_chats
    return cfg


# ---------------------------------------------------------------------------
# Worker builder
# ---------------------------------------------------------------------------

def build_workers(
    cfg: Dict[str, Any],
    feishu: FeishuClient,
    llm: LLMClient,
    storage: Storage,
) -> list[ChatWorker]:
    """Build ChatWorker instances from validated config."""
    defaults = cfg.get("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}

    segment_prompt = (cfg.get("segment_prompt") or "").strip()
    report_prompt = (cfg.get("report_prompt") or "").strip()
    if not segment_prompt or not report_prompt:
        logger.error("配置错误: segment_prompt 和 report_prompt 不能为空，请在 config.yaml 中配置提示词。")
        sys.exit(1)

    workers: list[ChatWorker] = []
    for idx, entry in enumerate(cfg["chats"]):
        chat_id = str(entry["chat_id"]).strip()
        name = str(entry.get("name", "")).strip()

        def _int(key: str, fallback: int, _entry=entry, _defaults=defaults, _idx=idx) -> int:
            val = _entry.get(key, _defaults.get(key, fallback))
            try:
                return max(1, int(val))
            except (ValueError, TypeError):
                logger.warning("chats[%d].%s 值无效 (%s), 使用默认值 %d", _idx, key, val, fallback)
                return fallback

        chat_config = ChatConfig(
            chat_id=chat_id,
            name=name,
            enabled=bool(entry.get("enabled", defaults.get("enabled", True))),
            mention_all=bool(entry.get("mention_all", defaults.get("mention_all", False))),
            segment_interval=_int("segment_interval", 10),
            report_cycle=_int("report_cycle", 6),
            max_chars_per_fetch=_int("max_chars_per_fetch", 30000),
        )
        workers.append(ChatWorker(
            cfg=chat_config,
            feishu=feishu,
            llm=llm,
            storage=storage,
            segment_prompt=segment_prompt,
            report_prompt=report_prompt,
        ))

    return workers


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    logger.info("=" * 60)
    logger.info("Feishu Alert Service 启动中...")
    logger.info("配置文件: %s", Path(config_path).resolve())
    logger.info("=" * 60)

    cfg = load_config(config_path)
    hermes_home = cfg.get("hermes_home")

    # --- Feishu credentials (from Hermes) ---
    try:
        creds = load_feishu_credentials(hermes_home)
    except HermesConfigError as exc:
        logger.error("Hermes 飞书配置错误: %s", exc)
        sys.exit(1)

    try:
        feishu = FeishuClient(creds.app_id, creds.app_secret, creds.domain)
    except Exception as exc:
        logger.error("FeishuClient 初始化失败: %s", exc)
        sys.exit(1)

    # --- LLM config (from Hermes, optional model override) ---
    model_override = cfg.get("model")
    try:
        llm_cfg = load_llm_config(
            hermes_home,
            model_override if isinstance(model_override, str) else None,
        )
    except HermesConfigError as exc:
        logger.error("Hermes LLM 配置错误: %s", exc)
        sys.exit(1)

    llm = LLMClient(base_url=llm_cfg.base_url, api_key=llm_cfg.api_key, model=llm_cfg.model)

    # Verify model is reachable
    try:
        llm.verify_model()
    except RuntimeError as exc:
        logger.error("模型验证失败: %s", exc)
        sys.exit(1)

    # --- Storage ---
    data_dir = Path(cfg.get("data_dir", "./data"))
    if not data_dir.is_absolute():
        # Resolve relative to config file location
        data_dir = Path(config_path).resolve().parent / data_dir
    try:
        storage = Storage(data_dir)
    except OSError:
        sys.exit(1)  # Storage.__init__ already logged the error

    # --- Workers ---
    workers = build_workers(cfg, feishu, llm, storage)

    # Verify all chat_ids are accessible (non-blocking)
    logger.info("验证群配置...")
    for w in workers:
        try:
            real_name = feishu.verify_chat(w.cfg.chat_id)
            if not w.cfg.name:
                logger.info("  群 %s 未设置 name，使用飞书群名: %s", w.cfg.chat_id, real_name)
        except RuntimeError as exc:
            logger.warning("群验证失败 (不影响启动): %s", exc)
            logger.warning("  [%s] 将在运行时重试，验证通过前不会拉取消息", w.label)

    logger.info("已配置 %d 个群: %s", len(workers), ", ".join(w.label for w in workers))

    # --- Graceful shutdown ---
    running = True

    def _shutdown(sig: int, _frame: Any) -> None:
        nonlocal running
        sig_name = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
        logger.info("收到信号 %s, 准备退出...", sig_name)
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # --- Run ---
    tick = min(w.cfg.segment_interval for w in workers if w.cfg.enabled) * 60 if any(w.cfg.enabled for w in workers) else 60
    engine = DigestEngine(workers, tick_interval=tick)

    try:
        engine.run_forever(stop_check=lambda: not running)
    except KeyboardInterrupt:
        pass

    logger.info("Feishu Alert Service 已停止")


if __name__ == "__main__":
    main()
