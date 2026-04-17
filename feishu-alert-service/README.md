# Feishu Alert Service

飞书告警摘要服务，作为 MCP 插件运行在 Hermes Agent 容器内。

## 功能

1. 定时告警摘要 — 定期拉取飞书群消息（包括 webhook 机器人发的告警），LLM 分段摘要 + 归总报告，自动发送到群，包含事件等级、原因分析和解决方案
2. 群历史查询 — 提供 MCP 工具，让 Hermes 被 @ 时能查询群历史消息
3. @所有人 — 归总报告可配置是否 @所有人（真正的飞书 @all 通知）

## 前置条件

- Hermes Agent 已通过 Docker Compose 部署并配置了飞书网关
- 飞书凭证和 LLM 配置自动从 Hermes 读取，无需重复配置

## 文件说明

```
mcp_server.py      ← 入口：MCP 工具 + 告警摘要引擎（后台线程）
feishu_client.py   ← 飞书 API（拉消息、发消息、@all）
llm_client.py      ← LLM 调用（带重试）
digest_engine.py   ← 分段摘要 + 归总报告逻辑
hermes_config.py   ← 从 Hermes 读取飞书凭证和 LLM 配置
main.py            ← 独立运行入口（非 Docker 场景备用）
config.yaml        ← 配置文件（群列表、提示词、参数）
```

## 部署步骤（Hermes Docker 环境）

### 1. 拉取代码

在 Hermes 数据目录下 clone：

```bash
git clone https://github.com/sz-wuyanzu/mcp-server.git
```

容器内路径为 `/opt/data/mcp-server/feishu-alert-service/`

### 2. 编辑 config.yaml

编辑 `/opt/data/mcp-server/feishu-alert-service/config.yaml`，
把 `chats` 里的 `chat_id` 换成真实的群 ID。

其他可调整项：
- `segment_interval` / `report_interval` — 摘要和报告频率
- `mention_all` — 报告是否 @所有人
- `segment_prompt` / `report_prompt` — LLM 提示词
- `model` — 可选，用更便宜的模型跑摘要

### 3. 配置 MCP Server

编辑 Hermes 配置文件 `/opt/data/config.yaml`，末尾加上：

```yaml
mcp_servers:
  feishu-alert:
    command: "/opt/hermes/.venv/bin/python"
    args:
      - "/opt/data/mcp-server/feishu-alert-service/mcp_server.py"
      - "/opt/data/mcp-server/feishu-alert-service/config.yaml"
```

### 4. 重启 Hermes

重启容器即可，不需要修改 docker-compose.yaml。
首次启动时会自动安装缺失的 Python 依赖。

### 5. 验证

- 在飞书群 @ 机器人问"帮我查一下这个群最近的消息"，调用了 `feishu_group_history` 工具就说明 MCP 对接成功
- 查看日志：`docker logs hermes 2>&1 | grep feishu-alert`
- 查看摘要数据：容器内 `/opt/data/mcp-server/feishu-alert-service/data/`
- 查看服务日志：容器内 `/opt/data/mcp-server/feishu-alert-service/logs/service.log`

## 工作原理

Hermes 通过 MCP 配置启动 `mcp_server.py`，这个进程同时做两件事：
- MCP 工具：通过 stdio 响应 Hermes 的查群历史请求
- 告警摘要：后台线程定时拉消息 → LLM 摘要 → 发报告到群

一个进程，一个配置入口，不需要额外的容器或 compose 改动。

## 更新

```bash
cd /opt/data/mcp-server
git pull
```

然后重启 Hermes 容器生效。
