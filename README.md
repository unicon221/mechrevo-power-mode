# MECHREVO 翼龙15Pro (GM5HG0A) 性能模式按键修复

[![tests](https://github.com/unicon221/mechrevo-power-mode/actions/workflows/tests.yml/badge.svg)](https://github.com/unicon221/mechrevo-power-mode/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![platform](https://img.shields.io/badge/platform-Arch%20Linux%20%7C%20systemd-blue)

让电源键旁边的 **办公 / 均衡 / 狂暴** 键真正生效，**且不依赖任何桌面环境**
（KDE、GNOME、Sway、Hyprland、Xfce 乃至纯 TTY 行为一致），
并让**按键旁边那盏三色指示灯跟随模式变化**（绿=办公 / 蓝=均衡 / 紫=狂暴）。

## 快速开始

```bash
git clone https://github.com/unicon221/mechrevo-power-mode.git
cd mechrevo-power-mode
sudo ./install.sh --with-led        # --no-led 则只要切档、不要指示灯
```

装完按一下性能模式键即可。卸载 `sudo ./install.sh --undo`，
查状态 `sudo ./install.sh --status`。细节见第三节，
指示灯不亮/不变见 5.1 与第七节故障排查。

> **适用范围**：本项目针对 MECHREVO 翼龙15Pro（`GM5HG0A`，同方 TongFang
> 准系统）开发并实测。**其他同方准系统的贴牌机（MECHREVO / Hasee 等）很可能
> 同样适用** —— 按键修复靠 `force=1`，指示灯靠 ACPI 方法而非 DMI 白名单，
> 都没有写死机型。但 EC 寄存器地址与颜色编码可能不同，请先用 `--probe`
> 只读确认，再用 `--scan-led` 标定。

## 一、问题原因

这台机器的性能模式键是**厂商热键**，不是操作系统功能键。按下时 EC（嵌入式控制器）
通过 WMI 上报一个事件码，必须由对应的 Linux 内核驱动接收并转换成输入事件；
没有驱动接收，按键就完全没有反应。

本机实际情况（已逐项核实）：

| 项目 | 状态 |
|---|---|
| 机型 | MECHREVO `Yilong15ProSeriesGM5HG0A`，主板 `GM5HG0A`（同方 TongFang 准系统，`ct10`，`pfaHPT`） |
| ACPI 设备 `INOU0000:00` | 存在，但**原先无驱动绑定** |
| WMI 热键设备 `ABBC0F6A` … `ABBC0F72` | 全部存在，**原先全部未绑定驱动** |
| 内核自带 `uniwill_laptop` 模块 | 已编译（`CONFIG_UNIWILL_LAPTOP=m`），但**原先永远不加载** |
| 事件输入设备 | `/proc/bus/input/devices` 里原先**没有** Uniwill 热键设备 |

**根本原因**：内核自带 `uniwill_laptop` 驱动的 DMI 白名单里，厂商字段只写了
`TUXEDO`（54 条别名全部要求 `svn*TUXEDO*`）。这台机器是同一套同方准系统贴的
**MECHREVO** 牌，因此 `uniwill_init()` 匹配失败、直接返回 `-ENODEV`，
`INOU0000:00` 一直没有驱动 → 热键 WMI 事件无人接收 → **按键无反应**。

同一套准系统的 `GMxHGxx` 机型在内核里**其实已经有完整支持**
（`tux_featureset_4_nvidia_descriptor`），只是被 `TUXEDO` 这个厂商名挡住了。

### 1.1 指示灯为什么也不亮/不变

按键修好、切档也生效之后，**按键旁边那盏灯仍然不动**，这是第二个独立问题。

WMI 热键事件码 `0xB0 (UNIWILL_OSD_PERFORMANCE_MODE_TOGGLE)` 在主线驱动里只是
被翻译成 `KEY_F14` 上报给用户空间，**驱动自己不碰模式灯**。模式灯由 EC 直接驱动，
对应寄存器 `0x0751`，而主线 `uniwill_laptop` **从来没有写过它**：

- `EC_ADDR_MANUAL_FAN_CTRL 0x0751` 这个宏在驱动源码里**只有 `#define` 一处，
  零处使用**（`grep -n EC_ADDR_MANUAL_FAN_CTRL` 只返回定义行）。
- `0x0751` 也**不在** `uniwill_writeable_reg()` 白名单里，所以没有任何 sysfs
  路径能写到它。
- 更关键的是，`uniwill_ec_init()` 会把 `0x0741 (AP_OEM)` 的
  `ENABLE_MANUAL_CTRL` 位置 1 —— 等于告诉 EC "模式改由系统接管"，
  然后**一个模式都没有下发**。EC 于是始终停在开机时那一档，灯自然不跟着变。

这正好对应厂商驱动里单独命名的一类机型
`uniwill_profile_v1_three_profs_leds_only`，注释写得很明白：
*"Devices where profile mainly controls power profile LED status"*
（这类机型切档主要就是切指示灯颜色）。而 `leds_only` **没有被并进**
`uniwill_profile_v1`，所以连厂商驱动对这类机型也只写模式寄存器、不碰风扇曲线。

## 二、澄清四个容易混淆的层

性能模式按键失效**不**等于"性能没变化"。本机实际有三层独立机制：

1. **厂商热键层**（本次要修的）—— 按键 → EC → WMI 事件 → 驱动 → 输入事件。
2. **ACPI `platform_profile` 层** —— 由 `amd-pmf` 提供，工作正常，
   `choices = low-power balanced performance`。
3. **CPU 层**（`amd-pstate-epp`）—— governor、EPP、频率控制工作正常。

再加上第 4 层：**EC 模式灯 `0x0751`** —— 它和前两层完全无关。
`amd-pmf` 只管 CPU 策略，根本不知道这盏灯的存在；所以只切
`platform_profile` 永远不会让灯变色。

已实测第 2、3 层随 `powerprofilesctl` 联动生效：

| profile | EPP | governor | max freq | platform_profile |
|---|---|---|---|---|
| `power-saver` | `power` | `powersave` | 3801000 | `low-power` |
| `balanced` | `balance_performance` | `powersave` | 5137904 | `balanced` |
| `performance` | `performance` | `performance` | 5137904 | `performance` |

## 三、修复方案

### 推荐方案：一条命令搞定（驱动 + 守护进程 + 指示灯）

```bash
sudo ./install.sh              # 会询问是否安装 acpi_call 以启用指示灯
# 或明确指定：
sudo ./install.sh --with-led   # 直接装上 acpi_call，启用指示灯
sudo ./install.sh --no-led     # 只要切档，不要指示灯功能
```

装完**按一下性能模式键就会切换、灯也跟着变色**，无需在任何桌面环境里再配置快捷键。

安装脚本做四件事：

1. **修好按键**：写 `/etc/modprobe.d/90-mechrevo-uniwill.conf`
   （`options uniwill_laptop force=1` + 屏蔽抢占同一批 GUID 的 `eeepc_wmi`、`msi_wmi_platform`）
   与 `/etc/modules-load.d/uniwill-laptop.conf`，并立即 `modprobe`。
2. **装 udev 规则**：`/etc/udev/rules.d/72-mechrevo-hotkeys.rules`，
   给热键设备加 `uaccess`，让普通用户也能直接 `evtest` 验证（非必需）。
3. **启用指示灯**（可选）：安装 `acpi_call` 并写
   `/etc/modules-load.d/acpi_call.conf` 让它开机自动加载。
4. **装守护进程**：`/usr/local/bin/mechrevo-keyd` +
   `/etc/systemd/system/mechrevo-keyd.service`，开机自启、崩溃自动重启。

卸载：`sudo ./install.sh --undo`　　查状态：`sudo ./install.sh --status`

### 指示灯是怎么写进去的（以及为什么这样是安全的）

模式灯寄存器 `0x0751` 由厂商驱动以固定算法写入，本方案**逐字复刻**该算法
（TUXEDO `uw_set_performance_profile_v1()`）：

```
current = 读(0x0751)
next    = current & ~(0xA0 | 0x10)      # 只清这两位（= 清 0xB0）
next   |= { power-saver:0xA0, balanced:0x00, performance:0x10 }[档位]
写(0x0751, next)
```

三点关键安全性质：

- **只改 `0xA0|0x10` 两位**，其余位逐位保留。`0x40` 是厂商驱动的"全速风扇"位、
  低 3 位是风扇档位掩码 —— 都不会被碰到。**绝不整字节覆盖**。
- **走的是内核既有通路**：`acpi_call` 调用 ACPI 方法 `\_SB_.INOU.ECRW`，
  和 `uniwill_laptop` 驱动内部 `uniwill_ec_reg_write()` 调用的
  **是同一个 ACPI 方法**。没有绕过内核，也没有新增权限模型、不需要
  `CAP_SYS_RAWIO`、不需要 DSDT 覆盖、不需要 DKMS。
- **失败自动降级**：没装/没加载 `acpi_call`（没有 `/proc/acpi/call`）时，
  该功能静默关闭，只切电源模式 —— 行为与旧版完全一致。

### 为什么 `force=1` 是安全的

本机 Secure Boot 已关闭（`/sys/kernel/security/lockdown` 显示 `[none]`），
`CONFIG_MODULE_SIG_FORCE` 未设置，所以 `force` 这个 `module_param_unsafe`
参数可以正常使用。`force` 只是跳过 DMI 白名单检查，让驱动按
"支持全部特性"加载；**不修改 BIOS、不碰 DSDT**。
（指示灯功能会写 EC 的 1 个寄存器，但如上所述只动 2 个位，且算法与厂商一致。）

### 按键如何变成切换动作

内核驱动的按键映射表里明确有：

```c
{ KE_KEY, UNIWILL_OSD_PERFORMANCE_MODE_TOGGLE, { KEY_F14 }},
```

即性能模式键（WMI 事件码 `0xB0`）会被上报为 **`KEY_F14`（184）**。
`mechrevo-keyd` 直接读取该输入设备的事件流，命中
`EV_KEY` / `KEY_F14` / `value==1`（按下）后循环切换：
**办公 → 均衡 → 狂暴 → 办公**。

## 四、为什么不用桌面环境的快捷键绑定

这是本方案的核心设计选择，也是它能在**任意 DE** 下工作的原因。

| | 桌面快捷键绑定 | 本方案（输入层守护进程） |
|---|---|---|
| KDE | 可用 | 可用 |
| GNOME / Sway / Hyprland | 每种都要单独配置，语法各不相同 | **无需任何配置** |
| 纯 TTY、无桌面 | 完全不可用 | **可用** |
| 多用户切换 / 重新登录 | 快捷键配置随用户、随 DE 走 | **全局一致** |
| 外部键盘的 F14 | 可能被误触发 | **不会**（只认这一个具体设备） |

具体实现要点：

* 通过**设备名精确匹配**定位设备
  （扫描 `/sys/class/input/event*/device/name` 找 `"Uniwill WMI hotkeys"`），
  失败再退回稳定链接 `/dev/input/by-path/platform-INOU0000:00-event`，
  因此内核重新分配 `eventN` 编号也不受影响。
* 切换走 **`powerprofilesctl`**（即 `power-profiles-daemon`, PPD），
  好处是 PPD 自己的状态、以及其它监听方都能同步。
  PPD 不可用时退化为直接写 `/sys/firmware/acpi/platform_profile`
  —— 这两者**真的等价**，因为 PPD 正是用 `GFileMonitor` 监视该 sysfs 文件
  （见 `ppd-driver-platform-profile.c`），所以写 sysfs 同样会被 PPD 感知。
* 通知用 **`notify-send`**（freedesktop 通知规范），
  KDE 用 plasmashell、GNOME 用 gnome-shell、Sway 用 mako/dunst 都实现了它，
  因此通知也是 DE 无关的。

### 两个容易踩的坑（本方案已规避）

1. **绝不 `EVIOCGRAB` 独占设备。**
   同一个"Uniwill WMI hotkeys"设备还承载 `KEY_RFKILL`（飞行模式）、
   `KEY_MICMUTE`（麦克风静音）、`KEY_KBDILLUMUP/DOWN`（键盘背光）
   等**必须交给桌面环境**的按键。独占会把这些全部吞掉。
   本守护进程只做"旁路监听"（只读，不独占），因此这些键照常工作。
   代价仅是 `F14` 也会同时到达桌面——而 F14 默认没有任何绑定，无副作用。

2. **udev 规则必须是 `72-` 而不是 `99-`。**
   `TAG+="uaccess"` 本身不创建 ACL；真正施加 ACL 的是系统自带的
   `73-seat-late.rules` 里的
   `TAG=="uaccess|xaccess-*", ENV{MAJOR}!="", RUN{builtin}+="uaccess"`。
   这一行在"规则执行到 73 那一刻"判断 TAG。如果规则排在 73 之后
   （比如常见的 `99-xxx.rules`），TAG 设得太晚，条件判为假，
   **ACL 永远不会被创建，规则静默失效**。

## 五、验证方法

### 1. 确认按键已修好

```bash
# 驱动是否绑上（应出现 uniwill/INOU0000:00）
ls -d /sys/bus/platform/drivers/*/INOU0000:00

# 热键输入设备是否存在
grep -A3 "Uniwill WMI hotkeys" /proc/bus/input/devices
```

### 2. 确认守护进程在跑

```bash
systemctl status mechrevo-keyd
journalctl -u mechrevo-keyd -f        # 实时日志
mechrevo-keyd --status                # 设备、当前档位、可用档位
```

### 3. 抓包确认真实按键事件

装上 udev 规则后**普通用户**即可（无需 sudo）：

```bash
evtest /dev/input/by-path/platform-INOU0000:00-event
```

按一下性能模式键，应看到：

```
Event: time ..., type 4 (EV_MSC), code 4 (MSC_SCAN), value b0000
Event: time ..., type 1 (EV_KEY), code 184 (KEY_F14), value 1
```

### 4. 不按键也能测试切换

```bash
mechrevo-keyd --once        # 手动切一档
mechrevo-keyd --dry-run     # 只显示将切到哪档，不真切换
```

### 5. 验证并标定指示灯

先**只读**地看一眼寄存器现状（本命令全程不写任何东西）：

```bash
sudo mechrevo-keyd --probe
```

输出的第一行就是**版本号**，第三行是**自身路径**。这两行是刻意加的：
曾经因为 `/usr/local/bin/mechrevo-keyd` 是旧版，导致排查时看到的行为
和手里的源码对不上，白走了一整轮。若这里显示的版本/路径不是你刚改过的那份，
先重跑 `sudo ./install.sh`（它现在会强制核对安装结果与源码逐字节一致）。

接着列出 `0x0751` 的原始值，并解析出 `0xA0（办公位）`/`0x10（狂暴位）`/
`0x40（全速风扇，本程序会保留）`各是什么状态，以及"当前应显示"哪一档。

#### 5.1 为什么以前所有读数都是"失败"：一个 NUL 字节

`acpi_call` 的 `acpi_proc_read()` 传给 `simple_read_from_buffer()` 的长度是
**字符串长度 + 1**（反汇编里是 `lea r8,[rax+0x1]`），也就是把结尾的 **NUL
也一起返回给用户态**。C 的字符串函数与 shell 的 `$(cat ...)` 都会自动忽略
NUL，所以这个问题**只在 Python 里暴露**：`"\x00"` 不是空白字符，
`str.strip()` 去不掉它，于是 `int("0x19\x00", 0)` 抛异常 → 判为"读取失败"。
（在 shell 里跑 `tools/diag-acpi.sh` 反而一切正常，就是因为 bash 顺手把 NUL 丢了。）

修复方式是在任何解析之前先删掉它：`text.replace("\x00", "").strip()`；
`_acpi_call()` 与 `_parse_int()` 各自独立清一次，任何调用路径都不会漏。
端到端模拟测试（`ec_sim_test.py`）现在也会**照抄内核这一行为**输出带 NUL 的
结果，所以这个 bug 不可能再悄悄回来。

#### 5.2 `ECRW` 返回的 `0xfffffffe` 不是错误

写 EC 走 `ECRW`，真机实测返回 `0xfffffffe`。那是 DSDT 里 `MMRW` 的
`Local0` 初值——`ECRW` 走的是写分支，永远不会覆盖它：

```asl
Method (MMRW, 4, NotSerialized)
{
    Local0 = 0xFFFFFFFE
    If ((Arg1 == Zero))   // 只有读分支才会给 Local0 赋真实值
    ...
}
Method (ECRW, 2, NotSerialized)   // 没有 Return，只是调 MMRW
{
    Local0 = (0xFED50000 + Arg0)
    MMRW (Local0, One, Zero, Arg1)
}
```

所以只有 `Error`/`AE_*` 才算写失败，`0xfffffffe` 必须当成功。

#### 5.3 默认编码已在 GM5HG0A 上验证成立

默认映射（办公 `0xA0` / 均衡 `0x00` / 狂暴 `0x10`）来自 TUXEDO 驱动
`uw_set_performance_profile_v1()`，已在本机实测确认：`--set-led performance`
能让灯变紫，说明 `0x10` 就是狂暴位。

不过 EC 这类编码是**看 BIOS/准系统**的，换机型未必相同。所以保留了实测标定
工具，不必靠猜：

```bash
sudo mechrevo-keyd --scan-led            # 会改 EC，请盯着那颗灯
sudo mechrevo-keyd --scan-led --dry-run  # 只列出会试哪些值，不写
```

它把 `0xB0` 位域的全部 8 种组合逐个写进去，每步问一句"看到什么颜色"，
结束后**无条件恢复**成进入时的原始值（`finally` 里做，`Ctrl-C` 也照样恢复）。
与常规写入一样，它只动 `0xB0` 这三位，`0x40` 全速风扇位与低 3 位风扇档位
全程保留（自测里对每一次写入都做了断言）。换机型时用它定出映射，
改 `EC_LED_VALUES` 即可。

> 另外注意 `0xA0` 是**两位**（bit7|bit5）。判断当前档位必须整段比较
> `(cur & 0xB0) == 0xA0`，不能写成 `cur & 0xA0` —— 那样只要 bit5 单独置位
> 就会误报"办公位置位"。`--probe` 现在会逐位打印 `bit7/bit5/bit4`，不会看错。

**如果每个寄存器都显示"失败"，`--probe` 不会只丢一句"读取失败"**，
而会做三件可排查的事：

1. 逐个候选作用域（`\_SB_.INOU` → `\_SB_` → 根）真实调用一次，
   把 `acpi_call` 的**原始返回**原样打出来；
2. 做一组**对照实验**，把以前都表现为同一句"读取失败"的几种病因分开：
   - **正面**：调用 `\_SB_.INOU._STA`。这条路径由内核自己证明存在——
     驱动探测设备时调过它并拿到 `status=11`，所以它**一定**能返回整数。
     连它都失败 ⇒ `acpi_call` **通路**本身有问题（权限/模块），与 EC 路径无关；
   - **负面**：故意写一个绝对不存在的路径。`acpi_call` 找不到句柄时
     **必定**在内核日志打印 `Cannot get handle`。日志新增 ⇒ 通路正常、
     问题在**路径**；日志纹丝不动 ⇒ 报错没进日志；
3. 把内核日志里 `acpi_call` 自己的报错直接贴出来。

也可以直接跑附带的、更详细的诊断脚本（需要 root）：

```bash
sudo tools/diag-acpi.sh
```

它会额外反汇编 DSDT，直接给出 `ECRR` 在全限定路径下的**真正归属**
（内核驱动用的是相对名，ACPI 会沿作用域链向上找，所以 `ECRR` 未必
就在 `\_SB_.INOU` 正下方）。脚本未装 `acpica` 时会退化为字符串搜索，
并提示 `sudo pacman -S acpica` 以获得权威结论。

然后把三种颜色逐个试一遍：

```bash
sudo mechrevo-keyd --set-led power-saver     # 期望绿灯
sudo mechrevo-keyd --set-led balanced        # 期望蓝灯
sudo mechrevo-keyd --set-led performance     # 期望紫灯
```

若某个档位颜色与预期不符，说明本机的颜色映射与厂商驱动的取值不同，
把上面的输出发回来，改 `mechrevo-keyd.py` 里的 `EC_LED_VALUES` 即可
（寄存器地址与清位掩码算法不用动）。

若模式与灯曾经对不上（比如手动改过档位、或睡眠唤醒后），可以强制对齐：

```bash
sudo mechrevo-keyd --sync-led
```

#### 5.4 dmesg 里的 ACPI 报错：哪些是本程序造成的，哪些不是

`dmesg`/`journalctl -k` 里能看到几类 ACPI 报错。**必须分开看**，因为其中一类
是本程序早期版本自己制造的：

| 报错 | 来源 | 能否修 |
|---|---|---|
| `acpi_call: Cannot get handle: AE_NOT_FOUND` | **本程序**（1.4 及更早） | ✅ 1.5 已修，见下 |
| `ACPI BIOS Error (bug): Could not resolve symbol [\_SB.ACDC.RTAC]`<br>`ACPI Error: Aborting method \_SB.PEP._DSM` | 固件（AMD UPEP SSDT） | ❌ 只能靠 DSDT 覆盖，见下 |
| `ACPI BIOS Error (bug): Failure creating named object [...WLAN...]`<br>`AE_ALREADY_EXISTS` | 固件（重复定义） | ❌ 良性，见下 |
| `acpi PNP0C02:01: Could not reserve [mem ...]` | 固件（AML 里重复声明了保留区） | ❌ 良性，见下 |

**① 本程序造成的（1.5 已修）**

`acpi_call` 在路径不存在时会用 `KERN_ERR` 往内核日志打一条
`Cannot get handle`。1.4 及更早的实现有三处会**自己制造**这种报错：

- 探测作用域时**命中后仍把其余候选全部试完**（没有 `break`）；
- 候选表里有 ACPI 意义上的**重复名** —— ACPI 名字固定 4 字符、不足补
  下划线，所以 `\_SB` 和 `\_SB_`、`\_SB.INOU` 和 `\_SB_.INOU` 是**同一个
  对象**，重复试等于对同一个不存在的路径重复报错；
- `discover_ec()` 探测完**不写缓存**，而 `--probe` 会先调它、紧接着又调
  `_ec_scope()`，后者见缓存为空便**再探测一遍**，报错数直接翻倍
  （日志里那几组 6 条 = 3 × 2 就是这么来的）。

于是每次新进程启动会刷出 3~6 条 `Cannot get handle`。1.5 改为：
优先读 sysfs 里内核自己给出的权威路径（`firmware_node/path`，即本机的
`\_SB_.INOU`），按 ACPI 规范名去重、**命中即停**，并把探测结论**写入缓存**。
健康机型上第一次调用就命中，**报错数从 3 降到 0**。可以用 `--probe -v`
看到实际用的是哪个作用域。

**② 固件自身的（改不了，但基本无害）**

- `\_SB.ACDC.RTAC` 找不到 + `\_SB.PEP._DSM` 中止：只在**每次睡眠唤醒**时出现
  一次。DSDT 里根本没有 `ACDC` 设备，是 AMD 的 `UPEP` SSDT 引用了不存在的
  符号。实测不影响任何功能（电池、无线、亮度、调频、挂起恢复都正常）——
  它就是一条无害的固件 bug 提示。
- `AE_ALREADY_EXISTS`（`WLAN._DSM`、`GPP6._PRW` 等）：固件在不同表里把同一个
  对象定义了两次，内核保留第一个、忽略后者。**良性**。
- `PNP0C02:01: Could not reserve [mem 0xfec00000-...]`：AML 里重复声明了
  这些保留区，内核发现已被占用于是放弃。**良性**，是 2020 年以前就已知的
  固件写法问题。

**想彻底消掉 ② 的话**，只有 DSDT 覆盖一条路（本机内核
`CONFIG_ACPI_TABLE_UPGRADE=y`，条件具备）：

```bash
sudo pacman -S acpica                          # 提供 acpidump / iasl
# 1) 导出并反汇编
sudo acpidump -o tables.dat && sudo acpixtract -a tables.dat
iasl -d dsdt.dat                               # 得到 dsdt.dsl
# 2) 改掉出错的 AML（删除对 ACDC.RTAC 的引用、去掉重复定义）
iasl -ve -tc dsdt.dsl                          # 重新编译成 dsdt.aml
# 3) 让 initrd 带上它（Arch 用 mkinitcpio）
#    /etc/mkinitcpio.conf: FILES=(... /path/to/dsdt.aml)，然后
sudo mkinitcpio -P && sudo reboot
```

**但这不划算，也不建议**：要自己维护一份固件补丁、每次内核/BIOS 更新都可能
失效，而且一旦 AML 改错，ACPI 初始化失败会导致**开不了机**（比现在这几条
无害日志严重得多）。上面三类报错都不影响功能，建议**留着不动**；
真正值得关心的只有第 ① 类，而它已经修好了。

### 6. 自测（不需要 root、不需要真设备）

三套测试都在 `tests/` 下，共 **235 项断言**，全部不需要 root、不需要真设备：

```bash
python3 tests/selftest.py            # 182 项：解析、循环、--probe、--scan-led
python3 tests/ec_sim_test.py         #  43 项：写 EC 的完整链路（含调用字符串格式）
python3 tests/integration_test.py    #  10 项：主循环（用 FIFO 模拟输入设备）
```

`selftest.py` 用合成输入事件覆盖了 23 组、182 项断言：`struct` 布局、只认
`KEY_F14`/`value==1`、一次读取多个事件、跨 `read` 边界的残包、三档循环、
未知档位回退、写失败返回码、名称映射、图标名有效性；
以及指示灯部分的**读-改-写不破坏无关位**（逐个验证 `0x40` 全速风扇位与
低 3 位风扇档位在任何起始值下都不被改动）、幂等性、`acpi_call` 返回值
形态的解析（含**尾部 NUL** 这一真机形态）、无 `acpi_call` 时静默跳过、
指示灯异常不影响切档、`--probe` 全程零次写 EC、对照实验在"日志已满"等
边界下不误判、`--scan-led` 只动 `0xB0` 且结束恢复原值。

`ec_sim_test.py` 用一个假的 ACPI 端点当 EC 寄存器堆，模拟端点**照抄内核
返回尾部 NUL 的行为**（否则这个 bug 会假绿）。它包含一项穷举验证：
**256 种起始值 × 3 个档位**下，`0x0751` 里除了 `0xA0|0x10` 两位之外的
**所有位都不被改动** —— 这正是"绝不整字节覆盖"这条安全承诺的机器化证明。

`integration_test.py` 真的把 `main_loop` 跑起来，经历
`select()` → `os.read()` → 拆包 → 触发切换 的完整路径，并覆盖
"`acpi_call` 比守护进程晚就绪时会补做一次对齐"这一开机时序场景。

## 六、文件说明

| 文件 | 作用 |
|---|---|
| `install.sh` | 一键安装 / `--undo` 卸载 / `--status` 查状态 / `--with-led`·`--no-led`（需 sudo） |
| `mechrevo-keyd.py` → `/usr/local/bin/mechrevo-keyd` | **核心**：输入层按键守护进程（DE 无关，仅用 Python 标准库） |
| `system/mechrevo-keyd.service` | systemd 系统服务（开机自启、崩溃重启、安全加固） |
| `system/72-mechrevo-hotkeys.rules` | udev 规则，给热键设备加 `uaccess`（**必须是 72-，见上文坑 2**） |
| `system/90-mechrevo-uniwill.conf` | modprobe 配置（`force=1` + 屏蔽冲突驱动） |
| `system/uniwill-laptop.conf` | 开机自动加载模块 |
| `tools/cycle-power-profile.sh` | 可选的命令行小工具；安装守护进程后**并非必需** |
| `tools/diag-acpi.sh` | **root** 诊断脚本：对照实验 + 逐路径试调 + 反汇编 DSDT 定位 `ECRR` 归属 |
| `tests/selftest.py` | 守护进程自测（23 组 / 182 项断言） |
| `tests/ec_sim_test.py` | 指示灯写 EC 的端到端模拟测试（43 项断言，模拟端点照抄内核的尾部 NUL 行为） |
| `tests/integration_test.py` | 主循环集成测试（用 FIFO 模拟输入设备，10 项断言，含 acpi_call 晚就绪的补对齐） |

目录结构：

```
mechrevo-power-mode/
├── install.sh              一键安装脚本（入口）
├── mechrevo-keyd.py        核心守护进程
├── README.md
├── LICENSE
├── system/                 → 安装到 /etc 的系统文件
│   ├── mechrevo-keyd.service
│   ├── 72-mechrevo-hotkeys.rules
│   ├── 90-mechrevo-uniwill.conf
│   └── uniwill-laptop.conf
├── tools/                  辅助脚本（不安装，按需手动运行）
│   ├── diag-acpi.sh
│   └── cycle-power-profile.sh
└── tests/                  三套测试，均不需要 root / 真机
    ├── selftest.py
    ├── ec_sim_test.py
    └── integration_test.py
```

关于 `cycle-power-profile.sh`：装上 `install.sh` 后按键由守护进程处理，
**不需要**它。保留它只是为了命令行手动切换，或你确实想额外绑一个快捷键时
当作动作命令——因为它只是个普通命令，在任何 DE 下都能用。

### 守护进程新增的命令行开关

除了原有的 `--once` / `--status` / `--dry-run` / `--no-notify` / `-v`：

| 开关 | 作用 |
|---|---|
| `--probe` | **只读**打印 EC 寄存器现状（`0x0751`、`0x0741`、`0x07A5` 等），解析当前灯色。绝不写任何东西 |
| `--set-led 档位` | 只把指示灯设成指定档位颜色，用于标定/验证（接受 `power-saver`/`balanced`/`performance` 及 `绿`/`办公` 等别名） |
| `--sync-led` | 把指示灯对齐到当前电源模式（纠正手动改档、睡眠唤醒后的错位） |
| `--scan-led` | **会改 EC**：逐个试 `0xB0` 位域 8 种组合并询问颜色，用于实测标定本机编码；结束无条件恢复原值。加 `--dry-run` 只预览 |

## 七、故障排查

| 现象 | 检查 |
|---|---|
| 按键仍无反应 | `journalctl -u mechrevo-keyd -n 50`；`lsmod \| grep uniwill` |
| 守护进程反复重启 | 多半是找不到设备：`grep -i uniwill /proc/bus/input/devices` |
| 有事件但档位不变 | `powerprofilesctl set performance` 手动试；看 `journalctl -u mechrevo-keyd` |
| 切换很快被弹回 | 某个程序通过 PPD 的 `HoldProfile` 持有档位：`powerprofilesctl` 或 PowerDevil 里查 profile holds |
| 通知不出现 | 通知是"尽力而为"的，失败不影响切换；确认 `notify-send` 可用 |
| **档位切了但灯不变** | 没启用指示灯：`ls /proc/acpi/call`。启用：`sudo ./install.sh --with-led` |
| **灯的颜色和档位对不上** | 默认映射（`0xA0`/`0x00`/`0x10`）已在本机验证成立；若换机型后对不上，跑 `sudo mechrevo-keyd --scan-led` 实测标定，再改 `EC_LED_VALUES` |
| **重启后灯又失效** | `/etc/modules-load.d/acpi_call.conf` 没写：`echo acpi_call \| sudo tee /etc/modules-load.d/acpi_call.conf` |
| 日志出现"读 EC 0x0751 失败" | `acpi_call` 已在但 ACPI 方法调用失败；不影响切档，可正常使用 |
| **`--probe` 里每个寄存器都"失败"** | 先确认版本号是 `1.4-led` 或更新（见 5.1：尾部 NUL 那个 bug 会让**所有**读数假失败）。若不是，`sudo ./install.sh --with-led` 重装；已是新版仍失败，就看它自动做的**对照实验**：连 `_STA` 都失败 ⇒ `acpi_call` 通路问题；`_STA` 正常但日志无新增 ⇒ 报错没进日志；`_STA` 正常且日志有新增 ⇒ 路径问题（再跑 `sudo tools/diag-acpi.sh`） |
| **改了代码但行为没变** | 先看 `--probe` 第一行版本号、第三行自身路径，确认跑的是同一份；`install.sh` 会核对安装结果与源码逐字节一致。**更隐蔽的是进程级过期**：文件一致 ≠ 进程一致，systemd 只在启动时读一次脚本。若 `systemctl show -p ExecMainStartTimestamp mechrevo-keyd` 早于文件更新时间，就是进程仍在跑旧代码 —— `sudo systemctl restart mechrevo-keyd` |
| **切档正常但灯不动** | 老进程是最常见原因（见上一行）。另外 `journalctl -u mechrevo-keyd \| grep 指示灯` 若**一行都没有**，说明运行中的进程里根本没有指示灯代码（旧版 `install.sh` 用 `enable --now`，服务已 active 时不会重启，现已改为显式 `restart` 并会在结尾校验进程新鲜度） |
| 想看设备权限 | `getfacl /dev/input/by-path/platform-INOU0000:00-event`（应看到 `user:你的用户名:rw-`） |
| 想看灯的日志 | `journalctl -u mechrevo-keyd \| grep 指示灯` |
| **dmesg 里的 ACPI 报错** | 先分类：`acpi_call: Cannot get handle` 是本程序产生的（1.5 起应完全消失，若仍有请报 issue）；`ACDC.RTAC`/`PEP._DSM`、`AE_ALREADY_EXISTS`、`PNP0C02 Could not reserve` 都是固件自身的良性提示，详见 5.4 |

常见临时处置：

```bash
sudo systemctl restart mechrevo-keyd     # 重启守护进程（会重新扫描设备）
sudo modprobe -r uniwill_laptop          # 卸载驱动（按键立即失效）
sudo modprobe -r acpi_call               # 关掉指示灯功能（切档不受影响）
sudo ./install.sh --undo                 # 完全还原
```

### 万一指示灯寄存器被写坏

本程序只动 `0x0751` 的 `0xA0|0x10` 两位，且每次写之前都先读回来，
不存在累积污染。若仍想手动恢复成"均衡"：

```bash
sudo mechrevo-keyd --set-led balanced    # 等价于清掉这两位
```

EC 寄存器在**冷启动/断电**后一定会回到 BIOS 默认值；最坏情况
长按电源键强制关机重启即可彻底复位。这也是为什么本方案不做任何
"写入 BIOS/DSDT"的持久化修改。

## 八、长期方案

真正干净的修法是给上游内核的 `uniwill_dmi_table[]` 补一条
MECHREVO / `GM5HG0A` 条目（同款准系统的 `GMxHGxx` 描述符已经现成，
只差厂商名），这样无需 `force` 即可自动加载，本仓库的驱动部分就可以删掉，
只留守护进程。

### 备选：DKMS 完整厂商栈（最彻底，也最重）

AUR 的 `mechrevo-drivers-dkms` 打了补丁，向 TUXEDO 驱动的 DMI 匹配表里
加入了 `MECHREVO` 厂商项，可加载完整厂商栈。

代价：需要 `dkms` + `linux-headers`（本机**尚未安装**），每次内核升级要重新
编译，且会引入 `blacklist uniwill_laptop` 与 `eeepc_wmi` 抢占同一批 GUID 的
冲突，需要额外处理。

> **注意**：就"模式灯跟随档位"这个需求而言，**不需要** DKMS ——
> 本仓库用 `acpi_call` 调同一个 ACPI `ECRW` 方法已经能写到 `0x0751`
> （见 1.1 与三）。DKMS 只有在你要**超越模式灯**、想要 EC 级风扇曲线/
> 功耗墙切换时才值得上。

> 注：`amd-pmf` 的 `platform_profile` 确实会影响 EC 的风扇策略，
> 但本机 `pwm1_enable=2`（EC 自动控制），空闲温度相同则 `pwm` 读数必然相同，
> 因此"风扇完全不受影响"这一说法并未被数据证明，也不能反过来说已经生效。
> 若你需要明确的、可测量的风扇曲线变化，请考虑 DKMS 方案。

## 九、为什么不用 `/sys/kernel/debug/regmap` 写 EC

内核的 `regmap` 有个 debugfs 接口（`/sys/kernel/debug/regmap/uniwill/access`），
理论上能直接读写 EC 寄存器。本方案没走它，原因有三：

1. `0x0751` **不在** `uniwill_writeable_reg()` 白名单里，该接口受同一套
   白名单约束，写不进去。
2. 该 regmap 带 `REGCACHE_MAPLE` 缓存，绕过驱动直接写会和驱动的缓存不一致。
3. 本机 `debugfs` 挂载为 `ro`，而 `debugfs=rw` 会显著扩大攻击面。

`acpi_call` 则精确对应驱动内部使用的同一 ACPI 方法，既不受白名单限制，
也不引入新的权限模型。

### 上游修复建议

主线驱动值得补两处（本仓库是用户空间规避，不依赖它们）：

1. `uniwill_dmi_table[]` 里补 MECHREVO / `GM5HG0A` 条目；
2. 为 `uniwill_profile_v1_three_profs_leds_only` 这类机型补一个
   `platform_profile` 实现，在 `.profile_set` 里写 `0x0751`
   （顺带把从未使用的 `EC_ADDR_MANUAL_FAN_CTRL` 宏用起来）——
   这样灯就能在主线里跟随档位，本仓库的 `acpi_call` 部分也可删掉。
