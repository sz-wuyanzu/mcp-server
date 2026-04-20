"""Read Feishu credentials and LLM config from Hermes installation.

This module never calls sys.exit(). All errors are raised as exceptions
so the caller (main.py / mcp_server.py) can decide how to handle them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_HERMES_HOME = Path.home() / ".hermes"


class HermesConfigError(Exception):
    """Raised when Hermes configuration is missing or invalid."""


@dataclass(frozen=True)
class FeishuCredentials:
    app_id: str
    app_secret: str
    domain: str  # "feishu" or "lark"


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str


def _find_hermes_home(override: Optional[str] = None) -> Path:
    """Resolve Hermes home directory.

    Priority: explicit override > HERMES_HOME env > ~/.hermes

    If an explicit override is given, it is returned as-is (even if it doesn't
    exist) so the caller can produce a clear error message.
    """
    if override:
        p = Path(override).expanduser()
        logger.debug("Using hermes_home override: %s (exists=%s)", p, p.exists())
        return p

    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        p = Path(env_home)
        logger.debug("Using HERMES_HOME env: %s (exists=%s)", p, p.exists())
        return p

    return DEFAULT_HERMES_HOME


def _load_dotenv(env_path: Path) -> Dict[str, str]:
    """Parse a simple .env file into a dict.

    Supports:
      - KEY=value
      - KEY="value" / KEY='value' (quotes stripped)
      - # comments and blank lines (skipped)
    """
    result: Dict[str, str] = {}
    if not env_path.exists():
        logger.debug(".env file not found: %s", env_path)
        return result

    try:
        for lineno, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                logger.debug(".env line %d skipped (no '='): %s", lineno, line[:40])
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            result[key] = value
    except OSError as exc:
        logger.warning("Failed to read .env file %s: %s", env_path, exc)

    return result


def load_feishu_credentials(hermes_home_override: Optional[str] = None) -> FeishuCredentials:
    """Load Feishu credentials from Hermes .env file.

    Raises:
        HermesConfigError: if Hermes is not installed or Feishu is not configured.
    """
    hermes_home = _find_hermes_home(hermes_home_override)

    if not hermes_home.exists():
        raise HermesConfigError(
            f"Hermes 未安装: {hermes_home} 不存在。"
            f"请先安装并配置 Hermes Agent 的飞书网关。"
        )

    env_path = hermes_home / ".env"
    env_vars = _load_dotenv(env_path)

    app_id = env_vars.get("FEISHU_APP_ID", "").strip()
    app_secret = env_vars.get("FEISHU_APP_SECRET", "").strip()
    domain = env_vars.get("FEISHU_DOMAIN", "feishu").strip()

    if not app_id or not app_secret:
        raise HermesConfigError(
            f"Hermes 未配置飞书: 在 {env_path} 中未找到 FEISHU_APP_ID / FEISHU_APP_SECRET。"
            f"请先运行 'hermes gateway' 配置飞书平台。"
        )

    # Log with masked secret for security
    masked_secret = app_secret[:4] + "****" + app_secret[-4:] if len(app_secret) > 8 else "****"
    logger.info(
        "从 Hermes 读取飞书凭证: app_id=%s, app_secret=%s, domain=%s",
        app_id, masked_secret, domain,
    )
    return FeishuCredentials(app_id=app_id, app_secret=app_secret, domain=domain)


def load_llm_config(
    hermes_home_override: Optional[str] = None,
    model_override: Optional[str] = None,
) -> LLMConfig:
    """Load LLM config from Hermes, optionally override model name only.

    Raises:
        HermesConfigError: if Hermes config.yaml is missing or has no valid model config.
    """
    hermes_home = _find_hermes_home(hermes_home_override)
    config_path = hermes_home / "config.yaml"

    if not config_path.exists():
        raise HermesConfigError(f"Hermes config.yaml 不存在: {config_path}")

    try:
        with open(config_path, encoding="utf-8") as f:
            hermes_cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise HermesConfigError(f"读取 Hermes config.yaml 失败: {exc}") from exc

    model_cfg = hermes_cfg.get("model", {})
    if not isinstance(model_cfg, dict):
        raise HermesConfigError("Hermes config.yaml 中 model 节点格式错误")

    model_name = str(model_cfg.get("default", "")).strip()
    provider_name = str(model_cfg.get("provider", "")).strip()
    base_url = str(model_cfg.get("base_url", "")).strip()
    api_key = str(model_cfg.get("api_key", "")).strip()

    # If a named provider is set, look up its base_url/api_key from custom_providers
    if provider_name:
        matched = False
        for cp in hermes_cfg.get("custom_providers", []):
            if isinstance(cp, dict) and str(cp.get("name", "")).strip() == provider_name:
                cp_base_url = str(cp.get("base_url", "")).strip()
                cp_api_key = str(cp.get("api_key", "")).strip()
                if cp_base_url:
                    base_url = cp_base_url
                if cp_api_key:
                    api_key = cp_api_key
                if not model_name:
                    model_name = str(cp.get("model", "")).strip()
                logger.info("匹配 custom_provider '%s': base_url=%s", provider_name, base_url)
                matched = True
                break
        if not matched:
            logger.warning("未找到 custom_provider '%s', 使用 model.base_url=%s", provider_name, base_url)

    if not base_url:
        raise HermesConfigError(
            "Hermes config.yaml 中未找到 model.base_url 或对应 provider 的 base_url。"
            "请确保 Hermes 已配置 LLM provider。"
        )
    if not model_name:
        raise HermesConfigError(
            "Hermes config.yaml 中未找到 model.default。请确保 Hermes 已配置默认模型。"
        )

    if model_override:
        logger.info("LLM 配置: model=%s (覆盖), base_url=%s", model_override, base_url)
        model_name = model_override
    else:
        logger.info("LLM 配置: model=%s, base_url=%s (from Hermes)", model_name, base_url)

    return LLMConfig(base_url=base_url, api_key=api_key, model=model_name)
