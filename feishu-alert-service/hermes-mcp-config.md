# 部署说明（Hermes Docker 环境）

## 环境说明

Hermes 通过 Docker Compose 运行，数据目录挂载到容器内 `/opt/data/`。

本服务通过 git clone 放到 Hermes 数据目录中，容器内路径为：
`/opt/data/mcp-server/feishu-alert-service/`

## 部署步骤

### 1. 拉取代码

在 Hermes 数据目录下 clone：

```bash
git clone https://github.com/sz-wuyanzu/mcp-server.git
```

### 2. 编辑 config.yaml

编辑 `/opt/data/mcp-server/feishu-alert-service/config.yaml`，
把 `chats` 里的 `chat_id` 换成真实的群 ID。

`hermes_home` 不需要设置，容器内已有 `HERMES_HOME=/opt/data` 环境变量。

### 3. 配置 MCP Server

编辑 Hermes 配置文件 `/opt/data/config.yaml`，末尾加上：

```yaml
mcp_servers:
  feishu-alert:
    command: "python3"
    args:
      - "/opt/data/mcp-server/feishu-alert-service/mcp_server.py"
      - "/opt/data/mcp-server/feishu-alert-service/config.yaml"
```

> 如果启动报 `ModuleNotFoundError`，说明 `python3` 不是 Hermes venv 的 python，
> 把 `command` 改成 `"/opt/hermes/.venv/bin/python"`

### 4. 重启 Hermes

重启容器即可，不需要修改 docker-compose.yaml。
Hermes 启动后会自动通过 MCP 配置启动 mcp_server.py，
告警摘要引擎作为后台线程同时运行。

### 5. 验证

在飞书群 @ 机器人问"帮我查一下这个群最近的消息"，
调用了 `feishu_group_history` 工具就说明 MCP 对接成功。

查看摘要数据：
```bash
ls /opt/data/mcp-server/feishu-alert-service/data/
```

## 工作原理

Hermes 通过 MCP 配置启动 `mcp_server.py`，这个进程同时做两件事：
- **MCP 工具**：通过 stdio 响应 Hermes 的查群历史请求
- **告警摘要**：后台线程定时拉消息 → LLM 摘要 → 发报告到群

一个进程，一个配置入口，不需要额外的容器或 compose 改动。
