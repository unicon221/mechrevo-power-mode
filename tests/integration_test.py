#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mechrevo-keyd 集成测试：用 FIFO 模拟真实输入设备，端到端验证主循环。

与 selftest.py 的区别：selftest 只测解析/循环这些纯函数；
本测试真的把 main_loop 跑起来，让它经历 select() -> os.read() ->
拆包 -> 触发切换 的完整路径，验证事件流被正确消费。

运行： python3 integration_test.py
不需要 root，也不需要真实输入设备。
"""

import importlib.util
import os
import shutil
import tempfile
import threading
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

EV_SYN, EV_KEY, EV_MSC = 0x00, 0x01, 0x04
FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(label)


def ev(etype, code, value):
    return keyd.INPUT_EVENT.pack(0, 0, etype, code, value)


class Args:
    debounce = 0.0
    dry_run = True      # 不真的改系统电源模式


tmp = tempfile.mkdtemp(prefix="keyd-it-")
fifo = os.path.join(tmp, "event-sim")
os.mkfifo(fifo)

# 让守护进程“发现”我们的 FIFO，而不是真实设备
keyd.find_device = lambda: fifo

switched = []
keyd.cycle = lambda dry_run=False: (switched.append(1), 0)[1]

args = Args()
stop = threading.Event()

print("== 启动 main_loop，监听 FIFO 模拟设备 ==")
t = threading.Thread(target=keyd.main_loop, args=(args,), daemon=True)
t.start()
time.sleep(1.0)                      # 等它打开 FIFO 并进入 select
check("守护进程已启动", t.is_alive(), True)

# 必须以写方式打开 FIFO 才会让读者的 select 就绪
print("\n== 场景1：写入一次真实的按键序列 ==")
w = os.open(fifo, os.O_WRONLY)
os.write(w, ev(EV_MSC, 4, 0xB0000) + ev(EV_KEY, keyd.KEY_F14, 1) + ev(EV_SYN, 0, 0))
os.write(w, ev(EV_KEY, keyd.KEY_F14, 0) + ev(EV_SYN, 0, 0))
time.sleep(0.5)
check("按下一次 → 切换一次", len(switched), 1)

print("\n== 场景2：只写别的键，不应触发 ==")
before = len(switched)
os.write(w, ev(EV_KEY, 247, 1) + ev(EV_SYN, 0, 0))     # KEY_RFKILL
os.write(w, ev(EV_KEY, 248, 1) + ev(EV_SYN, 0, 0))     # KEY_MICMUTE
os.write(w, ev(EV_KEY, 230, 1) + ev(EV_SYN, 0, 0))     # KEY_KBDILLUMUP
time.sleep(0.5)
check("其它热键不触发切换", len(switched), before)

print("\n== 场景3：一次写入含多次按键（快速连按）==")
before = len(switched)
blob = b""
for _ in range(3):
    blob += ev(EV_KEY, keyd.KEY_F14, 1) + ev(EV_KEY, keyd.KEY_F14, 0) + ev(EV_SYN, 0, 0)
os.write(w, blob)
time.sleep(0.5)
check("连按 3 次 → 切换 3 次", len(switched), before + 3)

print("\n== 场景4：跨 write 边界的事件能正确拼接 ==")
before = len(switched)
full = ev(EV_KEY, keyd.KEY_F14, 1)
os.write(w, full[:10])          # 先写半截
time.sleep(0.3)
check("半截事件不应触发", len(switched), before)
os.write(w, full[10:])          # 再写剩下
time.sleep(0.5)
check("补齐后触发切换", len(switched), before + 1)

print("\n== 场景5：设备消失后守护进程必须存活 ==")
os.close(w)
time.sleep(1.5)
check("写端关闭后仍存活", t.is_alive(), True)

print("\n== 收尾：SIGTERM 应能优雅退出 ==")
keyd._running = False
t.join(timeout=4)
check("已停止", t.is_alive(), False)

print("\n== 场景6：acpi_call 晚于守护进程就绪时，补做一次指示灯对齐 ==")
# 背景：acpi_call 由 systemd-modules-load 加载，时机不保证早于本服务。
# 若启动时它还没到，那次"开机对齐"会静默跳过 —— 必须能自动补上，且只补一次。
s6 = {"acpi": False, "syncs": 0, "logs": []}
_orig = {n: getattr(keyd, n) for n in
         ("ec_available", "set_led_profile", "read_profile", "find_device", "log")}
keyd.ec_available = lambda: s6["acpi"]
keyd.set_led_profile = lambda p: (s6.__setitem__("syncs", s6["syncs"] + 1), True)[1]
keyd.read_profile = lambda: "balanced"
keyd.log = lambda m: s6["logs"].append(m)
keyd.find_device = lambda: None          # 永远找不到设备，循环靠 sleep 推进
_orig_retry = keyd._sleep_retry
def _retry(_w):
    s6["acpi"] = True                    # 第一轮之后 acpi_call 才出现
    time.sleep(0.02)
    return len(s6["logs"]) < 10
keyd._sleep_retry = _retry
_orig_running = keyd._running
keyd._running = True
try:
    keyd.main_loop(args)
finally:
    keyd._running = _orig_running
    keyd._sleep_retry = _orig_retry
    for n, v in _orig.items():
        setattr(keyd, n, v)
check("acpi_call 晚到后补对齐了 1 次（不是 0 次、也不是每次都补）", s6["syncs"], 1)
check("日志说明了是补做的对齐",
      any("补做了一次对齐" in x for x in s6["logs"]), True)

shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：{FAILED}")
    raise SystemExit(1)
print("✅ 集成测试全部通过")
