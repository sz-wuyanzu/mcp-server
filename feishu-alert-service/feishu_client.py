"""Feishu API client — fetch group messages and send messages."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FeishuClient:
    """Thin wrapper around lark-oapi for message fetching and sending."""

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu"):
        import lark_oapi as lark
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

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

    def fetch_messages(
        self,
        chat_id: str,
        since_ts: Optional[str] = None,
        max_chars: int = 30000,
    ) -> tuple[List[str], Optional[str]]:
        """Fetch messages from a chat since ``since_ts`` (epoch-ms).

        Returns (formatted_lines, latest_create_time_ms).
        """
        from lark_oapi.api.im.v1 import ListMessageRequest

        lines: List[str] = []
        latest_ts = since_ts
        page_token: Optional[str] = None
        total_chars = 0

        for _ in range(20):  # max 20 pages
            builder = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .page_size(50)
                .sort_type("ByCreateTimeAsc")
            )
            if since_ts:
                start_sec = str(int(int(since_ts) / 1000) + 1)
                builder = builder.start_time(start_sec)
            if page_token:
                builder = builder.page_token(page_token)

            request = builder.build()
            try:
                response = self._client.im.v1.message.list(request)
            except Exception:
                logger.warning("API call failed for chat %s", chat_id, exc_info=True)
                break

            if not response or not response.success():
                code = getattr(response, "code", "?")
                msg = getattr(response, "msg", "?")
                logger.warning("List messages failed for %s: [%s] %s", chat_id, code, msg)
                break

            items = getattr(getattr(response, "data", None), "items", None) or []

            for item in items:
                create_time = getattr(item, "create_time", "") or ""
                if since_ts and create_time and create_time <= since_ts:
                    continue

                sender_obj = getattr(item, "sender", None)
                sender_id = getattr(sender_obj, "id", "") or ""
                sender_type = getattr(sender_obj, "sender_type", "") or ""
                body = getattr(item, "body", None)
                msg_type = getattr(item, "msg_type", "") or ""
                raw_content = getattr(body, "content", "") or ""

                text = self._extract_text(msg_type, raw_content)
                if not text:
                    continue

                sender_name = self._sender_cache.get(sender_id, sender_id or "unknown")
                if sender_type in ("app", "bot"):
                    sender_name = f"{sender_name}[bot]"

                ts_str = ""
                if create_time:
                    try:
                        ts_str = datetime.fromtimestamp(int(create_time) / 1000).strftime("%m-%d %H:%M")
                    except (ValueError, OSError):
                        pass

                line = f"[{ts_str}] {sender_name}: {text}" if ts_str else f"{sender_name}: {text}"
                lines.append(line)
                total_chars += len(line)

                if create_time and (not latest_ts or create_time > latest_ts):
                    latest_ts = create_time

            has_more = getattr(getattr(response, "data", None), "has_more", False)
            page_token = getattr(getattr(response, "data", None), "page_token", None)
            if not has_more or not page_token:
                break
            if total_chars >= max_chars:
                break

        return lines, latest_ts

    def send_message(self, chat_id: str, text: str) -> bool:
        """Send a text message to a chat."""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        # Try rich-text post first, fall back to plain text
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
                return True
            code = getattr(response, "code", "?")
            msg = getattr(response, "msg", "?")
            logger.warning("Send failed for %s: [%s] %s", chat_id, code, msg)
            return False
        except Exception:
            logger.warning("Send error for %s", chat_id, exc_info=True)
            return False

    def fetch_recent_history(self, chat_id: str, limit: int = 50) -> List[str]:
        """Fetch the most recent N messages (for MCP tool)."""
        from lark_oapi.api.im.v1 import ListMessageRequest

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
                return []

            items = getattr(getattr(response, "data", None), "items", None) or []
            lines: List[str] = []
            for item in reversed(items):
                sender_obj = getattr(item, "sender", None)
                sender_id = getattr(sender_obj, "id", "") or ""
                sender_type = getattr(sender_obj, "sender_type", "") or ""
                body = getattr(item, "body", None)
                msg_type = getattr(item, "msg_type", "") or ""
                raw_content = getattr(body, "content", "") or ""
                create_time = getattr(item, "create_time", "") or ""

                text = self._extract_text(msg_type, raw_content)
                if not text:
                    continue

                sender_name = self._sender_cache.get(sender_id, sender_id or "unknown")
                if sender_type in ("app", "bot"):
                    sender_name = f"{sender_name}[bot]"

                ts_str = ""
                if create_time:
                    try:
                        ts_str = datetime.fromtimestamp(int(create_time) / 1000).strftime("%m-%d %H:%M")
                    except (ValueError, OSError):
                        pass

                line = f"[{ts_str}] {sender_name}: {text}" if ts_str else f"{sender_name}: {text}"
                lines.append(line)

            return lines
        except Exception:
            logger.warning("Failed to fetch history for %s", chat_id, exc_info=True)
            return []

    @staticmethod
    def _extract_text(msg_type: str, raw_content: str) -> str:
        """Extract plain text from a Feishu message payload."""
        try:
            payload = json.loads(raw_content) if raw_content else {}
        except (json.JSONDecodeError, TypeError):
            return ""

        if msg_type == "text":
            return (payload.get("text") or "").strip()

        if msg_type == "post":
            # Extract text from rich-text post
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
            # Card messages — extract header + text elements
            header = payload.get("header", {})
            title = header.get("title", {}).get("content", "")
            return title.strip() if title else ""

        return ""
