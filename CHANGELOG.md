# 更新记录

## 未发布

### 修复

- **`install.sh` 的进程新鲜度校验在挂起过的机器上误报**。原判据用
  `/proc/uptime`（`CLOCK_BOOTTIME`，**含**挂起时间）减 systemd 的
  `ExecMainStartTimestampMonotonic`（`CLOCK_MONOTONIC`，**不含**挂起）来算
  “进程年龄”，笔记本睡过一觉后进程年龄会虚高出一个“累计挂起时长”，于是
  刚重启的健康进程被误判成“仍在跑旧代码”（本机实测虚高约 1782s，正好等于
  累计挂起时长）。现改为比较**同一把墙钟**的绝对时刻：进程启动时刻取
  `/proc/<pid>` 目录的 `mtime`（内核把它设为进程启动时间），脚本写入时刻取
  文件 `mtime`，进程早于文件 2 秒以上才判定为旧代码。
- **`tools/diag-acpi.sh` 第 2d 步的候选表残留 ACPI 同义重复**：
  `\_SB_.INOU.ECRR` 与 `\_SB.INOU.ECRR`、`\_SB_.ECRR` 与 `\_SB.ECRR`
  在 ACPI 命名空间里是同一个对象（名字固定 4 字符、不足补下划线），
  重复试调只会对同一路径重复刷 `Cannot get handle`，与守护进程 1.5 的
  “候选去重”相矛盾。已按规范名去重为 4 个候选。

### 文档

- README 第 6 节的断言数由过时的 **212（165/37/10）** 更正为实际的
  **235（182/43/10）**。

### 说明

- 本次未改动 `mechrevo-keyd.py`（守护进程仍为 1.5-led），因此上述修复无需
  重装或 `restart` 即生效；`diag-acpi.sh` 与 README 都是按需运行/阅读。

## 1.5 — 作用域探测不再往 dmesg 里写自己的报错

### 修复

- **探测 ACPI 作用域时会自己制造内核报错**。`acpi_call` 在路径不存在时用
  `KERN_ERR` 打印 `Cannot get handle`，而 1.4 及更早的实现有**三处**会主动
  触发它：
  - 顶层循环**命中后没有 `break`**，已经找到正确路径仍把其余候选全部试完；
  - 候选表把 ACPI 意义上的**同一个对象**当成多个候选 —— ACPI 名字固定
    4 字符、不足补下划线，`\_SB` ≡ `\_SB_`、`\_SB.INOU` ≡ `\_SB_.INOU`；
  - `discover_ec()` **不写缓存**，而 `probe()` 先调它、紧接着又调
    `_ec_scope()`，后者见缓存为空便**再探测一遍** —— 报错数直接翻倍
    （真机日志里那几组 6 条 = 3 条 × 2 次，就是这么来的）。

  三者叠加，每次新进程启动可产生 3~6 条 `Cannot get handle`。现在：
  1. 优先读 sysfs 里内核给出的权威路径
     （`/sys/bus/acpi/devices/INOU0000:00/firmware_node/path` → `\_SB_.INOU`），
     它正是内核绑驱动时用的基准作用域，第一次调用就命中；
  2. 候选按 `_acpi_name_key()` 归一化去重（4 字符补下划线）；
  3. 命中即停；
  4. 探测结论写入 `_ec_scope_cache`（失败也写空串），杜绝重复探测。

  健康机型上内核报错数 **3 → 0**；只有换机型、sysfs 也读不到时才会回退到
  去重后的 3 个候选。新增测试组 23 / 23b 把这些行为全部钉住。

### 说明（非代码问题）

- 澄清 `dmesg` 里另外三类 ACPI 报错均为**固件自身**且基本无害，不是本项目
  引入的：`\_SB.ACDC.RTAC` 找不到 + `\_SB.PEP._DSM` 中止（每次睡眠唤醒一次，
  DSDT 里根本没有 `ACDC` 设备，是 AMD `UPEP` SSDT 的引用错误）、
  `AE_ALREADY_EXISTS`（固件重复定义对象）、`PNP0C02 Could not reserve`
  （AML 重复声明保留区）。三者都不影响电池/无线/亮度/调频/挂起恢复
  （已实测）。README 新增 5.4 节给出分类表，并说明彻底消除只能靠 DSDT
  覆盖、以及为什么不建议这么做。

## 1.4 — 修复指示灯全部读取失败 + 部署陷阱

### 修复

- **`acpi_call` 返回值尾部 NUL 导致所有 EC 读数被判为失败**（关键修复）。
  `acpi_call` 的 `acpi_proc_read()` 传给 `simple_read_from_buffer()` 的长度是
  *字符串长度 + 1*，即把结尾的 NUL 也返回给用户态。C 的字符串函数与 shell 的
  `$(cat ...)` 都会忽略 NUL，所以只在 Python 里暴露：`"\x00"` 不属于空白字符，
  `str.strip()` 去不掉它，于是 `int("0x19\x00", 0)` 抛异常、整个指示灯功能
  静默失效。现在在任何解析之前先清掉 NUL（`_clean_reply()`），
  并在 `_acpi_call()` 与 `_parse_int()` 里各自独立清一次。
- **`--probe` 的位域显示把 `0xA0` 当成单个位**。`0xA0` 是 bit7|bit5 两位，
  原写法 `cur & 0xA0` 在只有 bit5 置位时会误报"办公位置位"。改为整段比较
  `(cur & 0xB0) == 0xA0`，并逐位打印 `bit7/bit5/bit4`。
- **I/O 失败时丢失 errno**：原来打印 `打不开设备文件（None）`，现在打印
  实际原因（如 `Permission denied（errno=13）`）。

### 新增

- **`--probe` 自带对照实验**，把以前都塌缩成同一句"读取失败"的几种病因分开：
  - *正面*：调用 `\_SB_.INOU._STA` —— 这条路径由内核自己证明存在
    （驱动探测时调过它并拿到 `status=11`），连它都失败即为 **通路**问题；
  - *负面*：故意调用不存在的路径，`acpi_call` 必然 printk
    `Cannot get handle`，据此判断内核日志是否真的记录，以及问题是否在**路径**。
- **`--scan-led`**：把 `0xB0` 位域全部 8 种组合逐个写入并询问颜色，用于在
  其他机型上实测标定编码。只改 `0xB0`，`0x40` 全速风扇位与低 3 位风扇档位
  全程保留，结束后**无条件恢复**原值（`finally` 中做，`Ctrl-C` 亦然）。
- **`tools/diag-acpi.sh`**：root 诊断脚本，含上述对照实验、逐候选路径试调、
  以及 DSDT 反汇编以定位 `ECRR` 的真实归属。
- **CI**：`.github/workflows/tests.yml` 在 Python 3.9–3.13 上运行三套测试，
  并做 shell 语法检查。

### 修复（部署）

- **`install.sh` 用 `systemctl enable --now` 在服务已 active 时不重启**，
  导致替换了文件却仍跑旧代码（曾表现为"切档正常但灯不动"，且
  `journalctl | grep 指示灯` 一行都没有）。改为显式 `systemctl restart`。
- **`install.sh` 新增进程新鲜度校验**：文件一致 ≠ 进程一致，systemd 只在
  启动时读一次脚本。现在会比较进程已运行秒数与脚本更新时间，过期即告警。
  （用 systemd 单调时钟 + `/proc/uptime` 计算，不依赖 `ps` 或 `/proc/<pid>`。）
- **`install.sh` 新增安装结果与源码逐字节 `cmp` 校验**，防止静默装上旧文件。
- **服务加 `After=systemd-modules-load.service`**，确保启动时那次指示灯对齐
  能看到 `/proc/acpi/call`；主循环另加**自愈补对齐**，若 `acpi_call` 晚就绪
  则补做一次（只补一次）。

### 测试

212 项断言（`selftest.py` 165 / `ec_sim_test.py` 37 / `integration_test.py` 10）。
`ec_sim_test.py` 的模拟端点现在**照抄内核返回尾部 NUL 的行为**，
否则这个 bug 会假绿。

## 1.0 — 首次可用

- 修好 MECHREVO 翼龙15Pro (GM5HG0A) 的性能模式按键：内核自带 `uniwill_laptop`
  驱动的 DMI 白名单只写了 `TUXEDO`，同方准系统的 MECHREVO 贴牌机匹配失败，
  `INOU0000:00` 无驱动绑定、WMI 热键事件无人接收。用 `force=1` 强制加载。
- 新增 DE 无关的按键守护进程 `mechrevo-keyd`：读输入层 `KEY_F14`，
  循环切换 `power-profiles-daemon`，通过 `notify-send` 按 freedesktop 规范
  发通知。刻意**不** `EVIOCGRAB`（该设备还承载 KEY_RFKILL/KEY_MICMUTE 等）。
- 指示灯跟随模式：主线驱动从不写 EC `0x0751`，且 `uniwill_ec_init()` 会把
  `0x0741` 的 `ENABLE_MANUAL_CTRL` 置 1（等于声明"模式由系统接管"）
  却从不下发模式，灯因此永不变色。改用 `acpi_call` 调同一个 ACPI `ECRW`
  方法读写 `0x0751`，读-改-写只动 `0xA0|0x10`（绿=办公 / 蓝=均衡 / 紫=狂暴），
  保留 `0x40` 全速风扇位。
