#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mechrevo-keyd —— MECHREVO 翼龙15Pro (GM5HG0A) 性能模式按键守护进程

【为什么不用桌面环境的快捷键绑定】
  本程序工作在内核输入层，完全独立于桌面环境。
  无论 KDE / GNOME / Sway / Hyprland / Xfce / 纯 console，
  只要内核把性能模式键上报为 KEY_F14，本进程就能捕获并切换电源模式。
  因此不需要、也不应该在任何 DE 里再绑定一次快捷键。
  同一个按键在多个 DE 下、在多用户切换后，行为完全一致。

【原理】
  内核 uniwill_laptop 驱动把厂商热键 WMI 事件 0xB0
  (UNIWILL_OSD_PERFORMANCE_MODE_TOGGLE) 上报为输入设备
  "Uniwill WMI hotkeys" 上的 KEY_F14 (184)。
  本进程直接读 /dev/input/eventN，按 24 字节 struct input_event 解析，
  命中 EV_KEY / KEY_F14 / value==1（按下）后循环切换电源模式。

【为什么不 EVIOCGRAB 独占设备】
  同一设备还承载 KEY_RFKILL(飞行模式)、KEY_MICMUTE(麦克风静音)、
  KEY_KBDILLUMUP/DOWN(键盘背光) 等必须交给桌面环境的按键。
  独占会把它们全部吞掉，所以本进程只做“旁路监听”，不独占。
  不独占的唯一代价是 F14 也会同时到达 DE —— 而 F14 默认没有任何
  绑定，不会产生副作用（本程序也不需要在 DE 里绑定它）。

【切换目标】
  优先调用 powerprofilesctl（即 power-profiles-daemon，PPD），
  这样 PPD 自己的状态、通知、以及其他监听方都能同步。
  PPD 不可用时退化为直接写 /sys/firmware/acpi/platform_profile
  （PPD 正是用 GFileMonitor 监视这个文件，两者等价）。

【模式指示灯（按键旁边那盏三色灯）】
  只切 platform_profile 是不够的：那套（amd-pmf 驱动）只管 CPU 性能策略，
  跟按键旁的指示灯没有任何关系。指示灯由 EC（嵌入式控制器）自己控制，
  具体是 EC 寄存器 0x0751，而主线 uniwill_laptop 驱动从来没有写过它
  （该寄存器的宏 EC_ADDR_MANUAL_FAN_CTRL 在驱动源码里只有 #define 一处，
  零处使用）——它却在 uniwill_ec_init() 里把 0x0741 的 ENABLE_MANUAL_CTRL
  置位，等于告诉 EC“模式由系统接管”，然后一个模式都没下发。EC 于是始终
  停在开机时那一档，灯自然不跟着变。
  本程序在每次切换时顺带写 0x0751（TUXEDO 的 uw_set_performance_profile_v1
  做法：清 0xA0|0x10，再按档位上 0xA0 / 0x00 / 0x10），让灯跟上。
  写 EC 走的是 acpi_call 调 \\_SB_.INOU 上的 ECRW —— 与内核驱动内部调用
  的完全是同一个 ACPI 方法，不绕过内核，也不新增权限模型；acpi_call 未装/
  未加载时该功能自动静默关闭，只切换电源模式，不影响其它任何行为。

依赖：仅 Python 3 标准库（指示灯功能额外需要 acpi_call 内核模块，可选）。
"""

import argparse
import errno
import glob
import os
import pwd
import select
import signal
import struct
import subprocess
import sys
import time

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
DEVICE_NAME = "Uniwill WMI hotkeys"
# 平台设备 ID 固定，by-path 是稳定链接（eventN 编号会随启动顺序变化）
DEVICE_BYPATH = "/dev/input/by-path/platform-INOU0000:00-event"

EV_KEY = 0x01
KEY_F14 = 184
# x86-64 上 struct input_event = 24 字节：
#   struct timeval { long sec; long usec; } + __u16 type + __u16 code + __s32 value
INPUT_EVENT = struct.Struct("@llHHi")

SYSFS_PROFILE = "/sys/firmware/acpi/platform_profile"
SYSFS_CHOICES = "/sys/firmware/acpi/platform_profile_choices"

# 三档循环顺序（与 BIOS/Windows 下 办公→均衡→狂暴 的直觉一致）
CYCLE_ORDER = ["power-saver", "balanced", "performance"]

# PPD 名称 与 内核 platform_profile 名称 的对应关系
PPD_TO_SYSFS = {
    "power-saver": "low-power",
    "balanced": "balanced",
    "performance": "performance",
}
SYSFS_TO_PPD = {v: k for k, v in PPD_TO_SYSFS.items()}

LABELS = {"power-saver": "办公", "balanced": "均衡", "performance": "狂暴"}
# 图标名必须用 breeze 主题里真实存在的（power-profile-* 在 breeze 中不存在）
ICONS = {
    "power-saver": "battery-profile-powersave",
    "balanced": "battery-profile-balanced",
    "performance": "battery-profile-performance",
}

# --------------------------------------------------------------------------
# 模式指示灯（EC 寄存器 0x0751）
# --------------------------------------------------------------------------
# 按键旁边那颗灯的三种颜色由 EC 直接驱动，和 CPU 性能策略是两套东西。
# 写入规则照搬 TUXEDO 驱动 uw_set_performance_profile_v1()：
#   先清掉 0xA0 | 0x10 = 0xB0，再按档位 OR 进对应位。
# 其中 0x40 是 TUXEDO 的“全速风扇”位，0xB0 不含它，因此读改写会自动
# 保留它——绝不能直接整字节覆盖，否则可能误开全速风扇。
EC_LED_REG = 0x0751
EC_LED_CLEAR_BITS = 0xA0 | 0x10          # = 0xB0，与厂商驱动完全一致
EC_LED_VALUES = {
    "power-saver": 0xA0,                 # 办公  -> 绿灯
    "balanced": 0x00,                    # 均衡  -> 蓝灯
    "performance": 0x10,                 # 狂暴  -> 紫（品红）灯
}
EC_LED_BLUE_ONLY = 0x00                  # 0xB0 全清时即“均衡”观感

ACPI_CALL_PATH = "/proc/acpi/call"
ACPI_DEVICE = "\\_SB_.INOU"              # 本机平台设备 INOU0000:00 的 ACPI 路径

# 打印在 --probe/--status 里，方便确认跑的是哪一版（曾经因为装了旧版
# 而误判过问题，加个版本号能省掉一整轮排查）。
VERSION = "1.5-led"      # 1.5：作用域探测不再自造 dmesg 报错（sysfs 权威路径 + 去重 + 命中即停）
                         # 1.4：修掉 acpi_call 返回值尾部 NUL 导致全部读数被判失败

_PROFILE_ALIASES = {
    "power-saver": "power-saver",
    "low-power": "power-saver",
    "powersave": "power-saver",
    "power-save": "power-saver",
    "power_saver": "power-saver",
    "saver": "power-saver",
    "office": "power-saver",
    "办公": "power-saver",
    "balanced": "balanced",
    "balance": "balanced",
    "均衡": "balanced",
    "performance": "performance",
    "perf": "performance",
    "boost": "performance",
    "turbo": "performance",
    "狂暴": "performance",
}


def normalize_profile(name):
    """把各种写法归一成 PPD 风格名称，认不出返回 None。"""
    if not name:
        return None
    return _PROFILE_ALIASES.get(str(name).strip().lower())


_prog = os.path.basename(sys.argv[0])
_last_press = 0.0
_verbose = False


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd, timeout=10):
    """执行命令，返回 (returncode, stdout)。任何异常都退化为失败，不抛出。"""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            text=True,
        )
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        if _verbose:
            log(f"执行 {cmd[0]} 失败: {e}")
        return 127, ""


# --------------------------------------------------------------------------
# 设备发现
# --------------------------------------------------------------------------
def find_device():
    """定位性能模式键所在的事件设备，返回 /dev/input/eventN 或 None。

    优先按设备名精确匹配（最可靠），失败再退回 by-path 稳定链接。
    """
    for sysdev in sorted(glob.glob("/sys/class/input/event*")):
        namefile = os.path.join(sysdev, "device", "name")
        try:
            with open(namefile, encoding="utf-8", errors="replace") as f:
                if f.read().strip() == DEVICE_NAME:
                    return "/dev/input/" + os.path.basename(sysdev)
        except OSError:
            continue
    if os.path.exists(DEVICE_BYPATH):
        return DEVICE_BYPATH
    return None


# --------------------------------------------------------------------------
# 电源模式读写
# --------------------------------------------------------------------------
def available_order():
    """读取内核实际支持的档位，按 CYCLE_ORDER 排序。

    部分机型没有 low-power，这里做适配而不是硬编码三档。
    """
    choices = []
    try:
        with open(SYSFS_CHOICES, encoding="utf-8") as f:
            choices = f.read().split()
    except OSError:
        pass
    mapped = [SYSFS_TO_PPD.get(c, c) for c in choices]
    order = [p for p in CYCLE_ORDER if p in mapped]
    return order or list(CYCLE_ORDER)


def read_profile():
    """读取当前电源模式，返回 PPD 风格名称；无法判断时返回 None。"""
    rc, out = run(["powerprofilesctl", "get"])
    if rc == 0 and out in CYCLE_ORDER:
        return out
    try:
        with open(SYSFS_PROFILE, encoding="utf-8") as f:
            return SYSFS_TO_PPD.get(f.read().strip())
    except OSError:
        return None


def write_profile(name):
    """设置电源模式。优先 PPD，退化写 sysfs。成功返回 True。"""
    rc, _ = run(["powerprofilesctl", "set", name])
    if rc == 0:
        return True
    sysname = PPD_TO_SYSFS.get(name, name)
    try:
        with open(SYSFS_PROFILE, "w", encoding="utf-8") as f:
            f.write(sysname + "\n")
        return True
    except OSError as e:
        log(f"设置 {name} 失败：PPD 不可用，且写 {SYSFS_PROFILE} 出错：{e}")
        return False


# --------------------------------------------------------------------------
# 模式指示灯：经 acpi_call 调 ACPI ECRR/ECRW 读写 EC 寄存器
#
# 与内核驱动内部完全同一条通路（uniwill_acpi.c 的 uniwill_ec_reg_read/write
# 就是 acpi_evaluate_integer(handle,"ECRR") / acpi_evaluate_object(handle,"ECRW")，
# handle 正是 \_SB_.INOU）。因此这里没有“绕过内核”一说，也不引入新的权限模型。
# --------------------------------------------------------------------------
def ec_available():
    """指示灯功能是否可用（acpi_call 已加载）。不可用则整个功能静默跳过。"""
    return os.path.exists(ACPI_CALL_PATH)


# 关键：内核驱动用的是「设备 handle + **相对名**」——
#     acpi_evaluate_integer(data->handle, "ECRR", ...)
# ACPI 解析相对名时会沿作用域链向上搜索，所以 ECRR 的真实位置是
# \_SB_.INOU.ECRR、\_SB_.ECRR、\_ECRR 三者之一（不能更远）。
# 而 acpi_call 只认**全限定路径**去 acpi_get_handle()，路径写错就报
# "Cannot get handle"。
#
# 注意这里**不是**盲试一大堆候选：路径写错时 acpi_call 会用 KERN_ERR 往内核
# 日志里打一条 "Cannot get handle"，试错本身就是 dmesg 噪音。所以顺序是：
#   1) 先读 sysfs 里内核自己给出的权威路径（firmware_node/path），它就是
#      驱动调用 ECRR 时的基准作用域，健康机型一次命中、零报错；
#   2) 再退回到去重后的少数几个祖先作用域，只在 sysfs 读不到时才用。
#
# 另外 ACPI 名字是「4 字符、不足补下划线」，所以 \_SB 与 \_SB_、
# \_SB.INOU 与 \_SB_.INOU 在命名空间里**是同一个对象**。原先把这些写法
# 全部列成候选，等于对同一个不存在的路径重复报错。这里统一归一化去重。
_EC_FALLBACK_SCOPES = [
    "\\_SB_.INOU",   # 设备自身（正常情况；也正是 sysfs 会给出的答案）
    "\\_SB_",        # 父作用域
    "",              # 根命名空间 → \ECRR
]

# 设备在 sysfs 里暴露 ACPI 路径的位置，按可靠性排序。
_ACPI_PATH_FILES = [
    "/sys/bus/acpi/devices/{dev}/firmware_node/path",
    "/sys/bus/acpi/devices/{dev}/path",
]
_ACPI_DEVICE_ID = "INOU0000:00"   # 平台设备名（= ACPI_DEVICE 对应的实例）

_last_raw = None         # 最近一次 acpi_call 的原始返回，用于诊断
_ec_scope_cache = None   # None=未探测；""=探测过但都不通；否则=可用作用域


def _acpi_name_key(path):
    """把 ACPI 路径归一化成命名空间视角的规范名，用于去重。

    ACPI 名字固定 4 字符，不足部分用 '_' 补齐，因此 `\\_SB` 和 `\\_SB_`
    指向同一个对象、`\\_SB.INOU` 和 `\\_SB_.INOU` 也是。不归一化的话，
    同一个不存在的路径会被当成多个候选、重复往内核日志里刷同样的报错。
    """
    if not path:
        return "\\"
    segs = [s for s in path.lstrip("\\").split(".") if s]
    return "\\" + ".".join((s + "____")[:4] for s in segs)


def _device_acpi_path():
    """从 sysfs 读内核给出的本机 ACPI 设备路径（如 `\\_SB_.INOU`）。

    这是最权威的答案：它正是内核绑驱动时用的那个 handle 对应的路径，
    因此以它为作用域去调 ECRR 必然命中，不必试错、也就不会产生
    "Cannot get handle" 噪音。读不到时返回 None，由调用方回退。
    """
    for tpl in _ACPI_PATH_FILES:
        try:
            with open(tpl.format(dev=_ACPI_DEVICE_ID), encoding="utf-8") as f:
                p = _clean_reply(f.read())
        except OSError:
            continue
        if p.startswith("\\"):
            return p
    return None


def _candidate_scopes():
    """给出待试的 ACPI 作用域，已按命名空间归一化去重。

    sysfs 权威路径排第一；其余候选仅在它读不到时才有机会被用到。
    """
    out = []
    seen = set()
    for scope in ([_device_acpi_path()] + _EC_FALLBACK_SCOPES):
        if scope is None:
            continue
        key = _acpi_name_key(scope)
        if key in seen:
            continue
        seen.add(key)
        out.append(scope)
    return out


# 兼容旧名字：外部（含测试）可能引用过 _EC_SCOPES。
_EC_SCOPES = _EC_FALLBACK_SCOPES


def _method_path(scope, name):
    """拼出全限定方法路径。scope 为空表示根命名空间。"""
    return f"{scope}.{name}" if scope else f"\\{name}"


def _clean_reply(text):
    """清掉 acpi_call 返回里的 NUL 字节。

    这是本方案最关键的一处兼容处理：acpi_call 的 acpi_proc_read() 传给
    simple_read_from_buffer() 的长度是「字符串长度 + 1」（反汇编里可见
    `lea r8,[rax+0x1]`），也就是**把结尾的 NUL 也一起返回给用户态**。

    C 的字符串函数和 shell 的 $(cat ...) 都会自动忽略 NUL，所以这个问题
    只在 Python 里暴露；而 NUL 不属于空白字符，str.strip() **去不掉它**，
    结果就是 int("0x19\\x00", 0) 抛异常、被误判成"读取失败"。
    必须在任何解析之前先把它删掉。
    """
    return text.replace("\x00", "").strip()


def _acpi_call(invocation):
    """执行一次 acpi_call，返回去 NUL/去空白的原始输出；IO 失败返回 None。"""
    global _last_raw
    try:
        with open(ACPI_CALL_PATH, "w", encoding="utf-8") as f:
            f.write(invocation)
        with open(ACPI_CALL_PATH, encoding="utf-8", errors="replace") as f:
            raw = _clean_reply(f.read())
    except OSError as e:
        # 写 /proc/acpi/call 时的 EINVAL/EACCES 通常意味着输入没被解析器
        # 接受、或权限不对，这里把 errno 也带出来，避免只剩一句"读取失败"。
        _last_raw = f"{e.strerror}（errno={e.errno}）"
        if _verbose:
            log(f"acpi_call 失败：{_last_raw}")
        return None
    _last_raw = raw
    return raw


def _parse_int(text):
    """解析 acpi_call 的返回值。支持 0x 前缀；Error/空/异常返回 None。"""
    if not text:
        return None
    # 这里再清一次 NUL：_parse_int 是独立的纯函数，不该假设调用方已经清过。
    t = _clean_reply(text)
    if not t or t.lower().startswith("error") or t.lower() == "not called":
        return None
    # 有些版本返回 "0x0 (0)" 之类，只取第一个 token
    t = t.split()[0].rstrip(",")
    try:
        return int(t, 0)
    except ValueError:
        return None


def _classify(raw):
    """把 acpi_call 的原始返回归类，便于诊断。"""
    if raw is None:
        return "io"
    r = _clean_reply(raw)
    low = r.lower()
    if not r or low == "not called":
        return "notcalled"          # 写入没生效，或还没调用过
    if low.startswith("error"):
        if "cannot get handle" in low:
            return "nohandle"       # 路径在命名空间里不存在
        if "method call failed" in low:
            return "nocall"         # 路径存在，但方法调用失败
        return "error"
    return "ok" if _parse_int(r) is not None else "unknown"


def discover_ec(verbose=False):
    """找出能用的 ACPI 作用域。返回 (作用域 or None, 诊断行列表)。

    设计要点：这是 dmesg 噪音的唯一来源，所以三条规则必须守住——
      1) **命中即停**。原先命中后仍把其余候选全部试完，而每个错误路径都会让
         acpi_call 打一条 "Cannot get handle"。健康机型上这是纯粹的自我噪音。
      2) 候选**先去重**。ACPI 名字 4 字符补下划线，`\\_SB`／`\\_SB_` 等同名，
         重复试等于对同一个不存在对象重复报错。
      3) 探测结论**写入缓存**。原先不写，于是 `probe()` 先调本函数、紧接着
         `_ec_scope()` 又因缓存为空**再探测一遍**，把报错数直接翻倍
         （真机日志里那几组 6 条报错就是这么来的）。
    """
    global _ec_scope_cache
    lines = []
    found = None
    for scope in _candidate_scopes():
        path = _method_path(scope, "ECRR")
        raw = _acpi_call(f"{path} 0x0740")
        kind = _classify(raw)
        val = _parse_int(raw)
        if val is not None:
            found = scope
            shown = f"✔ 可读，0x0740 = 0x{val:02X}"
        elif kind == "nohandle":
            shown = "✘ 命名空间里没有这个路径"
        elif kind == "nocall":
            shown = "△ 路径存在但调用失败"
        elif kind == "notcalled":
            shown = "? 写入未生效（原始返回 not called）"
        elif kind == "io":
            # raw 为 None，真正的原因（errno/权限）在 _last_raw 里，
            # 不能直接打印 raw，否则只会看到一句没用的“None”。
            shown = f"? 打不开 {ACPI_CALL_PATH}：{_last_raw}"
        elif kind == "error":
            shown = f"? 返回 {raw!r}"
        else:
            shown = f"? 无法解析的返回 {raw!r}"
        lines.append(f"    {path:<26} {shown}")
        if verbose:
            lines.append(f"      原始返回：{raw!r}")
        if found is not None:
            # 命中即停：后面的候选必然是错的，继续试只会往内核日志里
            # 刷 "Cannot get handle"，属于自己制造 dmesg 噪音。
            break
    # 把结论写进缓存，避免调用方随后再问一次作用域时重复整轮探测。
    _ec_scope_cache = found if found is not None else ""
    return found, lines


def _ec_scope():
    """返回可用的 ACPI 作用域；结果缓存，探测失败时退回 sysfs 权威路径。"""
    global _ec_scope_cache
    if _ec_scope_cache is not None:
        return _ec_scope_cache or _default_scope()
    found, _ = discover_ec()
    _ec_scope_cache = found if found is not None else ""
    return _ec_scope_cache or _default_scope()


def _default_scope():
    """探测失败时的兜底作用域：优先 sysfs 权威路径，其次设备自身写法。"""
    return _device_acpi_path() or _EC_FALLBACK_SCOPES[0]


def control_test():
    """对照实验：区分「路径写错」和「调用根本没生效」。

    做两次调用，一次负面、一次正面：

    1) 负面对照：故意写一个绝对不存在的路径。
       acpi_call 找不到句柄时**必定**用 KERN_ERR 打印 "Cannot get handle"。
         · 日志出现新记录 -> acpi_call 工作正常
         · 日志毫无变化   -> 调用没真正执行

    2) 正面对照：调用 \\_SB_.INOU._STA。
       这条路径由内核自己证明是存在的 —— 驱动探测设备时调过 _STA 并拿到了
       状态 0x0B（=status 11，见 /sys/bus/acpi/devices/INOU0000:00/status）。
       所以它**一定**能返回一个整数；若连它都失败，问题与 EC 寄存器无关。

    返回 (结论字符串, 说明行列表)。两次都只调用只读方法，绝不写 EC。
    """
    before = _recent_acpi_call_dmesg(limit=None)
    if before is None:
        return "unknown", ["  无法读取内核日志，跳过对照实验。"]
    n_before = len(before)

    lines = []

    # ---- 正面对照：_STA 必然存在且返回整数 ----
    sta_raw = _acpi_call(f"{ACPI_DEVICE}._STA")
    sta_val = _parse_int(sta_raw)
    lines.append(f"    ① 正面对照 \\_SB_.INOU._STA -> {sta_raw!r}")
    if sta_val is not None:
        lines.append(f"       ✔ acpi_call 通路正常（_STA 返回 0x{sta_val:X}）")
        transport_ok = True
    else:
        lines.append("       ✘ 连必然存在的 _STA 都调不通 —— 问题在 acpi_call")
        lines.append("         通路本身（权限 / 模块 / 内核不匹配），与 EC 路径无关。")
        transport_ok = False

    # ---- 负面对照：不存在的路径必须让内核打印 Cannot get handle ----
    bad_raw = _acpi_call("\\_SB_.NOSUCHTHING_XYZ")
    time.sleep(0.5)          # 给 printk 一点时间落到日志里
    after = _recent_acpi_call_dmesg(limit=None)
    n_after = len(after) if after else 0
    lines.append(f"    ② 负面对照 \\_SB_.NOSUCHTHING_XYZ -> {bad_raw!r}")
    if n_after > n_before:
        lines.append(f"       内核日志新增 {n_after - n_before} 条报错，符合预期：")
        for ln in (after or [])[-2:]:
            lines.append(f"         {ln}")
        log_ok = True
    else:
        lines.append(f"       内核日志没有新增（前后都是 {n_before} 条），"
                     "报错没进日志。")
        log_ok = False

    lines.append("")
    if transport_ok and log_ok:
        if sta_val is not None:
            lines.append("    => 结论：acpi_call 通路**完全正常**，")
            lines.append("       所以问题确实出在 ECRR/ECRW 的**路径**上。")
            return "path", lines
    if not transport_ok:
        lines.append("    => 结论：acpi_call **通路不通**（连 _STA 都失败）。")
        lines.append("       请把本节输出连同以下命令的结果一起发回：")
        lines.append("         sudo dmesg | tail -20")
        lines.append("         ls -l /proc/acpi/call && id -u")
        return "notransport", lines
    lines.append("    => 结论：调用能返回，但内核报错没进日志（可能日志源不同）。")
    lines.append("       请把本节输出连同 `sudo dmesg | tail -20` 一起发回。")
    return "silent", lines


def _recent_acpi_call_dmesg(limit=15):
    """取内核日志里 acpi_call 自己打印的记录，返回字符串列表或 None。

    acpi_call 在「找不到句柄」和「方法调用失败」时都用 KERN_ERR 打印，
    这是最权威的判据：有记录说明调用真的失败了；没有记录则往往意味着
    调用其实成功了，只是返回值没被解析出来。

    limit=None 表示返回全部记录（对照实验要靠总数判断有没有新增，
    所以不能截断——如果日志里已有满满一屏相同的报错，只看尾部会误判）。
    """
    for cmd in (["journalctl", "-k", "--no-pager"], ["dmesg"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if not p.stdout:
            continue
        hits = [ln for ln in p.stdout.splitlines() if "acpi_call" in ln.lower()]
        if limit is None:
            return hits
        return hits[-limit:]
    return None            # 两条命令都不可用，无从判断


def ec_read(addr):
    """读 EC 寄存器，返回 0-255；失败返回 None。"""
    path = _method_path(_ec_scope(), "ECRR")
    val = _parse_int(_acpi_call(f"{path} 0x{addr:04X}"))
    if val is None or not 0 <= val <= 0xFF:
        return None
    return val


def ec_write(addr, value):
    """写 EC 寄存器，成功返回 True。"""
    path = _method_path(_ec_scope(), "ECRW")
    out = _acpi_call(f"{path} 0x{addr:04X} 0x{value:02X}")
    # ECRW 本身不返回值（DSDT 里它只是 MMRW 调用，没有 Return）。
    # 真机实测返回 0xfffffffe，那是 MMRW 里 Local0 的初值：
    #     Local0 = 0xFFFFFFFE
    #     If ((Arg1 == Zero))  ... 才会把 Local0 赋成真实值
    # ECRW 走的是 Arg1=One（写）分支，所以永远返回这个初值。
    # 它是**回显/哨兵**，不是错误码 —— 只有 Error/AE_* 才算失败。
    if out is None:
        return False
    return not out.lower().startswith("error")


def set_led_profile(profile, dry_run=False):
    """让按键旁的指示灯切到该档位颜色。

    严格照搬 TUXEDO uw_set_performance_profile_v1() 的读-改-写语义：
    只动 0xA0|0x10 这两位，其余位（尤其是 0x40 全速风扇位）原样保留。
    任何一步失败都只记日志、返回 False，绝不抛出，也绝不影响电源模式切换。
    """
    if profile not in EC_LED_VALUES:
        return False

    want = EC_LED_VALUES[profile]

    if not ec_available():
        # 试运行时仍然把"本来会写什么"告诉用户，便于在没装 acpi_call
        # 的情况下也能先看清映射关系。
        if dry_run:
            log(f"[试运行] 指示灯 0x{EC_LED_REG:04X} 将设为 0x{want:02X}"
                f"（{LABELS.get(profile, profile)}）；acpi_call 未加载，实际不会写")
            return True
        if _verbose:
            log("未加载 acpi_call，跳过指示灯（可安装 acpi_call 后启用）")
        return False

    cur = ec_read(EC_LED_REG)
    if cur is None:
        log(f"读 EC 0x{EC_LED_REG:04X} 失败，指示灯未更新")
        return False
    new = (cur & ~EC_LED_CLEAR_BITS & 0xFF) | want
    if new == cur:
        if _verbose:
            log(f"指示灯已正确：0x{EC_LED_REG:04X}=0x{cur:02X}")
        return True

    if dry_run:
        log(f"[试运行] 指示灯 0x{EC_LED_REG:04X}: 0x{cur:02X} -> 0x{new:02X}")
        return True

    if not ec_write(EC_LED_REG, new):
        log(f"写 EC 0x{EC_LED_REG:04X} 失败，指示灯未更新")
        return False
    log(f"指示灯 -> {LABELS.get(profile, profile)}"
        f"（EC 0x{EC_LED_REG:04X}: 0x{cur:02X} -> 0x{new:02X}）")
    return True


# 0xB0 位域里的三个独立位。TUXEDO 的编码是 办公=0xA0(bit7|bit5)、
# 均衡=0x00、狂暴=0x10(bit4)；本机实测 0xA0/0x00/0x10 三个都成立，
# 所以默认映射就是它。保留这个扫描工具，是为了在**别的**机型/BIOS 上
# 万一位域编码不同（比如只用了 bit5 而不是 bit7|bit5）能实测出来。
LED_SCAN_BITS = [b for b in (0x80, 0x20, 0x10) if EC_LED_CLEAR_BITS & b]


def led_scan(interactive=True):
    """标定颜色编码：把 0xB0 位域的全部 8 种组合逐个写进去，肉眼确认颜色。

    背景：默认映射（办公=0xA0 / 均衡=0x00 / 狂暴=0x10）来自 TUXEDO 驱动，
    已在本机验证成立。但这类 EC 编码是**看 BIOS/准系统**的，换机型可能不同
    （例如 0xA0 实际只用了 bit5 而非 bit7|bit5）。这个工具把 0xB0 位域的
    8 种组合逐个试一遍，用肉眼确定真实编码，不靠猜。

    安全性：与 set_led_profile 完全一致 —— 只改 0xB0 这三位，
    0x40 全速风扇位与低 3 位风扇档位原样保留；切换前先读回，
    结束后无条件恢复成进入时的原始值。任何一步失败都立即停止。
    """
    if not ec_available():
        print(f"未加载 acpi_call，或 {ACPI_CALL_PATH} 不存在，无法标定。")
        return 1

    orig = ec_read(EC_LED_REG)
    if orig is None:
        print(f"读 EC 0x{EC_LED_REG:04X} 失败，无法标定。")
        print("先用 `--probe` 把 EC 读取修通，再回来标定。")
        return 1

    combos = []
    for i in range(1 << len(LED_SCAN_BITS)):
        v = 0
        for j, b in enumerate(LED_SCAN_BITS):
            if i & (1 << j):
                v |= b
        combos.append(v)
    combos.sort()

    print(f"当前 0x{EC_LED_REG:04X} = 0x{orig:02X}"
          f"（0xB0 位域 = 0x{orig & EC_LED_CLEAR_BITS:02X}）")
    print(f"将依次试 {len(combos)} 种组合，请盯着按键旁那颗灯。")
    print(f"每一步只改 0xB0，0x40 全速风扇位与低 3 位风扇档位都保留。")
    print()

    if not interactive:
        for v in combos:
            print(f"  (试运行) 0xB0 位域 -> 0x{v:02X}")
        return 0

    seen = []
    try:
        for idx, want in enumerate(combos, 1):
            cur = ec_read(EC_LED_REG)
            if cur is None:
                print("读 EC 失败，中止标定。")
                return 1
            new = (cur & ~EC_LED_CLEAR_BITS & 0xFF) | want
            if not ec_write(EC_LED_REG, new):
                print("写 EC 失败，中止标定。")
                return 1
            print(f"  [{idx}/{len(combos)}] 0xB0 位域 = 0x{want:02X}"
                  f"（整字节 0x{new:02X}）"
                  + ("   ← 与当前值相同" if want == (orig & EC_LED_CLEAR_BITS) else ""))
            try:
                note = input("        看到什么颜色？（直接回车=没变化）: ").strip()
            except EOFError:
                note = ""
            seen.append((want, note))
            print()
    except KeyboardInterrupt:
        print("\n已中止。")
    finally:
        # 无条件恢复原始值，绝不把标定过程留在 EC 里
        cur = ec_read(EC_LED_REG)
        if cur is not None:
            back = (cur & ~EC_LED_CLEAR_BITS & 0xFF) | (orig & EC_LED_CLEAR_BITS)
            if ec_write(EC_LED_REG, back):
                print(f"已恢复 0x{EC_LED_REG:04X} = 0x{back:02X}"
                      f"（进入时为 0x{orig:02X}）")
            else:
                print(f"恢复 0x{EC_LED_REG:04X} 失败！建议冷启动复位。")

    # 汇总成可直接填进 EC_LED_VALUES 的结论
    hits = {n for _, n in seen if n}
    if hits:
        print()
        print("观察结果汇总（据此修改 EC_LED_VALUES）：")
        for want, note in seen:
            if note:
                print(f"  0x{want:02X} -> {note}")
        print()
        print("把这份对应关系发回来即可改成默认映射；")
        print(f"当前默认是 办公=0x{EC_LED_VALUES['power-saver']:02X}"
              f" / 均衡=0x{EC_LED_VALUES['balanced']:02X}"
              f" / 狂暴=0x{EC_LED_VALUES['performance']:02X}。")
    return 0


# --------------------------------------------------------------------------
# 桌面通知（尽力而为，失败绝不影响切换）
# --------------------------------------------------------------------------
def _session_uids():
    """收集可能的图形会话 UID：先活跃会话，再退回 /run/user/*。"""
    uids = []
    rc, txt = run(["loginctl", "list-sessions", "--no-legend"])
    if rc == 0:
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            sid, uid = parts[0], parts[1]
            rc2, props = run(["loginctl", "show-session", sid, "-p", "Active",
                              "-p", "Type", "-p", "Remote"])
            if rc2 != 0:
                continue
            d = dict(p.split("=", 1) for p in props.split() if "=" in p)
            if d.get("Active") != "yes" or d.get("Remote") == "yes":
                continue
            if d.get("Type") in ("wayland", "x11", "mir"):
                uids.append(uid)
    # 兜底：任何存在会话总线的普通用户
    for d in sorted(glob.glob("/run/user/*"), reverse=True):
        uid = os.path.basename(d)
        if uid.isdigit() and 1000 <= int(uid) < 60000 and os.path.exists(f"{d}/bus"):
            uids.append(uid)
    seen, out = set(), []
    for u in uids:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def notify(profile):
    """以登录用户身份发送桌面通知（DE 无关：走 freedesktop 通知规范）。"""
    if os.geteuid() != 0:
        # 非 root 运行时直接尝试当前环境
        run(["notify-send", "-i", ICONS.get(profile, "battery"), "-t", "1800",
             "性能模式", f"{LABELS.get(profile, profile)} ({profile})"], timeout=5)
        return
    for uid in _session_uids():
        bus = f"/run/user/{uid}/bus"
        if not os.path.exists(bus):
            continue
        try:
            user = pwd.getpwuid(int(uid)).pw_name
        except (KeyError, ValueError):
            continue
        rc, _ = run([
            "runuser", "-u", user, "--", "env",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}",
            "XDG_RUNTIME_DIR=" + f"/run/user/{uid}",
            "notify-send", "-i", ICONS.get(profile, "battery"), "-t", "1800",
            "性能模式", f"{LABELS.get(profile, profile)} ({profile})",
        ], timeout=6)
        if rc == 0:
            return


# --------------------------------------------------------------------------
# 核心：切换
# --------------------------------------------------------------------------
def cycle(dry_run=False):
    order = available_order()
    cur = read_profile()
    if cur in order:
        nxt = order[(order.index(cur) + 1) % len(order)]
    else:
        nxt = order[0]
    log(f"切换：{cur or '未知'} -> {nxt}（{LABELS.get(nxt, nxt)}）")
    if dry_run:
        # 试运行时也顺带展示指示灯会怎么写，便于标定颜色
        set_led_profile(nxt, dry_run=True)
        return 0
    if not write_profile(nxt):
        return 1
    # 指示灯跟随档位；失败不影响切换结果（电源模式已经切好了）
    try:
        set_led_profile(nxt)
    except Exception as e:  # 必须绝不影响主流程
        log(f"更新指示灯时异常（已忽略）：{e!r}")
    notify(nxt)
    return 0


def sync_led(dry_run=False):
    """把指示灯对齐到当前电源模式（启动时调用，纠正开机/重启后的错位）。"""
    cur = read_profile()
    if cur not in EC_LED_VALUES:
        log(f"当前档位 {cur!r} 无法映射到指示灯，跳过同步")
        return 1
    ok = set_led_profile(cur, dry_run=dry_run)
    log(f"指示灯同步：{LABELS.get(cur, cur)} -> {'成功' if ok else '未完成'}")
    return 0 if ok else 1


def on_press(args):
    global _last_press
    now = time.monotonic()
    if now - _last_press < args.debounce:
        # 抑制按键抖动 / 长按重复
        return
    _last_press = now
    try:
        cycle(dry_run=args.dry_run)
    except Exception as e:  # 守护进程必须存活
        log(f"切换时发生异常（已忽略）：{e!r}")


_leftover = b""


def _reset_leftover():
    """丢弃尚未凑成一个完整事件的残包（设备重开时必须调用）。"""
    global _leftover
    _leftover = b""


def handle_bytes(data, args):
    """解析一段输入事件字节流，返回其中检测到的 KEY_F14 按下次数。

    单独成函数以便用合成数据自测（沙箱内无法访问真实输入设备）。

    正常情况下 evdev 的 read() 总是返回完整的 24 字节事件，不会切开；
    但这里仍然把不足一个事件的尾部字节缓存到下次拼接，做到既不丢事件
    也不误解析。
    """
    global _leftover
    data = _leftover + data
    usable = len(data) - (len(data) % INPUT_EVENT.size)
    _leftover = data[usable:]

    presses = 0
    for off in range(0, usable, INPUT_EVENT.size):
        _sec, _usec, etype, code, value = INPUT_EVENT.unpack_from(data, off)
        if etype == EV_KEY and code == KEY_F14 and value == 1:
            if _verbose:
                log("检测到性能模式键按下")
            presses += 1
            on_press(args)
    return presses


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
_running = True


def _on_term(signum, _frame):
    global _running
    _running = False


def _sleep_retry(seconds):
    """可被 SIGTERM 立即打断的等待，返回 False 表示应当退出。"""
    deadline = time.monotonic() + seconds
    while _running and time.monotonic() < deadline:
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return _running


def main_loop(args):
    # 信号只能在主线程注册；被嵌入其它程序（或测试）于子线程调用时跳过。
    try:
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
    except ValueError:
        log("提示：非主线程运行，跳过信号处理注册（由调用方负责停止）。")

    if os.geteuid() != 0:
        log("警告：未以 root 运行。若无法打开输入设备，请用 sudo 或安装 udev 规则。")

    log(f"启动：监听「{DEVICE_NAME}」上的 KEY_F14（与桌面环境无关）")
    log(f"可用档位：{' -> '.join(available_order())}；当前：{read_profile()}")
    led_boot_synced = False
    if ec_available():
        log("指示灯：acpi_call 已就绪，切换时同步更新 EC 0x0751")
        # 开机/重启后 EC 与系统可能不一致，先对齐一次
        try:
            set_led_profile(read_profile())
            led_boot_synced = True
        except Exception as e:
            log(f"启动同步指示灯失败（已忽略）：{e!r}")
    else:
        log(f"指示灯：未找到 {ACPI_CALL_PATH}，仅切换电源模式"
            "（安装 acpi_call 并加载模块后即可点亮随模式变化的灯）")

    fd = None
    wait = 1.0            # 重试间隔，失败时翻倍，成功打开后复位
    while _running:
        # 若启动时 acpi_call 还没加载（它由 systemd-modules-load 提供，
        # 时机不保证），这里补做一次对齐；一旦成功就不再重复。
        # 按键路径本来就会重新检查，所以这只影响"开机首次对齐"。
        if not led_boot_synced and ec_available():
            led_boot_synced = True
            try:
                set_led_profile(read_profile())
                log("指示灯：acpi_call 已就绪，补做了一次对齐")
            except Exception as e:
                log(f"补做指示灯对齐失败（已忽略）：{e!r}")

        if fd is None:
            path = find_device()
            if not path:
                log(f"尚未找到该输入设备，{wait:.0f} 秒后重试……")
                if not _sleep_retry(wait):
                    break
                wait = min(wait * 2, 60.0)
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                log(f"已打开 {path}（os.read 监听中）")
                # 新设备/新连接：丢弃上一轮可能残留的半截事件，
                # 否则旧字节会和新事件流错位拼接。
                _reset_leftover()
                wait = 1.0
            except OSError as e:
                hint = ""
                if e.errno in (errno.EACCES, errno.EPERM):
                    hint = "：权限不足（需 root，或安装 udev uaccess 规则）"
                log(f"打开 {path} 失败：{e.strerror}{hint}，{wait:.0f} 秒后重试")
                if not _sleep_retry(wait):
                    break
                wait = min(wait * 2, 60.0)
                continue

        try:
            ready, _, _ = select.select([fd], [], [], 1.0)
        except InterruptedError:
            continue
        except OSError as e:
            log(f"select 出错：{e}，重新打开设备")
            os.close(fd)
            fd = None
            continue

        if not ready:
            continue

        try:
            data = os.read(fd, INPUT_EVENT.size * 64)
        except BlockingIOError:
            continue
        except InterruptedError:
            continue
        except OSError as e:
            # ENODEV：设备被移除（如模块卸载 / 重启），重新扫描
            log(f"读取设备出错：{e.strerror}，重新打开")
            os.close(fd)
            fd = None
            continue

        if not data:
            log("设备返回 EOF（可能已被移除），重新打开")
            os.close(fd)
            fd = None
            continue

        handle_bytes(data, args)

    if fd is not None:
        os.close(fd)
    log("已退出。")
    return 0


# --------------------------------------------------------------------------
# 只读诊断
# --------------------------------------------------------------------------
# 这些寄存器只用于“看”，本程序只会写 EC_LED_REG (0x0751) 的 0xA0|0x10 两位。
PROBE_REGS = [
    (0x0740, "PROJECT_ID 项目号"),
    (0x0741, "AP_OEM（bit0=ENABLE_MANUAL_CTRL）"),
    (0x0751, "MANUAL_FAN_CTRL ← 模式指示灯就写这里"),
    (0x07A5, "OEM_3（bit1:0=POWER_LED_*，未使用）"),
    (0x0782, "BIOS_OEM_2（bit2=FAN_TABLE_OFFICE_MODE）"),
    (0x0748, "LIGHTBAR_AC_CTRL（键盘灯带，不是模式灯）"),
    (0x0749, "LIGHTBAR_AC_RED"),
]


def probe():
    """只读地打印 EC 寄存器现状，绝不写任何东西。"""
    print(f"版本           : {VERSION}")
    print(f"自身路径       : {os.path.abspath(__file__)}")
    print(f"ACPI 设备      : {ACPI_DEVICE}（平台设备 INOU0000:00）")
    print(f"acpi_call 通路 : {ACPI_CALL_PATH} "
          + ("（就绪）" if ec_available() else "（不可用）"))
    print(f"当前电源模式   : {read_profile() or '未知'}")
    print()
    if not ec_available():
        print(f"无法读取：未加载 acpi_call，{ACPI_CALL_PATH} 不存在。")
        print("安装并加载后重试：")
        print("  sudo pacman -S acpi_call        # 或 paru -S acpi_call")
        print("  sudo modprobe acpi_call")
        return 1

    # 先确认能不能读通任意一个 EC 寄存器；不通就把原始错误摊开，
    # 并逐个试候选作用域，避免"读取失败"这种没法排查的提示。
    found, lines = discover_ec(verbose=_verbose)
    if found is None:
        print("EC 读取不通，ACPI 路径诊断如下（探针寄存器 0x0740）：")
        for ln in lines:
            print(ln)
        print()

        # 对照实验：正面对照用必然存在的 _STA，负面对照用不存在的路径。
        # 这一步能把「路径写错」「通路本身不通」「日志源不同」彻底分开，
        # 否则三种情况都表现成同一句没头没脑的"读取失败"。
        print("  对照实验（判断是路径问题还是通路问题）：")
        verdict, info = control_test()
        for ln in info:
            print(ln)
        print()

        # 模块自己的报错最权威。
        dmesg = _recent_acpi_call_dmesg()
        if dmesg:
            print("  内核日志里 acpi_call 的报错（最权威的判据）：")
            for ln in dmesg:
                print(f"    {ln}")
            print()
        if verdict == "path":
            print("  下一步：运行 sudo tools/diag-acpi.sh，用 DSDT 反汇编")
            print("          找出 ECRR 的真正归属（第 4 节）。")
        else:
            print("  背景：内核 uniwill 驱动读 EC 用的是「设备 handle + 相对名 ECRR」，")
            print("        ACPI 会沿作用域链向上找，所以 ECRR 必然在这几处之一。")
            print("        驱动自己读得到（hwmon 有温度/风扇转速），说明方法确实存在。")
            print()
            print("  下一步：运行 sudo tools/diag-acpi.sh，并把完整输出发回。")
        return 1
    if _verbose:
        print(f"  已确认可用作用域：{_method_path(found, 'ECRR')}")
        for ln in lines:
            print(ln)
        print()

    print("EC 寄存器现状（只读）：")
    print(f"  {'地址':<8} {'值':<6} 说明")
    for addr, desc in PROBE_REGS:
        # 失败时把 acpi_call 的原始返回一起打出来。这一步是刻意的：
        # "读取失败"这种提示无法区分「路径不存在」「方法调用失败」
        # 「返回值解析不了」，而原始字符串能直接指认原因。
        raw = _acpi_call(f"{_method_path(_ec_scope(), 'ECRR')} 0x{addr:04X}")
        val = _parse_int(raw)
        if val is not None and 0 <= val <= 0xFF:
            shown = f"0x{val:02X}"
        else:
            shown = f"失败 ← acpi_call 返回 {raw!r}"
        print(f"  0x{addr:04X}   {shown:<6} {desc}")

    cur = ec_read(EC_LED_REG)
    print()
    print("指示灯解析（0x0751）：")
    if cur is None:
        print("  读取失败，无法解析。")
    else:
        field = cur & EC_LED_CLEAR_BITS
        print(f"  原始值        : 0x{cur:02X}")
        print(f"  0xB0 位域     : 0x{field:02X}（决策只看这一段）")
        # 注意：0xA0 是**两位**（bit7|bit5），必须整段比较，
        # 不能写成 `cur & 0xA0` —— 那样只要 bit5 单独置位就会误报"办公位置位"。
        for bit, bitname in ((0x80, "bit7"), (0x20, "bit5"), (0x10, "bit4")):
            print(f"    {bitname} (0x{bit:02X}) : {'置位' if field & bit else '清零'}")
        print(f"  0x40 全速风扇 : "
              f"{'置位（本程序会保留，不动它）' if cur & 0x40 else '清零'}")
        print(f"  风扇档位掩码  : 0x{cur & 0x07:X}")
        matched = [p for p, v in EC_LED_VALUES.items() if field == v]
        if matched:
            print(f"  当前应显示    : {LABELS[matched[0]]}（{matched[0]}）")
        else:
            bits = " | ".join(f"0x{b:02X}" for b in LED_SCAN_BITS
                              if (cur & EC_LED_CLEAR_BITS) & b) or "（全清）"
            print(f"  当前应显示    : 未知组合（0xB0 位域 = "
                  f"0x{cur & EC_LED_CLEAR_BITS:02X}，置位的是 {bits}）")
            print("                  可能由 BIOS/其它软件占用；也可能是本机编码")
            print("                  与 TUXEDO 参考实现不同 —— 用 --scan-led 实测标定。")
    print()
    print("注：本命令全程只读，不修改 EC。")
    return 0


# --------------------------------------------------------------------------
def main():
    global _verbose
    ap = argparse.ArgumentParser(
        prog=_prog,
        description="MECHREVO 性能模式按键守护进程（与桌面环境无关）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  sudo %(prog)s            以守护方式监听按键（systemd 服务即调用此形式）
  %(prog)s --once          手动切一档（等价于“按一下键”），用于测试
  %(prog)s --status        打印当前档位与设备信息
  %(prog)s --dry-run       只打印将要切换到的档位，不真正切换
  sudo %(prog)s --probe    只读：打印 EC 0x0751 等寄存器现状（不改动任何东西）
  sudo %(prog)s --set-led performance
                           只把指示灯设成某档颜色，用于标定/验证
  sudo %(prog)s --sync-led 把指示灯对齐到当前电源模式
""" % {"prog": _prog},
    )
    ap.add_argument("--once", action="store_true", help="立即切换一档后退出")
    ap.add_argument("--status", action="store_true", help="显示当前状态后退出")
    ap.add_argument("--dry-run", action="store_true", help="只显示不切换")
    ap.add_argument("--no-notify", action="store_true", help="不发送桌面通知")
    ap.add_argument("--debounce", type=float, default=0.6,
                    help="两次切换之间的最小间隔秒数（默认 0.6）")
    ap.add_argument("--probe", action="store_true",
                    help="只读地打印 EC 寄存器现状，用于确认指示灯寄存器")
    ap.add_argument("--set-led", metavar="档位",
                    help="把指示灯设为指定档位颜色（power-saver/balanced/performance）")
    ap.add_argument("--sync-led", action="store_true",
                    help="把指示灯对齐到当前电源模式")
    ap.add_argument("--scan-led", action="store_true",
                    help="标定指示灯颜色编码：逐个试 0xB0 位域组合，肉眼确认（会改 EC）")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出更多信息")
    args = ap.parse_args()
    _verbose = args.verbose

    if args.no_notify:
        global notify
        notify = lambda _p: None

    if args.probe:
        return probe()

    if args.scan_led:
        return led_scan(interactive=not args.dry_run)

    if args.sync_led:
        return sync_led(dry_run=args.dry_run)

    if args.set_led:
        prof = normalize_profile(args.set_led)
        if not prof:
            print(f"无法识别的档位：{args.set_led}", file=sys.stderr)
            print("可用值：power-saver / balanced / performance（也接受 办公/均衡/狂暴）",
                  file=sys.stderr)
            return 2
        ok = set_led_profile(prof, dry_run=args.dry_run)
        if not ok:
            # 明确告诉用户为什么没成功，而不是静默返回 1
            if not ec_available():
                print(f"指示灯未启用：{ACPI_CALL_PATH} 不存在（acpi_call 未加载）。",
                      file=sys.stderr)
                print("启用： sudo ./install.sh --with-led", file=sys.stderr)
                print("或：   sudo pacman -S acpi_call && sudo modprobe acpi_call",
                      file=sys.stderr)
            else:
                print(f"写入 EC 0x{EC_LED_REG:04X} 失败，请用 sudo 运行，"
                      "并检查 journalctl -u mechrevo-keyd", file=sys.stderr)
        return 0 if ok else 1

    if args.status:
        dev = find_device()
        print(f"设备        : {dev or '未找到'}")
        print(f"当前档位    : {read_profile() or '未知'}")
        print(f"可用档位    : {' -> '.join(available_order())}")
        print(f"运行身份    : uid {os.geteuid()}")
        print(f"指示灯      : ", end="")
        if not ec_available():
            print(f"不可用（未加载 acpi_call，缺 {ACPI_CALL_PATH}）")
        else:
            cur = ec_read(EC_LED_REG)
            print(f"acpi_call 就绪，EC 0x{EC_LED_REG:04X} = "
                  + (f"0x{cur:02X}" if cur is not None else "读取失败"))
        print("内核 platform_profile choices: ", end="")
        try:
            with open(SYSFS_CHOICES, encoding="utf-8") as f:
                print(f.read().strip())
        except OSError:
            print("(不可读)")
        return 0

    # --dry-run 单独使用时等同于“试切一次”，避免误当守护进程挂住终端
    if args.once or args.dry_run:
        return cycle(dry_run=args.dry_run)

    return main_loop(args)


if __name__ == "__main__":
    sys.exit(main())
