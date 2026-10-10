#!/usr/bin/env bash
# ===========================================================================
# diag-acpi.sh — 一次性定位「acpi_call 读不到 EC」的真正原因
#
# 背景：内核 uniwill 驱动读 EC 用的是「设备 handle + 相对名 ECRR」：
#         acpi_evaluate_integer(data->handle, "ECRR", ...)
#       ACPI 解析相对名会沿作用域链**向上**找，所以 ECRR 必然位于
#         \_SB_.INOU.ECRR   或   \_SB_.ECRR   或   \ECRR
#       三者之一（不可能更远）。而 acpi_call 只接受全限定路径。
#
#       驱动自己是好的（hwmon 有温度/风扇转速），说明 ECRR 确实存在。
#       所以本脚本不只知道列路径，还做一组**对照实验**来区分两类病因：
#         (a) 路径写错          -> 内核日志会出现 "Cannot get handle"
#         (b) acpi_call 没生效  -> 连故意写错路径都不报错，日志一片安静
#
# 用法：  sudo ./diag-acpi.sh
# ===========================================================================
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root（要读 /sys/firmware/acpi/tables/DSDT 与内核日志）：" >&2
    echo "  sudo $0" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DSDT_BIN="$WORK/DSDT.bin"
DSDT_DAT="$WORK/dsdt.dat"

# 取内核日志里与 acpi_call 有关的记录（用于前后对比）。
log_snapshot() {
    if command -v journalctl >/dev/null 2>&1; then
        journalctl -k --no-pager 2>/dev/null | grep -i "acpi_call"
    else
        dmesg 2>/dev/null | grep -i "acpi_call"
    fi
}

# 读一次 /proc/acpi/call 的结果。
# acpi_call 会把结尾的 NUL 也返回给用户态（acpi_proc_read 传给
# simple_read_from_buffer 的长度是字符串长度+1），bash 的 $(...) 会自动
# 丢弃它但会打印一条"忽略输入中的 null 字节"警告。这里先用 tr 去掉，
# 既得到干净结果，也不再刷警告。
acpi_reply() {
    tr -d '\0' < /proc/acpi/call 2>/dev/null || echo '<读不到>'
}

echo "=============================================================="
echo " 1) 基本环境"
echo "=============================================================="
echo "内核          : $(uname -r)"
echo "ACPI 设备     : $(cat /sys/bus/acpi/devices/INOU0000:00/path 2>/dev/null || echo 未找到)"
echo "acpi_call 模块: $(grep -qw acpi_call /proc/modules 2>/dev/null && echo 已加载 || echo '未加载')"
if [ -e /proc/acpi/call ]; then
    echo "/proc/acpi/call: 存在 $(stat -c '(权限 %a, 属主 %U:%G)' /proc/acpi/call 2>/dev/null)"
    echo "当前 uid      : $(id -u)（必须是 0，否则下面必然全失败）"
else
    echo "/proc/acpi/call: 不存在 -> 请先 sudo modprobe acpi_call"
fi
echo

echo "=============================================================="
echo " 2) 对照实验（最关键，用来判断属于哪一类病因）"
echo "=============================================================="
if [ ! -e /proc/acpi/call ]; then
    echo "  跳过：acpi_call 未加载。"
else
    BEFORE="$(log_snapshot)"

    echo "  --- 2a) 初始状态（未调用时应显示 not called）---"
    printf '  /proc/acpi/call = %s\n' "$(acpi_reply)"

    # 正面对照：调用 \_SB_.INOU._STA。
    # 这条路径由内核自己证明存在 —— 驱动探测设备时调过它并拿到了状态 0x0B
    # （见 /sys/bus/acpi/devices/INOU0000:00/status，值为 11）。
    # 所以它**一定**能返回一个整数；连它都失败就说明通路本身有问题。
    echo
    echo "  --- 2b) 正面对照：调用必然存在的 \\_SB_.INOU._STA ---"
    printf '\\_SB_.INOU._STA' > /proc/acpi/call 2>/dev/null
    STA_RAW="$(acpi_reply)"
    echo "  返回 = $STA_RAW"
    case "$STA_RAW" in
        0x*) echo "  [OK] acpi_call 通路正常（_STA 返回 $STA_RAW）" ;;
        *)   echo "  [!!] 连必然存在的 _STA 都调不通 -> 问题在 acpi_call 通路本身"
             echo "       （权限 / 模块 / 内核不匹配），与 EC 路径无关。" ;;
    esac

    # 负面对照：故意用一个绝对不存在的路径。
    # acpi_call 找不到句柄时**必定**打印 "Cannot get handle"。
    #   -> 日志出现它  = acpi_call 工作正常，问题在路径
    #   -> 日志仍安静  = 模块的报错没进内核日志
    echo
    echo "  --- 2c) 负面对照：故意写一个不存在的路径 ---"
    printf '\\_SB_.NOSUCHTHING_XYZ' > /proc/acpi/call 2>&1
    printf '  返回 = %s\n' "$(acpi_reply)"
    sleep 1
    AFTER_BAD="$(log_snapshot)"
    if [ "$AFTER_BAD" != "$BEFORE" ]; then
        echo "  [OK] 负面对照生效：内核日志出现了新记录"
        echo "$AFTER_BAD" | tail -3 | sed 's/^/       /'
    else
        echo "  [!!] 负面对照未生效：内核日志没有任何新记录。"
        echo "       说明模块的报错没进内核日志（第 3 步再确认一次）。"
    fi

    echo
    echo "  --- 2d) 逐个候选路径真实调用（探针寄存器 0x0740）---"
    # 注意：ACPI 名字固定 4 字符、不足补下划线，所以 \_SB 与 \_SB_、
    # \_SB.INOU 与 \_SB_.INOU 在命名空间里是**同一个对象**。旧表把同义写法
    # 当成不同候选，会对同一不存在的路径重复试调、重复刷 "Cannot get handle"，
    # 而这个脚本本来就是用来排查 dmesg 噪音的。这里只保留规范写法（带下划线），
    # 与守护进程 1.5 的候选去重保持一致。
    for path in \
        '\_SB_.INOU.ECRR' \
        '\_SB_.ECRR' \
        '\ECRR' \
        '\_SB_.INOU.ECRW'
        do
        printf '%s 0x0740' "$path" > /proc/acpi/call 2>/dev/null
        if [ $? -ne 0 ]; then
            printf '  %-22s -> 写 /proc/acpi/call 失败（权限或模块问题）\n' "$path"
            continue
        fi
        raw="$(acpi_reply)"
        case "$raw" in
            0x*)                    verdict="[OK] 成功" ;;
            *"Cannot get handle"*)  verdict="路径不存在" ;;
            *"Method call failed"*) verdict="方法调用失败" ;;
            "not called")           verdict="[!!] 调用未生效（写没被受理）" ;;
            "")                     verdict="[!!] 返回为空" ;;
            *)                      verdict="?" ;;
        esac
        printf '  %-22s -> %-26s %s\n' "$path" "${raw:-<空>}" "$verdict"
    done

    echo
    echo "  --- 2e) 再取一次内核日志（看 2c/2d 期间的新记录）---"
    NOW="$(log_snapshot)"
    if [ "$NOW" == "$BEFORE" ]; then
        echo "  整个 2d 过程内核日志依旧毫无记录。"
    else
        echo "$NOW" | tail -10 | sed 's/^/    /'
    fi
    echo
    echo "  判读办法："
    echo "    · 2b 里 _STA 返回 0x??    -> acpi_call 通路正常，继续往下看"
    echo "    · 2b 里 _STA 也失败       -> 通路问题（权限/模块），与 EC 路径无关"
    echo "    · 2d 里出现 0x??          -> 该路径可用，记下来即可"
    echo "    · 2d 全是 Cannot get handle -> 路径问题，看第 4 步 DSDT 给出的真名"
    echo "    · 2d 全是 not called / 空  -> 写进去的调用没被受理"
fi
echo

echo "=============================================================="
echo " 3) 内核日志原始证据（acpi_call 自己的报错）"
echo "=============================================================="
SNAP="$(log_snapshot)"
if [ -n "$SNAP" ]; then
    echo "$SNAP" | tail -20
else
    echo "  （日志里没有 acpi_call 相关记录）"
    echo
    echo "  提示：如果在第 2 步确实写过 /proc/acpi/call，却在这里看不到任何"
    echo "        记录，请确认读的是同一个内核日志源："
    echo "          sudo dmesg | tail -20"
    echo "          journalctl -k -n 20 --no-pager"
fi
echo

echo "=============================================================="
echo " 4) 从 DSDT 挖出 ECRR 的真实归属（不依赖 acpi_call，最权威）"
echo "=============================================================="
if [ ! -r /sys/firmware/acpi/tables/DSDT ]; then
    echo "  无法读取 DSDT，跳过。"
    exit 0
fi
cat /sys/firmware/acpi/tables/DSDT > "$DSDT_BIN" 2>/dev/null
echo "  DSDT 大小：$(stat -c %s "$DSDT_BIN" 2>/dev/null) 字节"

echo
echo "  --- A) DSDT 里出现 ECRR / ECRW 的原始位置 ---"
if strings -a -t d "$DSDT_BIN" | grep -E 'ECRR|ECRW' | head -20; then
    :
else
    echo "  strings 没直接命中（名字可能被拆开存），改用十六进制搜索："
    xxd -p "$DSDT_BIN" | tr -d '\n' | grep -o -b -E '45435252|45435257' | head -10
fi

# 方式 B：用 acpica 反汇编出嵌套结构 —— 唯一能给出全限定路径的办法。
echo
echo "  --- B) 反汇编后看 ECRR 所在的 Scope/Device（权威）---"
if command -v acpidump >/dev/null 2>&1 && command -v iasl >/dev/null 2>&1; then
    ( cd "$WORK" && acpidump -n DSDT -b >/dev/null 2>&1 )
    [ -f "$WORK/dsdt.dat" ] && mv "$WORK/dsdt.dat" "$DSDT_DAT" 2>/dev/null
    if [ -f "$DSDT_DAT" ]; then
        iasl -d "$DSDT_DAT" >/dev/null 2>&1
        DSL="${DSDT_DAT%.dat}.dsl"
        if [ -f "$DSL" ]; then
            echo "  已反汇编，ECRR / ECRW 定义所在行："
            grep -n "Method (ECRR" "$DSL" | head | sed 's/^/    /'
            grep -n "Method (ECRW" "$DSL" | head | sed 's/^/    /'
            ln="$(grep -n 'Method (ECRR' "$DSL" | head -1 | cut -d: -f1)"
            if [ -n "${ln:-}" ]; then
                start=$(( ln > 80 ? ln - 80 : 1 ))
                echo
                echo "  它所属的 Scope/Device 链（往上找）："
                sed -n "${start},${ln}p" "$DSL" \
                    | grep -nE '^ *(Scope|Device|Method) \(' | tail -20 | sed 's/^/    /'
                echo
                echo "  这段的完整原文（据此写出全限定路径）："
                sed -n "${start},$(( ln + 30 ))p" "$DSL" | sed 's/^/    /'
            fi
            if cp "$DSL" ./dsdt.dsl 2>/dev/null; then
                echo
                echo "  已另存一份到当前目录：dsdt.dsl"
            fi
        else
            echo "  iasl 反汇编失败。"
        fi
    else
        echo "  acpidump 没有产出 dsdt.dat。"
    fi
else
    echo "  未安装 acpica（acpidump/iasl），无法给出权威结论。"
    echo
    echo "  >>> 强烈建议装一下，这是唯一能确定路径的办法："
    echo "        sudo pacman -S acpica"
    echo "      然后重跑本脚本。"
fi

echo
echo "=============================================================="
echo " 完成。请把以上**完整**输出发回。"
echo "=============================================================="
