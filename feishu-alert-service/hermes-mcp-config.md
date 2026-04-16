# 在 Hermes 中配置 MCP 连接

在 `~/.hermes/config.yaml` 中添加：

```yaml
mcp_servers:
  feishu-alert:
    command: python
    args: ["/path/to/feishu-alert-service/mcp_server.py"]
```

或者如果用 Docker 部署，使用 stdio 模式：

```yaml
mcp_servers:
  feishu-alert:
    command: docker
    args: ["exec", "-i", "feishu-alert-service", "python", "mcp_server.py"]
```

配置后，在飞书群 @ Hermes 时可以使用：
- `feishu_group_history(chat_id, limit)` — 查询群最近消息
- `feishu_list_monitored_chats()` — 列出监控的群
