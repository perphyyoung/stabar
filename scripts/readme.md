# scripts —— 资源检测脚本

用于测量 stabar 三种形态(**源码运行 / 打包未裁剪 / 打包裁剪后**)的磁盘体积、启动耗时、
内存、CPU、线程与句柄,并把结论留档。所有命令都在**项目根目录**(`d:\py-code\stabar`)执行。

| 文件 | 作用 |
|---|---|
| `measure-resources.ps1` | 启动目标程序,从外部测量启动耗时、内存、CPU、线程/句柄 |
| `bench-refresh-cost.py` | 进程内高精度拆解 CPU 成本(读数据 / refresh / 重绘) |
| `make-icon.py` | 生成 `stabar.ico`(托盘与 exe 共用的图标,7 档尺寸的 PNG 条目) |
| `readme.md` | 本文件:用法 + 检测结论 |

---

## 一、`measure-resources.ps1` —— 外部资源测量

### 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `-Label` | 是 | 输出里的标题 |
| `-Exe` | 是 | 可执行文件路径 |
| `-ProcName` | 是 | 进程名(不带 `.exe`)。**源码运行传 `python`,打包后传 `stabar`** |
| `-ArgLine` | 否 | 传给程序的参数,源码运行要传 `"main.py"` |
| `-WorkDir` | 否 | 工作目录,默认取 `$Exe` 所在目录 |
| `-IdleSeconds` | 否 | 空闲采样时长,默认 30 |

### 用法

```powershell
# 1) 源码运行(注意 ProcName 是 python,且要指定工作目录)
.\scripts\measure-resources.ps1 -Label "源码运行" `
    -Exe ".\.venv\Scripts\python.exe" -ProcName python `
    -ArgLine "main.py" -WorkDir "." -IdleSeconds 30

# 2) 打包后(onedir)
.\scripts\measure-resources.ps1 -Label "onedir" `
    -Exe ".\dist\stabar\stabar.exe" -ProcName stabar -IdleSeconds 45
```

### 输出字段

```
进程数             : 1                 按进程名抓到的整棵进程树数量
启动 -> WS>=30MB   : 637 ms            工作集越过 30MB 的时间(Qt 已加载的代理指标)
启动 -> 窗口出现   : 688 ms            主窗口句柄出现的时间
稳定后 WS / 私有   : 55.7 MB / 23.3 MB 工作集(含共享的 Qt DLL 代码页)/ 私有提交
空闲 45s WS 均值/峰值                   采样期间工作集是否稳定(有泄漏会看到均值上升)
线程 / 句柄                             资源句柄数
空闲 45s CPU 时间                       采样窗口内累计消耗的 CPU 时间
折算 CPU                                换算成单核 / 整机百分比
```

### 两个必须知道的坑

**1. 进程 CPU 计时器只有 15.6ms 的更新粒度。**
空闲 CPU 会在 `0 / 15.6 / 46.9ms` 之间跳,这些数字**不能直接当结论**。想拿精确值用
`bench-refresh-cost.py`。要观察趋势就把 `-IdleSeconds` 拉到 45 以上。

**2. 源码运行是 2 个进程。**
uv 创建的 venv 里,`Scripts\python.exe` 是个转发进程,真正跑 Qt 的是它的子进程。脚本按
进程名汇总整棵树,所以 `进程数 : 2` 是正常的,不是脚本算错。

`WS>=30MB` 与 `窗口出现` 都受文件系统缓存影响,同一形态重复测会相差几百毫秒,比较时要看趋势。

---

## 二、`bench-refresh-cost.py` —— 进程内 CPU 拆解

用 `time.process_time()` 把一次刷新的成本拆成三段,避开进程计时器的粒度问题。

```powershell
uv run python .\scripts\bench-refresh-cost.py
```

会弹出小组件窗口约两秒(需要真实窗口才能测重绘),然后输出:

```
只读磁盘数据(两个盘符): CPU 0.000 ms/次, 墙钟 0.108 ms/次
refresh() 本身:         CPU 0.000 ms/次, 墙钟 0.149 ms/次
refresh() + 事件循环重绘: CPU 1.042 ms/次, 墙钟 1.649 ms/次
```

---

## 三、检测结论

### 三种形态对比

| 指标 | 源码运行 | onedir 未裁剪 | **onedir 裁剪后** |
|---|---|---|---|
| 磁盘体积 | (.venv 675 MB) | 111.6 MB | **52.55 MB** |
| 进程数 | 2 | 1 | 1 |
| 启动 → 窗口 | 1136 ms | 670 ms | 688 ms(冷)/ 383 ms(热) |
| 常驻 WS | 66.0 MB | 56.0 MB | **55.7 MB** |
| 私有内存 | 26.9 MB | 23.7 MB | **23.3 MB** |
| 线程 / 句柄 | 16 / 400 | 11 / 300 | 12 / 304 |
| 空闲 CPU | — | — | **≈0** |

### 三条结论

1. **裁剪只减磁盘,不减内存和启动。** 111.6 MB → 52.55 MB 省下 59 MB,但内存只从
   56.0 变到 55.7 MB(噪声范围)。原因是那些被删掉的 DLL **从来没有被加载过**,只是躺在
   磁盘上。想减内存只能换技术栈,不是靠裁剪。
2. **空闲 CPU 基本为零。** 唯一的周期性成本是每 15 秒一次重绘,约 **1.04 ms CPU**,
   平均 ≈ **0.007% 单核**(`1.04 / 15000`)。数据读取和 `refresh()` 本身的 CPU 都是 0.000 ms
   —— 瓶颈全在重绘。想再降只有一个杠杆:把 `REFRESH_MS` 从 15 s 拉长。
3. **源码运行比打包后慢一倍多**(1136 ms vs 670 ms)。原因是 venv 的转发进程 + site-packages
   扫描;打包后是单进程,直接加载 PYZ。

### 裁剪明细(共省约 59 MB)

| 项目 | 省下 | 为什么用不到 |
|---|---|---|
| `opengl32sw.dll` | 19.68 MB | Mesa 软件 OpenGL 回退,本组件只用 QPainter 光栅绘制 |
| Qt6Quick + Qt6Qml + QmlModels + QmlMeta + QmlWorkerScript | 12.5 MB | QML 栈,完全没导入 |
| `libcrypto-3-x64` + `libssl-3-x64` | 6.5 MB | 只被 Qt6Network 拉起 |
| `PySide6\translations\*` | 6.44 MB | Qt 标准对话框的翻译;界面文字全是自绘 |
| `Qt6Pdf` | 4.41 MB | PDF 渲染 |
| Qt6OpenGL / Qt6Network / QtNetwork.pyd / Qt6Svg / Qt6VirtualKeyboard | 4.5 MB | 均未导入 |
| 插件(图片格式 10 个 + qdirect2d/qoffscreen/qminimal + TLS 3 个 + 触屏/虚拟键盘/网络信息) | 3.9 MB | 界面无图片、无网络、无触屏 |

裁剪后 `_internal\PySide6` 只剩 14 个文件:`Qt6Core/Gui/Widgets.dll` + 三个 `.pyd` +
`pyside6.abi3.dll` + `qwindows.dll`(唯一必需平台插件)+ `qmodernwindowsstyle.dll` + MSVC 运行时。

保留 `pyside6-addons` 不动,只是打包时不收。

---

## 四、复测流程

```powershell
# 1) 重新打包(裁剪逻辑都在 stabar.spec 里)
uv run pyinstaller --noconfirm --clean stabar.spec

# 2) 看体积
(Get-ChildItem .\dist\stabar -Recurse -File | Measure-Object Length -Sum).Sum / 1MB

# 3) 测资源
.\scripts\measure-resources.ps1 -Label "onedir 裁剪后" -Exe ".\dist\stabar\stabar.exe" `
    -ProcName stabar -IdleSeconds 45

# 4) 需要精确 CPU 就再跑
uv run python .\scripts\bench-refresh-cost.py
```

---

## 五、其他注意事项

- **onedir 必须整目录分发**:`dist\stabar\stabar.exe` 单独拷出去不能运行,要带 `_internal`。
- **配置文件位置随运行方式变化**(见 `main.py` 的 `settings_path()`):
  - 源码运行:项目根目录的 `stabar.ini`
  - 打包运行:`%LOCALAPPDATA%\stabar\stabar.ini`
  **两份是独立的**,改一份不影响另一份。
- **配置文件里的键**:
  - `pos_x` / `pos_y`:窗口位置,拖动后自动写入
  - `idle_opacity`:平时前景不透明度。程序**启动时会把生效值回写**,所以打开文件就能看到
    实际用的是多少。代价是回写之后 ini 成为权威值,再改代码里的 `FIRST_RUN_IDLE_OPACITY`
    不会生效 —— 想回到那个初值,把 ini 里那一行删掉即可。
  - 注意 `QSettings` 重写文件时**不保留注释**,别在 ini 里写说明。
- **托盘图标**会让常驻工作集增加约 1.4 MB(图标 + 一个隐藏窗口):加托盘后实测
  57.1 MB / 私有 23.8 MB / 线程 12 / 句柄 308。左键单击切换显示,右键出菜单,
  悬停显示各盘明细。**鼠标穿透只能从托盘菜单关闭**,开启后窗口不再接收鼠标事件。
- **图标只有一份文件 `stabar.ico`,托盘与 exe 共用**:图案设计写在 `scripts/make-icon.py`
  的 `draw_icon_pixmap()` 里,由它生成 .ico;程序运行时托盘直接读这个文件(打包后从
  `_internal\stabar.ico` 读,靠 `stabar.spec` 的 `datas` 带进包),exe 的文件图标嵌的也是
  同一份。**改了图案要重新生成再打包**:
  ```powershell
  uv run python .\scripts\make-icon.py
  uv run pyinstaller --noconfirm --clean stabar.spec
  ```
- 未采用 Nuitka。它的优势是防反编译和纯 Python 计算加速,对"Qt 巨库 + 每 15 秒读一次系统
  调用"的小组件没有收益,代价是需要配 C 工具链和分钟级构建。
