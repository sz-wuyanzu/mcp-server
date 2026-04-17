#!/bin/sh
# ============================================================
# MCP Server 服务管理脚本
#
# 用法:
#   ./mcp-server.sh start                  # 启动所有服务
#   ./mcp-server.sh start feishu-alert-service  # 启动单个服务
#   ./mcp-server.sh stop                   # 停止所有服务
#   ./mcp-server.sh stop feishu-alert-service
#   ./mcp-server.sh restart                # 重启所有服务
#   ./mcp-server.sh restart feishu-alert-service
#   ./mcp-server.sh status                 # 查看所有服务状态
#   ./mcp-server.sh status feishu-alert-service
#
# 每个服务子目录下需要有 main.py 和 config.yaml
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${HERMES_PYTHON:-/opt/hermes/.venv/bin/python}"

# 检查 Python 是否可用
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "错误: Python 不存在: $PYTHON" >&2
    echo "请设置 HERMES_PYTHON 环境变量指向正确的 Python 路径" >&2
    exit 1
fi

# 发现所有服务（包含 main.py 的子目录）
_discover_services() {
    for dir in "$SCRIPT_DIR"/*/; do
        [ -f "$dir/main.py" ] && [ -f "$dir/config.yaml" ] && basename "$dir"
    done
}

# 获取服务列表（指定的或全部）
_get_services() {
    if [ -n "$1" ]; then
        if [ ! -d "$SCRIPT_DIR/$1" ]; then
            echo "错误: 服务 '$1' 不存在" >&2
            exit 1
        fi
        echo "$1"
    else
        _discover_services
    fi
}

_pid_file() { echo "$SCRIPT_DIR/$1/data/service.pid"; }

_is_running() {
    pf="$(_pid_file "$1")"
    [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null
}

_start_one() {
    svc="$1"
    svc_dir="$SCRIPT_DIR/$svc"
    pf="$(_pid_file "$svc")"

    if _is_running "$svc"; then
        echo "[$svc] 已在运行 (PID: $(cat "$pf"))"
        return 0
    fi

    mkdir -p "$svc_dir/data" "$svc_dir/logs"
    nohup "$PYTHON" "$svc_dir/main.py" "$svc_dir/config.yaml" >/dev/null 2>&1 &
    echo "$!" > "$pf"
    echo "[$svc] 已启动 (PID: $!)"
}

_stop_one() {
    svc="$1"
    pf="$(_pid_file "$svc")"

    if ! _is_running "$svc"; then
        echo "[$svc] 未在运行"
        rm -f "$pf"
        return 0
    fi

    pid=$(cat "$pf")
    kill "$pid"
    echo "[$svc] 已停止 (PID: $pid)"
    rm -f "$pf"
}

_status_one() {
    svc="$1"
    pf="$(_pid_file "$svc")"

    if _is_running "$svc"; then
        echo "[$svc] ● 运行中 (PID: $(cat "$pf"))"
    else
        echo "[$svc] ○ 未运行"
        rm -f "$pf"
    fi
}

_list() {
    services="$(_discover_services)"
    if [ -z "$services" ]; then
        echo "未发现任何服务"
        return
    fi
    echo "已发现 $(echo "$services" | wc -w) 个服务:"
    echo ""
    for svc in $services; do
        svc_dir="$SCRIPT_DIR/$svc"
        # 读取 config.yaml 中的 name 信息（简单提取）
        desc=""
        if [ -f "$svc_dir/config.yaml" ]; then
            desc=$(grep '^\s*-\?\s*name:' "$svc_dir/config.yaml" 2>/dev/null | sed 's/.*name:\s*["]*\([^"]*\).*/\1/' | tr '\n' ',' | sed 's/,$//')
        fi
        if _is_running "$svc"; then
            status="● 运行中 (PID: $(cat "$(_pid_file "$svc")"))"
        else
            status="○ 未运行"
        fi
        if [ -n "$desc" ]; then
            echo "  $svc  [$desc]  $status"
        else
            echo "  $svc  $status"
        fi
    done
}

# 主逻辑
ACTION="${1:-}"
TARGET="${2:-}"

case "$ACTION" in
    start)
        for svc in $(_get_services "$TARGET"); do _start_one "$svc"; done
        ;;
    stop)
        for svc in $(_get_services "$TARGET"); do _stop_one "$svc"; done
        ;;
    restart)
        for svc in $(_get_services "$TARGET"); do _stop_one "$svc"; sleep 1; _start_one "$svc"; done
        ;;
    status)
        for svc in $(_get_services "$TARGET"); do _status_one "$svc"; done
        ;;
    list)
        _list
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|list} [服务名]"
        echo ""
        _list
        exit 1
        ;;
esac
