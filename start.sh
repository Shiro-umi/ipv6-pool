#!/bin/bash
# IPv6代理池快速启动脚本 (uv 版本)
# 注意: 需要 root 权限运行动态配置 IPv6 地址

set -e

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 默认配置
PORT=8899
POOL_SIZE=1000
BIND_ALL=true
ALLOW_LAN=true
INTERFACE="lo"
EXTRA_ARGS=""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --port|-p)
            PORT="$2"
            shift 2
            ;;
        --pool-size)
            POOL_SIZE="$2"
            shift 2
            ;;
        --interface|-i)
            INTERFACE="$2"
            shift 2
            ;;
        --local-only)
            BIND_ALL=false
            ALLOW_LAN=false
            EXTRA_ARGS="$EXTRA_ARGS --deny-lan"
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            # 其他参数传递给 Python 脚本
            EXTRA_ARGS="$EXTRA_ARGS $1"
            shift
            ;;
    esac
done

show_help() {
    cat << 'EOF'
用法: ./start.sh [选项]

选项:
  --port, -p PORT      代理端口 (默认: 8899)
  --pool-size SIZE     IPv6地址池大小 (默认: 1000)
  --interface, -i IF   网卡接口 (默认: lo)
  --local-only         仅允许本地访问(127.0.0.1)，禁止局域网
  --help, -h           显示帮助

其他参数直接传递给 ipv6_proxy_pool.py

示例:
  sudo ./start.sh                      # 启动（默认允许局域网）
  sudo ./start.sh --local-only         # 仅本地访问
  sudo ./start.sh --port 8080 --pool-size 2000
  sudo ./start.sh --allowed-ips "192.168.1.0/24"

注意: 本脚本需要 root 权限运行动态配置 IPv6 地址
EOF
}

# 查找 uv 可执行文件
find_uv() {
    # 首先尝试 PATH 中的 uv
    if command -v uv &> /dev/null; then
        echo "uv"
        return 0
    fi

    # 尝试常见安装路径
    local uv_paths=(
        "$HOME/.local/bin/uv"
        "/root/.local/bin/uv"
        "/usr/local/bin/uv"
        "/usr/bin/uv"
    )

    # 如果 SUDO_USER 存在，尝试该用户的家目录
    if [[ -n "$SUDO_USER" ]]; then
        uv_paths=("/home/$SUDO_USER/.local/bin/uv" "${uv_paths[@]}")
    fi

    for path in "${uv_paths[@]}"; do
        if [[ -x "$path" ]]; then
            echo "$path"
            return 0
        fi
    done

    return 1
}

# 检查 uv 是否安装
check_uv() {
    UV_PATH=$(find_uv)
    if [[ -n "$UV_PATH" ]]; then
        # 创建一个全局可用的 uv 函数
        uv() {
            "$UV_PATH" "$@"
        }
        export -f uv 2>/dev/null || true
        return 0
    else
        return 1
    fi
}

# 安装 uv
install_uv() {
    echo -e "${RED}未检测到 uv${NC}"
    echo ""
    echo "可能的原因:"
    echo "  1. uv 未安装"
    echo "  2. 使用 sudo 时环境变量被重置"
    echo ""
    echo "解决方法:"
    echo ""
    echo "方法1 - 使用 sudo -E 保留环境变量:"
    echo "  sudo -E $0 $*"
    echo ""
    echo "方法2 - 先切换到 root 用户:"
    echo "  sudo -i"
    echo "  cd $(pwd)"
    echo "  $0 $*"
    echo ""
    echo "方法3 - 手动指定 uv 路径:"
    echo "  sudo env PATH=\$PATH:$HOME/.local/bin $0 $*"
    echo ""
    echo "如果 uv 未安装，请参考: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
}

# 获取本机IP
get_local_ip() {
    hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1"
}

# 检查 root 权限
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}错误: 需要 root 权限运行动态配置 IPv6 地址${NC}"
        echo ""
        echo "请使用 sudo 运行:"
        echo "  sudo $0 $*"
        echo ""
        echo "或者先切换到 root 用户:"
        echo "  sudo -i"
        echo "  $0 $*"
        exit 1
    fi
}

# 主函数
main() {
    echo -e "${BLUE}==============================================${NC}"
    echo -e "${BLUE}  IPv6代理池启动脚本 (即用即弃模式)${NC}"
    echo -e "${BLUE}==============================================${NC}"
    echo ""

    # 检查 root 权限
    check_root

    # 检查 uv
    if ! check_uv; then
        install_uv
    fi

    echo -e "${GREEN}✓ uv 已安装: $UV_PATH${NC}"
    echo ""

    # 检查/同步依赖
    if [[ ! -d ".venv" ]]; then
        echo -e "${YELLOW}创建虚拟环境...${NC}"
        "$UV_PATH" sync
    fi

    # 显示模式信息
    echo -e "${GREEN}✓ 即用即弃模式${NC}"
    echo "  - 每个IP只用一次，永不重复"
    echo "  - 动态配置到网卡: $INTERFACE"
    echo "  - 保持 $POOL_SIZE 个可用IP"
    echo "  - 默认允许局域网访问"
    echo ""

    # 构建命令
    CMD_ARGS="--port $PORT --pool-size $POOL_SIZE --interface $INTERFACE"

    if [[ "$BIND_ALL" == true ]]; then
        CMD_ARGS="$CMD_ARGS --host 0.0.0.0"
    else
        CMD_ARGS="$CMD_ARGS --host 127.0.0.1"
    fi

    if [[ "$ALLOW_LAN" == true ]]; then
        CMD_ARGS="$CMD_ARGS --allow-lan"
    else
        CMD_ARGS="$CMD_ARGS --deny-lan"
    fi

    # 添加额外参数
    if [[ -n "$EXTRA_ARGS" ]]; then
        CMD_ARGS="$CMD_ARGS $EXTRA_ARGS"
    fi

    # 显示启动信息
    echo -e "${BLUE}启动配置:${NC}"
    echo "  端口: $PORT"
    echo "  地址池: $POOL_SIZE"
    echo "  网卡: $INTERFACE"
    echo "  命令: $UV_PATH run python -m ipv6_proxy_pool.cli.main $CMD_ARGS"
    echo ""

    if [[ "$ALLOW_LAN" == true ]]; then
        LOCAL_IP=$(get_local_ip)
        echo -e "${GREEN}代理地址:${NC}"
        echo "  本地:   http://127.0.0.1:$PORT"
        echo "  局域网: http://$LOCAL_IP:$PORT"
    else
        echo -e "${GREEN}代理地址: http://127.0.0.1:$PORT${NC}"
    fi

    echo -e "${GREEN}管理接口: http://127.0.0.1:$((PORT + 1))/stats${NC}"
    echo ""
    echo -e "${YELLOW}按 Ctrl+C 停止${NC}"
    echo -e "${BLUE}==============================================${NC}"
    echo ""

    # 使用 uv run 启动
    exec "$UV_PATH" run python -m ipv6_proxy_pool.cli.main $CMD_ARGS
}

# 运行
main "$@"
