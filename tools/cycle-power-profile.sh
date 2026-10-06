#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 手动循环切换性能模式（可选的小工具，不依赖桌面环境）
#
# 说明：安装 install.sh 之后，按键由 mechrevo-keyd 守护进程在内核输入层
#       处理，**不需要**本脚本。本脚本保留给两种场景：
#         1. 手动/命令行切换档位；
#         2. 你想在某个桌面环境里额外绑一个快捷键时作为动作命令。
#
# 因为它只是一个普通命令，所以在任何 DE、任何启动器、任何 TTY 下都能用。
#
# 用法：  ./cycle-power-profile.sh         切到下一档
#         ./cycle-power-profile.sh balanced 直接切到指定档
# ---------------------------------------------------------------------------
set -euo pipefail

# 图标名必须是主题里真实存在的。breeze/oxygen 里是 battery-profile-*，
# 而 power-profile-* 只存在于 Adwaita，用错会显示成破图标。
icon_for() {
    case "$1" in
        power-saver) echo "battery-profile-powersave" ;;
        balanced)    echo "battery-profile-balanced" ;;
        performance) echo "battery-profile-performance" ;;
        *)           echo "battery" ;;
    esac
}

label_for() {
    case "$1" in
        power-saver) echo "办公" ;;
        balanced)    echo "均衡" ;;
        performance) echo "狂暴" ;;
        *)           echo "$1" ;;
    esac
}

# 优先用已安装的守护进程（同一套逻辑，避免两处实现漂移）
if command -v mechrevo-keyd >/dev/null 2>&1; then
    if [[ $# -ge 1 ]]; then
        powerprofilesctl set "$1"
        notify-send -i "$(icon_for "$1")" -t 1800 \
            "性能模式" "$(label_for "$1") ($1)" 2>/dev/null || true
        echo "已切换为：$(label_for "$1") ($1)"
    else
        mechrevo-keyd --once
    fi
    exit 0
fi

# 退化路径：守护进程未安装时自己算下一档
if ! command -v powerprofilesctl >/dev/null 2>&1; then
    echo "错误：找不到 powerprofilesctl（请先安装并启动 power-profiles-daemon）" >&2
    exit 1
fi

if [[ $# -ge 1 ]]; then
    NEXT="$1"
else
    case "$(powerprofilesctl get 2>/dev/null)" in
        power-saver) NEXT="balanced" ;;
        balanced)    NEXT="performance" ;;
        performance) NEXT="power-saver" ;;
        *)           NEXT="balanced" ;;
    esac
fi

if powerprofilesctl set "$NEXT"; then
    notify-send -i "$(icon_for "$NEXT")" -t 1800 \
        "性能模式" "$(label_for "$NEXT") ($NEXT)" 2>/dev/null || true
    echo "已切换为：$(label_for "$NEXT") ($NEXT)"
else
    notify-send -i "dialog-error" -t 3000 "性能模式" "切换失败" 2>/dev/null || true
    echo "切换失败" >&2
    exit 1
fi
