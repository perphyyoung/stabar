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
REFRESH_MS = 15_000  # 磁盘数据刷新间隔(毫秒)

# --- 前景不透明度:文字、进度条、百分比同步生效 ---
# 平时的值不在这里,而在配置文件里,也可以从托盘菜单的「透明度」滑杆改
# (见 FIRST_RUN_IDLE_OPACITY / settings_path() / StatusBarWindow.set_idle_opacity)
LIVE_OPACITY = 1.00  # 鼠标悬停 / 自动提亮时的不透明度
MIN_OPACITY = 0.05  # 配置文件允许的最小值(5%,再小就真的看不见了;滑杆的最小格也是它)
OPACITY_STEP = 0.05  # 滑杆步进(5%):一格一档,拖动与键盘都是这个粒度
OPACITY_STEPS = round(1.0 / OPACITY_STEP)  # 滑杆格数(1 格 = 5%,共 20 格)
FADE_MS = 260  # 自动提亮的补间时长(毫秒);鼠标悬停是硬切,不走这里

# --- 自动提亮:占用达到水位才变亮,低于水位严格按配置文件的不透明度显示 ---
REVEAL_START = 0.8  # 提亮起点。低于它完全不提亮,你的盘常驻 80% 所以保持隐身
WARN_THRESHOLD = 0.90  # 黄色警告线,同时也是完全提亮的点
DANGER_THRESHOLD = 0.95  # 红色危险线
COLOR_HYSTERESIS = 0.005  # 颜色切换滞回,避免卡在阈值上反复闪烁

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


def reveal_level(max_ratio: float) -> float:
    """占用率 -> 提亮程度 0~1。

    低于 REVEAL_START 返回 0(严格按配置文件的不透明度显示),到 WARN_THRESHOLD 提亮到 1。
    起点是独立常量,不再由 WARN_THRESHOLD 加减得出:那个隐式加减很坑,
    改 WARN_THRESHOLD 会同时挪动提亮起点,看起来改了却几乎没效果。
    """
    if max_ratio <= REVEAL_START:
        return 0.0
    if max_ratio >= WARN_THRESHOLD:
        return 1.0
    span = WARN_THRESHOLD - REVEAL_START
    return (max_ratio - REVEAL_START) / span if span > 0 else 1.0


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

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = path
        self._label = path[0] + ":"
        self._stats = read_disk(path)
        self._alpha = 255.0
        self._warned = False  # 警告色滞回标记
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

    def _bar_color(self, ratio: float) -> QColor:
        if ratio >= DANGER_THRESHOLD:
            self._warned = True
            return with_alpha(COLOR_DANGER, self._alpha)
        floor = WARN_THRESHOLD - COLOR_HYSTERESIS if self._warned else WARN_THRESHOLD
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

        percent_w = 44
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
            f"{ratio * 100:.0f}%",
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
        log.debug("event=config file=%s idle_opacity=%s", settings_path(), self._idle_opacity)

        self._bars: list[DiskBar] = []
        for i, path in enumerate(DRIVES):
            bar = DiskBar(path, self)
            bar.setGeometry(PAD, PAD + i * (ROW_H + GAP), WINDOW_W - PAD * 2, ROW_H)
            self._bars.append(bar)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._apply_level)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

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
        target = 1.0 if self._hovered else reveal_level(self._max_ratio())
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


class TrayIcon(QSystemTrayIcon):
    """托盘图标:小组件可以调到几乎看不见,这里是唯一稳定的入口。"""

    def __init__(self, window: StatusBarWindow) -> None:
        super().__init__(make_tray_icon())
        self._window = window
        self.setToolTip(APP_NAME)

        self._menu = QMenu()
        self._visibility = self._menu.addAction("隐藏", self._toggle_visible)
        self._menu.addAction("立即刷新", window.refresh)

        # 平时的不透明度:一行滑杆(位置由这里的调用点决定:紧跟"立即刷新")
        self._add_opacity_row(window)

        self._click_through = self._menu.addAction("鼠标穿透")
        self._click_through.setCheckable(True)
        self._click_through.toggled.connect(window.set_click_through)

        self._autostart = self._menu.addAction("开机自启")
        self._autostart.setCheckable(True)
        self._autostart.toggled.connect(self._set_autostart)

        self._menu.addSeparator()
        self._menu.addAction("退出", QApplication.quit)
        self._menu.aboutToShow.connect(self._sync_state)
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

        blocked = bool(self._window.windowFlags() & Qt.WindowTransparentForInput)
        self._click_through.blockSignals(True)  # 否则会回环触发 set_click_through
        self._click_through.setChecked(blocked)
        self._click_through.blockSignals(False)

        self._autostart.blockSignals(True)  # 同上,避免回环
        self._autostart.setChecked(is_autostart_enabled())
        self._autostart.blockSignals(False)

    def _add_opacity_row(self, window: StatusBarWindow) -> None:
        """在菜单里插一行「透明度」滑杆(未报警时的不透明度)。

        用官方机制 `QWidgetAction` + `setDefaultWidget` 把控件放进菜单。**实测**:菜单 popup 里
        控件能收到 press / move / release,真鼠标按住拖动确实改值,菜单也不会被按下就关掉。

        拖动期间只预览,松手 / 菜单关闭时才提交落盘(见 `_commit_opacity`)。
        """
        holder = QWidget(self._menu)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(MENU_ITEM_INDENT, 4, 10, 4)  # 左边距补上菜单项那段缩进
        layout.setSpacing(8)

        self._opacity_slider = QSlider(Qt.Horizontal, holder)
        # 一格 = OPACITY_STEP(5%),量程就是配置允许的整个区间(5% ~ 100%)
        self._opacity_slider.setRange(1, OPACITY_STEPS)
        self._opacity_slider.setValue(opacity_units(window.idle_opacity))
        self._opacity_slider.setMinimumWidth(SLIDER_WIDTH)
        self._opacity_slider.setToolTip("未报警时的前景不透明度")
        self._opacity_value = QLabel(percent_text(window.idle_opacity), holder)
        self._opacity_value.setMinimumWidth(36)

        layout.addWidget(QLabel("透明度", holder))
        layout.addWidget(self._opacity_slider)
        layout.addWidget(self._opacity_value)

        action = QWidgetAction(self._menu)
        action.setText("透明度")  # 控件行本身没有文字,给 action 一个名字便于断言与调试
        action.setDefaultWidget(holder)
        self._menu.addAction(action)  # 插在哪由 __init__ 里的调用点决定

        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self._opacity_slider.sliderReleased.connect(self._commit_opacity)
        self._menu.aboutToHide.connect(self._commit_opacity)  # 拖到一半直接点别处也算提交

    def _on_opacity_changed(self, step: int) -> None:
        """拖动过程中:只预览,不落盘(拖一次会发几十个信号)。"""
        value = opacity_from_step(step)
        self._opacity_value.setText(percent_text(value))
        self._window.preview_idle_opacity(value)

    def _commit_opacity(self) -> None:
        """松手 / 菜单关闭:提交(落盘 + 一条 `event=idle_opacity`);同一个值重复提交会被去重。"""
        self._window.set_idle_opacity(opacity_from_step(self._opacity_slider.value()))

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
