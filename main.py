"""stabar —— Win11 磁盘占用桌面小组件。

无标题条、无背景、置顶、可拖动。平时前景(盘符 / 进度条 / 百分比)按配置的
不透明度贴着壁纸显示;占用达到提亮水位或鼠标悬停时立即变亮。

不透明度以配置文件里的 idle_opacity 为准(首次运行用代码常量 FIRST_RUN_IDLE_OPACITY),
文字与进度条同步变化。

运行:   uv run python main.py
打包:   uv run pyinstaller --noconfirm --clean stabar.spec   (产物在 dist\\stabar)
配置:   开发时 = 项目目录下的 stabar.ini
        打包后 = %LOCALAPPDATA%\\stabar\\stabar.ini   (见 settings_path())
托盘:   左键单击托盘图标 = 切换显示(小组件可以调到几乎看不见,托盘是稳定的入口)
        右键托盘图标 = 显示/隐藏、立即刷新、鼠标穿透、退出
"""

from __future__ import annotations

import shutil
import sys
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
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

# ======================================================================
# 观感调节区:想让小组件更醒目,只动这一块(平时的不透明度在配置文件里)
# ======================================================================

APP_NAME = "stabar"  # 决定打包后配置目录的名字
DRIVES = ("C:/", "D:/")  # 要监控的盘符
REFRESH_MS = 15_000  # 磁盘数据刷新间隔(毫秒)

# --- 前景不透明度:文字、进度条、百分比同步生效 ---
# 平时的值不在这里,而在配置文件里(见 FIRST_RUN_IDLE_OPACITY / settings_path())
LIVE_OPACITY = 1.00  # 鼠标悬停 / 自动提亮时的不透明度
MIN_OPACITY = 0.01  # 配置文件允许的最小值,再小就真的是彻底看不见了
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


def settings_path() -> Path:
    """配置文件位置:开发时放项目目录,打包后放用户的配置目录。

    开发时留在项目目录是为了方便直接手改;打包后不能再写 exe 旁边,原因有两个:
    一是 onedir 的 __file__ 指向 _internal 子目录,配置会藏进包内部、升级就丢;
    二是装到 Program Files 之类的路径下可能根本没有写权限。
    """
    if getattr(sys, "frozen", False):  # PyInstaller 打包运行时的标志
        base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
        directory = Path(base) if base else Path.home() / f".{APP_NAME}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / SETTINGS_NAME
    return Path(__file__).with_name(SETTINGS_NAME)


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
        print(f"警告: 图标解码失败({icon_path()}),请检查打包是否漏了 qico.dll",
              file=sys.stderr)
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
            fill.addRoundedRect(
                bar_x, bar_y, max(fill_w, bar_h), bar_h, bar_h / 2, bar_h / 2
            )
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
        # 把生效值回写,让配置文件自解释:不用翻代码就知道程序实际用的是多少。
        # 代价是回写之后 ini 成为权威值,再改 FIRST_RUN_IDLE_OPACITY 不会生效;
        # 想回到那个初值,把 ini 里这一行删掉即可。sync() 是为了让它立刻落盘,
        # 免得进程被强杀时这次回写丢掉。
        self._settings.setValue("idle_opacity", self._idle_opacity)
        self._settings.sync()

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
            value = float(
                self._settings.value("idle_opacity", FIRST_RUN_IDLE_OPACITY)
            )
        except (TypeError, ValueError):
            return FIRST_RUN_IDLE_OPACITY
        return min(1.0, max(MIN_OPACITY, value))

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

    def _save_position(self) -> None:
        self._settings.setValue("pos_x", self.x())
        self._settings.setValue("pos_y", self.y())

    # ---- 数据刷新 ----
    def refresh(self) -> None:
        for bar in self._bars:
            bar.reload()
        summary = "\n".join(bar.detail_text() for bar in self._bars)
        self.setToolTip(summary)
        self.updated.emit(summary)  # 托盘图标靠它同步悬停提示
        self._retarget()

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
        self._retarget(animate=False)

    def leaveEvent(self, event) -> None:
        self._hovered = False
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

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_position()

    # ---- 右键菜单:无标题条,刷新和退出都在这里 ----
    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.addAction("立即刷新", self.refresh)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        menu.exec(event.globalPos())

        # 菜单是 popup,弹出期间窗口收不到 enter / leave 事件。
        # 所以关掉后必须按光标实际位置重新判定,否则鼠标已经移开了却仍算悬停,
        # 窗口会一直停在最亮(而且要等下次重新进入再离开才能恢复)。
        self._hovered = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._retarget(animate=False)


class TrayIcon(QSystemTrayIcon):
    """托盘图标:小组件可以调到几乎看不见,这里是唯一稳定的入口。"""

    def __init__(self, window: StatusBarWindow) -> None:
        super().__init__(make_tray_icon())
        self._window = window
        self.setToolTip(APP_NAME)

        self._menu = QMenu()
        self._visibility = self._menu.addAction("隐藏小组件", self._toggle_visible)
        self._menu.addAction("立即刷新", window.refresh)

        self._click_through = self._menu.addAction("鼠标穿透")
        self._click_through.setCheckable(True)
        self._click_through.toggled.connect(window.set_click_through)

        self._menu.addSeparator()
        self._menu.addAction("退出", QApplication.quit)
        self._menu.aboutToShow.connect(self._sync_state)
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)
        window.updated.connect(self.setToolTip)
        # 窗口在托盘创建之前就 refresh 过一次,那次信号没人接,这里补上当前值
        self.setToolTip(window.toolTip())

    def _sync_state(self) -> None:
        """菜单弹出前,把文字和勾选对齐到窗口的真实状态。"""
        self._visibility.setText(
            "隐藏小组件" if self._window.isVisible() else "显示小组件"
        )
        blocked = bool(self._window.windowFlags() & Qt.WindowTransparentForInput)
        self._click_through.blockSignals(True)  # 否则会回环触发 set_click_through
        self._click_through.setChecked(blocked)
        self._click_through.blockSignals(False)

    def _toggle_visible(self) -> None:
        if self._window.isVisible():
            self._window.hide()
        else:
            self._window.show()
        self._sync_state()

    def _on_activated(self, reason) -> None:
        # 左键单击切换显示;右键是菜单,由系统直接弹出,不走这里
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visible()


def main() -> int:
    app = QApplication(sys.argv)
    # QStandardPaths 靠应用名决定配置目录,必须在创建窗口(也就是读配置)之前设置
    app.setApplicationName(APP_NAME)
    # 有托盘图标,隐藏小组件后程序要继续活着,不能因为窗口不可见就退出
    app.setQuitOnLastWindowClosed(False)

    window = StatusBarWindow()
    window.show()

    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = TrayIcon(window)
        tray.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
