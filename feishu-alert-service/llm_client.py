"""LLM client for summarization."""

from __future__ import annotations

import logging
import time
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


class LLMClient:
    """Simple OpenAI-compatible LLM client with retry."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=120)
        self._model = model

    def summarize(self, text: str, prompt: str) -> str:
        """Summarize text with retry on 504/timeout."""
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ]

        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=2000,
                )
                return resp.choices[0].message.content.strip()
            except Exception as e:
                last_err = e
                err_str = str(e)
                if "504" in err_str or "timeout" in err_str.lower():
                    wait = (attempt + 1) * 10
                    logger.warning("LLM attempt %d/3 failed, retry in %ds: %s", attempt + 1, wait, err_str[:100])
                    time.sleep(wait)
                    continue
                raise

        logger.warning("LLM summarization failed after 3 attempts: %s", last_err)
        return ""
