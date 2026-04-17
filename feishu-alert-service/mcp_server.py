"""Feishu Alert MCP Server + Digest Engine.

Single entry point: Hermes launches this via mcp_servers config.
- MCP tools (feishu_group_history, feishu_list_monitored_chats) run on stdio
- Digest engine (periodic alert summarization) runs in a background thread

Usage by Hermes:
    command: "python3"
    args: ["/opt/data/mcp-server/feishu-alert-service/mcp_server.py",
           "/opt/data/mcp-server/feishu-alert-service/config.yaml"]
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

# Ensure sibling modules are importable regardless of working directory
_SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SELF_DIR))

# ---------------------------------------------------------------------------
# Auto-install dependencies on first run
# ---------------------------------------------------------------------------

def _ensure_dependencies() -> None:
    """Check required packages and install from requirements.txt if missing."""
    required = ["lark_oapi", "openai", "yaml", "mcp"]
    missing = []
    for mod in required:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if not missing:
        return

    req_file = _SELF_DIR / "requirements.txt"
    if not req_file.exists():
        print(f"[feishu-alert] 缺少依赖 {missing}，且找不到 {req_file}", file=sys.stderr)
        sys.exit(1)

    print(f"[feishu-alert] 首次运行，自动安装依赖: {missing}", file=sys.stderr)
    import subprocess
    # Try uv first (installs to current venv), fall back to pip with current interpreter
    for cmd in [
        ["uv", "pip", "install", "--python", sys.executable, "-r", str(req_file)],
        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                print(f"[feishu-alert] 依赖安装成功", file=sys.stderr)
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue

    print(f"[feishu-alert] 依赖安装失败，请手动执行:", file=sys.stderr)
    print(f"  uv pip install -r {req_file}", file=sys.stderr)
    sys.exit(1)

_ensure_dependencies()

# ---------------------------------------------------------------------------

import yaml
from mcp.server.fastmcp import FastMCP

from feishu_client import FeishuClient
from llm_client import LLMClient
from hermes_config import HermesConfigError, load_feishu_credentials, load_llm_config
from digest_engine import (
    ChatConfig, ChatWorker, DigestEngine, Storage,
)

# ---------------------------------------------------------------------------
# Logging: stderr (container console) + file
# stdout is reserved for MCP protocol, so all logs go to stderr + file
# ---------------------------------------------------------------------------

_LOG_PREFIX = "mcp-server/feishu-alert-service"
_LOG_FORMAT = f"%(asctime)s %(levelname)-7s [{_LOG_PREFIX}] [%(name)s] %(message)s"
_LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

def _setup_logging() -> None:
    """Configure root logger to output to stderr + log file."""
    log_dir = _SELF_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_dir = None

    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT))
    root.addHandler(stderr_handler)

    if log_dir:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_dir / "service.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FMT))
        root.addHandler(file_handler)

_setup_logging()
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_mcp = FastMCP("feishu-alert-service")
_feishu_client: FeishuClient | None = None
_monitored_chats: Dict[str, str] = {}
_digest_stop = False


# ---------------------------------------------------------------------------
# Config loading & validation
# ---------------------------------------------------------------------------

def _load_config(config_path: str) -> Dict[str, Any]:
    """Load and validate config.yaml."""
    p = Path(config_path)
    if not p.exists():
        logger.error("配置文件不存在: %s", p.resolve())
        sys.exit(1)

    try:
        with open(p, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as exc:
        logger.error("配置文件读取失败: %s — %s", config_path, exc)
        sys.exit(1)

    if not isinstance(cfg, dict):
        logger.error("配置文件格式错误")
        sys.exit(1)

    # Validate chats
    chats = cfg.get("chats")
    if not chats or not isinstance(chats, list):
        logger.error("配置错误: 缺少 'chats' 列表。请在 config.yaml 中添加至少一个群配置。")
        sys.exit(1)

    valid_chats = []
    for i, entry in enumerate(chats):
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("chat_id", "")).strip()
        if not cid:
            continue
        if cid.startswith("oc_xxx") or cid.startswith("oc_yyy"):
            logger.error("chats[%d] 的 chat_id 还是示例值 (%s)，请替换为真实的群 ID。", i, cid)
            sys.exit(1)
        valid_chats.append(entry)

    if not valid_chats:
        logger.error("没有有效的群配置。")
        sys.exit(1)

    cfg["chats"] = valid_chats
    return cfg


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def _init_all(config_path: str) -> None:
    """Initialize Feishu client, LLM client, and start digest engine."""
    global _feishu_client, _monitored_chats, _digest_stop

    cfg = _load_config(config_path)
    hermes_home = cfg.get("hermes_home")

    # --- Feishu ---
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
    for entry in cfg["chats"]:
        cid = str(entry["chat_id"]).strip()
        name = str(entry.get("name", "")).strip()
        _monitored_chats[cid] = name or cid

    logger.info("飞书客户端就绪: %d 个监控群", len(_monitored_chats))

    # --- LLM ---
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

    try:
        llm.verify_model()
    except RuntimeError as exc:
        logger.error("模型验证失败: %s", exc)
        sys.exit(1)

    # --- Storage ---
    data_dir = Path(cfg.get("data_dir", "./data"))
    if not data_dir.is_absolute():
        data_dir = Path(config_path).resolve().parent / data_dir
    try:
        storage = Storage(data_dir)
    except OSError:
        sys.exit(1)

    # --- Build workers ---
    defaults = cfg.get("defaults", {}) or {}
    segment_prompt = (cfg.get("segment_prompt") or "").strip()
    report_prompt = (cfg.get("report_prompt") or "").strip()
    if not segment_prompt or not report_prompt:
        logger.error("配置错误: segment_prompt 和 report_prompt 不能为空，请在 config.yaml 中配置提示词。")
        sys.exit(1)

    workers: List[ChatWorker] = []
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

        workers.append(ChatWorker(
            cfg=ChatConfig(
                chat_id=chat_id,
                name=name,
                enabled=bool(entry.get("enabled", defaults.get("enabled", True))),
                mention_all=bool(entry.get("mention_all", defaults.get("mention_all", False))),
                segment_interval=_int("segment_interval", 10),
                report_cycle=_int("report_cycle", 6),
                max_chars_per_fetch=_int("max_chars_per_fetch", 30000),
            ),
            feishu=_feishu_client,
            llm=llm,
            storage=storage,
            segment_prompt=segment_prompt,
            report_prompt=report_prompt,
        ))

    # Verify chats (non-blocking)
    for w in workers:
        try:
            _feishu_client.verify_chat(w.cfg.chat_id)
        except RuntimeError as exc:
            logger.warning("群验证失败 (不影响启动): %s", exc)

    logger.info("已配置 %d 个群: %s", len(workers), ", ".join(w.label for w in workers))

    # --- Start digest engine in background thread ---
    if workers and any(w.cfg.enabled for w in workers):
        tick = min(w.cfg.segment_interval for w in workers if w.cfg.enabled) * 60
        engine = DigestEngine(workers)

        def _run_digest_forever():
            """Run digest engine with auto-restart on crash."""
            import time as _time
            restart_count = 0
            while not _digest_stop:
                try:
                    if restart_count > 0:
                        wait = min(restart_count * 10, 60)
                        logger.info("告警摘要引擎将在 %ds 后重启 (第 %d 次)", wait, restart_count)
                        for _ in range(wait):
                            if _digest_stop:
                                return
                            _time.sleep(1)
                    logger.info("告警摘要引擎启动 (后台线程)")
                    engine.run_forever(stop_check=lambda: _digest_stop)
                    break
                except Exception:
                    restart_count += 1
                    logger.error("告警摘要引擎崩溃 (第 %d 次), 将自动重启", restart_count, exc_info=True)
            logger.info("告警摘要引擎已停止")

        t = threading.Thread(target=_run_digest_forever, name="digest-engine", daemon=True)
        t.start()
    else:
        logger.info("无启用的群，告警摘要引擎未启动")


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
        logger.error("feishu_group_history: FeishuClient 未初始化")
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
    logger.info("Feishu Alert Service 启动中...")
    logger.info("配置文件: %s", Path(config_path).resolve())
    logger.info("=" * 50)

    _init_all(config_path)

    def _on_exit(sig, _frame):
        global _digest_stop
        _digest_stop = True
        sig_name = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
        logger.info("收到退出信号: %s", sig_name)

    signal.signal(signal.SIGTERM, _on_exit)
    signal.signal(signal.SIGINT, _on_exit)

    logger.info("MCP Server 就绪 (stdio 模式)")
    _mcp.run(transport="stdio")
