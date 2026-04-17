# 部署说明（Hermes Docker 环境）

## 你的 Hermes 环境

```
/data/hermes/
├── docker-compose.yaml      ← Hermes compose
└── data/                    ← Hermes 数据目录（容器内 /opt/data）
    ├── .env                 ← 飞书凭证
    └── config.yaml          ← Hermes 配置
```

## 部署步骤

### 1. 把代码放到 Hermes 数据目录

```bash
# 在服务器上
mkdir -p /data/hermes/data/mcp/feishu-alert
# 把所有 py 文件和配置复制过去
cp *.py /data/hermes/data/mcp/feishu-alert/
cp config.yaml /data/hermes/data/mcp/feishu-alert/
cp requirements.txt /data/hermes/data/mcp/feishu-alert/
```

容器内路径对应关系：
- 宿主机 `/data/hermes/data/mcp/feishu-alert/`
- 容器内 `/opt/data/mcp/feishu-alert/`

### 2. 进 Hermes 容器安装依赖

```bash
# 先确认容器内的 python 路径
docker exec hermes which python3
# 通常是 /usr/bin/python3 或 /opt/hermes/.venv/bin/python

# 用容器内的 pip 安装（如果是 venv 环境）
docker exec hermes /opt/hermes/.venv/bin/pip install \
  -r /opt/data/mcp/feishu-alert/requirements.txt

# 如果上面报错，试系统 pip
docker exec hermes pip install \
  -r /opt/data/mcp/feishu-alert/requirements.txt
```

### 3. 编辑告警服务的 config.yaml

```bash
vi /data/hermes/data/mcp/feishu-alert/config.yaml
```

把 `chat_id` 换成真实的群 ID，设置 `hermes_home`：

```yaml
hermes_home: "/opt/data"
# ... 其他配置
```

因为在容器内，Hermes 数据目录是 `/opt/data`，不是 `~/.hermes`。

### 4. 配置 MCP Server（让 Hermes 能查群历史）

编辑 `/data/hermes/data/config.yaml`（Hermes 的配置），末尾加上：

```yaml
mcp_servers:
  feishu-alert:
    command: "python3"
    args:
      - "/opt/data/mcp/feishu-alert/mcp_server.py"
      - "/opt/data/mcp/feishu-alert/config.yaml"
```

Hermes 会在需要时自动启动 mcp_server.py，通过 stdio 通信，不需要你手动运行。

> 如果 `python3` 找不到依赖，改成 `/opt/hermes/.venv/bin/python`

### 5. 修改 Hermes 的 docker-compose.yaml

把告警摘要引擎（main.py）加到 Hermes 容器的启动命令里：

```yaml
version: "3.9"
services:
  hermes:
    image: harbor.sisensing.com/base/hermes-agent:20260413
    container_name: hermes
    user: root
    entrypoint: ""
    command: >
      /bin/sh -c "
        chown -R 10000:10000 /opt/data &&
        su -s /bin/sh hermes -c '
          /opt/hermes/.venv/bin/hermes gateway &
          /opt/hermes/.venv/bin/hermes dashboard --host 0.0.0.0 &
          python3 /opt/data/mcp/feishu-alert/main.py /opt/data/mcp/feishu-alert/config.yaml &
          wait
        '
      "
    volumes:
      - ${PWD}/data:/opt/data
    environment:
      - TZ=Asia/Shanghai
    ports:
      - "9119:9119"
    stdin_open: true
    tty: true
    restart: unless-stopped
```

唯一的改动是在 command 里多加了一行：
```
python3 /opt/data/mcp/feishu-alert/main.py /opt/data/mcp/feishu-alert/config.yaml &
```

### 6. 重启 Hermes

```bash
cd /data/hermes
docker compose down
docker compose up -d
```

### 7. 验证

```bash
# 查看告警摘要引擎日志
docker exec hermes cat /opt/data/mcp/feishu-alert/data/*.md

# 查看进程是否在运行
docker exec hermes ps aux | grep main.py

# 在飞书群 @ 机器人问 "帮我查一下这个群最近的消息"
# 如果调用了 feishu_group_history 工具就说明 MCP 对接成功
```

## 总结

```
Hermes 容器内同时运行：
├── hermes gateway          ← 飞书网关（接收 @ 消息）
├── hermes dashboard        ← Web 管理面板
├── main.py                 ← 告警摘要引擎（定时拉消息、LLM 摘要、发报告）
└── mcp_server.py           ← MCP 工具（由 Hermes 按需启动，查群历史）
```

所有东西都在一个容器里，共享 /opt/data，不需要额外的容器。
