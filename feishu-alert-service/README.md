# Feishu Alert Service

独立的飞书告警摘要服务，与 Hermes Agent 零耦合。

## 功能

1. **定时告警摘要** — 定期拉取飞书群消息，LLM 分段摘要 + 归总报告，自动发送到群
2. **群历史查询 MCP** — 提供 MCP server，让 Hermes Agent 被 @ 时能查询群历史消息（包括 webhook 机器人发的告警）

## 前置条件

- Hermes Agent 已通过 Docker 部署并配置了飞书网关
- 飞书凭证和 LLM 配置自动从 Hermes 数据目录读取

## 部署

```bash
# 编辑 config.yaml，填入群 chat_id
docker compose up -d --build
docker compose logs -f
```

## Hermes MCP 对接

详见 [hermes-mcp-config.md](hermes-mcp-config.md)
