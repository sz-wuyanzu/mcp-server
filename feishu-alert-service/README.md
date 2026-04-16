# Feishu Alert Service

独立的飞书告警摘要服务，与 Hermes Agent 零耦合。

## 功能

1. **定时告警摘要** — 定期拉取飞书群消息，LLM 分段摘要 + 归总报告，发送到群
2. **群历史查询 MCP** — 提供 MCP server，让 Hermes Agent @ 时能查询群历史消息

## 部署

```bash
cd feishu-alert-service
pip install -r requirements.txt
cp config.yaml.example config.yaml  # 编辑配置
python main.py                       # 启动服务
```

## Docker

```bash
docker compose up -d
```
