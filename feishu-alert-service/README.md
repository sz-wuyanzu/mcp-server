# Feishu Alert Service

独立的飞书告警摘要服务，部署在 Hermes 容器内运行。

## 功能

1. **定时告警摘要** — 定期拉取飞书群消息，LLM 分段摘要 + 归总报告，自动发送到群
2. **群历史查询 MCP** — 提供 MCP 工具，让 Hermes 被 @ 时能查询群历史消息（包括 webhook 机器人发的告警）

## 前置条件

- Hermes 已通过 Docker Compose 部署并配置了飞书网关

## 部署

代码通过 git clone 放到 Hermes 数据目录中，在 Hermes 容器内运行。

详见 [hermes-mcp-config.md](hermes-mcp-config.md)
