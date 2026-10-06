#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# MECHREVO 翼龙15Pro (GM5HG0A) 性能模式按键修复 —— 一键安装
#
# 本脚本做三件事：
#   1. 加载内核自带 uniwill_laptop 驱动，让 EC 上报的性能模式键
#      变成 KEY_F14 输入事件（否则按键完全无反应）。
#   2. 安装 mechrevo-keyd 守护进程，在**内核输入层**捕获 KEY_F14
#      并循环切换电源模式。
#   3. （可选）加载 acpi_call，让守护进程能顺带写入 EC 寄存器 0x0751，
#      使按键旁边那盏三色指示灯跟随模式变化（绿=办公/蓝=均衡/紫=狂暴）。
#      主线 uniwill_laptop 驱动从不写这个寄存器，所以灯原本不会变。
#
# 为什么不用桌面环境的快捷键：
#   守护进程不依赖 KDE/GNOME/Sway/任何 DE，在任意桌面、任意登录会话、
#   TTY 下行为都一致。已安装的 DE 无需任何额外配置。
#
# 用法：  sudo ./install.sh              安装并立即生效
#         sudo ./install.sh --with-led   同时用 pacman 安装 acpi_call 以启用指示灯
#         sudo ./install.sh --no-led     明确不要指示灯功能，只切换电源模式
#         sudo ./install.sh --undo       完全卸载还原
#         sudo ./install.sh --status     查看当前状态
#
# 安全：  - 新增 /etc/modprobe.d、/etc/modules-load.d、/etc/udev/rules.d 下各一个文件
#         - 新增 /usr/local/bin/mechrevo-keyd 与 systemd 服务
#         - 不修改 DSDT/BIOS。电源模式只走内核 platform_profile。
#         - 指示灯功能会对 EC 寄存器 0x0751 做**读-改-写**，且只改 0xA0|0x10
#           两位（与 TUXEDO 厂商驱动完全相同的算法），其余位——包括 0x40
#           “全速风扇”位与低 3 位风扇档位——逐位保留，绝不整字节覆盖。
#           未安装 acpi_call 时该功能自动关闭，脚本行为与旧版完全一致。
#         - --undo 会删除以上全部文件并卸载模块（但不会卸载 acpi_call）
# ---------------------------------------------------------------------------
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODPROBE_CONF="/etc/modprobe.d/90-mechrevo-uniwill.conf"
MODULES_LOAD_CONF="/etc/modules-load.d/uniwill-laptop.conf"
UDEV_RULE="/etc/udev/rules.d/72-mechrevo-hotkeys.rules"
DAEMON_BIN="/usr/local/bin/mechrevo-keyd"
SERVICE="/etc/systemd/system/mechrevo-keyd.service"
DOC_DIR="/usr/local/share/doc/mechrevo-power-mode"
MODULE="uniwill_laptop"
ACPI_CALL_MODULES_LOAD="/etc/modules-load.d/acpi_call.conf"

# 指示灯策略：ask（默认，能装就问）/ yes / no
LED_MODE="ask"

if [[ "${EUID}" -ne 0 ]]; then
    # --help 不需要 root；其它操作需要
    for a in "$@"; do
        case "$a" in
            -h|--help) sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        esac
    done
    echo "错误：需要 root 权限，请用 sudo 运行。" >&2
    exit 1
fi

# ---------------------------- 参数解析 -------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --undo|--status) break ;;
        --with-led) LED_MODE="yes"; shift ;;
        --no-led)   LED_MODE="no";  shift ;;
        -h|--help)
            sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数：$1（试 --help）" >&2; exit 2 ;;
    esac
done

# ------------------------------ 卸载 ---------------------------------------
if [[ "${1:-}" == "--undo" ]]; then
    echo "==> 停止并禁用守护进程"
    systemctl disable --now mechrevo-keyd.service 2>/dev/null || true
    rm -f "$SERVICE"
    systemctl daemon-reload 2>/dev/null || true

    echo "==> 删除已安装文件"
    rm -f "$DAEMON_BIN" "$UDEV_RULE" "$MODPROBE_CONF" "$MODULES_LOAD_CONF"
    rm -f "$ACPI_CALL_MODULES_LOAD"
    # 清理早期版本可能留下的 99- 规则名
    rm -f /etc/udev/rules.d/99-mechrevo-hotkeys.rules
    rm -rf "$DOC_DIR"

    echo "==> 卸载 $MODULE"
    modprobe -r "$MODULE" 2>/dev/null || true
    # 只卸载模块，不卸载 acpi_call 软件包（可能有别的用途）
    modprobe -r acpi_call 2>/dev/null || true

    echo "==> 刷新 udev"
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=input 2>/dev/null || true

    echo "完成。重启后恢复原状。"
    exit 0
fi

# ------------------------------ 状态 ---------------------------------------
if [[ "${1:-}" == "--status" ]]; then
    echo "== 驱动 =="
    if [[ -e "/sys/bus/platform/drivers/uniwill/INOU0000:00" ]]; then
        echo "  [OK] uniwill 驱动已绑定 INOU0000:00"
    else
        echo "  [!!] 驱动未绑定"
    fi
    lsmod | grep -q "^${MODULE}" && echo "  [OK] 模块已加载" || echo "  [!!] 模块未加载"

    echo "== 热键设备 =="
    grep -A3 'Name="Uniwill WMI hotkeys"' /proc/bus/input/devices 2>/dev/null | grep -E 'Handlers|Name' || echo "  [!!] 未找到"

    echo "== 守护进程 =="
    systemctl is-active mechrevo-keyd.service 2>/dev/null | sed 's/^/  active: /'
    systemctl is-enabled mechrevo-keyd.service 2>/dev/null | sed 's/^/  enabled: /'

    echo "== 电源模式 =="
    if command -v mechrevo-keyd >/dev/null 2>&1; then
        mechrevo-keyd --status
    else
        echo "  当前: $(powerprofilesctl get 2>/dev/null || cat /sys/firmware/acpi/platform_profile 2>/dev/null)"
    fi

    echo "== 模式指示灯 =="
    if [[ -e /proc/acpi/call ]]; then
        echo "  [OK] acpi_call 已加载，/proc/acpi/call 就绪"
        if [[ -f "$ACPI_CALL_MODULES_LOAD" ]]; then
            echo "  [OK] 开机自动加载已配置：$ACPI_CALL_MODULES_LOAD"
        else
            echo "  [!!] 未配置开机自动加载，重启后指示灯会失效"
            echo "       修复： echo acpi_call | sudo tee $ACPI_CALL_MODULES_LOAD"
        fi
        # 只读地显示指示灯寄存器
        if command -v mechrevo-keyd >/dev/null 2>&1; then
            cur="$(mechrevo-keyd --probe 2>/dev/null | sed -n 's/^  0x0751 *\(0x[0-9A-F]*\).*/\1/p')"
            [[ -n "$cur" ]] && echo "  EC 0x0751 = $cur"
        fi
    else
        echo "  [--] 未启用（无 /proc/acpi/call）；只切换电源模式，不改灯"
        echo "       启用： sudo ./install.sh --with-led"
    fi
    exit 0
fi

# --------------------------- 前置检查 --------------------------------------
BOARD="$(cat /sys/class/dmi/id/board_name 2>/dev/null || echo unknown)"
VENDOR="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo unknown)"
echo "==> 检测到机型：$VENDOR / $BOARD"

if ! modinfo "$MODULE" >/dev/null 2>&1; then
    echo "错误：找不到内核模块 $MODULE。请确认已安装 linux 包（当前 $(uname -r)）。" >&2
    exit 1
fi

if [[ -e "/sys/bus/platform/drivers/uniwill/INOU0000:00" ]]; then
    echo "==> 驱动已处于绑定状态。"
fi

# 依赖检查：守护进程需要 python3；通知需要 powerprofilesctl
command -v python3 >/dev/null 2>&1 || { echo "错误：找不到 python3。" >&2; exit 1; }
if ! command -v powerprofilesctl >/dev/null 2>&1; then
    echo "警告：找不到 powerprofilesctl（power-profiles-daemon 未安装）。" >&2
    echo "      守护进程会退化为直接写 /sys/firmware/acpi/platform_profile。" >&2
fi
if ! command -v pacman >/dev/null 2>&1; then
    echo "提示：未找到 pacman，指示灯功能需要手动安装 acpi_call。" >&2
fi

# --------------------------- 1) 驱动部分 -----------------------------------
echo
echo "==> [1/4] 配置内核驱动"
install -Dm644 "$SRC_DIR/system/90-mechrevo-uniwill.conf" "$MODPROBE_CONF"
install -Dm644 "$SRC_DIR/system/uniwill-laptop.conf" "$MODULES_LOAD_CONF"
echo "    已写入 $MODPROBE_CONF 与 $MODULES_LOAD_CONF"

echo "==> 加载 $MODULE (force=1)"
if modprobe "$MODULE" force=1 2>/dev/null; then
    echo "    已加载。"
else
    echo "    警告：modprobe 返回非零（可能已加载），重启后仍会生效。"
fi

# --------------------------- 2) udev 部分 ----------------------------------
echo
echo "==> [2/4] 安装 udev 规则（便于普通用户 evtest 验证）"
install -Dm644 "$SRC_DIR/system/72-mechrevo-hotkeys.rules" "$UDEV_RULE"
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger --subsystem-match=input 2>/dev/null || true
systemd-hwdb update 2>/dev/null || true

# --------------------- 3) 模式指示灯（可选）--------------------------------
echo
echo "==> [3/4] 模式指示灯（EC 0x0751）"

install_acpi_call() {
    echo "    安装 acpi_call（来自官方仓库，与当前内核 $(uname -r) 匹配）……"
    if pacman -S --needed --noconfirm acpi_call; then
        echo "    acpi_call 已安装。"
    else
        echo "    警告：pacman 安装 acpi_call 失败，将跳过指示灯功能。" >&2
        return 1
    fi
}

# 判断是否已具备 acpi_call
HAS_ACPI_CALL="no"
if modinfo acpi_call >/dev/null 2>&1; then
    HAS_ACPI_CALL="yes"
fi

if [[ "$LED_MODE" == "no" ]]; then
    echo "    按要求跳过（--no-led）：只切换电源模式，灯保持原样。"
else
    if [[ "$HAS_ACPI_CALL" == "no" ]]; then
        if [[ "$LED_MODE" == "yes" ]]; then
            install_acpi_call || LED_MODE="no"
        elif [[ -t 0 ]]; then
            echo "    按键旁边那盏灯由 EC 寄存器 0x0751 控制，主线驱动从不写它。"
            echo "    要用 acpi_call 写入该寄存器（只读-改-写 2 个位）才能让灯跟随模式。"
            read -r -p "    现在安装 acpi_call 以启用指示灯？[Y/n] " ans
            case "${ans:-Y}" in
                [Yy]*) install_acpi_call || LED_MODE="no" ;;
                *) echo "    跳过。之后可运行 sudo ./install.sh --with-led 启用。"
                   LED_MODE="no" ;;
            esac
        else
            echo "    未安装 acpi_call 且非交互，跳过指示灯。"
            echo "    之后可运行 sudo ./install.sh --with-led 启用。"
            LED_MODE="no"
        fi
    fi
fi

if [[ "$LED_MODE" != "no" ]] && modinfo acpi_call >/dev/null 2>&1; then
    # 开机自动加载，保证守护进程启动时接口已就绪
    install -d /etc/modules-load.d
    printf 'acpi_call\n' > "$ACPI_CALL_MODULES_LOAD"
    echo "    已写入 $ACPI_CALL_MODULES_LOAD（开机自动加载）"
    if modprobe acpi_call 2>/dev/null; then
        if [[ -e /proc/acpi/call ]]; then
            echo "    [OK] acpi_call 已加载，/proc/acpi/call 就绪，指示灯将在切档时同步。"
        else
            echo "    提示：模块已加载但 /proc/acpi/call 未出现，指示灯将自动跳过。" >&2
        fi
    else
        echo "    提示：本内核无法加载 acpi_call，指示灯将自动跳过（不影响切档）。" >&2
        LED_MODE="no"
    fi
fi

# --------------------------- 4) 守护进程 -----------------------------------
echo
echo "==> [4/4] 安装按键守护进程（与桌面环境无关）"
install -Dm755 "$SRC_DIR/mechrevo-keyd.py" "$DAEMON_BIN"
install -Dm644 "$SRC_DIR/system/mechrevo-keyd.service" "$SERVICE"
install -Dm644 "$SRC_DIR/README.md" "$DOC_DIR/README.md" 2>/dev/null || true
echo "    已安装 $DAEMON_BIN"
echo "    已安装 $SERVICE"

# 曾经踩过的坑：/usr/local/bin/mechrevo-keyd 是旧版，导致排查时看到的
# 行为与实际源码不一致。这里强制核对，安装后两者必须逐字节相同。
if ! cmp -s "$SRC_DIR/mechrevo-keyd.py" "$DAEMON_BIN"; then
    echo "[!!]  安装后脚本与源码不一致，请检查 $DAEMON_BIN" >&2
    exit 1
fi
echo "    [OK] 已安装脚本与源码逐字节一致"

systemctl daemon-reload
systemctl enable mechrevo-keyd.service
# 必须显式 restart，不能用 `enable --now`：
# `--now` 在服务**已经 active** 时是空操作，不会重新加载。
# 曾经因此让守护进程继续跑 2.5 小时前的旧代码（那份还没有指示灯功能），
# 表现为"文件明明是新的、按键却不同步灯"，排查时极易误判。
systemctl restart mechrevo-keyd.service

sleep 2

# ---------------------------- 结果验证 -------------------------------------
echo
echo "================== 验证 =================="

BOUND_DRV=""
for d in /sys/bus/platform/drivers/*/; do
    if [[ -e "${d}INOU0000:00" ]]; then
        BOUND_DRV="$(basename "$d")"
        break
    fi
done
if [[ -n "$BOUND_DRV" ]]; then
    echo "[OK]  INOU0000:00 已绑定到平台驱动：$BOUND_DRV"
else
    echo "[!!]  INOU0000:00 仍未被驱动绑定"
fi

if grep -qiE 'Name="Uniwill WMI hotkeys"' /proc/bus/input/devices 2>/dev/null; then
    echo "[OK]  热键输入设备已创建：Uniwill WMI hotkeys"
else
    echo "[!!]  未发现 Uniwill WMI hotkeys 输入设备"
fi

if systemctl is-active --quiet mechrevo-keyd.service; then
    echo "[OK]  mechrevo-keyd 守护进程正在运行"
    # 关键：确认**运行中的进程**确实是刚装的那份代码，而不是重启前留下的旧进程。
    # 文件一致不代表进程一致 —— systemd 只在启动时读一次脚本，
    # 之后替换文件对已在运行的进程毫无影响（曾因此误判一整轮）。
    #
    # 判据：进程已运行秒数 > 脚本被修改后经过的秒数
    #       => 进程启动得比脚本更新还早 => 跑的是旧代码。
    # 进程年龄用 systemd 的单调时钟（ExecMainStartTimestampMonotonic，微秒）
    # 减 /proc/uptime 算，不依赖 ps 或 /proc/<pid>（沙箱/隐藏 pid 下可能是空的）。
    SVC_PID="$(systemctl show -p MainPID --value mechrevo-keyd.service 2>/dev/null)"
    SVC_START_US="$(systemctl show -p ExecMainStartTimestampMonotonic --value \
                    mechrevo-keyd.service 2>/dev/null)"
    FILE_MTIME="$(stat -c %Y "$DAEMON_BIN" 2>/dev/null)"
    if [[ "$SVC_START_US" =~ ^[0-9]+$ && "$SVC_START_US" -gt 0 \
          && "$FILE_MTIME" =~ ^[0-9]+$ ]]; then
        UPTIME_US="$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)"
        PROC_AGE=$(( (UPTIME_US - SVC_START_US) / 1000000 ))
        FILE_AGE=$(( $(date +%s) - FILE_MTIME ))
        if (( PROC_AGE > FILE_AGE + 2 )); then
            echo "[!!]  守护进程(PID $SVC_PID)已运行 ${PROC_AGE}s，而脚本是 ${FILE_AGE}s 前更新的"
            echo "        => 进程很可能仍在跑旧代码，请执行："
            echo "             sudo systemctl restart mechrevo-keyd"
        else
            echo "[OK]  守护进程(PID $SVC_PID)已加载最新脚本"
        fi
    else
        echo "[--]  无法判定进程是否已重载（拿不到单调启动时刻），"
        echo "        保险起见请执行： sudo systemctl restart mechrevo-keyd"
    fi
else
    echo "[!!]  守护进程未运行，查看日志："
    echo "        journalctl -u mechrevo-keyd -n 30 --no-pager"
fi

if [[ -L /dev/input/by-path/platform-INOU0000:00-event || -e /dev/input/by-path/platform-INOU0000:00-event ]]; then
    echo "[OK]  稳定设备链接存在：/dev/input/by-path/platform-INOU0000:00-event"
fi

if [[ -e /proc/acpi/call ]]; then
    echo "[OK]  指示灯通路就绪（acpi_call -> /proc/acpi/call）"
else
    echo "[--]  指示灯未启用（可选功能，不影响切档）"
fi

# 用合成数据跑一遍自测，确认装上去的守护进程逻辑完好
if [[ -f "$SRC_DIR/tests/selftest.py" ]]; then
    if python3 "$SRC_DIR/tests/selftest.py" >/dev/null 2>&1; then
        echo "[OK]  自测通过（合成输入事件）"
    else
        echo "[!!]  自测未通过，请运行： python3 $SRC_DIR/tests/selftest.py"
    fi
fi
if [[ -f "$SRC_DIR/tests/ec_sim_test.py" ]]; then
    if python3 "$SRC_DIR/tests/ec_sim_test.py" >/dev/null 2>&1; then
        echo "[OK]  指示灯写 EC 模拟测试通过"
    else
        echo "[!!]  指示灯模拟测试未通过，请运行： python3 $SRC_DIR/tests/ec_sim_test.py"
    fi
fi

echo
if command -v mechrevo-keyd >/dev/null 2>&1; then
    mechrevo-keyd --status 2>/dev/null | sed 's/^/  /' || true
fi

echo
echo "-------------------------------------------------------------"
echo "现在按一下性能模式键（电源键旁边那个），应当立即切换档位"
echo "并弹出桌面通知。三档循环：办公 -> 均衡 -> 狂暴 -> 办公"
echo
if [[ -e /proc/acpi/call ]]; then
    echo "按键旁边那盏灯应随之变色（绿=办公 / 蓝=均衡 / 紫=狂暴）。"
    echo "若颜色不对，先只读地看一眼寄存器："
    echo "    sudo mechrevo-keyd --probe       # 全程只读，不改动任何东西"
    echo "再逐个试三种颜色，记下哪个值出哪种颜色："
    echo "    sudo mechrevo-keyd --set-led power-saver    # 期望绿灯"
    echo "    sudo mechrevo-keyd --set-led balanced       # 期望蓝灯"
    echo "    sudo mechrevo-keyd --set-led performance    # 期望紫灯"
    echo "哪个档位颜色不对，把上面的输出发我，改 EC 0x0751 的取值即可。"
else
    echo "指示灯未启用。如需让按键旁的灯跟随模式："
    echo "    sudo ./install.sh --with-led"
fi
echo
echo "不想按键也能测试："
echo "    mechrevo-keyd --once      # 手动切一档"
echo "    mechrevo-keyd --status    # 查看状态"
echo
echo "看日志："
echo "    journalctl -u mechrevo-keyd -f"
echo
echo "普通用户自行抓包验证（无需 sudo，靠刚装的 udev 规则）："
echo "    evtest /dev/input/by-path/platform-INOU0000:00-event"
echo "    按性能模式键应看到：Event code 184 (KEY_F14)"
echo
echo "完全卸载：  sudo ./install.sh --undo"
echo "-------------------------------------------------------------"
echo "完成。重启后依然生效（无需任何桌面环境配置）。"
