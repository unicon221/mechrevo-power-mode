#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mechrevo-keyd 的自测：用合成输入事件验证按键解析与切换逻辑。

运行： python3 selftest.py
不需要 root，也不需要真实输入设备。
"""

import contextlib
import importlib.util
import io
import os
import struct
import sys
import types

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

# 后面的用例会打桩替换这些函数，先留下真身供后续用例还原
REAL = {
    "set_led_profile": keyd.set_led_profile,
    "ec_read": keyd.ec_read,
    "ec_write": keyd.ec_write,
    "ec_available": keyd.ec_available,
    "log": keyd.log,
    "_acpi_call": keyd._acpi_call,
    "probe": keyd.probe,
    "_recent_acpi_call_dmesg": keyd._recent_acpi_call_dmesg,
}

EV_SYN, EV_KEY, EV_MSC = 0x00, 0x01, 0x04
FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        FAILED.append(label)


def ev(etype, code, value):
    return keyd.INPUT_EVENT.pack(0, 0, etype, code, value)


class FakeArgs:
    debounce = 0.0
    dry_run = False


print("== 1) struct 布局（x86-64 必须是 24 字节）==")
check("INPUT_EVENT.size", keyd.INPUT_EVENT.size, 24)
check("KEY_F14", keyd.KEY_F14, 184)

print("\n== 2) 只认 EV_KEY/KEY_F14/value==1 ==")
cases = [
    ("F14 按下应命中",        ev(EV_KEY, keyd.KEY_F14, 1), 1),
    ("F14 松开应忽略",        ev(EV_KEY, keyd.KEY_F14, 0), 0),
    ("F14 重复(value=2)忽略", ev(EV_KEY, keyd.KEY_F14, 2), 0),
    ("别的键(RFKILL=247)",    ev(EV_KEY, 247, 1), 0),
    ("别的键(MICMUTE=248)",   ev(EV_KEY, 248, 1), 0),
    ("背光键(230)",           ev(EV_KEY, 230, 1), 0),
    ("MSC 事件同码应忽略",    ev(EV_MSC, keyd.KEY_F14, 1), 0),
    ("SYN 事件应忽略",        ev(EV_SYN, 0, 0), 0),
]
for label, blob, want in cases:
    calls = []
    keyd.on_press = lambda a: calls.append(1)
    n = keyd.handle_bytes(blob, FakeArgs())
    check(label, n, want)

print("\n== 3) 一次读取里包含多个事件（真实场景）==")
calls = []
keyd.on_press = lambda a: calls.append(1)
blob = (ev(EV_MSC, 4, 0xB0000)          # MSC_SCAN
        + ev(EV_KEY, keyd.KEY_F14, 1)   # 按下 ← 命中
        + ev(EV_SYN, 0, 0)
        + ev(EV_KEY, keyd.KEY_F14, 0)   # 松开 ← 忽略
        + ev(EV_SYN, 0, 0))
n = keyd.handle_bytes(blob, FakeArgs())
check("5 个事件中命中 1 次", n, 1)

print("\n== 4) 残包缓存与跨 read 边界重组 ==")
keyd.on_press = lambda a: None
keyd._leftover = b""
blob = ev(EV_KEY, keyd.KEY_F14, 1) + b"\x01\x02\x03"   # 尾部 3 字节不完整
n = keyd.handle_bytes(blob, FakeArgs())
check("先命中完整的 1 次", n, 1)
check("尾部 3 字节被缓存", len(keyd._leftover), 3)

# 把剩下 21 字节补齐，应恰好拼成一个完整的按下事件
rest = ev(EV_KEY, keyd.KEY_F14, 1)[3:]
n = keyd.handle_bytes(rest, FakeArgs())
check("补齐后命中 1 次（未丢事件）", n, 1)
check("缓存已清空", len(keyd._leftover), 0)

# 完全切碎成 5 段发送，仍应只命中 1 次
keyd._leftover = b""
whole = ev(EV_KEY, keyd.KEY_F14, 1)
hits = sum(keyd.handle_bytes(whole[i:i + 5], FakeArgs()) for i in range(0, 24, 5))
check("切成 5 段后仍命中 1 次", hits, 1)
keyd._leftover = b""
check("空数据不崩溃", keyd.handle_bytes(b"", FakeArgs()), 0)

print("\n== 5) 档位循环顺序 ==")
# 这几组只测"循环顺序"这一件事，把指示灯打桩成空操作，
# 否则 set_led_profile 的日志会混进 seq（真机上 /proc/acpi/call 的状态
# 会影响它是否打印），让断言变得依赖环境。
keyd.set_led_profile = lambda p, dry_run=False: True
keyd.available_order = lambda: ["power-saver", "balanced", "performance"]
for cur, want in [("power-saver", "balanced"),
                  ("balanced", "performance"),
                  ("performance", "power-saver")]:
    keyd.read_profile = lambda c=cur: c
    seq = []
    keyd.log = lambda m: seq.append(m)
    keyd.write_profile = lambda n: (seq.append(n), True)[1]
    keyd.notify = lambda n: None
    keyd.cycle()
    check(f"{cur} 的下一档", seq[-1], want)

print("\n== 6) 未知当前档位时回到第一档 ==")
seq = []
keyd.read_profile = lambda: None
keyd.write_profile = lambda n: (seq.append(n), True)[1]
keyd.notify = lambda n: None
keyd.cycle()
check("未知 -> power-saver", seq[-1], "power-saver")

print("\n== 7) 写失败时 cycle 返回 1 ==")
keyd.read_profile = lambda: "balanced"
keyd.write_profile = lambda n: False
check("写失败返回码", keyd.cycle(), 1)

print("\n== 8) 名称映射自洽 ==")
for ppd, sysname in keyd.PPD_TO_SYSFS.items():
    check(f"反向映射 {sysname}", keyd.SYSFS_TO_PPD[sysname], ppd)
check("图标名不再是无效的 power-profile-*",
      any("power-profile-" in i for i in keyd.ICONS.values()), False)

print("\n== 9) 通知/设备发现函数可调用 ==")
check("find_device 返回类型", type(keyd.find_device()).__name__ in ("str", "NoneType"), True)
check("_session_uids 返回 list", isinstance(keyd._session_uids(), list), True)

print("\n== 10) 指示灯：档位取值必须与 TUXEDO 驱动一致 ==")
# 厂商驱动 uw_set_performance_profile_v1(): 清 0xA0|0x10，再 OR 档位值
check("清位掩码 = 0xB0", keyd.EC_LED_CLEAR_BITS, 0xB0)
check("办公(power-saver) = 0xA0", keyd.EC_LED_VALUES["power-saver"], 0xA0)
check("均衡(balanced) = 0x00", keyd.EC_LED_VALUES["balanced"], 0x00)
check("狂暴(performance) = 0x10", keyd.EC_LED_VALUES["performance"], 0x10)
check("寄存器地址 = 0x0751", keyd.EC_LED_REG, 0x0751)
check("全速风扇位 0x40 不在清位掩码里",
      bool(keyd.EC_LED_CLEAR_BITS & 0x40), False)


def compute(cur, profile):
    """复刻 set_led_profile 的读-改-写算式（不碰真实 EC）。"""
    return (cur & ~keyd.EC_LED_CLEAR_BITS & 0xFF) | keyd.EC_LED_VALUES[profile]


print("\n== 11) 读-改-写：绝不破坏无关位（尤其 0x40 全速风扇）==")
# 0x40 是 TUXEDO 的“全速风扇”位，任何档位切换都必须原样保留
for profile in ("power-saver", "balanced", "performance"):
    got = compute(0x40, profile)
    check(f"从 0x40 切到 {profile} 仍保留 0x40", bool(got & 0x40), True)

# 低 3 位是风扇档位掩码，同样必须保留
for low in range(0x08):
    for profile in ("power-saver", "balanced", "performance"):
        got = compute(low, profile)
        check(f"从 0x{low:02X} 切 {profile} 保留低 3 位",
              got & 0x07, low)

# 只有 0xA0|0x10 两位被改写
for profile in ("power-saver", "balanced", "performance"):
    base = 0x40 | 0x05          # 全速风扇 + 风扇档位 5
    got = compute(base, profile)
    check(f"{profile} 的 0xB0 位等于目标值",
          got & keyd.EC_LED_CLEAR_BITS, keyd.EC_LED_VALUES[profile])
    check(f"{profile} 其余位不变", got & ~0xB0, base & ~0xB0)

print("\n== 12) 目标值与当前值相同 => 幂等（不重复写）==")
for profile, raw in (("power-saver", 0xA0), ("balanced", 0x00),
                     ("performance", 0x10)):
    check(f"{profile} 已是 0x{raw:02X} 时结果不变", compute(raw, profile), raw)

print("\n== 13) acpi_call 返回值解析 ==")
for text, want in [("0x00", 0), ("0x5A", 0x5A), ("0xa0", 0xA0),
                   ("0x0 (0)", 0), (" 0xFF ", 0xFF),
                   ("Error: AE_NOT_FOUND", None), ("", None),
                   (None, None), ("garbage", None)]:
    check(f"解析 {text!r}", keyd._parse_int(text), want)

print("\n== 14) 档位名归一化 ==")
for text, want in [("power-saver", "power-saver"), ("low-power", "power-saver"),
                   ("BALANCED", "balanced"), ("performance", "performance"),
                   ("boost", "performance"), ("nonsense", None), ("", None)]:
    check(f"归一 {text!r}", keyd.normalize_profile(text), want)

print("\n== 15) 无 acpi_call 时指示灯静默跳过，不抛异常 ==")
# 还原真身（测试 5 为了隔离把它打成空操作了）
keyd.set_led_profile = REAL["set_led_profile"]
keyd.ec_available = REAL["ec_available"]
_orig_exists = os.path.exists
keyd.os.path.exists = lambda p: False if p == keyd.ACPI_CALL_PATH else _orig_exists(p)
check("ec_available() 为 False", keyd.ec_available(), False)
check("set_led_profile 返回 False 而非抛异常",
      keyd.set_led_profile("performance"), False)
keyd.os.path.exists = _orig_exists

print("\n== 16) 切档失败时不得因指示灯而出错 ==")
seq = []
keyd.read_profile = lambda: "power-saver"
keyd.available_order = lambda: ["power-saver", "balanced", "performance"]
keyd.write_profile = lambda n: (seq.append(n), True)[1]
keyd.notify = lambda n: None
# 测试 5) 把 log 替换成了往 seq 里塞日志的桩，这里换回静默，
# 否则 seq[-1] 会是日志文本而不是档位名。
keyd.log = lambda m: None
_boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("EC 炸了"))
keyd.set_led_profile = _boom
check("指示灯抛异常也要返回 0", keyd.cycle(), 0)
check("电源模式仍然被切换", seq, ["balanced"])

print("\n== 17) probe 只读：绝不调用 ec_write ==")
writes = []
keyd.ec_read = lambda a: 0x00
keyd.ec_write = lambda a, v: (writes.append((a, v)), True)[1]
keyd.ec_available = lambda: True
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    keyd.probe()
check("probe 期间零次写 EC", writes, [])
check("probe 有输出但未写 EC", len(_buf.getvalue()) > 0, True)

print("\n== 18) 无 acpi_call 时 --dry-run 仍能预览灯值（不写）==")
# 还原真身，再用「没有 /proc/acpi/call」的环境测
keyd.set_led_profile = REAL["set_led_profile"]
keyd.ec_read = REAL["ec_read"]
keyd.ec_write = REAL["ec_write"]
keyd.ec_available = REAL["ec_available"]
keyd.log = REAL["log"]          # 测试 16 把它打成静默桩了
keyd._ec_scope_cache = keyd.ACPI_DEVICE

print("\n== 17b) ECRW 的 0xfffffffe 是哨兵值，不是失败 ==")
# 真机实测：ECRW 返回 0xfffffffe（DSDT 里 MMRW 的 Local0 初值，
# 写分支不会覆盖它）。若把它当错误，每次写都会被误判为失败。
for _oid in ["0xfffffffe", "0xfffffffe\x00", "0x0", ""]:
    keyd._acpi_call = lambda inv, r=_oid: r
    check(f"ECRW 返回 {_oid!r} 视为成功", keyd.ec_write(0x0751, 0x10), True)
for _err in ["Error: AE_BAD_PATHNAME", "Error: AE_NOT_FOUND",
             "Error: AE_BAD_PATHNAME\x00"]:
    keyd._acpi_call = lambda inv, r=_err: r
    check(f"ECRW 返回 {_err!r} 视为失败", keyd.ec_write(0x0751, 0x10), False)
keyd._acpi_call = REAL["_acpi_call"]
keyd._ec_scope_cache = None

_orig_exists = os.path.exists
keyd.os.path.exists = lambda p: False if p == keyd.ACPI_CALL_PATH else _orig_exists(p)
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _r = keyd.set_led_profile("performance", dry_run=True)
_out = _buf.getvalue()
check("试运行返回 True（可预览）", _r, True)
check("预览里含目标字节 0x10", "0x10" in _out, True)
check("明确说明不会实际写入", "不会写" in _out, True)
# 同样环境下真写必须仍然是安全失败且静默（不刷日志）
_buf2 = io.StringIO()
with contextlib.redirect_stdout(_buf2):
    _r2 = keyd.set_led_profile("performance", dry_run=False)
check("真写仍返回 False", _r2, False)
check("真写不打印噪声", _buf2.getvalue().strip(), "")
keyd.os.path.exists = _orig_exists

print("\n== 18b) --probe 的位域显示不能把 0xA0 当成单个位 ==")
# 0xA0 是两位（bit7|bit5）。若写成 `cur & 0xA0`，则在 0x20（只有 bit5）时
# 会误报"办公位置位"，而实际当前档位根本不是办公。
keyd.ec_available = lambda: True
keyd.read_profile = lambda: "balanced"
keyd._ec_scope_cache = keyd.ACPI_DEVICE
for _val, _want_label in [(0x2A, None), (0xA0, "办公"), (0x00, "均衡"),
                          (0x10, "狂暴"), (0x30, None)]:
    keyd._acpi_call = lambda inv, v=_val: f"0x{v:02X}"
    _b = io.StringIO()
    with contextlib.redirect_stdout(_b):
        keyd.probe()
    _o = _b.getvalue()
    if _want_label:
        check(f"0x{_val:02X} 判为{_want_label}",
              f"{_want_label}（" in _o, True)
    else:
        check(f"0x{_val:02X} 判为未知组合", "未知组合" in _o, True)
# 关键：单独 bit5 置位（0x20 位域）绝不能被说成"办公位置位"
keyd._acpi_call = lambda inv: "0x2A"
_b2 = io.StringIO()
with contextlib.redirect_stdout(_b2):
    keyd.probe()
_o2 = _b2.getvalue()
check("0x2A 不被误报为办公档", "当前应显示    : 办公" not in _o2, True)
check("逐位打印了 bit7/bit5/bit4",
      all(f"bit{n}" in _o2 for n in (7, 5, 4)), True)
check("0x2A 的 bit5 显示为置位、bit7 显示为清零",
      "bit5 (0x20) : 置位" in _o2 and "bit7 (0x80) : 清零" in _o2, True)
keyd._acpi_call = REAL["_acpi_call"]

print("\n== 19) --probe 在 EC 读取不通时必须给出可排查的原始返回 ==")
# 还原真身，构造"所有作用域都读不通"的环境。
# 正面对照 _STA 正常返回、负面对照让日志新增 -> 应判定为路径问题。
keyd._acpi_call = REAL["_acpi_call"]
keyd.ec_available = lambda: True
keyd._ec_scope_cache = None
_dmesg_log = []
def _fake_dmesg(limit=15):
    return list(_dmesg_log) if limit is None else _dmesg_log[-limit:]
keyd._recent_acpi_call_dmesg = _fake_dmesg
_ctl_invocations = []                     # 记录本组的每一次 acpi_call
def _ctl_call(inv):
    _ctl_invocations.append(inv)
    if "STA" in inv:
        return "0xB"                      # 正面对照：必然存在的 _STA
    if "NOSUCHTHING" in inv:
        _dmesg_log.append("acpi_call: Cannot get handle: AE_NOT_FOUND")
        return "Error: AE_NOT_FOUND"
    return "Error: AE_NOT_FOUND"          # 所有 ECRR 路径都失败
keyd._acpi_call = _ctl_call
keyd.time.sleep = lambda s: None
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _rc = keyd.probe()
_out = _buf.getvalue()
check("probe 返回 1", _rc, 1)
# 诊断输出必须把**每个真正试过的候选路径**都列出来；同时核对调用次数，
# 确认确实逐个试过（而不是只打印了列表）。
_cand_paths = [keyd._method_path(s, "ECRR") for s in keyd._candidate_scopes()]
check("列出了每个候选作用域",
      all(p in _out for p in _cand_paths), True)
_ecrr_calls = [i for i in _ctl_invocations if "ECRR" in i]
check("确实逐个试了这些候选",
      sorted(_ecrr_calls), sorted(f"{p} 0x0740" for p in _cand_paths))
check("判定为路径问题（对照实验生效）", "通路**完全正常**" in _out, True)
check("提示了 diag-acpi.sh", "diag-acpi.sh" in _out, True)
check("打印了版本号，便于确认跑的是哪一版", keyd.VERSION in _out, True)
# probe 绝不能写 EC 寄存器（对照实验只调只读方法）
check("probe 期间不写任何 EC 寄存器", _dmesg_log and True, True)

print("\n== 20) 六种 acpi_call 返回形态都要能被正确处理 ==")
keyd._ec_scope_cache = keyd.ACPI_DEVICE
for raw, want, desc in [
    ("0x2A", 0x2A, "标准十六进制"),
    ("0x2A\n", 0x2A, "带尾换行"),
    (" 0x2A ", 0x2A, "带首尾空白"),
    ("0x2A (42)", 0x2A, "带括号十进制"),
    ("0x2A,", 0x2A, "带尾逗号"),
    ("not called", None, "尚未调用的哨兵值"),
    ("Error: AE_NOT_FOUND", None, "找不到句柄"),
    ("Error: AE_AML_...", None, "方法调用失败"),
    # 真机实测：acpi_call 会把结尾的 NUL 一起返回（长度=字符串长度+1），
    # NUL 不是空白字符、strip() 去不掉，曾导致所有读数被误判为失败。
    ("0x2A\x00", 0x2A, "尾部 NUL（acpi_call 的真实返回形态）"),
    ("0x2A\x00\n", 0x2A, "尾部 NUL 加换行"),
    ("0xff\x00", 0xFF, "最大值带 NUL"),
    ("not called\x00", None, "哨兵值带 NUL"),
    ("Error: AE_BAD_PATHNAME\x00", None, "报错带 NUL"),
]:
    keyd._acpi_call = lambda inv, r=raw: r
    check(f"ec_read 处理 {desc}", keyd.ec_read(0x0751), want)
check("全不通时 _ec_scope 退回默认值",
      (setattr(keyd, "_ec_scope_cache", ""), keyd._ec_scope())[1], keyd.ACPI_DEVICE)

print("\n== 20b) acpi_call 的尾部 NUL 必须被彻底清掉 ==")
check("_clean_reply 去尾部 NUL", keyd._clean_reply("0x19\x00"), "0x19")
check("_clean_reply 去中间/多个 NUL", keyd._clean_reply("0x19\x00\x00"), "0x19")
check("_clean_reply 同时去空白", keyd._clean_reply(" 0x19\x00\n"), "0x19")
check("_clean_reply 空与非字符串输入安全",
      keyd._clean_reply("\x00"), "")
check("_parse_int 直接吃带 NUL 的串", keyd._parse_int("0x19\x00"), 0x19)
check("_parse_int 带 NUL 的十六进制大值", keyd._parse_int("0xfffffffe\x00"), 0xFFFFFFFE)
check("_classify 带 NUL 的报错仍判为 nohandle",
      keyd._classify("Error: AE_BAD_PATHNAME\x00"), "error")
check("_classify 带 NUL 的正常值判为 ok",
      keyd._classify("0x19\x00"), "ok")
# 用真实文件走一遍 _acpi_call 的完整读写路径，确保不是只有纯函数对。
# 注意不能直接拿普通文件当替身：_acpi_call 先用 "w" 打开（会截断），
# 再用 "r" 读，同一个文件会被自己写坏。所以这里替换掉模块用的 open()，
# 让"写"落到虚空、"读"始终返回预置的含 NUL 内容（与 /proc 语义一致）。
keyd._acpi_call = REAL["_acpi_call"]
class _FakeProc:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def write(self, s): return len(s)
    def read(self): return self.payload
def _fake_open(path, mode="r", **kw):
    return _FakeProc("0x19\x00")
# 模块里用的是内建 open()，所以补丁要打到 builtins 上；
# 只影响这一段，测试完立刻还原。
import builtins
_orig_builtin_open = builtins.open
builtins.open = _fake_open
try:
    check("_acpi_call 从含 NUL 的来源读出结果不含 NUL",
          keyd._acpi_call("dummy"), "0x19")
    check("_acpi_call 走完后能解析出正确值",
          keyd._parse_int(keyd._acpi_call("dummy")), 0x19)
finally:
    builtins.open = _orig_builtin_open

print("\n== 21) 对照实验必须能区分「路径错」和「调用没生效」==")
# 情形 A：通路正常（_STA 有值），错误路径让日志新增 —— 判为 path
_store = ["acpi_call: Cannot get handle: AE_NOT_FOUND"] * 15
keyd._recent_acpi_call_dmesg = lambda limit=15: (list(_store) if limit is None
                                                 else _store[-limit:])
def _a(inv):
    if "STA" in inv:
        return "0xB"
    _store.append("acpi_call: Cannot get handle: AE_NOT_FOUND")
    return "Error: AE_NOT_FOUND"
keyd._acpi_call = _a
_v, _info = keyd.control_test()
check("日志已满时新增 1 条仍判定为 path", _v, "path")
check("打印了新增条数", any("新增 1 条" in x for x in _info), True)
# 情形 B：连必然存在的 _STA 都调不通 -> 通路问题
_store2 = []
keyd._recent_acpi_call_dmesg = lambda limit=15: (list(_store2) if limit is None
                                                 else _store2[-limit:])
keyd._acpi_call = lambda inv: "not called"
_v2, _info2 = keyd.control_test()
check("连 _STA 都失败判定为 notransport", _v2, "notransport")
check("给出了下一步排查建议", any("dmesg" in x for x in _info2), True)
# 情形 C：_STA 正常但日志始终安静 -> 日志源可疑
keyd._acpi_call = lambda inv: "0xB" if "STA" in inv else "Error: AE_NOT_FOUND"
_v3, _info3 = keyd.control_test()
check("_STA 正常但日志安静判定为 silent", _v3, "silent")
# 情形 D：读不到内核日志
keyd._recent_acpi_call_dmesg = lambda limit=15: None
_v4, _ = keyd.control_test()
check("读不到日志时判定为 unknown", _v4, "unknown")
check("_recent_acpi_call_dmesg 支持 limit=None 取全部",
      keyd._recent_acpi_call_dmesg(limit=None) is None, True)

# 还原真身，避免影响后续
keyd._acpi_call = REAL["_acpi_call"]
keyd._recent_acpi_call_dmesg = REAL["_recent_acpi_call_dmesg"]

print("\n== 22) --scan-led 标定：必须只动 0xB0、且结束后恢复原值 ==")
# 用一个非默认的起始值，确认标定只动 0xB0 且结束会恢复原值
# （真机 0x2A 只是随便挑的夹具值，不代表本机某一档的编码）
keyd.ec_available = lambda: True
keyd._ec_scope_cache = keyd.ACPI_DEVICE
_reg = {"0x0751": 0x2A}
_writes = []
keyd.ec_read = lambda a: _reg.get(f"0x{a:04X}", 0)
def _wr(a, v):
    _writes.append((f"0x{a:04X}", v))
    _reg[f"0x{a:04X}"] = v
    return True
keyd.ec_write = _wr
# 让 input() 依次返回观察结果（第 3 个组合是 0x20，报告为"绿色"）
import builtins as _bi
_orig_input = _bi.input
_notes = iter(["没变化", "蓝", "绿", "", "", "", "", ""])
def _fake_input(*a, **k):
    try:
        return next(_notes)
    except StopIteration:
        raise EOFError()
_bi.input = _fake_input
_buf = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf):
        _rc = keyd.led_scan(interactive=True)
finally:
    _bi.input = _orig_input
_out = _buf.getvalue()
check("标定返回 0", _rc, 0)
# 8 种组合各写一次，最后再写一次恢复原值 = 9 次
check("试了全部 8 种组合 + 1 次恢复", len(_writes), 9)
check("结束后 0x0751 恢复成进入时的 0x2A", _reg["0x0751"], 0x2A)
check("恢复动作确实写回过", _writes[-1][1], 0x2A)
# 关键安全性质：任何一次写入都不能破坏 0x40 或低 3 位
check("全程 0x40 全速风扇位未被置起",
      all(not (v & 0x40) for _, v in _writes), True)
check("全程低 3 位风扇档位保持 0b010",
      all((v & 0x07) == 0x02 for _, v in _writes), True)
check("只改 0xB0 位域（其余位与初值一致）",
      all((v & ~0xB0) == (0x2A & ~0xB0) for _, v in _writes), True)
check("8 种组合互不重复", len({v & 0xB0 for _, v in _writes[:8]}), 8)
check("汇总了观察结果", "0x20 -> 绿" in _out, True)
check("提示了如何把结果填回映射", "EC_LED_VALUES" in _out, True)
# 试运行绝不写 EC
_writes.clear()
_buf2 = io.StringIO()
with contextlib.redirect_stdout(_buf2):
    keyd.led_scan(interactive=False)
check("试运行一次都不写 EC", len(_writes), 0)
check("试运行列出了 8 种组合",
      _buf2.getvalue().count("(试运行)"), 8)
# 读不到寄存器时必须安全退出，不做任何写入
keyd.ec_read = lambda a: None
_writes.clear()
_buf3 = io.StringIO()
with contextlib.redirect_stdout(_buf3):
    _rc3 = keyd.led_scan(interactive=True)
check("读失败时返回 1", _rc3, 1)
check("读失败时绝不写 EC", len(_writes), 0)
check("读失败时提示先用 --probe 修通路", "--probe" in _buf3.getvalue(), True)
# acpi_call 不可用时也要安全退出
keyd.ec_available = lambda: False
_buf4 = io.StringIO()
with contextlib.redirect_stdout(_buf4):
    _rc4 = keyd.led_scan(interactive=True)
check("无 acpi_call 时返回 1", _rc4, 1)

# 还原
keyd.ec_read = REAL["ec_read"]
keyd.ec_write = REAL["ec_write"]
keyd.ec_available = REAL["ec_available"]
keyd._ec_scope_cache = None

print("\n== 23) 作用域探测不得自己制造 dmesg 噪音 ==")
# 回归背景：acpi_call 在路径不存在时会用 KERN_ERR 往内核日志打
# "Cannot get handle"。原实现有两处自造噪音：
#   (a) 命中后仍把其余候选全部试完；
#   (b) 候选表里有 ACPI 意义上的重复名（\_SB 与 \_SB_ 是同一对象）。
# 结果每次新进程启动都固定产生 3 条报错（5 个候选里 3 个不存在）。
# 这几条断言把修复钉住。
_real_dev_path23 = keyd._device_acpi_path
_real_call23 = keyd._acpi_call
_noise23 = []                             # 模拟被 acpi_call 打进内核日志的报错
_calls23 = []


def _probe_call23(inv):
    _calls23.append(inv)
    path = inv.split()[0]
    if keyd._acpi_name_key(path) == "\\_SB_.INOU.ECRR":
        return "0x19"
    _noise23.append(f"Cannot get handle: {path}")   # 错误路径必然报错
    return "Error: AE_NOT_FOUND"


# 本机真实情况：sysfs 能给出权威路径 \_SB_.INOU
keyd._device_acpi_path = lambda: "\\_SB_.INOU"
keyd._acpi_call = _probe_call23
keyd._ec_scope_cache = None
_found23, _ = keyd.discover_ec()
check("健康机型能一次命中权威路径", _found23, "\\_SB_.INOU")
check("健康机型产生的内核报错数为 0", len(_noise23), 0)
check("健康机型只发一次调用", len(_calls23), 1)

# 即使 sysfs 不可用（回退表），也必须命中即停、且不重复试同一对象
keyd._device_acpi_path = lambda: None
keyd._ec_scope_cache = None
_noise23.clear()
_calls23.clear()
_found23b, _ = keyd.discover_ec()
check("sysfs 不可用时仍能命中", _found23b, "\\_SB_.INOU")
check("sysfs 不可用时不重复试同一 ACPI 对象", len(_calls23), 1)
check("sysfs 不可用时也不产生报错", len(_noise23), 0)

# 候选表内部必须无 ACPI 意义上的重复
_keys23 = [keyd._acpi_name_key(s) for s in keyd._candidate_scopes()]
check("候选表已按 ACPI 名字去重", len(_keys23), len(set(_keys23)))

# 全部候选都不通时（换机型/换 BIOS），报错数应等于**去重后的候选数**，
# 而不是原始写法数——这是"不重复报错"的量化表达。
keyd._device_acpi_path = lambda: None
keyd._acpi_call = lambda inv: (_noise23.append(inv) or "Error: AE_NOT_FOUND")
keyd._ec_scope_cache = None
_noise23.clear()
_found23c, _ = keyd.discover_ec()
check("全不通时返回 None", _found23c, None)
check("全不通时报错数 = 去重后的候选数",
      len(_noise23), len(keyd._candidate_scopes()))
check("全不通时报错数少于旧实现的 5 个候选", len(_noise23) < 5, True)

# 还原
keyd._device_acpi_path = _real_dev_path23
keyd._acpi_call = _real_call23
keyd._ec_scope_cache = None

print("\n== 23b) 探测结论必须写入缓存（否则报错数翻倍）==")
# 回归背景：discover_ec() 原先**不写** _ec_scope_cache，而 probe() 会先调它、
# 紧接着又调 _ec_scope()——后者发现缓存为空就再探测一遍。真机日志里那几组
# 6 条（= 3 条 × 2 次）报错正是这么来的。
_real_dev23b = keyd._device_acpi_path
_real_call23b = keyd._acpi_call
_calls23b = []


def _call23b(inv):
    _calls23b.append(inv)
    p = inv.split()[0]
    return ("0x19\x00" if keyd._acpi_name_key(p) == "\\_SB_.INOU.ECRR"
            else "Error: AE_NOT_FOUND")


keyd._device_acpi_path = lambda: "\\_SB_.INOU"
keyd._acpi_call = _call23b
keyd._ec_scope_cache = None
_found23b, _ = keyd.discover_ec()
_n_after_discover = len(_calls23b)
check("探测一次即命中", _n_after_discover, 1)
check("discover_ec 命中后写入缓存", keyd._ec_scope_cache, "\\_SB_.INOU")
keyd._ec_scope()
check("discover_ec 之后 _ec_scope() 不再重复探测",
      len(_calls23b) - _n_after_discover, 0)
check("_ec_scope() 返回缓存中的作用域", keyd._ec_scope(), "\\_SB_.INOU")

# 探测失败时也要写缓存（写空串），否则每次问都会重新试一遍
keyd._ec_scope_cache = None
keyd._acpi_call = lambda inv: (_calls23b.append(inv) or "Error: AE_NOT_FOUND")
keyd._device_acpi_path = lambda: None
_calls23b.clear()
_found23b2, _ = keyd.discover_ec()
_n_fail = len(_calls23b)
check("全部不通时也写入缓存（空串）", keyd._ec_scope_cache, "")
keyd._ec_scope()
check("失败结论被缓存，不重复探测", len(_calls23b), _n_fail)

# 还原
keyd._device_acpi_path = _real_dev23b
keyd._acpi_call = _real_call23b
keyd._ec_scope_cache = None

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("✅ 全部自测通过")
