"""stabar —— Win11 磁盘占用桌面小组件。

无标题条、无背景、置顶、可拖动。平时前景(盘符 / 进度条 / 百分比)按配置的
不透明度贴着壁纸显示;占用达到提亮水位或鼠标悬停时立即变亮。

不透明度以配置文件里的 idle_opacity 为准(首次运行用代码常量 FIRST_RUN_IDLE_OPACITY),
文字与进度条同步变化。

运行:   uv run python main.py
打包:   uv run pyinstaller --noconfirm --clean stabar.spec   (产物在 dist\\stabar)
配置:   开发时 = 项目目录下的 stabar.ini
        打包后 = %LOCALAPPDATA%\\stabar\\stabar.ini   (见 settings_path())
日志:   与配置同目录的 stabar.log,开发 INFO、打包 WARNING,可用 STABAR_LOG_LEVEL 覆盖
        (自动化测试用 DEBUG);实现见 logging_setup.py,用法见 日志使用说明.md
托盘:   左键单击托盘图标 = 切换显示(小组件可以调到几乎看不见,托盘是稳定的入口)
        右键托盘图标 = 显示/隐藏、立即刷新、鼠标穿透、退出
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import sys
import winreg
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QRectF,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionMenuItem,
    QSystemTrayIcon,
    QWidget,
    QWidgetAction,
)

import dialogs
import drives
import logging_setup
import single_instance

# ======================================================================
# 观感调节区:想让小组件更醒目,只动这一块(平时的不透明度:托盘菜单或配置文件)
# ======================================================================

APP_NAME = "stabar"  # 决定打包后配置目录的名字
# 版本号**不在这里**:唯一来源是 pyproject.toml 的 [project] version,运行时由 app_version() 读它。
# 不要为了省一次读文件而在代码里另抄一份 —— 抄了就会有一天忘了同步。
REFRESH_MS = 15_000  # 刷新间隔的默认值(毫秒);托盘菜单可改(见 refresh_seconds)
REFRESH_MIN_S = 1  # 托盘步进器允许的最小秒数
REFRESH_MAX_S = 60  # 托盘步进器允许的最大秒数
REFRESH_DEFAULT_S = REFRESH_MS // 1000  # 首次运行的默认秒数(与历史行为一致)

# --- 前景不透明度:文字、进度条、百分比同步生效 ---
# 平时的值不在这里,而在配置文件里,也可以从托盘菜单的「透明度」滑杆改
# (见 FIRST_RUN_IDLE_OPACITY / settings_path() / StatusBarWindow.set_idle_opacity)
LIVE_OPACITY = 1.00  # 鼠标悬停 / 自动提亮时的不透明度
MIN_OPACITY = 0.05  # 配置文件允许的最小值(5%,再小就真的看不见了;滑杆的最小格也是它)
OPACITY_STEP = 0.05  # 滑杆步进(5%):一格一档,拖动与键盘都是这个粒度
OPACITY_STEPS = round(1.0 / OPACITY_STEP)  # 滑杆格数(1 格 = 5%,共 20 格)
FADE_MS = 260  # 自动提亮的补间时长(毫秒);鼠标悬停是硬切,不走这里

# --- 自动提亮:占用达到水位才变亮,低于水位严格按配置文件的不透明度显示 ---
WARN_THRESHOLD = 0.85  # 橙色警戒线的首次运行默认值(**只影响颜色**);托盘菜单可改
DANGER_THRESHOLD = 0.90  # 红色危险线的首次运行默认值;托盘菜单可改
LIT_THRESHOLD = 0.90  # 常亮阈值的首次运行默认值(到它或红色线界面常亮);托盘菜单可改
COLOR_HYSTERESIS = 0.005  # 颜色切换滞回,避免卡在阈值上反复闪烁

# --- 三条阈值的可调范围(托盘菜单按百分比显示,存盘是 1% 粒度的小数) ---
THRESHOLD_MIN = 0.01
THRESHOLD_MAX = 1.00
THRESHOLD_STEP = 0.01  # 一格 = 1%
THRESHOLD_GAP = 0.01  # 橙色与常亮都必须比红色至少低一格(橙色与常亮之间不设约束)

# --- 尺寸与屏幕边距 ---
WINDOW_W = 248
ROW_H = 22
PAD = 6
GAP = 6
EDGE_MARGIN = 24

# --- 贴边隐藏 ---
# 收起 = **折叠**成贴在屏幕边上的一条**断线**:线只有一条,断成几段,一段表示一个盘;
# 指针蹭到它(或第二个实例启动)再长回原样。**不画底板 / 描边** —— 线段自己是彩色的,亮暗壁纸
# 上都看得清,少一层就少一份遮挡。
#
# 每段**长度固定**(只表示"这个盘在这儿"),状态**只靠颜色** —— 与展开时的进度条同源;
# 整条线**与屏幕边平行**:贴左 / 贴右是竖的,贴上 / 贴下是横的(见 `tail_layout`)。
# 尺寸是**固定的设备像素**,不随主界面缩放变(「调整尺寸」只管展开形态)。
#
# 尾巴**整块都在屏幕内**,所以鼠标总能蹭到它 —— 这满足命中规则的要求
# (见 README 必读第 2 条:窗口得在屏幕内留下 alpha ≥ 1 的像素,否则再也点不回来)。
TAIL_SEGMENT = 6  # 每段(每块盘)的长度
TAIL_LINE_H = 2  # 线宽
TAIL_DASH_GAP = 2  # 两段之间的断开间隙(断开就靠它)
TAIL_MARGIN = 3  # 线到尾巴边缘的留白(尾巴同时是命中区:线只有 2px 粗,留点余量才好蹭)
# 线的透明度不跟着未报警透明度一路暗下去:它是收起时唯一能点回来的东西,淡到看不见就等于
# 窗口丢了。下限之外仍随提亮一起变亮(悬停 / 报警)。
TAIL_MIN_ALPHA = 90  # 线的不透明度下限
# 贴边判定的阈值:取**最近**那条边,它的"内缩量"不超过这个值就算贴上。
# 内缩量是**带符号**的:窗口还在屏幕内为正,压出屏幕为负 —— 所以"拖出一半"这种也判得上,
# 不必为"压边"另立一条规则(见 `snap_edge`)。
EDGE_SNAP_PX = 8
EDGE_HIDE_DELAY_MS = 250  # 指针离开后隔多久才收起:没有它,光标划过边缘会来回弹
EDGE_HIDE_DEFAULT = True  # 默认开:贴到边就收起成小尾巴
EDGE_HIDE_KEY = "edge_hide"

# --- 整窗缩放(托盘「调整尺寸」滑杆调)---
# 存的是**系数**而不是像素:窗口高度是"盘数 × 行高"推出来的,存绝对高会跟盘数打架;而上面的
# WINDOW_W / ROW_H / PAD / GAP 本来就是一套常量,乘同一个系数最自然。
UI_SCALE_DEFAULT = 1.0
UI_SCALE_MIN = 0.6  # 再小就看不清了
UI_SCALE_MAX = 2.0  # 再大就占满屏
SCALE_UNIT = 0.01  # 滑杆一格 = 1%

# 托盘悬停提示的长度上限:Windows 的 NOTIFYICONDATA.szTip 是 128 个字符的缓冲,超出会被 shell
# 从中间截掉(实测:三块盘的完整明细约 140 字符,托盘里第三行只剩"可用 2.")。所以托盘用紧凑文字。
TRAY_TIP_MAX = 127

# --- 托盘菜单 ---
# 控件行(滑杆 / 开关 / 按钮)没有菜单项那段缩进,要自己补上左边距才能与其它行文字左对齐。
# 实测(抓图 + 像素扫描):菜单里**没有可勾选项**之后,纯文字项从面板边缘 10~11px 处开始;
# 控件行自己补 8 的左边距正好落在同一列(量出来 11)。左右取同一个值,两侧留白也就相等了。
MENU_ITEM_INDENT = 8
SLIDER_WIDTH = 96  # 滑杆长度:够细调即可,太长会把整个菜单撑宽
# 三行阈值步进器:(行标签, action 名, hover 提示, 内部键)。这个顺序就是菜单里的顺序。
THRESHOLD_ROWS = (
    ("橙色阈值（%）", "橙色阈值", "占用到这条线变橙(只影响颜色)", "warn"),
    ("红色阈值（%）", "红色阈值", "占用到这条线变红;同时也会触发常亮", "danger"),
    ("常亮阈值（%）", "常亮阈值", "占用到这条线界面常亮(红色线也会触发)", "lit"),
)

# --- 图标文件:托盘图标与 exe 文件图标共用这一份,见 icon_path() ---
ICON_NAME = "stabar.ico"

# HIT_AREA_ALPHA 不是装饰,是功能代码,别改成 0:
# Windows 对分层窗口按像素做命中测试,alpha = 0 的像素鼠标会直接穿透到桌面。
# 背景纯透明后,整个窗口矩形就只有字形能接收鼠标 —— 悬停提亮会变得极难触发,
# 拖动也只能精确按住某个笔画。保留一层 alpha = 1 的隐形底板即可修复。
HIT_AREA_ALPHA = 1

# --- 文字描边:保证在浅色壁纸下也读得清 ---
OUTLINE_RGB = (12, 12, 14)
OUTLINE_WIDTH = 2.0  # 描边粗细(像素)
TRACK_RATIO = 0.15  # 进度条底槽不透明度 = 前景不透明度 × 该系数

# --- 持久化 ---
SETTINGS_NAME = "stabar.ini"  # 实际位置见 settings_path(),开发与打包后不同
# 只在配置文件里还没有 idle_opacity 这个键时生效(首次运行)。程序启动会把生效值回写,
# 所以之后改这里不会有任何反应 —— 要调不透明度请直接改配置文件。
FIRST_RUN_IDLE_OPACITY = 0.30
# 覆盖配置与日志目录(日志实现见 logging_setup.py),自动化测试靠它做隔离
CONFIG_DIR_ENV = "STABAR_CONFIG_DIR"

# --- 色相 ---
BASE_TEXT = (238, 238, 242)
BASE_DIM = (196, 196, 204)
BASE_TRACK = (255, 255, 255)
COLOR_ACCENT = (78, 154, 241)
COLOR_WARN = (233, 176, 78)
COLOR_DANGER = (231, 92, 92)


# ======================================================================
# 小工具
# ======================================================================


def _app_config_dir() -> Path:
    """打包后的用户目录(%LOCALAPPDATA%\\stabar)。

    依赖 Qt 的应用名,所以调用方必须等 `setApplicationName(APP_NAME)` 之后再用 ——
    应用名没设时 AppConfigLocation 会退化成 %LOCALAPPDATA% 本身(实测)。
    """
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    directory = Path(base) if base else Path.home() / f".{APP_NAME}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_dir() -> Path:
    """**配置文件**所在目录,优先级从高到低:

    1. 环境变量 `STABAR_CONFIG_DIR` —— 自动化测试用:把 ini 引到临时目录,不碰你真实的配置;
    2. 打包运行 -> %LOCALAPPDATA%\\stabar;
    3. 源码运行 -> 项目目录,方便直接手改。

    开发时留在项目目录是为了方便直接手改;打包后不能再写 exe 旁边,原因有两个:
    一是 onedir 的 __file__ 指向 _internal 子目录,配置会藏进包内部、升级就丢;
    二是装到 Program Files 之类的路径下可能根本没有写权限。
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        directory = Path(override)
    elif getattr(sys, "frozen", False):  # PyInstaller 打包运行时的标志
        directory = _app_config_dir()
    else:
        directory = Path(__file__).parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def log_dir() -> Path:
    """**日志**所在目录,**只有两种**(见 `日志使用说明.md`):

    - 打包运行 -> 用户配置目录(%LOCALAPPDATA%\\stabar),和打包后的 ini 同目录;
    - 其他情况(源码运行、跑测试)-> 项目目录。

    刻意**不理会** `STABAR_CONFIG_DIR`:那个变量只用来隔离**配置**。日志就两份,测试直接写
    项目目录这份,跑完能立刻看;代价是多个用例共用一个文件,所以读日志必须按偏移量切
    (见 `tests/e2e_support.read_since`)。
    """
    if getattr(sys, "frozen", False):
        return _app_config_dir()
    return Path(__file__).parent


def app_version() -> str:
    """版本号:**唯一来源是 `pyproject.toml` 的 `[project] version`**,这里只负责把它读出来。

    走标准库的 `importlib.metadata` —— 它读的是"已安装分发"的元数据:源码形态靠 `uv sync` 把本工程
    装成可编辑包,打包形态靠 `stabar.spec` 里的 `copy_metadata`(PyInstaller 官方做法)把那小份
    `dist-info` 带进 `_internal/`。**不必把 `pyproject.toml` 塞进包里**,代码里也不另抄一份。
    没有元数据(比如直接 `python main.py`、没同步过环境)就返回 `unknown` 并留一条日志。
    """
    try:
        return importlib.metadata.version(APP_NAME)
    except importlib.metadata.PackageNotFoundError:
        log.warning("event=version_unavailable hint=环境里没有本工程的分发,先 uv sync")
        return "unknown"


def settings_path() -> Path:
    """配置文件位置。"""
    return config_dir() / SETTINGS_NAME


def log_path() -> Path:
    """日志文件位置。"""
    return logging_setup.log_path(log_dir())


def open_path(path: Path) -> None:
    """用系统默认程序打开一个路径(这里只用来开目录)。

    单独一层是为了**能替换**:测试里换成替身,免得真弹资源管理器(与 `winreg` 那套注入同一个思路)。
    目录不存在就先建出来 —— 首次运行就点开时(还没写过任何文件)它可能真的还不存在。
    """
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    log.debug("event=open_dir path=%s", directory)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(value: int, low: int, high: int) -> int:
    """把 value 夹在 [low, high] 内(high 比 low 小时以 low 为准 —— 窗口比工作区还大时的兜底)。"""
    return max(low, min(high, value))


def snap_edge(
    x: int, y: int, width: int, height: int, area: tuple[int, int, int, int]
) -> str | None:
    """窗口贴在哪条屏幕边上:取**最近**那条边,它的内缩量 ≤ `EDGE_SNAP_PX` 就算贴上,否则 None。

    `area` 是**左下开区间**的矩形 (left, top, right, bottom)(Qt 的 `QRect.right()` 是闭区间,
    调用方要先 +1 换成这个口径)。

    内缩量**带符号**:窗口还在屏幕内时为正(离这条边还有多远),压出屏幕时为负(越出去多少)。
    取的是**最小**的那个 —— 都在屏幕内时它就等于"离得最近的那条",而压出去的那条是负数、
    自然排在最前。于是"贴住边"和"拖出去一半"是同一条规则,不必为后者再立一条。

    ⚠️ **只对展开状态用**:收起成尾巴时窗口只有十几像素,判定结果没有意义(尾巴本来就贴在那条边上)。
    贴边状态一旦定下来就保持,只有拖动结束、开关变化、缩放时才重新判定。
    """
    left, top, right, bottom = area
    inset = {
        "left": x - left,
        "right": right - (x + width),
        "top": y - top,
        "bottom": bottom - (y + height),
    }
    edge = min(inset, key=lambda name: inset[name])
    return edge if inset[edge] <= EDGE_SNAP_PX else None


def tail_pos(
    edge: str,
    x: int,
    y: int,
    width: int,
    height: int,
    tail_width: int,
    tail_height: int,
    area: tuple[int, int, int, int],
) -> tuple[int, int]:
    """收起成尾巴后尾巴的左上角:贴着那条边,沿边方向对齐展开时的中心。

    `x/y/width/height` 传**展开时**的矩形(锚点那份):尾巴落在它的中间,展开才不会横向跳。
    位置一律夹在工作区内 —— 窗口停在角落时,尾巴不能因为"对齐中心"而露到屏幕外(露出去就点不到了)。
    """
    left, top, right, bottom = area
    center_x = clamp(x + width // 2 - tail_width // 2, left, right - tail_width)
    center_y = clamp(y + height // 2 - tail_height // 2, top, bottom - tail_height)
    if edge == "left":
        return left, center_y
    if edge == "right":
        return right - tail_width, center_y
    if edge == "top":
        return center_x, top
    return center_x, bottom - tail_height  # bottom


def scaled_layout(scale: float, rows: int) -> tuple[int, int, int, int]:
    """整窗缩放后的 (边距 pad, 行距 gap, 行高 row_h, 宽度 width)。

    抽成纯函数是因为有两个消费方:窗口重排(`StatusBarWindow.apply_scale`)和"展开尺寸"的推算
    (收起 / 展开动画的目标、用例断言)—— 各推一遍迟早推歪。
    """
    return (
        round(PAD * scale),
        round(GAP * scale),
        max(1, round(ROW_H * scale)),
        max(1, round(WINDOW_W * scale)),
    )


def layout_size(scale: float, rows: int) -> QSize:
    """展开状态下整窗的尺寸(与 `apply_scale` 同一套算式,不另抄一份)。"""
    pad, gap, row_h, width = scaled_layout(scale, rows)
    return QSize(width, pad * 2 + row_h * rows + gap * (rows - 1))


def tail_layout(edge: str, rows: int) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """尾巴的尺寸与每块盘的线段矩形(设备像素,左上角为原点)—— 尾巴长什么样全在这一处。

    **一条线,断成几段**:所有线段排在同一条直线上(同一个 y),段间留 `TAIL_DASH_GAP` 的空隙,
    一段表示一个盘;每段长度固定(`TAIL_SEGMENT`),状态是颜色的事(见 `DiskBar.status_rgb`)。
    整条线**与屏幕边平行** —— 贴左 / 贴右把矩形转 90°(x↔y、宽高互换),线就是竖的。

    尺寸**不随主界面缩放变**:「调整尺寸」管的是展开形态,贴边后这个标记不该跟着变粗变长。
    只有盘数会影响线的长度(要装得下每块盘一段)。

    返回 (宽, 高, [线段矩形…]),矩形顺序与盘序一致 —— 调用方拿着直接画,不必再判断方向。
    """
    length = TAIL_MARGIN * 2 + rows * TAIL_SEGMENT + (rows - 1) * TAIL_DASH_GAP
    thick = TAIL_MARGIN * 2 + TAIL_LINE_H
    lines = [
        # 先按"线沿 x 轴"排;竖线把矩形转 90°(x↔y、宽高互换)即可,段的长度不变
        (
            TAIL_MARGIN + index * (TAIL_SEGMENT + TAIL_DASH_GAP),
            TAIL_MARGIN,
            TAIL_SEGMENT,
            TAIL_LINE_H,
        )
        for index in range(rows)
    ]
    if edge in ("left", "right"):
        return thick, length, [(y, x, h, w) for x, y, w, h in lines]
    return length, thick, lines


def with_alpha(rgb: tuple[int, int, int], alpha: float) -> QColor:
    """给颜色换一个 alpha。**alpha 是 0~255,不是 0~1** —— 写 0.55 会被取整成 1(几乎看不见)。"""
    color = QColor(*rgb)
    color.setAlpha(max(0, min(255, round(alpha))))
    return color


def is_lit(max_ratio: float, lit: float, danger: float) -> bool:
    """占用率是否该常亮:到「常亮阈值」或「红色阈值」都算 —— 取两者中**更低**的那条。

    红色线永远是"最迟必须亮"的兜底:就算把常亮阈值设得比它高(配置文件手写),到红线照样常亮。
    两条线都没到就**完全不提亮**,严格按配置文件的不透明度显示(没有渐亮斜坡 —— 那会让
    "平时隐身"变得不可预期)。
    """
    return max_ratio >= min(lit, danger)


def percent_text(value: float) -> str:
    """把 0.85 这样的值写成给人看的百分比(滑杆右侧标签、还原框里的阈值共用)。"""
    return f"{round(value * 100)}%"


def scale_units(scale: float) -> int:
    """缩放系数 -> 滑杆格数(一格 1%,所以格数就是百分数)。"""
    return min(
        round(UI_SCALE_MAX / SCALE_UNIT),
        max(round(UI_SCALE_MIN / SCALE_UNIT), round(scale / SCALE_UNIT)),
    )


def scale_from_units(units: int) -> float:
    """滑杆格数 -> 缩放系数。"""
    return min(UI_SCALE_MAX, max(UI_SCALE_MIN, units * SCALE_UNIT))


# 托盘菜单「还原配置」管的就是这几项:(ini 键, 显示名, 默认值, 把值写成给人看的文本)。
# **位置 pos_x / pos_y 故意不在其中** —— 还原不该把窗口搬走,ini 里始终留着位置。
# 默认值一律引用上面的常量、不再抄一份:改常量时还原跟着变。
RESETTABLE_SETTINGS = (
    ("refresh_seconds", "刷新频率", REFRESH_DEFAULT_S, lambda value: f"{value} 秒"),
    ("warn_threshold", "橙色阈值", WARN_THRESHOLD, percent_text),
    ("danger_threshold", "红色阈值", DANGER_THRESHOLD, percent_text),
    ("lit_threshold", "常亮阈值", LIT_THRESHOLD, percent_text),
    ("idle_opacity", "未报警透明度", FIRST_RUN_IDLE_OPACITY, percent_text),
    (EDGE_HIDE_KEY, "贴边隐藏", EDGE_HIDE_DEFAULT, lambda value: "开" if value else "关"),
    (
        drives.DRIVES_KEY,
        "显示的盘符",
        None,
        lambda value: "自动" if value is None else drives.drives_text(value),
    ),
)


def opacity_units(value: float) -> int:
    """不透明度 -> 滑杆格数。

    配置文件里可以写任意精度(如 0.33),滑杆表达不了就取最近的一格;标签仍显示真实值。
    """
    return min(OPACITY_STEPS, max(1, round(value * OPACITY_STEPS)))


def opacity_from_step(step: int) -> float:
    """滑杆格数 -> 不透明度。

    用"格数 / 总格数"而不是"格数 × 0.05":后者带浮点尾巴(14 × 0.05 = 0.7000000000000001),
    写进配置文件就成了难看的 `idle_opacity=0.7000000000000001`。
    """
    return step / OPACITY_STEPS


def draw_outlined_text(
    painter: QPainter,
    rect: QRectF,
    text: str,
    font: QFont,
    fill: QColor,
    outline: QColor,
    *,
    right_aligned: bool = False,
) -> None:
    """垂直居中画一行带暗色描边的文字(描边让文字在任何壁纸上都可读)。"""
    metrics = QFontMetricsF(font)
    width = metrics.horizontalAdvance(text)
    x = rect.right() - width if right_aligned else rect.left()
    y = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2

    path = QPainterPath()
    path.addText(x, y, font, text)
    pen = QPen(outline)
    pen.setWidthF(OUTLINE_WIDTH)
    painter.strokePath(path, pen)
    painter.fillPath(path, fill)


# ======================================================================
# 日志
#
# 实现全在 logging_setup.py(级别策略、落盘、Qt 消息桥接、异常钩子),这里只留一个
# logger。事件行统一写成 "event=<名词> key=value ...",完整清单见 日志使用说明.md。
# ======================================================================

log = logging.getLogger(APP_NAME)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ======================================================================
# 开机自启:写 HKCU 的 Run 键。不需要管理员权限,任务管理器 -> 启动 里也能关
# ======================================================================

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    """写进注册表的启动命令:打包后就是 exe 自身,开发时用 pythonw 跑本脚本。

    两头都给路径加引号,否则目录名带空格时会被系统当成多个参数切开。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    if not interpreter.exists():  # 少数环境没有 pythonw,退回 python(会带个控制台窗口)
        interpreter = Path(sys.executable)
    return f'"{interpreter}" "{Path(__file__).resolve()}"'


def is_autostart_enabled() -> bool:
    """注册表里的命令和当前运行方式一致才算开启 —— 换过路径就不算。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
    except OSError:
        return False
    return str(value).strip().casefold() == autostart_command().casefold()


def set_autostart(enabled: bool) -> None:
    """增删 Run 键;失败交给调用方处理(可能没有写权限或被组策略拦住)。"""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            return
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass


# ======================================================================
# 界面
# ======================================================================


def icon_path() -> Path:
    """图标文件位置:开发时在项目目录,打包后在 PyInstaller 的解包目录。

    打包后靠 stabar.spec 的 datas 把 .ico 带进 _internal,所以这里用 sys._MEIPASS 定位。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / ICON_NAME
    return Path(__file__).with_name(ICON_NAME)


def make_tray_icon() -> QIcon:
    """托盘图标:直接读 stabar.ico,与 exe 的文件图标同一份文件。"""
    icon = QIcon(str(icon_path()))
    if icon.isNull():
        # .ico 要靠 Qt 的 qico.dll 插件解码,打包时漏掉就会走到这里:
        # QIcon 不报错、只返回空图标,表现为托盘图标不出现,所以显式喊一声。
        # 这条必须是日志而不是 print:打包后 stderr 是 None,打印出去就没了。
        log.warning("event=icon_missing path=%s hint=检查打包是否漏了 qico.dll", icon_path())
    return icon


class DiskBar(QWidget):
    """单行:盘符 + 占用进度条 + 百分比。不透明度由窗口统一下发。"""

    def __init__(
        self,
        path: str,
        parent: QWidget | None = None,
        warn: float = WARN_THRESHOLD,
        danger: float = DANGER_THRESHOLD,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._label = path[0] + ":"
        self._stats = drives.read_disk(path)
        self._alpha = 255.0
        self._warned = False  # 警告色滞回标记
        # 两条警戒线可配(托盘菜单改),所以存在实例上;默认值就是常量的首次运行值
        self._warn = warn
        self._danger = danger
        self._font = QFont(self.font())
        self._font.setPointSizeF(9.0)
        # 让父窗口统一接收鼠标事件,拖动和悬停都更简单
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedHeight(ROW_H)
        self._scale = UI_SCALE_DEFAULT  # 由窗口下发,见 set_scale

    # ---- 数据 ----
    @property
    def path(self) -> str:
        """本行盯着的盘符(形如 "C:/")—— 盘集变化时靠它比对。"""
        return self._path

    def reload(self) -> None:
        self._stats = drives.read_disk(self._path)
        self.update()

    def ratio(self) -> float | None:
        if self._stats is None:
            return None
        used, total = self._stats
        return used / total if total else 0.0

    def detail_text(self) -> str:
        if self._stats is None:
            return f"{self._label}  不可用"
        used, total = self._stats
        ratio = used / total * 100 if total else 0.0
        return (
            f"{self._label}  已用 {human_size(used)} / {human_size(total)}"
            f"  可用 {human_size(total - used)}  ({ratio:.1f}%)"
        )

    def short_text(self) -> str:
        """同一行的**紧凑**写法:盘符 + 占比 —— 给托盘用(那里的提示有长度上限)。"""
        if self._stats is None:
            return f"{self._label} 不可用"
        return f"{self._label} {self.percent_label()}"

    # ---- 观感 ----
    def set_alpha(self, alpha: float) -> None:
        """前景不透明度(0~255),文字与进度条一起变。"""
        self._alpha = alpha
        self.update()

    def percent_label(self) -> str:
        """画在右侧的占用率文字:**带 1 位小数**。

        单独抽出来是为了能直接断言这一处格式(画到像素上去验证小数点不现实)。
        """
        used, total = self._stats or (0, 0)
        return f"{used / total * 100:.1f}%" if total else "0.0%"

    def set_thresholds(self, warn: float, danger: float) -> None:
        """更新两条警戒线并重绘(阈值可配,所以不能再直接读模块常量)。"""
        self._warn = warn
        self._danger = danger
        self.update()

    def _bar_rgb(self, ratio: float) -> tuple[int, int, int]:
        """状态色(含滞回):到红色线变红,过橙色线变橙,否则主色。进度条与尾巴共用这一处判定。"""
        if ratio >= self._danger:
            self._warned = True
            return COLOR_DANGER
        floor = self._warn - COLOR_HYSTERESIS if self._warned else self._warn
        self._warned = ratio >= floor
        return COLOR_WARN if self._warned else COLOR_ACCENT

    def _bar_color(self, ratio: float) -> QColor:
        return with_alpha(self._bar_rgb(ratio), self._alpha)

    def status_rgb(self) -> tuple[int, int, int]:
        """收起的尾巴上那条线段要的颜色(不带 alpha)。

        与进度条同源(`_bar_rgb`,含滞回),所以收起来之后颜色不会跟展开时对不上;长度不由它决定
        —— 线段长度是固定的,状态只靠颜色。盘不可用时给暗色(它不代表任何档位)。
        透明度由尾巴自己定(它比进度条更需要看得见)。
        """
        ratio = self.ratio()
        return BASE_DIM if ratio is None else self._bar_rgb(ratio)

    def set_scale(self, scale: float) -> None:
        """整窗缩放:只需设高度,其余交给绘制变换 —— 见 `paintEvent` 开头。"""
        self._scale = scale
        self.setFixedHeight(max(1, round(ROW_H * scale)))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 绘制代码一律按**基准像素**写(26 / 32 / 8 那些),缩放由画布变换完成 —— 这样加一个
        # 像素常量不必再记得"要不要乘系数",也不会漏。字号同理:9pt 经变换就是 9·s 的效果,
        # 不必去 setPointSizeF(缩小时还会触发 QFont 的"点大小必须 > 0"警告)。
        painter.scale(self._scale, self._scale)
        w, h = self.width() / self._scale, self.height() / self._scale
        text_color = with_alpha(BASE_TEXT, self._alpha)
        dim_color = with_alpha(BASE_DIM, self._alpha)
        outline = with_alpha(OUTLINE_RGB, self._alpha)

        draw_outlined_text(
            painter, QRectF(0, 0, 26, h), self._label, self._font, text_color, outline
        )

        # 列宽按"100.0%"量出来:写死会在换字体或高 DPI 下把小数点挤掉
        percent_w = QFontMetricsF(self._font).horizontalAdvance("100.0%") + 2
        bar_x = 32
        bar_w = max(60, w - bar_x - percent_w - 8)
        bar_h = 8
        bar_y = (h - bar_h) / 2

        track = QPainterPath()
        track.addRoundedRect(bar_x, bar_y, bar_w, bar_h, bar_h / 2, bar_h / 2)
        painter.fillPath(track, with_alpha(BASE_TRACK, self._alpha * TRACK_RATIO))

        if self._stats is None:
            draw_outlined_text(
                painter,
                QRectF(bar_x, 0, w - bar_x, h),
                "不可用",
                self._font,
                dim_color,
                outline,
            )
            return

        used, total = self._stats
        ratio = used / total if total else 0.0

        fill_w = bar_w * min(ratio, 1.0)
        if fill_w > 0:
            fill = QPainterPath()
            fill.addRoundedRect(bar_x, bar_y, max(fill_w, bar_h), bar_h, bar_h / 2, bar_h / 2)
            painter.fillPath(fill, self._bar_color(ratio))

        draw_outlined_text(
            painter,
            QRectF(w - percent_w, 0, percent_w, h),
            self.percent_label(),
            self._font,
            dim_color,
            outline,
            right_aligned=True,
        )


def summary_text(bars: list[DiskBar], *, compact: bool = False) -> str:
    """悬停提示的文字 —— **只有这一处**:完整版给主界面,紧凑版给托盘。

    托盘的提示有平台上限(见 `TRAY_TIP_MAX`),完整明细会被 Windows 从中间截掉;主界面的
    tooltip 没有这个限制,所以两种长度各取所需,而文字始终由这里产出。
    """
    if compact:
        return "  ".join(bar.short_text() for bar in bars)
    return "\n".join(bar.detail_text() for bar in bars)


class StatusBarWindow(QWidget):
    """无标题条、无背景的置顶窗口,负责提亮 / 还原与位置持久化。"""

    updated = Signal(str)  # 每次刷新后带出**托盘用**的紧凑状态文字(见 summary_text)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # Qt 文档:tooltip 默认只对**活动窗口**显示,而本窗口置顶、从不被激活(真人实测:
        # 详情会时有时无甚至一直不出现)。开了这条,非活动窗口也照常弹详情。
        self.setAttribute(Qt.WA_AlwaysShowToolTips, True)
        self.setFixedSize(layout_size(UI_SCALE_DEFAULT, 1))  # 占位,真实尺寸由 _build_bars 定

        self._settings = QSettings(str(settings_path()), QSettings.IniFormat)
        self._drag_offset: QPoint | None = None
        # 整窗缩放:记住生效值;托盘滑杆拖动时只预览,松手 / 关菜单才提交
        self._ui_scale = self._read_ui_scale()
        self._saved_ui_scale = self._ui_scale
        # 要显示哪些盘:ini 里的 `drives`(None = 自动,用运行时探测结果)
        self._wanted_drives = self._read_drives()

        # 贴边隐藏:只记"贴哪条边 + 展开时的锚点";收起 / 展开只改窗口本身,不动配置。
        # **必须排在下面的 apply_scale() 之前** —— 它会读 `self._edge` 决定要不要重算贴边。
        self._edge_hide = self._read_edge_hide()
        self._edge: str | None = None
        self._edge_hidden = False  # True = 尾巴形态(收起途中也算,见 _hide_to_edge)
        self._edge_anchor = QPoint()  # 展开时的位置(吸附后 = 与该屏幕边齐平)
        self._edge_start = QPoint()  # 形变动画的起点 / 终点(位置)
        self._edge_target = QPoint()
        self._edge_start_size = QSize()
        self._edge_target_size = QSize()
        self._edge_sliding = False  # 形变期间禁止被"离开"事件重新排上收起(见 leaveEvent)
        self._edge_timer = QTimer(self)
        self._edge_timer.setSingleShot(True)
        self._edge_timer.setInterval(EDGE_HIDE_DELAY_MS)
        self._edge_timer.timeout.connect(self._hide_to_edge)
        self._edge_anim = QVariantAnimation(self)
        self._edge_anim.setDuration(FADE_MS)  # 与提亮同一时长,观感一致
        self._edge_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._edge_anim.valueChanged.connect(self._morph_step)
        self._edge_anim.finished.connect(self._morph_done)
        self._tail_alpha = (
            float(FIRST_RUN_IDLE_OPACITY) * 255
        )  # 尾巴用的前景不透明度,见 _apply_level

        # 提亮程度:唯一的动画变量,0 = 平时的最暗档
        self._level = 0.0
        self._target = -1.0
        self._hovered = False
        self._idle_opacity = self._read_idle_opacity()
        # 已经提交(写进配置文件)的值:拖滑杆时只预览不落盘,提交时靠它去重 ——
        # 否则"松手"和"菜单关闭"两次提交会写出两条一样的日志
        self._saved_idle_opacity = self._idle_opacity
        # 把生效值回写,让配置文件自解释:不用翻代码就知道程序实际用的是多少。
        # 代价是回写之后 ini 成为权威值,再改 FIRST_RUN_IDLE_OPACITY 不会生效;
        # 想回到那个初值,把 ini 里这一行删掉即可。sync() 是为了让它立刻落盘,
        # 免得进程被强杀时这次回写丢掉。
        self._settings.setValue("idle_opacity", self._idle_opacity)
        self._settings.sync()
        self._refresh_seconds = self._read_refresh_seconds()
        self._saved_refresh_seconds = self._refresh_seconds
        # 与 idle_opacity 一样回写:打开配置文件就能看到程序实际用的间隔
        self._settings.setValue("refresh_seconds", self._refresh_seconds)

        self._warn_threshold = self._read_threshold("warn_threshold", WARN_THRESHOLD)
        self._danger_threshold = self._read_threshold("danger_threshold", DANGER_THRESHOLD)
        self._lit_threshold = self._read_threshold("lit_threshold", LIT_THRESHOLD)
        self._enforce_threshold_order()
        self._saved_thresholds = self.thresholds
        for key, value in self.thresholds.items():
            self._settings.setValue(f"{key}_threshold", value)
        self._settings.sync()
        log.debug(
            "event=config file=%s idle_opacity=%s refresh_seconds=%d warn=%.2f lit=%.2f danger=%.2f",
            settings_path(),
            self._idle_opacity,
            self._refresh_seconds,
            self._warn_threshold,
            self._lit_threshold,
            self._danger_threshold,
        )

        self._bars: list[DiskBar] = []
        self._refresh_drives(reason="startup", force=True)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._apply_level)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(self._refresh_seconds * 1000)

        self._apply_level(0.0)  # 先按最暗档铺一次,避免动画不触发时停留在全亮
        self.refresh()
        self._restore_position()
        if self._edge_hide:
            # 上次就贴在边上:重启后接着贴(否则会停在边上却不再收起)。
            # 收起的定时器交给 showEvent 去排 —— 这里排会被随后的 show() 清掉。
            self.snap_to_edge()

    # ---- 持久化 ----
    def _read_idle_opacity(self) -> float:
        try:
            value = float(self._settings.value("idle_opacity", FIRST_RUN_IDLE_OPACITY))
        except (TypeError, ValueError):
            log.warning("event=config_invalid key=idle_opacity fallback=%s", FIRST_RUN_IDLE_OPACITY)
            return FIRST_RUN_IDLE_OPACITY
        clamped = min(1.0, max(MIN_OPACITY, value))
        if clamped != value:
            # 夹取会静默改写你写的值,是"调了却不生效"的经典来源,必须留痕
            log.warning("event=config_clamped key=idle_opacity value=%s used=%s", value, clamped)
        return clamped

    @property
    def idle_opacity(self) -> float:
        """未报警时的不透明度(托盘菜单标题显示的、也是配置文件里那个值)。"""
        return self._idle_opacity

    def preview_idle_opacity(self, value: float) -> None:
        """拖滑杆时的实时预览:只改画面,**不写盘、不记日志**。

        拖一次会发几十个 `valueChanged`,每个都落盘既浪费又把日志刷爆;提交归
        `set_idle_opacity`(松手 / 菜单关闭时)。
        """
        value = min(1.0, max(MIN_OPACITY, value))
        if value == self._idle_opacity:
            return
        self._idle_opacity = value
        self._apply_level(self._level)

    def set_idle_opacity(self, value: float) -> None:
        """提交"未报警时的透明度":落盘 + 记一条 `event=idle_opacity`。

        只动最暗档那一端:`_apply_level` 里 `alpha = lerp(idle, LIVE, level)`,
        所以悬停 / 提亮中(level > 0)画面不受影响,松手回到最暗档才看得出差别。

        画面可能已经被 `preview_idle_opacity` 改过,所以**不能**拿"值没变"当跳过的理由 ——
        那样拖完之后根本不会写盘;去重看的是"这个值是否已经落过盘"。
        """
        value = min(1.0, max(MIN_OPACITY, value))
        self._idle_opacity = value
        if value == self._saved_idle_opacity:
            return  # 已经提交过同一个值(松手 + 菜单关闭会各来一次),不重复写盘与记日志
        self._saved_idle_opacity = value
        self._settings.setValue("idle_opacity", value)
        self._settings.sync()  # 立刻落盘:菜单里改完就断电也不该丢
        self._apply_level(self._level)
        log.info("event=idle_opacity value=%.2f", value)

    def _read_refresh_seconds(self) -> int:
        """读刷新间隔(秒)。配置里可以写任意整数,超出量程就夹取并留痕。"""
        try:
            value = int(self._settings.value("refresh_seconds", REFRESH_DEFAULT_S))
        except (TypeError, ValueError):
            log.warning("event=config_invalid key=refresh_seconds fallback=%d", REFRESH_DEFAULT_S)
            return REFRESH_DEFAULT_S
        clamped = min(REFRESH_MAX_S, max(REFRESH_MIN_S, value))
        if clamped != value:
            log.warning("event=config_clamped key=refresh_seconds value=%s used=%s", value, clamped)
        return clamped

    @property
    def refresh_seconds(self) -> int:
        """磁盘刷新间隔(秒;托盘步进器显示的就是它)。"""
        return self._refresh_seconds

    def preview_refresh_seconds(self, seconds: int) -> None:
        """改动过程中立刻生效(下次读盘就按新间隔),**不落盘、不记日志**。"""
        seconds = min(REFRESH_MAX_S, max(REFRESH_MIN_S, seconds))
        if seconds == self._refresh_seconds:
            return
        self._refresh_seconds = seconds
        self._timer.setInterval(seconds * 1000)

    def set_refresh_seconds(self, seconds: int) -> None:
        """提交刷新间隔:落盘 + 记一条 `event=refresh_interval`(菜单关闭 / 回车时调它)。"""
        seconds = min(REFRESH_MAX_S, max(REFRESH_MIN_S, seconds))
        self.preview_refresh_seconds(seconds)
        if seconds == self._saved_refresh_seconds:
            return  # 已经提交过同一个值,不重复写盘与记日志
        self._saved_refresh_seconds = seconds
        self._settings.setValue("refresh_seconds", seconds)
        self._settings.sync()
        log.info("event=refresh_interval seconds=%d", seconds)

    def _read_threshold(self, key: str, fallback: float) -> float:
        """读一条警戒线(0.01 ~ 1.00)。非法值退回默认,越界夹取并留痕。"""
        try:
            value = float(self._settings.value(key, fallback))
        except (TypeError, ValueError):
            log.warning("event=config_invalid key=%s fallback=%s", key, fallback)
            return fallback
        clamped = min(THRESHOLD_MAX, max(THRESHOLD_MIN, round(value, 2)))
        if clamped != value:
            log.warning("event=config_clamped key=%s value=%s used=%s", key, value, clamped)
        return clamped

    def _enforce_threshold_order(self) -> None:
        """只保证橙色比红色低一格(不然橙色永远轮不到);**常亮阈值不受红色约束**。

        常亮阈值可以高于红色:那样红色会先触发常亮(`is_lit` 取两者中更低的那条),
        也就是"红线即常亮" —— 常亮默认值与红色相同,走的正是这条路。
        """
        if self._danger_threshold - THRESHOLD_MIN < THRESHOLD_GAP:
            raised = THRESHOLD_MIN + THRESHOLD_GAP
            log.warning(
                "event=config_clamped key=danger_threshold value=%s used=%s",
                self._danger_threshold,
                raised,
            )
            self._danger_threshold = raised
        ceiling = self._danger_threshold - THRESHOLD_GAP
        if self._warn_threshold > ceiling:
            log.warning(
                "event=config_clamped key=warn_threshold value=%s used=%s",
                self._warn_threshold,
                ceiling,
            )
            self._warn_threshold = ceiling

    @property
    def thresholds(self) -> dict[str, float]:
        """当前生效的三条阈值:`warn`(橙,只影响颜色)、`danger`(红)、`lit`(常亮)。"""
        return {
            "warn": self._warn_threshold,
            "danger": self._danger_threshold,
            "lit": self._lit_threshold,
        }

    def preview_thresholds(self, *, warn: float, lit: float, danger: float) -> None:
        """改动过程中立刻生效(重绘颜色 + 重算常亮),**不落盘、不记日志**。

        常亮阈值与红色阈值都会改变"界面是否常亮",所以改完必须重算 `_retarget`。
        """
        warn = min(THRESHOLD_MAX - THRESHOLD_GAP, max(THRESHOLD_MIN, round(warn, 2)))
        lit = min(THRESHOLD_MAX, max(THRESHOLD_MIN, round(lit, 2)))
        danger = min(THRESHOLD_MAX, max(warn + THRESHOLD_GAP, round(danger, 2)))
        if (warn, lit, danger) == (
            self._warn_threshold,
            self._lit_threshold,
            self._danger_threshold,
        ):
            return
        self._warn_threshold = warn
        self._lit_threshold = lit
        self._danger_threshold = danger
        for bar in self._bars:
            bar.set_thresholds(warn, danger)
        if self._edge_hidden:
            self.update()  # 尾巴上的线段颜色也随阈值变;进度条被藏了,它得自己重画
        self._retarget(animate=False)

    def set_thresholds(self, *, warn: float, lit: float, danger: float) -> None:
        """提交三条阈值:落盘 + 一条 `event=thresholds`(菜单关闭 / 回车时调它)。"""
        self.preview_thresholds(warn=warn, lit=lit, danger=danger)
        current = self.thresholds
        if current == self._saved_thresholds:
            return  # 已经提交过同一组值,不重复写盘与记日志
        self._saved_thresholds = current
        for key, value in current.items():
            self._settings.setValue(f"{key}_threshold", value)
        self._settings.sync()
        log.info(
            "event=thresholds warn=%.2f lit=%.2f danger=%.2f",
            current["warn"],
            current["lit"],
            current["danger"],
        )

    def current_setting(self, key: str) -> float | bool | list[str] | None:
        """某个可还原项的当前生效值(对话框与执行都用它,免得两处各写一套取法)。"""
        if key == "idle_opacity":
            return self._idle_opacity
        if key == "refresh_seconds":
            return self._refresh_seconds
        if key == EDGE_HIDE_KEY:
            return self._edge_hide
        if key == drives.DRIVES_KEY:
            return self._wanted_drives
        return self.thresholds[key.removesuffix("_threshold")]

    def resettable_diff(self) -> list[tuple[str, str, str]]:
        """「还原配置」会改掉的项:(名字, 当前值文本, 默认值文本),**只列有差异的**。

        纯计算:不碰 ini、不弹窗,所以能直接断言(见 `tests/test_window.py`)。
        """
        rows = []
        for key, name, default, fmt in RESETTABLE_SETTINGS:
            current = self.current_setting(key)
            if current != default:
                rows.append((name, fmt(current), fmt(default)))
        return rows

    def reset_settings(self) -> list[str]:
        """把可还原项恢复成默认:立刻生效 + 落盘,返回被改掉的名字(只含有差异的)。

        位置 `pos_x` / `pos_y` **故意不在可还原项里** —— 还原不该把窗口搬走,ini 里始终留着位置。
        走的是与托盘菜单同一条通道(`set_idle_opacity` / `set_refresh_seconds` / `set_edge_hide` /
        `set_drives` / `set_thresholds`),所以不另写一套生效逻辑;三条阈值合成一次提交,
        免得写出三条 `event=thresholds`。
        """
        targets = {
            key: default
            for key, _name, default, _fmt in RESETTABLE_SETTINGS
            if self.current_setting(key) != default
        }
        if "idle_opacity" in targets:
            self.set_idle_opacity(targets["idle_opacity"])
        if "refresh_seconds" in targets:
            self.set_refresh_seconds(targets["refresh_seconds"])
        if EDGE_HIDE_KEY in targets:
            self.set_edge_hide(bool(targets[EDGE_HIDE_KEY]))
        if drives.DRIVES_KEY in targets:
            self.set_drives(targets[drives.DRIVES_KEY])
        wanted = self.thresholds
        for key in ("warn_threshold", "danger_threshold", "lit_threshold"):
            if key in targets:
                wanted[key.removesuffix("_threshold")] = targets[key]
        if wanted != self.thresholds:
            self.set_thresholds(warn=wanted["warn"], lit=wanted["lit"], danger=wanted["danger"])
        log.info("event=config_reset keys=%s", ",".join(sorted(targets)))
        return [name for key, name, _default, _fmt in RESETTABLE_SETTINGS if key in targets]

    def _restore_position(self) -> None:
        x = self._settings.value("pos_x")
        y = self._settings.value("pos_y")
        screen = QApplication.primaryScreen()

        if x is not None and y is not None:
            self.move(int(x), int(y))
        elif screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - EDGE_MARGIN, area.top() + EDGE_MARGIN)

        if screen is not None:  # 换过显示器后别把窗口留在屏幕外
            area = screen.availableGeometry()
            self.move(
                min(max(self.x(), area.left()), area.right() - self.width()),
                min(max(self.y(), area.top()), area.bottom() - self.height()),
            )
        log.debug("event=position x=%d y=%d", self.x(), self.y())

    def _save_position(self) -> None:
        self._settings.setValue("pos_x", self.x())
        self._settings.setValue("pos_y", self.y())
        log.debug("event=position_saved x=%d y=%d", self.x(), self.y())

    # ---- 盘集:显示哪些盘 ----
    def _read_drives(self) -> list[str] | None:
        """读 ini 的 `drives`:auto(默认)= 用探测结果;写了字母表就是白名单。非法项丢掉并留痕。"""
        raw = self._settings.value(drives.DRIVES_KEY, drives.DRIVES_AUTO)
        wanted, dropped = drives.parse_drives(raw)
        if dropped:
            log.warning(
                "event=config_invalid key=%s value=%s dropped=%s",
                drives.DRIVES_KEY,
                raw,
                ",".join(dropped),
            )
        return wanted

    @property
    def drives(self) -> list[str] | None:
        """要显示的盘符(None = 自动),也是 ini 里 `drives` 的生效值。"""
        return self._wanted_drives

    def set_drives(self, wanted: list[str] | None) -> None:
        """提交要显示的盘符:落盘 + 立刻按新集合重建盘条。"""
        self._wanted_drives = wanted
        self._settings.setValue(drives.DRIVES_KEY, drives.drives_text(wanted))
        self._settings.sync()
        self._refresh_drives(reason="config", force=True)

    def _build_bars(self, paths: list[str]) -> None:
        """按盘符列表重建盘条 —— 盘数决定窗口高度,盘集一变就整批换掉(逐个增删不划算)。"""
        for bar in self._bars:
            bar.hide()
            bar.setParent(None)  # 立刻脱离窗口树,别等 deleteLater 那一轮事件循环
            bar.deleteLater()
        self._bars = []
        for path in paths:
            bar = DiskBar(path, self, self._warn_threshold, self._danger_threshold)
            # 详情文字挂在**窗口**上,而盘条铺满整块区域、自己又不处理 tooltip —— 不给它设这个,
            # 悬停在盘条上时那条 tooltip 就归盘条(它没有文字 → 不弹),表现为"详情时有时无"。
            # 让盘条不接收鼠标事件,悬停统一归窗口,整块区域都能弹(不必逐个给子控件设 tooltip)。
            bar.setAttribute(Qt.WA_TransparentForMouseEvents)
            self._bars.append(bar)
        self.apply_scale(self._ui_scale)  # 尺寸、每个盘条的位置与高度;贴边锚点也跟着重算
        # 窗口刚被改过:按光标位置重新判断要不要收起 —— 正收着的时候插上移动硬盘,重建之后
        # 不该一直摊开着(尺寸重排本来就会先把它展开)
        self._settle_edge()

    def _refresh_drives(self, *, reason: str, force: bool = False) -> bool:
        """盘集变了就重建盘条(每次刷新都来看一眼),返回是否重建了。"""
        detected = drives.detect_drives()
        paths = drives.visible_drives(detected, self._wanted_drives)
        if not force and paths == [bar.path for bar in self._bars]:
            return False
        log.info(
            "event=drives use=%s detected=%s reason=%s",
            ",".join(paths) or "-",
            ",".join(detected) or "-",
            reason,
        )
        self._build_bars(paths)
        return True

    # ---- 数据刷新 ----
    def refresh(self) -> None:
        self._refresh_drives(reason="refresh")  # 先看盘集变没变(插拔移动硬盘就靠这里)
        for bar in self._bars:
            bar.reload()
        detail = self.summary()  # 完整明细:挂在窗口上,也进调试日志
        self.setToolTip(detail)
        self.updated.emit(self.summary(compact=True))  # 托盘:紧凑版,靠它同步悬停提示
        self._retarget()
        if self._edge_hidden:
            self.update()  # 尾巴上的小线段跟着数据走:进度条被藏了,刷新得自己重画
        # target 是同步算出的终值(动画还在跑时它也已确定),所以断言提亮结果不必等动画
        log.debug(
            "event=refresh max_ratio=%.4f level=%.2f target=%.2f %s",
            self._max_ratio(),
            self._level,
            self._target,
            " | ".join(detail.split("\n")),
        )

    def summary(self, *, compact: bool = False) -> str:
        """状态文字(悬停提示):完整版挂在窗口上,紧凑版发给托盘 —— 出处只有 `summary_text`。"""
        return summary_text(self._bars, compact=compact)

    def _max_ratio(self) -> float:
        ratios = [r for bar in self._bars if (r := bar.ratio()) is not None]
        return max(ratios) if ratios else 0.0

    # ---- 提亮状态机 ----
    def _retarget(self, *, animate: bool = True) -> None:
        """按“磁盘状态”与“是否悬停”重算提亮目标。

        悬停走硬切(animate=False):鼠标一动就要立刻响应,不能有拖尾;
        阈值提亮保留补间,因为磁盘水位变化缓慢,直接跳变反而突兀。
        """
        lit_by_usage = is_lit(self._max_ratio(), self._lit_threshold, self._danger_threshold)
        target = 1.0 if (self._hovered or lit_by_usage) else 0.0
        if target == self._target:
            return
        self._target = target
        self._anim.stop()
        log.debug(
            "event=reveal target=%.2f from=%.2f reason=%s",
            target,
            self._level,
            "hover" if self._hovered else "usage",
        )
        if not animate:
            self._apply_level(target)
            return
        self._anim.setStartValue(self._level)
        self._anim.setEndValue(target)
        self._anim.start()

    def _apply_level(self, value) -> None:
        """动画每帧回调:把提亮程度换算成前景不透明度下发。

        这里刻意不使用 setWindowOpacity —— 那会把整层像素相乘,
        连隐形命中层一起压到 alpha 0,鼠标就会穿透窗口,拖动和悬停失效。
        """
        self._level = float(value)
        alpha = lerp(self._idle_opacity, LIVE_OPACITY, self._level) * 255
        self._tail_alpha = alpha
        for bar in self._bars:
            bar.set_alpha(alpha)
        if self._edge_hidden:
            self.update()  # 尾巴形态下进度条不可见:变亮得自己重画一次

    # ---- 鼠标悬停唤醒:硬切,移开立即还原 ----
    def enterEvent(self, event) -> None:
        self._hovered = True
        log.debug("event=hover state=enter")
        self._retarget(animate=False)
        # 收成尾巴时,指针蹭到尾巴就长回来 —— 这就是"贴边自动隐藏"的唤醒入口
        self.show_handle_view()

    def leaveEvent(self, event) -> None:
        self._hovered = False
        log.debug("event=hover state=leave")
        self._retarget(animate=False)
        if self._edge is not None and not self._edge_sliding:
            # 延迟收起:没有它,光标划过屏幕边缘会带着窗口来回弹。
            # 形变期间不收:窗口自己在动,Qt 会跟着补一个 Leave ——
            # 不管它的话,刚蹭出来的窗口会立刻又被排上收起(实测就是这个"反着来"的现象)。
            self._edge_timer.start()

    # ---- 绘制:隐形命中层;尾巴形态再补上小尾巴本身 ----
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, HIT_AREA_ALPHA))
        if self._edge_hidden:
            self._paint_tail(painter)

    def _paint_tail(self, painter: QPainter) -> None:
        """尾巴:**一条断线**,一段表示一个盘(每段长度固定、颜色随阈值、整条线平行于屏幕边)。

        没有底板也没有描边 —— 因此窗口那一层隐形矩形(见 `paintEvent`)仍是唯一的命中来源,别动它;
        线只有 2px 粗,能蹭到全靠那层矩形。

        按 `tail_layout` 给的尺寸画,再让画布缩放到**当前窗口大小** —— 收起途中窗口一直在缩,
        同一段代码顺带画出了"缩成一条"的过程;停稳时两者相等,缩放正好是 1,像素对齐。方向
        (横 / 竖)由布局给,这里没有分支。
        """
        painter.setRenderHint(QPainter.Antialiasing)
        width, height, lines = tail_layout(self._edge or "top", len(self._bars))
        painter.scale(self.width() / max(1, width), self.height() / max(1, height))
        alpha = max(self._tail_alpha, TAIL_MIN_ALPHA)
        for (x, y, w, h), bar in zip(lines, self._bars, strict=False):
            painter.fillRect(QRectF(x, y, w, h), with_alpha(bar.status_rgb(), alpha))

    # ---- 鼠标穿透:开启后事件直接落到桌面,不能拖动、不能悬停 ----
    def set_click_through(self, enabled: bool) -> None:
        """只能从托盘菜单关闭,所以别把它默认打开。"""
        if bool(self.windowFlags() & Qt.WindowTransparentForInput) == enabled:
            return
        self.setWindowFlag(Qt.WindowTransparentForInput, enabled)
        self.show()  # 改窗口标志会隐藏窗口,必须重新显示
        self._hovered = False
        self._retarget(animate=False)
        # 开启后只能从托盘关掉,日志里必须留下这一笔,否则"窗口怎么点都没反应"查不出来
        log.info("event=click_through enabled=%s", enabled)
        if enabled and self._edge_hide:
            # 穿透开着窗口收不到任何鼠标事件,贴边必然失效 —— 两者互斥,这里直接把贴边关掉
            self.set_edge_hide(False)

    def reveal(self) -> None:
        """把小组件叫出来(第二个实例启动时用,见 `single_instance.py`)。

        穿透开着就一并关掉:否则显示出来也点不到,用户只会觉得"启动了但没反应"。
        """
        if self.windowFlags() & Qt.WindowTransparentForInput:
            self.set_click_through(False)
        if not self.isVisible():
            self.show()
            log.info("event=window state=shown reason=second_instance")
        self.show_handle_view()  # 收起成尾巴时也要长回来:叫它就是让人看见它
        self.raise_()  # 置顶窗口之间的次序:确保它不在别的窗口后面

    def showEvent(self, event) -> None:
        """显示时:贴着边的就展开,指针不在上面就重新排上收起。

        启动路径就是走这里:窗口构造完才 `show()`,所以"重启后接着贴边"的定时器必须在这里排
        (`__init__` 里排会被这次 `show()` 清掉)。
        """
        super().showEvent(event)
        self.show_handle_view()
        # 只决定要不要排收起,**不动悬停态**:刚 show 出来的窗口还没收到 Enter,悬停由事件负责
        # (重判悬停那套见 `_settle_edge`)。
        if self._edge is not None and not self._cursor_on_window():
            self._edge_timer.start()

    # ---- 隐藏:恢复入口只有托盘 ----
    def hide_widget(self) -> None:
        """隐藏小组件,并把悬停态清干净 —— 否则下次显示出来会停在全亮。"""
        self._hovered = False
        self._retarget(animate=False)
        self.hide()
        log.info("event=window state=hidden")

    # ---- 贴边隐藏:贴住边就折叠成小尾巴,指针蹭到尾巴再长回来 ----
    def _read_edge_hide(self) -> bool:
        """读"贴边隐藏"开关:非法值退回默认并留痕(与其它配置项同一套做法)。"""
        raw = self._settings.value(EDGE_HIDE_KEY, EDGE_HIDE_DEFAULT)
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        log.warning("event=config_invalid key=%s fallback=%s", EDGE_HIDE_KEY, EDGE_HIDE_DEFAULT)
        return EDGE_HIDE_DEFAULT

    @property
    def edge_hide(self) -> bool:
        """贴边隐藏开关(托盘那个勾选,也是 ini 里 `edge_hide` 的值)。"""
        return self._edge_hide

    @property
    def edge(self) -> str | None:
        """当前贴在哪条边(left / right / top / bottom);没贴边是 None。"""
        return self._edge

    @property
    def edge_hidden(self) -> bool:
        """此刻是否是尾巴形态(收起途中也算)。"""
        return self._edge_hidden

    def full_size(self) -> QSize:
        """展开形态的尺寸(当前缩放下的)—— 用例与形变目标都用它,不另推一遍。"""
        return layout_size(self._ui_scale, len(self._bars) or 1)

    def tail_size(self, edge: str | None = None) -> QSize:
        """尾巴尺寸:**固定的设备像素**,不随主界面缩放变(那是展开形态的事)。

        `edge` 决定线的方向(见 `tail_layout`),默认用当前贴着的那条边;没贴边时按横线那套算。
        """
        rows = len(self._bars) or 1
        width, height, _ = tail_layout(edge or self._edge or "top", rows)
        return QSize(width, height)

    def set_edge_hide(self, enabled: bool) -> None:
        """提交开关:落盘 + 一条日志;开启时立刻按当前位置判定一次,关闭时立刻退出贴边。"""
        self._edge_hide = enabled
        self._settings.setValue(EDGE_HIDE_KEY, enabled)
        self._settings.sync()
        log.info("event=edge_hide enabled=%s", enabled)
        if enabled:
            self.snap_to_edge()
        else:
            self.leave_edge()

    def _screen_bounds(self) -> tuple[int, int, int, int] | None:
        """窗口所在屏幕的工作区,换成**左下开区间**口径(`snap_edge` / `tail_pos` 用这个口径)。

        用工作区而不是整块屏幕:底边自动避开任务栏,贴上去才真的蹭得到。
        """
        screen = QApplication.screenAt(self.frameGeometry().center()) or (
            QApplication.primaryScreen()
        )
        if screen is None:
            return None
        area = screen.availableGeometry()
        return (area.left(), area.top(), area.right() + 1, area.bottom() + 1)

    def _expand_now(self, position: QPoint | None = None) -> None:
        """立刻回到展开形态(不补间):尺寸、子控件可见性、位置一次到位。

        "立刻"是刻意的:判定贴边(要量展开矩形)、按尾巴开始拖动、关掉开关,这些都在用户手上,
        补间会让这几步对不上量到的尺寸。
        """
        self._edge_anim.stop()
        self._edge_sliding = False
        self._edge_hidden = False
        self.setFixedSize(self.full_size())
        for bar in self._bars:
            bar.show()
        if position is not None:
            self.move(position)

    def snap_to_edge(self) -> None:
        """按当前位置判定贴哪条边:够近就吸附齐平,不够近就退出贴边(拖动结束、开关打开时调)。

        判定要用**展开矩形**:一是量尺寸(尾巴只有十几像素,量不出"贴哪条边"),二是量位置。
        位置分两种情形,拿错一种就会把用户的操作撤销掉:

        - 拖动松手时:窗口已经在用户放下的位置 —— **就地**展开,绝不搬回旧锚点(搬回去之后再判,
          量到的是屏幕边,判定结果就不再是用户放下的位置了);
        - 收在尾巴里时:窗口的位置是尾巴的,得先长回锚点(那才是展开位置)。

        压出屏幕的那部分也算贴上(见 `snap_edge`),所以"拖出去一半松手"会被拉回边上摆正。
        """
        self._expand_now(self._edge_anchor if self._edge_hidden else None)
        bounds = self._screen_bounds()
        if bounds is None:
            return
        edge = snap_edge(self.x(), self.y(), self.width(), self.height(), bounds)
        if edge is None:
            self.leave_edge(keep_position=True)  # 已经拖离屏幕边:窗口留在放下的位置
            return
        if edge == "left":
            anchor = QPoint(bounds[0], self.y())
        elif edge == "right":
            anchor = QPoint(bounds[2] - self.width(), self.y())
        elif edge == "top":
            anchor = QPoint(self.x(), bounds[1])
        else:
            anchor = QPoint(self.x(), bounds[3] - self.height())
        self._edge = edge
        self._edge_anchor = anchor
        self._edge_timer.stop()
        self.move(anchor)
        log.info("event=edge_snap edge=%s x=%d y=%d", edge, anchor.x(), anchor.y())

    def leave_edge(self, *, keep_position: bool = False) -> None:
        """退出贴边:取消定时器、长回展开形态、清干净状态(没贴边时什么也不做)。

        `keep_position=True` 用于"窗口已经被拖到别处"的场合(松手时判成不贴边):位置要留在用户
        放下的地方 —— 搬回旧锚点就是把这趟拖动撤销掉。开关关掉时相反:窗口该回到贴边的展开位置。
        """
        self._edge_timer.stop()
        if self._edge is None:
            return
        self._edge = None
        self._expand_now(None if keep_position else self._edge_anchor)
        log.debug("event=edge_hide state=off")

    def show_handle_view(self) -> None:
        """从尾巴状态长回展开形态(指针蹭到尾巴、或第二个实例把窗口叫出来时调)。"""
        self._edge_timer.stop()
        if not self._edge_hidden:
            return
        self._edge_hidden = False
        for bar in self._bars:
            bar.show()  # 先显示:窗口在长大的过程中会把它们一点点露出来
        self._morph_to(self._edge_anchor, self.full_size())
        log.debug("event=edge_hide state=shown edge=%s", self._edge)

    def _hide_to_edge(self) -> None:
        """收起:折叠成贴着屏幕边的尾巴。指针还在窗口上、或正在拖动时不动(否则会跟用户抢)。"""
        if (
            self._edge is None
            or self._edge_hidden
            or self._hovered
            or self._drag_offset is not None
        ):
            return
        bounds = self._screen_bounds()
        if bounds is None:
            return
        tail = self.tail_size(self._edge)  # 方向随边:贴左/右是竖线段
        full = self.full_size()
        target = tail_pos(
            self._edge,
            self._edge_anchor.x(),
            self._edge_anchor.y(),
            full.width(),
            full.height(),
            tail.width(),
            tail.height(),
            bounds,
        )
        self._edge_hidden = True
        for bar in self._bars:
            bar.hide()  # 尾巴里没有进度条的位置,那点地方只放得下小线段
        self._morph_to(QPoint(*target), tail)
        log.debug("event=edge_hide state=hidden edge=%s", self._edge)

    def _morph_to(self, target: QPoint, size: QSize) -> None:
        """在展开形态与尾巴之间补间:位置与尺寸同时插值(与提亮共用 QVariantAnimation 那一套)。"""
        self._edge_start = self.pos()
        self._edge_target = QPoint(target)
        self._edge_start_size = self.size()
        self._edge_target_size = QSize(size)
        self._edge_anim.stop()
        self._edge_sliding = True
        self._edge_anim.setStartValue(0.0)
        self._edge_anim.setEndValue(1.0)
        self._edge_anim.start()

    def _morph_done(self) -> None:
        """形变完了:窗口已停稳,于是按光标的**实际位置**重判一次(`_settle_edge`)。

        必须补这一下:形变期间来的 Leave 是被跳过的(见 `leaveEvent` —— 窗口自己在动,Qt 会
        补发假的 Leave),可用户若在那 250ms 里就把鼠标移开了,那个 Leave 就是**真的** ——
        跳掉之后没有任何事件会再来,窗口会一直展开着。
        """
        self._edge_sliding = False
        self._settle_edge()

    def _cursor_on_window(self) -> bool:
        """光标此刻是否落在窗口上 —— "悬停 / 该不该收起"这类判定的唯一依据。"""
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def _settle_edge(self) -> None:
        """按光标的**实际位置**重判悬停,并决定要不要(重新)排上收起 —— 贴边这件事只认这条判据。

        这几处都走它,共同点是"窗口刚被挪过 / 变过,此刻的 Enter / Leave 未必作数":
        形变结束(`_morph_done`)、拖动松手、右键菜单关闭、缩放提交(`set_ui_scale` —— 调整尺寸
        期间窗口是**暂时展开**的,结束之后得自己收回去)。菜单弹出期间窗口收不到鼠标事件,窗口
        自己动时 Qt 还会补发假的 Leave,所以不能指望事件。
        """
        if not self.isVisible():
            return  # 已经藏起来的窗口收不到 Enter / Leave,不能碰它的悬停态(见 contextMenuEvent)
        self._hovered = self._cursor_on_window()
        self._retarget(animate=False)
        if self._edge is not None and not self._hovered and not self._edge_sliding:
            self._edge_timer.start()

    def _morph_step(self, value) -> None:
        """动画每帧回调:按进度在起点与终点之间插值(位置与尺寸一起动)。"""
        progress = float(value)
        self.setFixedSize(
            max(
                1,
                round(
                    lerp(self._edge_start_size.width(), self._edge_target_size.width(), progress)
                ),
            ),
            max(
                1,
                round(
                    lerp(self._edge_start_size.height(), self._edge_target_size.height(), progress)
                ),
            ),
        )
        self.move(
            round(lerp(self._edge_start.x(), self._edge_target.x(), progress)),
            round(lerp(self._edge_start.y(), self._edge_target.y(), progress)),
        )

    # ---- 整窗缩放(托盘「调整尺寸」滑杆)----
    def _read_ui_scale(self) -> float:
        """读记住的缩放系数。非法值退回默认,越界夹取并留痕(与其它配置项一套做法)。"""
        try:
            value = float(self._settings.value("ui_scale", UI_SCALE_DEFAULT))
        except (TypeError, ValueError):
            log.warning("event=config_invalid key=ui_scale fallback=%s", UI_SCALE_DEFAULT)
            return UI_SCALE_DEFAULT
        clamped = min(UI_SCALE_MAX, max(UI_SCALE_MIN, round(value, 2)))
        if clamped != value:
            log.warning("event=config_clamped key=ui_scale value=%s used=%s", value, clamped)
        return clamped

    @property
    def ui_scale(self) -> float:
        """整窗缩放系数(拖动时生效的那个,也是 ini 里 `ui_scale` 那个值)。"""
        return self._ui_scale

    def preview_ui_scale(self, scale: float) -> None:
        """拖动过程中即时生效,**不落盘、不记日志**(与透明度滑杆同一套分工)。"""
        scale = min(UI_SCALE_MAX, max(UI_SCALE_MIN, round(scale, 3)))
        if scale == self._ui_scale:
            return
        self._ui_scale = scale
        self.apply_scale(scale)

    def set_ui_scale(self, scale: float) -> None:
        """提交缩放:落盘 + 一条 `event=ui_scale`(滑杆松手 / 菜单关闭时调)。

        提交即"调整结束":顺手按光标位置重新判一次贴边(`_settle_edge`)—— 拖动期间窗口是
        **暂时展开**的(用户正在调大小,看得见才好对齐),收着的那份得在结束之后自己收回去。
        """
        self.preview_ui_scale(scale)
        self._settle_edge()
        if self._ui_scale == self._saved_ui_scale:
            return
        self._saved_ui_scale = self._ui_scale
        self._settings.setValue("ui_scale", self._ui_scale)
        self._settings.sync()  # 立刻落盘:调完就断电也不该丢
        log.info("event=ui_scale value=%.2f", self._ui_scale)

    def apply_scale(self, scale: float) -> None:
        """按系数重排整窗:窗口尺寸、每个盘条的位置与高度、绘制变换都乘它。"""
        pad, gap, row_h, width = scaled_layout(scale, len(self._bars) or 1)
        self.setFixedSize(layout_size(scale, len(self._bars) or 1))
        if self._edge is not None:
            # 尺寸变了:齐平位置与尾巴大小都要重算。`snap_to_edge` 会先长回展开形态再判 ——
            # 用户正在调大小,看得见才好对齐(贴边的标记只有十几像素,量不出贴哪条边)。
            self.snap_to_edge()
        for index, bar in enumerate(self._bars):
            bar.setGeometry(pad, pad + index * (row_h + gap), width - pad * 2, row_h)
            bar.set_scale(scale)
        self.update()

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        if self._edge_hidden:
            # 按尾巴 = 要挪动它:立刻长回展开形态再照常拖 —— 否则会拖着一条小线段跑,
            # 而且拖动偏移量是相对尾巴算的,一动就对不上。
            self._expand_now(self._edge_anchor)
            self._edge_timer.stop()
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            # 先按当前位置判定贴边(吸附会顺手把位置改成与屏幕边齐平),再存盘 ——
            # 存的是**展开位置**,否则重启后窗口会"少一截"。
            if self._edge_hide:
                self.snap_to_edge()
            self._save_position()
            # 松手时指针未必还在窗口上(拖出去一半再松手,窗口被拉回边上摆正,指针就落在窗口外):
            # 这时不会再有 Leave 事件进来,得按光标位置重新判一次
            self._settle_edge()

    # ---- 右键菜单:无标题条,隐藏 / 刷新 / 退出都在这里 ----
    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("隐藏", self.hide_widget)
        menu.addAction("立即刷新", self.refresh)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        log.debug("event=menu open=context")
        # 记下选中的是哪一项:它是"谁把窗口藏了/把穿透开了"的唯一证据
        menu.triggered.connect(lambda action: log.info("event=menu item=%s", action.text()))
        menu.exec(event.globalPos())

        # 菜单是 popup,弹出期间窗口收不到 enter / leave 事件,所以关掉后必须按光标实际位置重判
        # (否则鼠标已经移开了却仍算悬停,窗口会一直停在最亮)。选中的若是"隐藏",窗口此刻已经
        # 不在了 —— 那种窗口不能碰:它在窗口矩形内收不到鼠标,重判会让它带着"悬停全亮"被显示
        # 出来(实测:event=window state=hidden 紧接着 event=reveal target=1.00 reason=hover);
        # `_settle_edge` 里的 `isVisible()` 就是挡这一下的。
        self._settle_edge()


class MenuRow(QWidget):
    """托盘菜单里"控件行"的容器:自己画当前项底色。

    **实测**:菜单把当前项指到 `QWidgetAction` 那一行时,`QMenu` **不会**给它画高亮
    (纯文字项会:同一行左侧空白带的底色 30 → 43,而控件行纹丝不动)。所以想让控件行也高亮,
    只能自己画 —— 这里借用菜单项那套 style 控件来画,只画底色不画文字,于是换主题 / 换 DPI 时
    外观与纯文字项天然一致(而不是写死一个颜色)。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hovered = False

    def set_hovered(self, hovered: bool) -> None:
        """指针是否停在本行 —— 由 `TrayIcon` 的事件过滤器驱动:菜单自己收不到控件行的 move。"""
        if hovered == self._hovered:
            return
        self._hovered = hovered
        self.update()

    def paintEvent(self, event) -> None:
        if not self._hovered:
            return
        option = QStyleOptionMenuItem()
        option.initFrom(self)
        option.rect = self.rect()
        option.menuItemType = QStyleOptionMenuItem.MenuItemType.Normal
        option.checkType = QStyleOptionMenuItem.CheckType.NotCheckable
        option.menuHasCheckableItems = False
        option.maxIconWidth = 0
        option.text = ""  # 文字由行里的 QLabel 负责,这里只要那块底色
        option.state |= QStyle.StateFlag.State_Selected
        self.style().drawControl(QStyle.ControlElement.CE_MenuItem, option, QPainter(self), self)


class TrayIcon(QSystemTrayIcon):
    """托盘图标:小组件可以调到几乎看不见,这里是唯一稳定的入口。"""

    def __init__(self, window: StatusBarWindow) -> None:
        super().__init__(make_tray_icon())
        self._window = window
        self.setToolTip(APP_NAME)

        self._menu = QMenu()
        # 菜单项默认**不**显示提示(QMenu::toolTipsVisible 默认 false),要显式打开;
        # 文字来自每个 QAction 自己的 setToolTip —— 没设的项,开了也没有提示可显示
        self._menu.setToolTipsVisible(True)

        # 控件行容器 / 行内控件 各自指向该行的 action(见 eventFilter、_release_row_focus)
        self._row_actions: dict[QWidget, QAction] = {}
        self._row_controls: dict[QWidget, QAction] = {}

        # 菜单顺序:版本号放最上,两个开关其次,四行控件居中,**常用项靠下**贴近「退出」。
        # 位置完全由下面几段的调用先后决定 —— 想调顺序就是调它们的先后。
        self._add_version_row()
        self._add_data_dir_row()  # 紧挨版本号:排查时要的就是这一对(哪一版 / 文件在哪)
        self._menu.addSeparator()  # 上面两行是"关于 / 自检",下面全是操作,隔一道
        #
        # 两个开关都是行内右侧复选框:菜单里一个"可勾选菜单项"都不留,Qt 才不会保留勾选列
        # (勾选列要花掉约 20px,数字与做法见 开发经验第 39 条)
        self._click_through = QCheckBox(self._menu)
        self._click_through.setToolTip("开启后窗口不再接收鼠标,只能从本菜单关掉")
        self._click_through.toggled.connect(window.set_click_through)
        self._add_row("鼠标穿透", self._click_through, "鼠标穿透", self._click_through.toolTip())

        self._autostart = QCheckBox(self._menu)
        self._autostart.setToolTip("开机后自动启动,可回本菜单取消")
        self._autostart.toggled.connect(self._set_autostart)
        self._add_row("开机自启", self._autostart, "开机自启", self._autostart.toolTip())

        self._edge_hide = QCheckBox(self._menu)
        self._edge_hide.setToolTip("贴到屏幕边就收起成一个小尾巴;鼠标蹭到它就长回来")
        self._edge_hide.toggled.connect(window.set_edge_hide)
        self._add_row("贴边隐藏", self._edge_hide, "贴边隐藏", self._edge_hide.toolTip())

        # 「还原配置」不常用:紧跟着两个开关放,不占贴近「退出」的那段常用区
        self._add_reset_row(window)

        # 四行控件(顺序固定):刷新频率、橙色阈值、红色阈值、常亮阈值、透明度
        self._add_refresh_row(window)
        self._threshold_spins: dict[str, QSpinBox] = {}
        for label, name, tooltip, key in THRESHOLD_ROWS:
            self._add_threshold_row(window, key=key, label=label, name=name, tooltip=tooltip)
        self._add_scale_row(window)  # 缩放与透明度都是"外观尺寸类",排在一起
        self._add_opacity_row(window)
        # 四个步进器统一宽度,按各自的 sizeHint 取最大(含最大位数与加减箭头,随字体/DPI 自适应)
        # —— 写死宽度会挤掉内容(实测 72px 时"85%"只显示出"85'");右对齐之后左边缘也跟着齐
        spins = (self._refresh_spin, *self._threshold_spins.values())
        widest = max(spin.sizeHint().width() for spin in spins)
        for spin in spins:
            spin.setFixedWidth(widest)
        self._sync_threshold_rows()

        # 常用项靠下:贴着「退出」,不必在菜单里长距离找
        self._visibility = self._menu.addAction("隐藏", self._toggle_visible)
        self._visibility.setToolTip("显示 / 隐藏小组件(隐藏后只能从托盘恢复)")
        self._menu.addAction("立即刷新", window.refresh).setToolTip("立刻读一次磁盘占用")

        self._menu.addSeparator()
        self._menu.addAction("退出", QApplication.quit).setToolTip("退出 stabar")
        self._menu.aboutToShow.connect(self._sync_state)
        # 收起时清掉控件行的焦点:留着它,再打开时 Qt 会把焦点恢复给上次那个步进器
        self._menu.aboutToHide.connect(self._release_row_focus)
        # 托盘菜单弹出的唯一留痕:真右键托盘图标时只有它能证明"点击真的到了图标上"
        self._menu.aboutToShow.connect(lambda: log.debug("event=menu open=tray"))
        # 同窗口右键菜单:记下选中的是哪一项
        self._menu.triggered.connect(lambda action: log.info("event=menu item=%s", action.text()))
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)
        window.updated.connect(self.setToolTip)
        # 窗口在托盘创建之前就 refresh 过一次,那次信号没人接,这里补上当前值(托盘用紧凑版)
        self.setToolTip(window.summary(compact=True))

    def _sync_state(self) -> None:
        """菜单弹出前,把文字和勾选对齐到窗口的真实状态。"""
        self._visibility.setText("隐藏" if self._window.isVisible() else "显示")
        # 滑杆对齐生效值;配置文件里可以写任意精度(如 0.33),滑杆取最近的一格,
        # 标签仍显示真实值 —— 不假装能表达它
        self._opacity_slider.blockSignals(True)  # 对齐不该反过来触发预览
        self._opacity_slider.setValue(opacity_units(self._window.idle_opacity))
        self._opacity_slider.blockSignals(False)
        self._opacity_value.setText(percent_text(self._window.idle_opacity))

        # 步进器同理:对齐到生效值,且不反过来触发预览
        self._refresh_spin.blockSignals(True)
        self._refresh_spin.setValue(self._window.refresh_seconds)
        self._refresh_spin.blockSignals(False)
        self._sync_threshold_rows()

        blocked = bool(self._window.windowFlags() & Qt.WindowTransparentForInput)
        self._click_through.blockSignals(True)  # 否则会回环触发 set_click_through
        self._click_through.setChecked(blocked)
        self._click_through.blockSignals(False)

        # 尺寸滑杆同理:对齐生效值,且不反过来触发预览
        self._scale_slider.blockSignals(True)
        self._scale_slider.setValue(scale_units(self._window.ui_scale))
        self._scale_slider.blockSignals(False)
        self._scale_value.setText(percent_text(self._window.ui_scale))

        self._autostart.blockSignals(True)  # 同上,避免回环
        self._autostart.setChecked(is_autostart_enabled())
        self._autostart.blockSignals(False)

        # 贴边隐藏:以窗口的实际状态为准(开穿透时它会被自动关掉,勾选要跟着变)
        self._edge_hide.blockSignals(True)
        self._edge_hide.setChecked(self._window.edge_hide)
        self._edge_hide.blockSignals(False)

    def _add_row(
        self,
        label: str,
        widget: QWidget,
        name: str,
        tooltip: str,
        trailing: QWidget | None = None,
    ) -> None:
        """在菜单里插一行「标签 + 控件(+ 尾随标签)」,滑杆与步进器共用。

        左边距必须是 `MENU_ITEM_INDENT`:控件行没有菜单项那段缩进,不补上会比其它行更靠左
        (实测:菜单项文字从菜单面板边缘 27px 处开始)。控件用官方机制 `QWidgetAction` +
        `setDefaultWidget` 放进菜单。

        提示文字同时挂在 action、行容器与控件上:菜单项提示要求 action 有 tooltip(且菜单开了
        `toolTipsVisible`),而指针落在行内的标签上时归属的是行容器 —— 标签与尾随标签都不接收
        鼠标事件,所以**整行任意位置**都是同一句提示。
        """
        holder = MenuRow(self._menu)  # 自己画当前项底色,见 MenuRow
        layout = QHBoxLayout(holder)
        # 左右取同一个值:左侧要跟上菜单项文字(见 MENU_ITEM_INDENT),右侧跟它相等才不显得偏
        layout.setContentsMargins(MENU_ITEM_INDENT, 4, MENU_ITEM_INDENT, 4)
        layout.setSpacing(8)

        title = QLabel(label, holder)
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(title)
        # 顶住:标签贴左、控件贴右 —— 各行宽度被 QWidgetAction 拉齐后,右边缘因此必然对齐
        layout.addStretch(1)
        widget.setToolTip(tooltip)
        layout.addWidget(widget)
        if trailing is not None:
            trailing.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(trailing)

        holder.setToolTip(tooltip)
        # 控件行不参与 QMenu 的"当前项"跟踪(指针落在行里时事件被行内控件吃掉,菜单收不到 move),
        # 于是当前项会停在上一个纯文字项上;那时点本行空白处,事件冒泡到菜单 → 误触那一项
        # (**用户就是这样误开"鼠标穿透"的**)。两处兜住:
        #   1. 本行自己吃掉鼠标事件、不往上冒 —— 点空白处什么也不发生;
        #   2. 指针进入本行时把菜单当前项指到本行的 action —— 陈旧高亮立刻消失(见 eventFilter)。
        holder.setAttribute(Qt.WA_NoMousePropagation)

        action = QWidgetAction(self._menu)
        action.setText(name)  # 控件行本身没有文字,给 action 一个名字便于断言与调试
        action.setToolTip(tooltip)
        action.setDefaultWidget(holder)
        self._menu.addAction(action)  # 插在哪由 __init__ 里的调用点决定
        self._row_actions[holder] = action
        self._row_controls[widget] = action
        holder.installEventFilter(self)  # 进入 / 离开本行时同步菜单的当前项
        widget.installEventFilter(self)  # 滚轮不要改值(见 eventFilter)

    def eventFilter(self, obj, event) -> bool:
        """指针进出控件行时,把菜单的"当前项"跟着指过去。

        `QMenu` 只按自己的 `mouseMoveEvent` 跟踪当前项;指针在控件行里时事件被行内控件吃掉,
        菜单收不到 —— 当前项于是停在上一个纯文字项上(视觉上"高亮在鼠标穿透,而鼠标在别处"),
        点空白处还会误触它。这里补上这一段,行为就与纯文字项一致了。

        另:行内控件(步进器 / 滑杆)默认认滚轮,鼠标划过就改值 —— 菜单里这等于"手一抖就改配置",
        直接拦下(只在 DEBUG 留一行,便于回答"我滚了怎么没反应")。
        """
        action = self._row_actions.get(obj)
        if action is not None:
            if event.type() == QEvent.Type.Enter:
                if self._menu.isVisible():  # 菜单没弹时别去改它的当前项
                    self._menu.setActiveAction(action)
                obj.set_hovered(True)
            elif event.type() == QEvent.Type.Leave:
                self._menu.setActiveAction(None)
                obj.set_hovered(False)
        row = self._row_controls.get(obj)
        if row is not None and event.type() == QEvent.Type.Wheel:
            log.debug("event=wheel_ignored row=%s", row.text())
            return True
        return False

    def _release_row_focus(self) -> None:
        """菜单收起时把控件行的焦点交还出去(接线在 `__init__` 的 `aboutToHide`)。

        不清的话,那个步进器收起后仍是菜单窗口的焦点控件,**再打开时 Qt 会把焦点恢复给它**
        (实测:收起再打开,`hasFocus=True` 且文本仍是全选态),于是按 ↓ 想往下走菜单,实际改的是
        那个阈值 —— 按键先送到焦点控件,被它吃掉就不会进菜单。系统原生菜单收起后不留焦点,
        这里与它对齐。
        """
        for control in self._row_controls:
            control.clearFocus()
            line = getattr(control, "lineEdit", None)  # 步进器:顺带取消全选,免得看起来还"亮着"
            if line is not None:
                line().deselect()

    def _add_opacity_row(self, window: StatusBarWindow) -> None:
        """一行「透明度」滑杆(未报警时的不透明度)。

        **实测**:菜单 popup 里控件能收到 press / move / release,真鼠标按住拖动确实改值,
        菜单也不会被按下就关掉。拖动期间只预览,松手 / 菜单关闭时才提交落盘。
        """
        self._opacity_slider = QSlider(Qt.Horizontal, self._menu)
        # 一格 = OPACITY_STEP(5%),量程就是配置允许的整个区间(5% ~ 100%)
        self._opacity_slider.setRange(1, OPACITY_STEPS)
        self._opacity_slider.setValue(opacity_units(window.idle_opacity))
        self._opacity_slider.setMinimumWidth(SLIDER_WIDTH)
        self._opacity_value = QLabel(percent_text(window.idle_opacity), self._menu)
        self._opacity_value.setMinimumWidth(36)
        # 文字也贴右:否则数字左对齐,看着比步进器的右边缘短一截
        self._opacity_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self._add_row(
            "透明度", self._opacity_slider, "透明度", "未报警时的前景不透明度", self._opacity_value
        )

        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self._opacity_slider.sliderReleased.connect(self._commit_opacity)
        self._menu.aboutToHide.connect(self._commit_opacity)  # 拖到一半直接点别处也算提交

    def _add_version_row(self) -> None:
        """最上面一行:左边「版本号」、右边版本值(与其它行同一套左右结构)。

        静态展示,点了不做任何事;提示只讲版本 —— 目录信息单独占一行(见 `_add_data_dir_row`),
        版本行的悬停不再夹带路径。
        """
        version = app_version()
        self._version_label = QLabel(version, self._menu)
        self._version_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._add_row(
            "版本号",
            self._version_label,
            "版本号",
            f"当前运行的版本 {version}",  # 悬停只写用户能感知的事实;来源、路径之类不写进来
        )

    def _add_data_dir_row(self) -> None:
        """一行「配置和日志目录」:右边「打开」按钮,点了交给系统打开那个目录。

        目录取 `config_dir()` —— 打包后它和日志同目录(%LOCALAPPDATA%\\stabar),源码运行是项目目录。
        完整路径直接写在提示里:不用点开就知道文件在哪。
        """
        self._open_dir_button = QPushButton("打开", self._menu)
        self._open_dir_button.setToolTip(f"打开 {config_dir()}")
        self._open_dir_button.clicked.connect(self._open_data_dir)
        self._add_row(
            "配置和日志目录",
            self._open_dir_button,
            "配置和日志目录",
            self._open_dir_button.toolTip(),
        )

    def _open_data_dir(self) -> None:
        """点「打开」:先收起菜单再让系统打开 —— 这样资源管理器才会出现在前台、不被菜单挡着。"""
        self._menu.close()
        open_path(config_dir())

    def _add_scale_row(self, window: StatusBarWindow) -> None:
        """一行「调整尺寸」滑杆:一格 1%,量程 60% ~ 200%。

        与透明度滑杆同一套:拖动期间只预览(窗口立刻变大小),松手 / 菜单关闭才提交落盘。
        """
        self._scale_slider = QSlider(Qt.Horizontal, self._menu)
        self._scale_slider.setRange(scale_units(UI_SCALE_MIN), scale_units(UI_SCALE_MAX))
        self._scale_slider.setValue(scale_units(window.ui_scale))
        self._scale_slider.setMinimumWidth(SLIDER_WIDTH)
        self._scale_slider.setToolTip("小组件整体大小(60% ~ 200%)")
        self._scale_value = QLabel(percent_text(window.ui_scale), self._menu)
        self._scale_value.setMinimumWidth(40)
        self._scale_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._add_row(
            "调整尺寸", self._scale_slider, "调整尺寸", "小组件整体大小", self._scale_value
        )

        self._scale_slider.valueChanged.connect(self._on_scale_changed)
        self._scale_slider.sliderReleased.connect(self._commit_scale)
        self._menu.aboutToHide.connect(self._commit_scale)  # 拖到一半直接点别处也算提交

    def _on_scale_changed(self, units: int) -> None:
        """拖动过程中:只预览,不落盘(拖一次会发几十个信号)。"""
        scale = scale_from_units(units)
        self._scale_value.setText(percent_text(scale))
        self._window.preview_ui_scale(scale)

    def _commit_scale(self) -> None:
        """松手 / 菜单关闭:提交(落盘 + 一条 `event=ui_scale`);同一个值重复提交会被去重。"""
        self._window.set_ui_scale(scale_from_units(self._scale_slider.value()))

    def _add_reset_row(self, window: StatusBarWindow) -> None:
        """一行「还原配置」:标签在左、按钮在右(与上面几行同一套左右对齐)。

        托盘这层只管弹不弹、点了算不算;该还原哪些项、怎么生效都在窗口那边
        (见 `StatusBarWindow.resettable_diff` / `reset_settings`)。
        """
        button = QPushButton("还原", self._menu)
        button.setToolTip("把菜单里这些配置恢复成默认值(不含窗口位置)")
        button.clicked.connect(self._confirm_reset)
        self._add_row("还原配置", button, "还原配置", button.toolTip())

    def _confirm_reset(self) -> None:
        """点「还原」→ 列出有差异的项请人确认,确认了才真还原;没有差异就只说明一句。"""
        rows = self._window.resettable_diff()  # 空清单也照常弹,只是换成说明
        # 先收起菜单再弹模态框:popup 会请求键盘 / 鼠标抓取(离屏平台会打
        # "This plugin does not support grabbing the keyboard"),收起来最稳
        self._menu.close()
        if dialogs.ResetDialog(rows, QColor(*COLOR_DANGER)).exec() != QDialog.DialogCode.Accepted:
            log.debug("event=config_reset_cancelled")
            return
        if not rows:
            log.debug("event=config_reset_skipped reason=all_default")  # 只是告知,没有可改的
            return
        self._window.reset_settings()  # 日志与落盘都在里面(event=config_reset)

    def _add_refresh_row(self, window: StatusBarWindow) -> None:
        """一行「刷新频率（秒）」步进器:1 ~ 60 秒、步进 1、可直接点进去敲数字。

        单位写在行标签上,步进器本身不带后缀 —— 免得同一个单位在菜单里出现两次。
        """
        self._refresh_spin = QSpinBox(self._menu)
        self._refresh_spin.setRange(REFRESH_MIN_S, REFRESH_MAX_S)
        self._refresh_spin.setSingleStep(1)
        # 敲 "30" 时别在敲到 "3" 的那一刻就把间隔改成 3 秒
        self._refresh_spin.setKeyboardTracking(False)
        self._refresh_spin.setValue(window.refresh_seconds)

        self._add_row("刷新频率（秒）", self._refresh_spin, "刷新频率", "每几秒读一次磁盘占用")

        self._refresh_spin.valueChanged.connect(self._on_refresh_changed)
        self._refresh_spin.editingFinished.connect(self._commit_refresh)
        self._menu.aboutToHide.connect(self._commit_refresh)

    def _add_threshold_row(
        self, window: StatusBarWindow, *, key: str, label: str, name: str, tooltip: str
    ) -> None:
        """一行阈值步进器(按百分比显示、步进 1%)。

        橙色与红色的量程互相顶住(橙色最高 `红色 - 1%`,红色最低 `橙色 + 1%`),配不出反序;
        常亮阈值不受红色约束 —— 比红色高就等于"红线即常亮"。
        """
        spin = QSpinBox(self._menu)
        spin.setRange(1, 100)
        spin.setSingleStep(round(THRESHOLD_STEP * 100))
        spin.setKeyboardTracking(False)  # 敲 "85" 时别在敲到 "8" 的那一刻就改
        spin.setValue(round(window.thresholds[key] * 100))

        self._threshold_spins[key] = spin
        self._add_row(label, spin, name, tooltip)  # 提示文字由 _add_row 挂到三处

        spin.valueChanged.connect(lambda _value: self._preview_thresholds())
        spin.editingFinished.connect(self._commit_thresholds)
        self._menu.aboutToHide.connect(self._commit_thresholds)

    def _preview_thresholds(self) -> None:
        """改动过程中:只预览(重绘颜色 + 重算常亮),不落盘。

        顺手把三行的量程互相顶住 —— 只动边界、不动当前值,免得和用户正在敲的数字打架
        (值被顶到边界时 Qt 会自己夹取并再发一次信号,重入一次就收敛)。
        """
        percent = {key: spin.value() for key, spin in self._threshold_spins.items()}
        self._threshold_spins["warn"].setMaximum(percent["danger"] - 1)
        self._threshold_spins["danger"].setMinimum(percent["warn"] + 1)
        self._window.preview_thresholds(
            warn=percent["warn"] / 100,
            lit=percent["lit"] / 100,
            danger=percent["danger"] / 100,
        )

    def _commit_thresholds(self) -> None:
        """回车 / 菜单关闭:提交(落盘 + 一条 `event=thresholds`);同一组值会被去重。"""
        percent = {key: spin.value() for key, spin in self._threshold_spins.items()}
        self._window.set_thresholds(
            warn=percent["warn"] / 100,
            lit=percent["lit"] / 100,
            danger=percent["danger"] / 100,
        )

    def _sync_threshold_rows(self) -> None:
        """把三行对齐到生效值;橙色与红色的量程互相顶住,常亮阈值自由(1% ~ 100%)。"""
        percent = {key: round(value * 100) for key, value in self._window.thresholds.items()}
        for key, spin in self._threshold_spins.items():
            spin.blockSignals(True)  # 对齐不该反过来触发预览
            if key == "warn":
                spin.setRange(1, percent["danger"] - 1)
            elif key == "danger":
                spin.setRange(percent["warn"] + 1, 100)
            else:
                spin.setRange(1, 100)
            spin.setValue(percent[key])
            spin.blockSignals(False)

    def _on_opacity_changed(self, step: int) -> None:
        """拖动过程中:只预览,不落盘(拖一次会发几十个信号)。"""
        value = opacity_from_step(step)
        self._opacity_value.setText(percent_text(value))
        self._window.preview_idle_opacity(value)

    def _commit_opacity(self) -> None:
        """松手 / 菜单关闭:提交(落盘 + 一条 `event=idle_opacity`);同一个值重复提交会被去重。"""
        self._window.set_idle_opacity(opacity_from_step(self._opacity_slider.value()))

    def _on_refresh_changed(self, seconds: int) -> None:
        """改动过程中:立刻生效(下次读盘按新间隔),不落盘、不记日志。"""
        self._window.preview_refresh_seconds(seconds)

    def _commit_refresh(self) -> None:
        """回车 / 菜单关闭:提交(落盘 + 一条 `event=refresh_interval`);同一个值会被去重。"""
        self._window.set_refresh_seconds(self._refresh_spin.value())

    def _set_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except OSError as error:
            # 不要用 print(file=sys.stderr):打包成 --noconsole 后它是 None,打印会静默消失
            log.warning("event=autostart enabled=%s ok=0 error=%s", enabled, error)
        else:
            log.info("event=autostart enabled=%s ok=1", enabled)
        self._sync_state()  # 以注册表的实际结果为准,别让勾选状态骗人

    def _toggle_visible(self) -> None:
        if self._window.isVisible():
            self._window.hide_widget()  # 内部会记 event=window state=hidden
        else:
            self._window.show()
            log.info("event=window state=shown")
        self._sync_state()

    def _on_activated(self, reason) -> None:
        # 左键单击切换显示;右键是菜单,由系统直接弹出,不走这里
        # 记下来路:短时间内第二次点击会被当成双击(DoubleClick),这正是"点了没反应"的常见原因
        log.debug("event=tray activated=%s", reason.name)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visible()


def main() -> int:
    app = QApplication(sys.argv)
    # QStandardPaths 靠应用名决定配置目录,必须在创建窗口(也就是读配置)之前设置。
    # 日志目录同样由它算出,所以 setup_logging() 也必须排在这一行之后 ——
    # 实测:应用名没设时 AppConfigLocation 会退化成 %LOCALAPPDATA% 本身,日志就跟配置分家了。
    app.setApplicationName(APP_NAME)
    level = logging_setup.setup_logging(log_dir(), APP_NAME)
    log.info(
        "event=start version=%s frozen=%s level=%s config=%s log=%s",
        app_version(),  # 顺便把版本记在启动行:排查时"这是哪一版"最常被问
        bool(getattr(sys, "frozen", False)),
        logging.getLevelName(level),
        settings_path(),
        log_path(),
    )
    # 打包成 --noconsole 后 stderr 是 None:先记下来,省得以后怀疑日志缺了控制台那一半
    log.debug("event=start stderr=%s", "none" if sys.stderr is None else "available")

    # 单实例:同一份配置目录只允许一个进程 —— 两份会互相覆盖配置、共用一份日志
    # (启动时的"超限清空"会把对方的内容一起清掉),托盘还会多出一个图标让人分不清。
    # 再启动一次是**常见路径**:自启已经拉起过了,用户又双击了一次 exe。
    single = single_instance.SingleInstance(config_dir())
    if not single.acquire():
        log.info("event=single_instance action=exit")  # 上面的通知已让已有实例显示小组件
        return 0

    # 有托盘图标,隐藏小组件后程序要继续活着,不能因为窗口不可见就退出
    app.setQuitOnLastWindowClosed(False)

    window = StatusBarWindow()
    window.show()
    log.info("event=window state=shown pos=%d,%d", window.x(), window.y())

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayIcon(window)
        tray.show()
        log.info("event=tray state=created")
    else:
        # 托盘是隐藏 / 穿透之后唯一的恢复入口,没有它小组件藏起来就找不回来了
        log.warning("event=tray state=unavailable hint=隐藏后将无法从托盘恢复")

    single.activated(window.reveal)  # 第二个实例启动 → 把小组件叫出来
    app.aboutToQuit.connect(lambda: log.info("event=stop"))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
