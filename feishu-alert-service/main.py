#!/usr/bin/env python3
"""Feishu Alert Service — standalone alert digest + MCP server."""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict

import yaml

from feishu_client import FeishuClient
from llm_client import LLMClient
from digest_engine import ChatConfig, ChatWorker, DigestEngine, _Storage
from digest_engine import SEGMENT_PROMPT, REPORT_PROMPT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu-alert-service")


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        logger.error("Config file not found: %s", path)
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)

    # Feishu client
    feishu_cfg = cfg.get("feishu", {})
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    if not app_id or not app_secret:
        logger.error("feishu.app_id and feishu.app_secret are required")
        sys.exit(1)

    domain = feishu_cfg.get("domain", "feishu")
    feishu = FeishuClient(app_id, app_secret, domain)
    logger.info("Feishu client initialized (domain=%s)", domain)

    # LLM client
    llm_cfg = cfg.get("llm", {})
    llm = LLMClient(
        base_url=llm_cfg.get("base_url", ""),
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model", ""),
    )
    logger.info("LLM client initialized (model=%s)", llm_cfg.get("model"))

    # Storage
    data_dir = Path(cfg.get("data_dir", "./data"))
    storage = _Storage(data_dir)

    # Prompts
    segment_prompt = cfg.get("segment_prompt", "").strip() or SEGMENT_PROMPT
    report_prompt = cfg.get("report_prompt", "").strip() or REPORT_PROMPT

    # Build workers
    defaults = cfg.get("defaults", {})
    chats_cfg = cfg.get("chats", [])
    if not chats_cfg:
        logger.error("No chats configured")
        sys.exit(1)

    workers = []
    chat_map = {}
    for entry in chats_cfg:
        if not isinstance(entry, dict):
            continue
        chat_id = str(entry.get("chat_id", "")).strip()
        if not chat_id:
            continue
        name = str(entry.get("name", "")).strip()
        chat_config = ChatConfig(
            chat_id=chat_id,
            name=name,
            segment_interval=int(entry.get("segment_interval", defaults.get("segment_interval", 10))),
            report_interval=int(entry.get("report_interval", defaults.get("report_interval", 240))),
            max_chars_per_fetch=int(entry.get("max_chars_per_fetch", defaults.get("max_chars_per_fetch", 30000))),
        )
        workers.append(ChatWorker(
            cfg=chat_config,
            feishu=feishu,
            llm=llm,
            storage=storage,
            segment_prompt=segment_prompt,
            report_prompt=report_prompt,
        ))
        chat_map[chat_id] = name or chat_id

    logger.info("Configured %d chat(s): %s", len(workers), ", ".join(w.label for w in workers))

    # MCP server (optional)
    mcp_cfg = cfg.get("mcp", {})
    if mcp_cfg.get("enabled", False):
        from mcp_server import set_feishu_client, start_mcp_server
        set_feishu_client(feishu, chat_map)
        start_mcp_server(port=mcp_cfg.get("port", 8765))

    # Graceful shutdown
    def _shutdown(sig, frame):
        logger.info("Shutting down...")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Run digest engine
    tick = min(w.cfg.segment_interval for w in workers) * 60
    engine = DigestEngine(workers, tick_interval=tick)
    engine.run_forever()


if __name__ == "__main__":
    main()
