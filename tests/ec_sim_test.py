#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模式指示灯写 EC 的端到端模拟测试。

不需要 root、不需要 acpi_call、不需要真机。做法：
  用一个假的 ACPI 调用端点（临时目录里的 call 脚本 + 一个 ec.map 文本文件
  当作 EC 寄存器堆）替换 mechrevo-keyd 的 _acpi_call，然后跑真实的
  set_led_profile() / cycle() 代码路径。

覆盖两件其它测试覆盖不到的事：
  1. 发给 acpi_call 的调用字符串格式必须与内核约定完全一致
     （\\_SB_.INOU.ECRR 0x0751 / \\_SB_.INOU.ECRW 0x0751 0xA0）；
  2. 从「读」到「写」再到「寄存器实际变成什么」的完整链路，
     尤其是 0x40 全速风扇位与低 3 位风扇档位在真实写回后仍然完好。

运行： python3 ec_sim_test.py
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

def _find_daemon():
    """定位 mechrevo-keyd.py，兼容两种目录布局：
    平铺（脚本与守护进程同目录）或本仓库布局（脚本在 tests/ 下）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "mechrevo-keyd.py"),
                 os.path.join(here, os.pardir, "mechrevo-keyd.py")):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    raise SystemExit("找不到 mechrevo-keyd.py（应与本测试同目录，或在上一层目录）")


HERE = os.path.dirname(_find_daemon())
spec = importlib.util.spec_from_file_location("keyd", _find_daemon())
keyd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keyd)

FAILED = []
INVOCATIONS = []

# 假的 ACPI 端点：inbox 收调用字符串，ec.map 当寄存器堆
SIM = '''#!/usr/bin/env python3
import os, sys
D = os.path.dirname(os.path.abspath(__file__))
STORE, INBOX = os.path.join(D, "ec.map"), os.path.join(D, "inbox")
db = {}
if os.path.exists(STORE):
    for line in open(STORE):
        a, v = line.split()
        db[int(a, 16)] = int(v, 16)
inv = open(INBOX).read().strip()
os.remove(INBOX)
parts = inv.split()
op = parts[0].rsplit(".", 1)[1]        # ECRR 或 ECRW
addr = int(parts[1], 16)
if op == "ECRR":
    # 关键：真机 acpi_call 的 acpi_proc_read() 传给 simple_read_from_buffer
    # 的长度是「字符串长度+1」，也就是会把结尾的 NUL 一起交给用户态。
    # 模拟端点必须照抄这一点，否则这里会假绿 —— 真机上就因为这个 NUL
    # 让所有读数被误判为失败。
    sys.stdout.write("0x%02X\\x00" % db.get(addr, 0))
    sys.stdout.flush()
else:
    db[addr] = int(parts[2], 16)
    with open(STORE, "w") as f:
        for a, v in sorted(db.items()):
            f.write("%04X %02X\\n" % (a, v))
    # ECRW 没有 Return，MMRW 只会回显 Local0 的初值 0xFFFFFFFE
    sys.stdout.write("0xfffffffe\\x00")
    sys.stdout.flush()
'''


def check(label, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(label)


class EC:
    """模拟的 EC 寄存器堆 + acpi_call 端点。"""

    def __init__(self, tmp):
        self.dir = tmp
        self.map = os.path.join(tmp, "ec.map")
        self.inbox = os.path.join(tmp, "inbox")
        self.call = os.path.join(tmp, "call")
        with open(self.call, "w") as f:
            f.write(SIM)
        os.chmod(self.call, 0o755)
        self.writes = 0

    def set(self, regs):
        with open(self.map, "w") as f:
            for a, v in sorted(regs.items()):
                f.write("%04X %02X\n" % (a, v))

    def get(self, addr):
        for line in open(self.map):
            a, v = line.split()
            if int(a, 16) == addr:
                return int(v, 16)
        return 0

    def acpi_call(self, invocation):
        """替代 keyd._acpi_call：记下调用字符串并交给模拟端点执行。"""
        INVOCATIONS.append(invocation)
        with open(self.inbox, "w") as f:
            f.write(invocation)
        out = subprocess.run([self.call], capture_output=True, text=True).stdout
        return out.strip()


tmp = tempfile.mkdtemp(prefix="ecsim-")
try:
    ec = EC(tmp)
    keyd._acpi_call = ec.acpi_call
    keyd.ec_available = lambda: True
    keyd.log = lambda m: None
    # 关键：_ec_scope() 带缓存，且默认会先拿 0x0740 探测一次路径。
    # 显式预置缓存，保证这里测的是「读 0x0751 / 写 0x0751」的精确格式。
    keyd._ec_scope_cache = keyd.ACPI_DEVICE

    print("== 1) 发给 acpi_call 的调用字符串格式 ==")
    INVOCATIONS.clear()
    ec.set({0x0741: 0x01, 0x0751: 0x00})
    keyd.set_led_profile("performance")
    check("恰好两次调用（一次读、一次写）", len(INVOCATIONS), 2)
    # 用切片而非下标，这样读数解析失败时给出可读的 FAIL 而不是崩溃
    check("读调用格式", INVOCATIONS[0:1], ["\\_SB_.INOU.ECRR 0x0751"])
    check("写调用格式", INVOCATIONS[1:2], ["\\_SB_.INOU.ECRW 0x0751 0x10"])
    check("ACPI 作用域与平台设备一致", keyd.ACPI_DEVICE, "\\_SB_.INOU")

    print("\n== 2) 三种档位写出的字节 ==")
    for prof, want, color in [("power-saver", 0xA0, "绿"),
                              ("balanced", 0x00, "蓝"),
                              ("performance", 0x10, "紫")]:
        ec.set({0x0741: 0x01, 0x0751: 0x00})
        keyd.set_led_profile(prof)
        check(f"{prof} -> 0x{want:02X}（{color}）", ec.get(0x0751), want)

    print("\n== 3) 回归：0x40 全速风扇位 + 低 3 位风扇档位必须保留 ==")
    # 0x45 = 0x40(全速风扇) | 0x05(风扇档位 5)
    for prof, want in [("power-saver", 0xE5), ("balanced", 0x45),
                       ("performance", 0x55)]:
        ec.set({0x0741: 0x01, 0x0751: 0x45})
        keyd.set_led_profile(prof)
        got = ec.get(0x0751)
        check(f"{prof}: 0x45 -> 0x{want:02X}", got, want)
        check(f"{prof}: 0x40 未被破坏", bool(got & 0x40), True)
        check(f"{prof}: 低 3 位未被破坏", got & 0x07, 0x05)

    print("\n== 4) 遍历全部起始值：只有 0xA0|0x10 两位会变 ==")
    bad = []
    for start in range(256):
        for prof in ("power-saver", "balanced", "performance"):
            ec.set({0x0751: start})
            keyd.set_led_profile(prof)
            got = ec.get(0x0751)
            if (got & ~0xB0) != (start & ~0xB0):
                bad.append((start, prof, got))
    check("256x3 种组合下无关位全部保留", bad, [])

    print("\n== 5) 幂等：已是目标值时不产生写操作 ==")
    ec.set({0x0751: 0xA0})
    INVOCATIONS.clear()
    keyd.set_led_profile("power-saver")
    check("只读了一次、没有写", len(INVOCATIONS), 1)

    print("\n== 6) ACPI 报错时必须安全失败且不动寄存器 ==")
    keyd._acpi_call = lambda inv: "Error: AE_NOT_FOUND"
    ec.set({0x0751: 0x00})
    check("返回 False 而非抛异常", keyd.set_led_profile("performance"), False)
    check("寄存器未被改动", ec.get(0x0751), 0x00)
    keyd._acpi_call = ec.acpi_call

    print("\n== 7) 真实 cycle() 端到端：切档同时点亮灯 ==")
    keyd.read_profile = lambda: "balanced"
    keyd.available_order = lambda: ["power-saver", "balanced", "performance"]
    keyd.write_profile = lambda n: True
    keyd.notify = lambda n: None
    ec.set({0x0741: 0x01, 0x0751: 0x00})
    check("cycle() 返回 0", keyd.cycle(), 0)
    check("灯已切到狂暴 0x10", ec.get(0x0751), 0x10)

    print("\n== 8) 写电源模式失败时不得写灯 ==")
    keyd.write_profile = lambda n: False
    ec.set({0x0751: 0x00})
    check("cycle() 返回 1", keyd.cycle(), 1)
    check("灯未被改动", ec.get(0x0751), 0x00)

    print("\n== 9) ACPI 作用域自动探测（不同 acpi_call 写法容忍度不同）==")
    # 模拟"只有省略 _SB_ 尾下划线的 \_SB.INOU 可用"的环境
    keyd._ec_scope_cache = None
    inv_seen = []

    def only_sb_dot(inv):
        inv_seen.append(inv)
        # 只有 \_SB.INOU.ECRR 能读通，其它一律找不到句柄
        if inv.startswith("\\_SB.INOU.ECRR"):
            return "0x42"
        return "Error: AE_NOT_FOUND"

    keyd._acpi_call = only_sb_dot
    check("能选出可用作用域", keyd._ec_scope(), "\\_SB.INOU")
    check("探测次数不超过候选数",
          len(inv_seen) <= len(keyd._EC_SCOPES), True)
    n_before = len(inv_seen)
    keyd._ec_scope()
    check("第二次调用不再探测", len(inv_seen), n_before)

    # 全部不通时必须优雅退回默认作用域，且不抛异常
    keyd._ec_scope_cache = None
    keyd._acpi_call = lambda inv: "Error: AE_NOT_FOUND"
    check("全不通时退回默认作用域", keyd._ec_scope(), keyd.ACPI_DEVICE)
    check("失败也会被缓存", keyd._ec_scope_cache, "")

    # 根命名空间必须拼成 \ECRR，不能是 ".ECRR"
    check("根作用域拼出 \\ECRR", keyd._method_path("", "ECRR"), "\\ECRR")
    check("有作用域时正常拼接",
          keyd._method_path("\\_SB_.INOU", "ECRW"), "\\_SB_.INOU.ECRW")

    # 分类函数要能区分两种失败，否则诊断信息没有价值
    check("区分找不到句柄",
          keyd._classify("Error: AE_NOT_FOUND"), "error")
    check("识别 not called", keyd._classify("not called"), "notcalled")
    check("识别正常数值", keyd._classify("0x2A"), "ok")

    print("\n== 10) EC 读取失败时不误判成 0 ==")
    keyd._ec_scope_cache = keyd.ACPI_DEVICE
    keyd._acpi_call = lambda inv: "Error: AE_NOT_FOUND"
    check("ec_read 失败返回 None（不是 0）", keyd.ec_read(0x0751), None)
    keyd._acpi_call = lambda inv: "0x1FF"      # 超过 u8
    check("超范围值被拒绝", keyd.ec_read(0x0751), None)
    keyd._acpi_call = lambda inv: "0xFF"
    check("正常值被接受", keyd.ec_read(0x0751), 0xFF)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("✅ 指示灯端到端模拟全部通过")
