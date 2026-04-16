"""MCP Server — exposes feishu group history as a tool for Hermes Agent."""

from __future__ import annotations

import json
import logging
import threading
from typing import Dict, List

from mcp.server.fastmcp import FastMCP

from feishu_client import FeishuClient

logger = logging.getLogger(__name__)

_mcp = FastMCP("feishu-alert-service")
_feishu_client: FeishuClient | None = None
_monitored_chats: Dict[str, str] = {}  # chat_id -> name


def set_feishu_client(client: FeishuClient, chats: Dict[str, str]) -> None:
    global _feishu_client, _monitored_chats
    _feishu_client = client
    _monitored_chats = chats


@_mcp.tool()
def feishu_group_history(chat_id: str, limit: int = 50) -> str:
    """获取飞书群最近的聊天记录（包括 webhook 机器人消息）。

    Args:
        chat_id: 飞书群的 chat_id（如 oc_xxx）
        limit: 最多返回的消息条数，默认 50
    """
    if not _feishu_client:
        return json.dumps({"error": "Feishu client not initialized"})

    lines = _feishu_client.fetch_recent_history(chat_id, limit=min(limit, 50))
    if not lines:
        return json.dumps({"messages": [], "count": 0})

    return json.dumps({"messages": lines, "count": len(lines)}, ensure_ascii=False)


@_mcp.tool()
def feishu_list_monitored_chats() -> str:
    """列出当前监控的飞书群列表。"""
    chats = [{"chat_id": cid, "name": name} for cid, name in _monitored_chats.items()]
    return json.dumps({"chats": chats}, ensure_ascii=False)


def start_mcp_server(port: int = 8765) -> None:
    """Start the MCP server in a background thread."""
    def _run():
        logger.info("MCP server starting on stdio (port config ignored for stdio mode)")
        _mcp.run(transport="stdio")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info("MCP server thread started")
