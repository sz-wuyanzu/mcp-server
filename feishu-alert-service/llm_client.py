"""OpenAI-compatible LLM client with structured retry logic.

Retries on transient errors (504, timeout). Non-retryable errors fail immediately.
All calls are logged with elapsed time.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# Errors that warrant a retry (gateway/proxy issues, not model errors)
_RETRYABLE_KEYWORDS = ("504", "timeout", "timed out", "connection", "502", "503")
_MAX_RETRIES = 3


class LLMClient:
    """OpenAI-compatible LLM client with automatic retry on transient failures."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        max_retries: int = _MAX_RETRIES,
    ) -> None:
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model
        self._max_retries = max_retries
        logger.info("LLMClient 初始化: model=%s, base_url=%s, timeout=%ds", model, base_url, timeout)

    @property
    def model(self) -> str:
        return self._model

    def verify_model(self) -> None:
        """Verify the model is reachable by sending a minimal request.

        Raises RuntimeError if the model is not available.
        """
        logger.info("验证模型可用性: %s ...", self._model)
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            if not resp.choices:
                raise RuntimeError(f"模型 {self._model} 返回空响应")
            logger.info("模型验证通过: %s", self._model)
        except Exception as e:
            err = str(e)
            # Distinguish model-not-found from network issues
            if any(kw in err.lower() for kw in ("not found", "does not exist", "invalid model", "unknown model", "no such model")):
                raise RuntimeError(
                    f"模型 '{self._model}' 不存在或不可用。"
                    f"请检查 config.yaml 中的 model 配置，或确认 LLM 服务端支持该模型。\n"
                    f"原始错误: {err[:200]}"
                ) from e
            if any(kw in err.lower() for kw in _RETRYABLE_KEYWORDS):
                logger.warning("模型验证时网络异常 (不影响启动): %s", err[:100])
                return  # Network flake — don't block startup
            raise RuntimeError(
                f"模型验证失败: {err[:200]}\n"
                f"请检查 LLM 服务是否正常运行。"
            ) from e

    def summarize(self, text: str, system_prompt: str) -> str:
        """Send *text* to the LLM with *system_prompt* and return the response.

        Returns empty string on failure (after retries exhausted).
        """
        messages: List[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        input_chars = len(text)

        last_err: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=2000,
                )
                elapsed = time.monotonic() - t0

                # Guard against empty response
                if not resp.choices:
                    logger.warning(
                        "LLM 返回空 choices: model=%s, input=%d chars, elapsed=%.1fs",
                        self._model, input_chars, elapsed,
                    )
                    return ""

                content = resp.choices[0].message.content or ""
                result = content.strip()

                logger.info(
                    "LLM 调用成功: model=%s, input=%d chars -> output=%d chars, elapsed=%.1fs",
                    self._model, input_chars, len(result), elapsed,
                )
                return result

            except Exception as e:
                elapsed = time.monotonic() - t0
                last_err = e
                err_str = str(e).lower()

                if any(kw in err_str for kw in _RETRYABLE_KEYWORDS):
                    wait = attempt * 10
                    logger.warning(
                        "LLM 调用失败 (可重试) [%d/%d]: %s, elapsed=%.1fs, %ds 后重试",
                        attempt, self._max_retries, str(e)[:100], elapsed, wait,
                    )
                    time.sleep(wait)
                    continue

                # Non-retryable error — fail immediately
                logger.error(
                    "LLM 调用失败 (不可重试): %s, elapsed=%.1fs",
                    str(e)[:200], elapsed,
                )
                return ""

        logger.error(
            "LLM 调用失败: %d 次重试均失败, 最后错误: %s",
            self._max_retries, last_err,
        )
        return ""
