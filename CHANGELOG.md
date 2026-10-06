# 更新记录

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
- **CI**：在 Python 3.9–3.13 上运行三套测试与 shell 语法检查。

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
