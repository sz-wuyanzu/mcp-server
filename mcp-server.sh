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
    nohup "$PYTHON" "$svc_dir/main.py" "$svc_dir/config.yaml" >> "$svc_dir/logs/service.log" 2>&1 &
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
        echo "[$svc] 运行中 (PID: $(cat "$pf"))"
    else
        echo "[$svc] 未运行"
        rm -f "$pf"
    fi
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
    *)
        echo "用法: $0 {start|stop|restart|status} [服务名]"
        echo ""
        echo "可用服务:"
        for svc in $(_discover_services); do echo "  - $svc"; done
        exit 1
        ;;
esac
