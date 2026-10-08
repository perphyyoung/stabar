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

import logging
import os
import shutil
import sys
import winreg
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QRectF,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionMenuItem,
    QSystemTrayIcon,
    QWidget,
    QWidgetAction,
)

import logging_setup

# ======================================================================
# 观感调节区:想让小组件更醒目,只动这一块(平时的不透明度:托盘菜单或配置文件)
# ======================================================================

APP_NAME = "stabar"  # 决定打包后配置目录的名字
DRIVES = ("C:/", "D:/")  # 要监控的盘符
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

# --- 托盘菜单 ---
# 控件行(滑杆)没有菜单项那段缩进,要自己补上左边距才能与其它行文字左对齐。
# 实测(抓图 + actionGeometry):菜单项文字从菜单面板边缘 27px 处开始 = 面板 3 + 图标列 20 + 间距 4。
MENU_ITEM_INDENT = 27
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


def settings_path() -> Path:
    """配置文件位置。"""
    return config_dir() / SETTINGS_NAME


def log_path() -> Path:
    """日志文件位置。"""
    return logging_setup.log_path(log_dir())


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def with_alpha(rgb: tuple[int, int, int], alpha: float) -> QColor:
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
    """把不透明度写成给人看的百分比(滑杆右侧那个标签)。"""
    return f"{round(value * 100)}%"


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


# ======================================================================
# 数据读取
# ======================================================================


def read_disk(path: str) -> tuple[int, int] | None:
    """返回 (已用字节, 总字节);盘符不存在时返回 None。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.used, usage.total


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
        self._stats = read_disk(path)
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

    # ---- 数据 ----
    def reload(self) -> None:
        self._stats = read_disk(self._path)
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

    def _bar_color(self, ratio: float) -> QColor:
        if ratio >= self._danger:
            self._warned = True
            return with_alpha(COLOR_DANGER, self._alpha)
        floor = self._warn - COLOR_HYSTERESIS if self._warned else self._warn
        self._warned = ratio >= floor
        return with_alpha(COLOR_WARN if self._warned else COLOR_ACCENT, self._alpha)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
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


class StatusBarWindow(QWidget):
    """无标题条、无背景的置顶窗口,负责提亮 / 还原与位置持久化。"""

    updated = Signal(str)  # 每次刷新后带出状态文字,供托盘图标的悬停提示使用

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # Qt 文档:tooltip 默认只对**活动窗口**显示,而本窗口置顶、从不被激活(真人实测:
        # 详情会时有时无甚至一直不出现)。开了这条,非活动窗口也照常弹详情。
        self.setAttribute(Qt.WA_AlwaysShowToolTips, True)
        self.setFixedSize(
            WINDOW_W,
            PAD * 2 + ROW_H * len(DRIVES) + GAP * (len(DRIVES) - 1),
        )

        self._settings = QSettings(str(settings_path()), QSettings.IniFormat)
        self._drag_offset: QPoint | None = None

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
        for i, path in enumerate(DRIVES):
            bar = DiskBar(path, self, self._warn_threshold, self._danger_threshold)
            bar.setGeometry(PAD, PAD + i * (ROW_H + GAP), WINDOW_W - PAD * 2, ROW_H)
            # 详情文字挂在**窗口**上,而盘条铺满整块区域、自己又不处理 tooltip —— 不给它设这个,
            # 悬停在盘条上时那条 tooltip 就归盘条(它没有文字 → 不弹),表现为"详情时有时无"。
            # 让盘条不接收鼠标事件,悬停统一归窗口,整块区域都能弹(不必逐个给子控件设 tooltip)。
            bar.setAttribute(Qt.WA_TransparentForMouseEvents)
            self._bars.append(bar)

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

    # ---- 数据刷新 ----
    def refresh(self) -> None:
        for bar in self._bars:
            bar.reload()
        summary = "\n".join(bar.detail_text() for bar in self._bars)
        self.setToolTip(summary)
        self.updated.emit(summary)  # 托盘图标靠它同步悬停提示
        self._retarget()
        # target 是同步算出的终值(动画还在跑时它也已确定),所以断言提亮结果不必等动画
        log.debug(
            "event=refresh max_ratio=%.4f level=%.2f target=%.2f %s",
            self._max_ratio(),
            self._level,
            self._target,
            " | ".join(summary.split("\n")),
        )

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
        for bar in self._bars:
            bar.set_alpha(alpha)

    # ---- 鼠标悬停唤醒:硬切,移开立即还原 ----
    def enterEvent(self, event) -> None:
        self._hovered = True
        log.debug("event=hover state=enter")
        self._retarget(animate=False)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        log.debug("event=hover state=leave")
        self._retarget(animate=False)

    # ---- 绘制:只画一层隐形矩形,让整个窗口都能接收鼠标 ----
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, HIT_AREA_ALPHA))

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

    # ---- 隐藏:恢复入口只有托盘 ----
    def hide_widget(self) -> None:
        """隐藏小组件,并把悬停态清干净 —— 否则下次显示出来会停在全亮。"""
        self._hovered = False
        self._retarget(animate=False)
        self.hide()
        log.info("event=window state=hidden")

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_position()

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

        # 菜单是 popup,弹出期间窗口收不到 enter / leave 事件。
        # 所以关掉后必须按光标实际位置重新判定,否则鼠标已经移开了却仍算悬停,
        # 窗口会一直停在最亮(而且要等下次重新进入再离开才能恢复)。
        #
        # 但选中的若是"隐藏",窗口此刻已经不在了:菜单就弹在窗口矩形内,隐藏后光标"还在窗口上",
        # 按位置重判会把 _hovered 又置回 True、把亮度拉满,而窗口已隐藏、不会再有 Enter / Leave
        # 来清它 —— 从托盘显示出来就是全亮的(实测:event=window state=hidden 紧接着
        # event=reveal target=1.00 reason=hover)。隐藏路径自己已经清干净悬停态(见 hide_widget),
        # 所以这里不能碰一个不在屏幕上、也收不到鼠标的窗口。
        if self.isVisible():
            self._hovered = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
            self._retarget(animate=False)


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

        self._visibility = self._menu.addAction("隐藏", self._toggle_visible)
        self._visibility.setToolTip("显示 / 隐藏小组件(隐藏后只能从托盘恢复)")
        self._menu.addAction("立即刷新", window.refresh).setToolTip("立刻读一次磁盘占用")

        # 四行控件(位置由这里的调用点决定):刷新频率紧跟"立即刷新",三个步进器挨在一起,
        # 透明度排在两条警戒线之后,再往后才是勾选项
        # 控件行容器 / 行内控件 各自指向该行的 action(见 eventFilter、_release_row_focus)
        self._row_actions: dict[QWidget, QAction] = {}
        self._row_controls: dict[QWidget, QAction] = {}
        self._add_refresh_row(window)
        self._threshold_spins: dict[str, QSpinBox] = {}
        for label, name, tooltip, key in THRESHOLD_ROWS:
            self._add_threshold_row(window, key=key, label=label, name=name, tooltip=tooltip)
        self._add_opacity_row(window)
        # 四个步进器统一宽度,按各自的 sizeHint 取最大(含最大位数与加减箭头,随字体/DPI 自适应)
        # —— 写死宽度会挤掉内容(实测 72px 时"85%"只显示出"85'");右对齐之后左边缘也跟着齐
        spins = (self._refresh_spin, *self._threshold_spins.values())
        widest = max(spin.sizeHint().width() for spin in spins)
        for spin in spins:
            spin.setFixedWidth(widest)
        self._sync_threshold_rows()

        self._click_through = self._menu.addAction("鼠标穿透")
        self._click_through.setCheckable(True)
        self._click_through.setToolTip("开启后窗口不再接收鼠标,只能从本菜单关掉")
        self._click_through.toggled.connect(window.set_click_through)

        self._autostart = self._menu.addAction("开机自启")
        self._autostart.setCheckable(True)
        self._autostart.setToolTip("写当前用户的注册表 Run 键,可回本菜单取消")
        self._autostart.toggled.connect(self._set_autostart)

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
        # 窗口在托盘创建之前就 refresh 过一次,那次信号没人接,这里补上当前值
        self.setToolTip(window.toolTip())

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

        self._autostart.blockSignals(True)  # 同上,避免回环
        self._autostart.setChecked(is_autostart_enabled())
        self._autostart.blockSignals(False)

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
        layout.setContentsMargins(MENU_ITEM_INDENT, 4, 10, 4)
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
        "event=start frozen=%s level=%s config=%s log=%s drives=%s",
        bool(getattr(sys, "frozen", False)),
        logging.getLevelName(level),
        settings_path(),
        log_path(),
        ",".join(DRIVES),
    )
    # 打包成 --noconsole 后 stderr 是 None:先记下来,省得以后怀疑日志缺了控制台那一半
    log.debug("event=start stderr=%s", "none" if sys.stderr is None else "available")

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

    app.aboutToQuit.connect(lambda: log.info("event=stop"))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
