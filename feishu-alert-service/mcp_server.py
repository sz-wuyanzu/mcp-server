"""MCP Server — exposes Feishu group history as tools for Hermes Agent.

Designed to run as a standalone stdio process launched by Hermes:
    command: python
    args: ["/path/to/mcp_server.py", "/path/to/config.yaml"]

Feishu credentials are read from Hermes (~/.hermes/.env).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Dict

# Ensure sibling modules are importable regardless of working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml
from mcp.server.fastmcp import FastMCP

from feishu_client import FeishuClient
from hermes_config import HermesConfigError, load_feishu_credentials

# Logs go to stderr — stdout is reserved for MCP protocol
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-feishu-alert")

# ---------------------------------------------------------------------------
# Global state (initialized once at startup)
# ---------------------------------------------------------------------------

_mcp = FastMCP("feishu-alert-service")
_feishu_client: FeishuClient | None = None
_monitored_chats: Dict[str, str] = {}  # chat_id -> display name


def _init_from_config(config_path: str) -> None:
    """Load config and initialize the Feishu client."""
    global _feishu_client, _monitored_chats

    p = Path(config_path)
    if not p.exists():
        logger.error("配置文件不存在: %s", p.resolve())
        sys.exit(1)

    try:
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        logger.error("配置文件读取失败: %s — %s", config_path, exc)
        sys.exit(1)

    hermes_home = cfg.get("hermes_home")

    # Feishu credentials from Hermes
    try:
        creds = load_feishu_credentials(hermes_home)
    except HermesConfigError as exc:
        logger.error("Hermes 飞书配置错误: %s", exc)
        sys.exit(1)

    try:
        _feishu_client = FeishuClient(creds.app_id, creds.app_secret, creds.domain)
    except Exception as exc:
        logger.error("FeishuClient 初始化失败: %s", exc)
        sys.exit(1)

    # Build monitored chats map
    for entry in cfg.get("chats", []):
        if isinstance(entry, dict):
            cid = str(entry.get("chat_id", "")).strip()
            name = str(entry.get("name", "")).strip()
            if cid:
                _monitored_chats[cid] = name or cid

    logger.info("MCP 初始化完成: %d 个监控群", len(_monitored_chats))
    for cid, name in _monitored_chats.items():
        logger.info("  - %s (%s)", name, cid)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@_mcp.tool()
def feishu_group_history(chat_id: str, limit: int = 50) -> str:
    """获取飞书群最近的聊天记录（包括 webhook 机器人消息）。

    Args:
        chat_id: 飞书群的 chat_id（如 oc_xxx）
        limit: 最多返回的消息条数，默认 50，最大 50
    """
    if not _feishu_client:
        logger.error("feishu_group_history 调用失败: FeishuClient 未初始化")
        return json.dumps({"error": "Feishu client not initialized"}, ensure_ascii=False)

    safe_limit = max(1, min(limit, 50))
    logger.info("feishu_group_history: chat_id=%s, limit=%d", chat_id, safe_limit)

    try:
        lines = _feishu_client.fetch_recent_history(chat_id, limit=safe_limit)
    except Exception as exc:
        logger.error("feishu_group_history 异常: %s", exc, exc_info=True)
        return json.dumps({"error": str(exc)[:200]}, ensure_ascii=False)

    logger.info("feishu_group_history 返回 %d 条消息", len(lines))
    return json.dumps({"messages": lines, "count": len(lines)}, ensure_ascii=False)


@_mcp.tool()
def feishu_list_monitored_chats() -> str:
    """列出当前监控的飞书群列表。"""
    chats = [{"chat_id": cid, "name": name} for cid, name in _monitored_chats.items()]
    logger.info("feishu_list_monitored_chats: 返回 %d 个群", len(chats))
    return json.dumps({"chats": chats}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    logger.info("=" * 50)
    logger.info("Feishu Alert MCP Server 启动中...")
    logger.info("配置文件: %s", Path(config_path).resolve())
    logger.info("=" * 50)

    _init_from_config(config_path)

    logger.info("MCP Server 就绪 (stdio 模式)")
    _mcp.run(transport="stdio")
