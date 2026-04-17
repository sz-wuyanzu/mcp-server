"""Feishu (Lark) API client — fetch group messages and send messages.

All external API calls are wrapped with exception handling and structured logging.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetChatRequest,
    ListMessageRequest,
)
from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

logger = logging.getLogger(__name__)


class FeishuClient:
    """Thin wrapper around lark-oapi for message fetching and sending.

    Thread-safe for read operations. Sender name cache is populated lazily.
    """

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu") -> None:
        domain_obj = FEISHU_DOMAIN if domain != "lark" else LARK_DOMAIN
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(domain_obj)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
        self._sender_cache: Dict[str, str] = {}
        logger.info("FeishuClient 初始化完成 (domain=%s)", domain)

    # ------------------------------------------------------------------
    # Startup verification
    # ------------------------------------------------------------------

    def verify_chat(self, chat_id: str) -> str:
        """Verify a chat_id exists and the bot has access.

        Returns the chat name on success.
        Raises RuntimeError with a clear message on failure.
        """
        try:
            request = GetChatRequest.builder().chat_id(chat_id).build()
            response = self._client.im.v1.chat.get(request)
        except Exception as exc:
            raise RuntimeError(
                f"验证群 {chat_id} 时网络异常: {exc}"
            ) from exc

        if not response or not response.success():
            code = getattr(response, "code", "?")
            msg = getattr(response, "msg", "?")

            if code == 230001 or "not exist" in str(msg).lower():
                raise RuntimeError(
                    f"群 {chat_id} 不存在。请检查 chat_id 是否正确。\n"
                    f"获取方式: 飞书群设置 → 群信息 → 群号"
                )
            if code == 230002 or "not in chat" in str(msg).lower() or "no permission" in str(msg).lower():
                raise RuntimeError(
                    f"机器人不在群 {chat_id} 中，或没有权限。\n"
                    f"请先将机器人添加到该群。"
                )
            raise RuntimeError(
                f"验证群 {chat_id} 失败: code={code}, msg={msg}"
            )

        data = getattr(response, "data", None)
        chat_name = getattr(data, "name", "") or chat_id
        logger.info("群验证通过: %s (%s)", chat_name, chat_id)
        return chat_name

    # ------------------------------------------------------------------
    # Sender resolution
    # ------------------------------------------------------------------

    def _resolve_sender_name(self, sender_id: str) -> str:
        """Resolve sender open_id to display name via contact API (cached)."""
        if not sender_id:
            return "unknown"
        if sender_id in self._sender_cache:
            return self._sender_cache[sender_id]

        try:
            from lark_oapi.api.contact.v3 import GetUserRequest

            request = (
                GetUserRequest.builder()
                .user_id(sender_id)
                .user_id_type("open_id")
                .build()
            )
            response = self._client.contact.v3.user.get(request)
            if response and response.success():
                user = getattr(getattr(response, "data", None), "user", None)
                name = getattr(user, "name", "") or ""
                if name:
                    self._sender_cache[sender_id] = name
                    return name
            else:
                code = getattr(response, "code", "?")
                logger.warning("解析用户名失败 sender_id=%s: code=%s", sender_id, code)
        except Exception:
            logger.warning("解析用户名异常 sender_id=%s", sender_id, exc_info=True)

        # Cache the raw ID to avoid repeated failed lookups
        self._sender_cache[sender_id] = sender_id
        return sender_id

    # ------------------------------------------------------------------
    # Message fetching (for digest engine)
    # ------------------------------------------------------------------

    def fetch_messages(
        self,
        chat_id: str,
        since_ts: Optional[str] = None,
        max_chars: int = 30000,
    ) -> Tuple[List[str], Optional[str]]:
        """Fetch messages from *chat_id* since *since_ts* (epoch-ms string).

        Paginates automatically (up to 20 pages / *max_chars* chars).

        Returns:
            (formatted_lines, latest_create_time_ms)
        """
        logger.debug("fetch_messages: chat_id=%s, since_ts=%s, max_chars=%d", chat_id, since_ts, max_chars)

        lines: List[str] = []
        latest_ts = since_ts
        page_token: Optional[str] = None
        total_chars = 0
        is_first_page = True
        page_count = 0

        for _ in range(20):
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .page_size(50)
                .sort_type("ByCreateTimeAsc")
            )
            if is_first_page and since_ts:
                start_sec = str(int(int(since_ts) / 1000) + 1)
                builder = builder.start_time(start_sec)
            if page_token:
                builder = builder.page_token(page_token)

            request = builder.build()
            try:
                response = self._client.im.v1.message.list(request)
            except Exception:
                logger.error("飞书 API 调用异常: chat_id=%s, page=%d", chat_id, page_count, exc_info=True)
                break

            if not response or not response.success():
                code = getattr(response, "code", "?")
                msg = getattr(response, "msg", "?")
                logger.warning("拉取消息失败: chat_id=%s, code=%s, msg=%s", chat_id, code, msg)
                break

            is_first_page = False
            page_count += 1
            items = getattr(getattr(response, "data", None), "items", None) or []

            for item in items:
                create_time = getattr(item, "create_time", "") or ""
                if since_ts and create_time and create_time <= since_ts:
                    continue

                line = self._format_message_item(item)
                if not line:
                    continue

                lines.append(line)
                total_chars += len(line)

                if create_time and (not latest_ts or create_time > latest_ts):
                    latest_ts = create_time

            has_more = getattr(getattr(response, "data", None), "has_more", False)
            page_token = getattr(getattr(response, "data", None), "page_token", None)
            if not has_more or not page_token:
                break
            if total_chars >= max_chars:
                logger.info("fetch_messages: 达到字符上限 %d, 停止分页", max_chars)
                break

        logger.debug(
            "fetch_messages 完成: chat_id=%s, pages=%d, messages=%d, chars=%d",
            chat_id, page_count, len(lines), total_chars,
        )
        return lines, latest_ts

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------

    def send_message(self, chat_id: str, text: str) -> bool:
        """Send a text message to *chat_id*. Returns True on success."""
        payload = json.dumps({"text": text}, ensure_ascii=False)
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(payload)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        try:
            response = self._client.im.v1.message.create(request)
            if response and response.success():
                msg_id = getattr(getattr(response, "data", None), "message_id", "?")
                logger.info("消息发送成功: chat_id=%s, message_id=%s", chat_id, msg_id)
                return True
            code = getattr(response, "code", "?")
            msg = getattr(response, "msg", "?")
            logger.warning("消息发送失败: chat_id=%s, code=%s, msg=%s", chat_id, code, msg)
            return False
        except Exception:
            logger.error("消息发送异常: chat_id=%s", chat_id, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Recent history (for MCP tool)
    # ------------------------------------------------------------------

    def fetch_recent_history(self, chat_id: str, limit: int = 50) -> List[str]:
        """Fetch the most recent *limit* messages (newest first, returned chronologically)."""
        logger.debug("fetch_recent_history: chat_id=%s, limit=%d", chat_id, limit)
        try:
            request = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .page_size(min(limit, 50))
                .sort_type("ByCreateTimeDesc")
                .build()
            )
            response = self._client.im.v1.message.list(request)
            if not response or not response.success():
                code = getattr(response, "code", "?")
                logger.warning("拉取历史消息失败: chat_id=%s, code=%s", chat_id, code)
                return []

            items = getattr(getattr(response, "data", None), "items", None) or []
            lines: List[str] = []
            for item in reversed(items):
                line = self._format_message_item(item)
                if line:
                    lines.append(line)

            logger.debug("fetch_recent_history 完成: chat_id=%s, count=%d", chat_id, len(lines))
            return lines
        except Exception:
            logger.error("拉取历史消息异常: chat_id=%s", chat_id, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Message formatting (internal)
    # ------------------------------------------------------------------

    def _format_message_item(self, item: Any) -> str:
        """Format a single API message item into ``[MM-DD HH:MM] sender: text``."""
        create_time = getattr(item, "create_time", "") or ""
        sender_obj = getattr(item, "sender", None)
        sender_id = getattr(sender_obj, "id", "") or ""
        sender_type = getattr(sender_obj, "sender_type", "") or ""
        body = getattr(item, "body", None)
        msg_type = getattr(item, "msg_type", "") or ""
        raw_content = getattr(body, "content", "") or ""

        text = self._extract_text(msg_type, raw_content)
        if not text:
            return ""

        sender_name = self._resolve_sender_name(sender_id)
        if sender_type in ("app", "bot"):
            sender_name = f"{sender_name}[bot]"

        ts_str = ""
        if create_time:
            try:
                ts_str = datetime.fromtimestamp(int(create_time) / 1000).strftime("%m-%d %H:%M")
            except (ValueError, OSError):
                pass

        return f"[{ts_str}] {sender_name}: {text}" if ts_str else f"{sender_name}: {text}"

    @staticmethod
    def _extract_text(msg_type: str, raw_content: str) -> str:
        """Extract plain text from a Feishu message JSON payload."""
        try:
            payload = json.loads(raw_content) if raw_content else {}
        except (json.JSONDecodeError, TypeError):
            return ""

        if msg_type == "text":
            return (payload.get("text") or "").strip()

        if msg_type == "post":
            parts: List[str] = []
            for lang_key in ("zh_cn", "en_us", "ja_jp"):
                locale = payload.get(lang_key)
                if not locale:
                    continue
                for para in locale.get("content", []):
                    for elem in (para if isinstance(para, list) else [para]):
                        tag = elem.get("tag", "")
                        if tag == "text":
                            parts.append(elem.get("text", ""))
                        elif tag == "a":
                            parts.append(elem.get("text", elem.get("href", "")))
                if parts:
                    break
            return " ".join(parts).strip()

        if msg_type == "interactive":
            header = payload.get("header", {})
            title = header.get("title", {}).get("content", "")
            return title.strip() if title else ""

        return ""
