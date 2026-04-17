# 部署说明（Hermes Docker 环境）

## 环境说明

Hermes 通过 Docker Compose 运行，数据目录挂载关系：
- 宿主机：`/data/hermes/data/`
- 容器内：`/opt/data/`

本服务通过 git clone 到 Hermes 数据目录中：
- 宿主机：`/data/hermes/data/mcp-server/feishu-alert-service/`
- 容器内：`/opt/data/mcp-server/feishu-alert-service/`

## 部署步骤

### 1. 拉取代码（已完成）

```bash
cd /data/hermes/data
git clone https://github.com/sz-wuyanzu/mcp-server.git
```

### 2. 编辑告警服务的 config.yaml

```bash
vi /data/hermes/data/mcp-server/feishu-alert-service/config.yaml
```

必须修改：
- `chats` 里的 `chat_id` — 换成真实的群 ID

`hermes_home` 不需要设置，容器内已有 `HERMES_HOME=/opt/data` 环境变量，代码会自动读取。

### 3. 进 Hermes 容器安装依赖

```bash
# 优先用 Hermes 自带的 venv
docker exec -it hermes /opt/hermes/.venv/bin/pip install \
  -r /opt/data/mcp-server/feishu-alert-service/requirements.txt

# 如果上面报错，试系统 pip
docker exec -it hermes pip install \
  -r /opt/data/mcp-server/feishu-alert-service/requirements.txt
```

### 4. 配置 MCP Server

编辑 Hermes 配置文件：

```bash
vi /data/hermes/data/config.yaml
```

末尾加上：

```yaml
mcp_servers:
  feishu-alert:
    command: "python3"
    args:
      - "/opt/data/mcp-server/feishu-alert-service/mcp_server.py"
      - "/opt/data/mcp-server/feishu-alert-service/config.yaml"
```

> 如果 `python3` 找不到依赖，改成 `/opt/hermes/.venv/bin/python`

### 5. 修改 Hermes 的 docker-compose.yaml

```bash
vi /data/hermes/docker-compose.yaml
```

在 command 里加一行启动告警摘要引擎：

```yaml
command: >
  /bin/sh -c "
    chown -R 10000:10000 /opt/data &&
    su -s /bin/sh hermes -c '
      /opt/hermes/.venv/bin/hermes gateway &
      /opt/hermes/.venv/bin/hermes dashboard --host 0.0.0.0 &
      python3 /opt/data/mcp-server/feishu-alert-service/main.py /opt/data/mcp-server/feishu-alert-service/config.yaml &
      wait
    '
  "
```

### 6. 重启 Hermes

```bash
cd /data/hermes
docker compose down
docker compose up -d
```

### 7. 验证

```bash
# 告警摘要进程是否在跑
docker exec hermes ps aux | grep main.py

# 查看日志
docker logs hermes 2>&1 | grep -i "feishu-alert\|digest"

# 在飞书群 @ 机器人问 "帮我查一下这个群最近的消息"
# 调用了 feishu_group_history 工具就说明 MCP 对接成功
```

## 容器内进程一览

```
Hermes 容器
├── hermes gateway          ← 飞书网关（接收 @ 消息）
├── hermes dashboard        ← Web 管理面板
├── main.py                 ← 告警摘要引擎（定时拉消息→LLM 摘要→发报告）
└── mcp_server.py           ← MCP 工具（Hermes 按需启动，查群历史）
```
