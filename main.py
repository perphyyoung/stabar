"""stabar —— Win11 磁盘占用桌面小组件。

无标题条、无背景、置顶、可拖动。平时前景(盘符 / 进度条 / 百分比)按配置的
不透明度贴着壁纸显示;占用达到提亮水位或鼠标悬停时立即变亮。

不透明度以配置文件里的 idle_opacity 为准(首次运行用代码常量 layout.FIRST_RUN_IDLE_OPACITY),
文字与进度条同步变化。

运行:   uv run python main.py
打包:   uv run pyinstaller --noconfirm --clean stabar.spec   (产物在 dist\\stabar)
配置:   开发时 = 项目目录下的 stabar.ini
        打包后 = %LOCALAPPDATA%\\stabar\\stabar.ini   (见 paths.settings_path())
日志:   与配置同目录的 stabar.log,开发 INFO、打包 WARNING,可用 STABAR_LOG_LEVEL 覆盖
        (自动化测试用 DEBUG);实现见 logging_setup.py,用法见 日志使用说明.md
托盘:   左键单击托盘图标 = 切换显示(小组件可以调到几乎看不见,托盘是稳定的入口)
        右键托盘图标 = 显示/隐藏、立即刷新、鼠标穿透、退出
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QPainter,
)
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)

import diskbar
import drives
import layout
import logging_setup
import paths
import single_instance
import tray

# ======================================================================
# 观感调节区:想让小组件更醒目,只动这一块(平时的不透明度:托盘菜单或配置文件)
# ======================================================================

REFRESH_MS = 15_000  # 刷新间隔的默认值(毫秒);托盘菜单可改(见 refresh_seconds)
REFRESH_DEFAULT_S = REFRESH_MS // 1000  # 首次运行的默认秒数(与历史行为一致)

# --- 前景不透明度:文字、进度条、百分比同步生效 ---
# 平时的值不在这里,而在配置文件里,也可以从托盘菜单的「透明度」滑杆改
# (见 layout.FIRST_RUN_IDLE_OPACITY / paths.settings_path() / StatusBarWindow.set_idle_opacity)
FADE_MS = 260  # 自动提亮的补间时长(毫秒);鼠标悬停是硬切,不走这里

# HIT_AREA_ALPHA 不是装饰,是功能代码,别改成 0:
# Windows 对分层窗口按像素做命中测试,alpha = 0 的像素鼠标会直接穿透到桌面。
# 背景纯透明后,整个窗口矩形就只有字形能接收鼠标 —— 悬停提亮会变得极难触发,
# 拖动也只能精确按住某个笔画。保留一层 alpha = 1 的隐形底板即可修复。
HIT_AREA_ALPHA = 1

# --- 自动提亮:占用达到水位才变亮,低于水位严格按配置文件的不透明度显示 ---
WARN_THRESHOLD = 0.85  # 橙色警戒线的首次运行默认值(**只影响颜色**);托盘菜单可改
DANGER_THRESHOLD = 0.90  # 红色危险线的首次运行默认值;托盘菜单可改
LIT_THRESHOLD = 0.90  # 常亮阈值的首次运行默认值(到它或红色线界面常亮);托盘菜单可改

# --- 贴边隐藏 ---
# 几何常量(线宽 / 每段长度 / 贴边阈值)在 `layout.py`,与 `layout.tail_layout` / `layout.snap_edge` 放一起。
# 这里只剩"什么时候收、收到什么程度":
TAIL_MIN_ALPHA = 90  # 断线的不透明度下限:它是收起时唯一能点回来的东西,不能淡到看不见
EDGE_HIDE_DELAY_MS = 250  # 指针离开后隔多久才收起:没有它,光标划过边缘会来回弹
EDGE_HIDE_DEFAULT = True  # 默认开:贴到边就收起成小尾巴
EDGE_HIDE_KEY = "edge_hide"

# --- 整窗缩放(托盘「调整尺寸」滑杆调)---
# 存的是**系数**而不是像素:窗口高度是"盘数 × 行高"推出来的,存绝对高会跟盘数打架;而上面的
# layout.WINDOW_W / layout.ROW_H / layout.PAD / layout.GAP 本来就是一套常量,乘同一个系数最自然。
# 系数与量程(`layout.UI_SCALE_MIN` / `layout.UI_SCALE_MAX` / `layout.SCALE_UNIT`)都在 `layout.py`。

# ======================================================================
# 小工具
# ======================================================================


# 托盘菜单「还原配置」管的就是这几项:(ini 键, 显示名, 默认值, 把值写成给人看的文本)。
# **位置 pos_x / pos_y 故意不在其中** —— 还原不该把窗口搬走,ini 里始终留着位置。
# 默认值一律引用上面的常量、不再抄一份:改常量时还原跟着变。
RESETTABLE_SETTINGS = (
    ("refresh_seconds", "刷新频率", REFRESH_DEFAULT_S, lambda value: f"{value} 秒"),
    ("warn_threshold", "橙色阈值", WARN_THRESHOLD, layout.percent_text),
    ("danger_threshold", "红色阈值", DANGER_THRESHOLD, layout.percent_text),
    ("lit_threshold", "常亮阈值", LIT_THRESHOLD, layout.percent_text),
    ("idle_opacity", "未报警透明度", layout.FIRST_RUN_IDLE_OPACITY, layout.percent_text),
    (EDGE_HIDE_KEY, "贴边隐藏", EDGE_HIDE_DEFAULT, lambda value: "开" if value else "关"),
    (
        drives.DRIVES_KEY,
        "显示的盘符",
        None,
        lambda value: "自动" if value is None else drives.drives_text(value),
    ),
)


# ======================================================================
# 日志
#
# 实现全在 logging_setup.py(级别策略、落盘、Qt 消息桥接、异常钩子),这里只留一个
# logger。事件行统一写成 "event=<名词> key=value ...",完整清单见 日志使用说明.md。
# ======================================================================

log = logging.getLogger(paths.APP_NAME)


# ======================================================================
# 界面
# ======================================================================


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
        self.setFixedSize(
            layout.layout_size(layout.UI_SCALE_DEFAULT, 1)
        )  # 占位,真实尺寸由 _build_bars 定

        self._settings = QSettings(str(paths.settings_path()), QSettings.IniFormat)
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
            float(layout.FIRST_RUN_IDLE_OPACITY) * 255
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
        # 代价是回写之后 ini 成为权威值,再改 layout.FIRST_RUN_IDLE_OPACITY 不会生效;
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
            paths.settings_path(),
            self._idle_opacity,
            self._refresh_seconds,
            self._warn_threshold,
            self._lit_threshold,
            self._danger_threshold,
        )

        self._bars: list[diskbar.DiskBar] = []
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
            value = float(self._settings.value("idle_opacity", layout.FIRST_RUN_IDLE_OPACITY))
        except (TypeError, ValueError):
            log.warning(
                "event=config_invalid key=idle_opacity fallback=%s", layout.FIRST_RUN_IDLE_OPACITY
            )
            return layout.FIRST_RUN_IDLE_OPACITY
        clamped = min(1.0, max(layout.MIN_OPACITY, value))
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
        value = min(1.0, max(layout.MIN_OPACITY, value))
        if value == self._idle_opacity:
            return
        self._idle_opacity = value
        self._apply_level(self._level)

    def set_idle_opacity(self, value: float) -> None:
        """提交"未报警时的透明度":落盘 + 记一条 `event=idle_opacity`。

        只动最暗档那一端:`_apply_level` 里 `alpha = layout.lerp(idle, LIVE, level)`,
        所以悬停 / 提亮中(level > 0)画面不受影响,松手回到最暗档才看得出差别。

        画面可能已经被 `preview_idle_opacity` 改过,所以**不能**拿"值没变"当跳过的理由 ——
        那样拖完之后根本不会写盘;去重看的是"这个值是否已经落过盘"。
        """
        value = min(1.0, max(layout.MIN_OPACITY, value))
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
        clamped = min(layout.REFRESH_MAX_S, max(layout.REFRESH_MIN_S, value))
        if clamped != value:
            log.warning("event=config_clamped key=refresh_seconds value=%s used=%s", value, clamped)
        return clamped

    @property
    def refresh_seconds(self) -> int:
        """磁盘刷新间隔(秒;托盘步进器显示的就是它)。"""
        return self._refresh_seconds

    def preview_refresh_seconds(self, seconds: int) -> None:
        """改动过程中立刻生效(下次读盘就按新间隔),**不落盘、不记日志**。"""
        seconds = min(layout.REFRESH_MAX_S, max(layout.REFRESH_MIN_S, seconds))
        if seconds == self._refresh_seconds:
            return
        self._refresh_seconds = seconds
        self._timer.setInterval(seconds * 1000)

    def set_refresh_seconds(self, seconds: int) -> None:
        """提交刷新间隔:落盘 + 记一条 `event=refresh_interval`(菜单关闭 / 回车时调它)。"""
        seconds = min(layout.REFRESH_MAX_S, max(layout.REFRESH_MIN_S, seconds))
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
        clamped = min(layout.THRESHOLD_MAX, max(layout.THRESHOLD_MIN, round(value, 2)))
        if clamped != value:
            log.warning("event=config_clamped key=%s value=%s used=%s", key, value, clamped)
        return clamped

    def _enforce_threshold_order(self) -> None:
        """只保证橙色比红色低一格(不然橙色永远轮不到);**常亮阈值不受红色约束**。

        常亮阈值可以高于红色:那样红色会先触发常亮(`layout.is_lit` 取两者中更低的那条),
        也就是"红线即常亮" —— 常亮默认值与红色相同,走的正是这条路。
        """
        if self._danger_threshold - layout.THRESHOLD_MIN < layout.THRESHOLD_GAP:
            raised = layout.THRESHOLD_MIN + layout.THRESHOLD_GAP
            log.warning(
                "event=config_clamped key=danger_threshold value=%s used=%s",
                self._danger_threshold,
                raised,
            )
            self._danger_threshold = raised
        ceiling = self._danger_threshold - layout.THRESHOLD_GAP
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
        warn = min(
            layout.THRESHOLD_MAX - layout.THRESHOLD_GAP, max(layout.THRESHOLD_MIN, round(warn, 2))
        )
        lit = min(layout.THRESHOLD_MAX, max(layout.THRESHOLD_MIN, round(lit, 2)))
        danger = min(layout.THRESHOLD_MAX, max(warn + layout.THRESHOLD_GAP, round(danger, 2)))
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
            self.move(
                area.right() - self.width() - layout.EDGE_MARGIN, area.top() + layout.EDGE_MARGIN
            )

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
            bar = diskbar.DiskBar(
                path, self, warn=self._warn_threshold, danger=self._danger_threshold
            )
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
        return diskbar.summary_text(self._bars, compact=compact)

    def _max_ratio(self) -> float:
        ratios = [r for bar in self._bars if (r := bar.ratio()) is not None]
        return max(ratios) if ratios else 0.0

    # ---- 提亮状态机 ----
    def _retarget(self, *, animate: bool = True) -> None:
        """按“磁盘状态”与“是否悬停”重算提亮目标。

        悬停走硬切(animate=False):鼠标一动就要立刻响应,不能有拖尾;
        阈值提亮保留补间,因为磁盘水位变化缓慢,直接跳变反而突兀。
        """
        lit_by_usage = layout.is_lit(self._max_ratio(), self._lit_threshold, self._danger_threshold)
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
        alpha = layout.lerp(self._idle_opacity, layout.LIVE_OPACITY, self._level) * 255
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

        按 `layout.tail_layout` 给的尺寸画,再让画布缩放到**当前窗口大小** —— 收起途中窗口一直在缩,
        同一段代码顺带画出了"缩成一条"的过程;停稳时两者相等,缩放正好是 1,像素对齐。方向
        (横 / 竖)由布局给,这里没有分支。
        """
        painter.setRenderHint(QPainter.Antialiasing)
        width, height, lines = layout.tail_layout(self._edge or "top", len(self._bars))
        painter.scale(self.width() / max(1, width), self.height() / max(1, height))
        alpha = max(self._tail_alpha, TAIL_MIN_ALPHA)
        for (x, y, w, h), bar in zip(lines, self._bars, strict=False):
            painter.fillRect(QRectF(x, y, w, h), layout.with_alpha(bar.status_rgb(), alpha))

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
        return layout.layout_size(self._ui_scale, len(self._bars) or 1)

    def tail_size(self, edge: str | None = None) -> QSize:
        """尾巴尺寸:**固定的设备像素**,不随主界面缩放变(那是展开形态的事)。

        `edge` 决定线的方向(见 `layout.tail_layout`),默认用当前贴着的那条边;没贴边时按横线那套算。
        """
        rows = len(self._bars) or 1
        width, height, _ = layout.tail_layout(edge or self._edge or "top", rows)
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
        """窗口所在屏幕的工作区,换成**左下开区间**口径(`layout.snap_edge` / `layout.tail_pos` 用这个口径)。

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

        压出屏幕的那部分也算贴上(见 `layout.snap_edge`),所以"拖出去一半松手"会被拉回边上摆正。
        """
        self._expand_now(self._edge_anchor if self._edge_hidden else None)
        bounds = self._screen_bounds()
        if bounds is None:
            return
        edge = layout.snap_edge(self.x(), self.y(), self.width(), self.height(), bounds)
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
        target = layout.tail_pos(
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
                    layout.lerp(
                        self._edge_start_size.width(), self._edge_target_size.width(), progress
                    )
                ),
            ),
            max(
                1,
                round(
                    layout.lerp(
                        self._edge_start_size.height(), self._edge_target_size.height(), progress
                    )
                ),
            ),
        )
        self.move(
            round(layout.lerp(self._edge_start.x(), self._edge_target.x(), progress)),
            round(layout.lerp(self._edge_start.y(), self._edge_target.y(), progress)),
        )

    # ---- 整窗缩放(托盘「调整尺寸」滑杆)----
    def _read_ui_scale(self) -> float:
        """读记住的缩放系数。非法值退回默认,越界夹取并留痕(与其它配置项一套做法)。"""
        try:
            value = float(self._settings.value("ui_scale", layout.UI_SCALE_DEFAULT))
        except (TypeError, ValueError):
            log.warning("event=config_invalid key=ui_scale fallback=%s", layout.UI_SCALE_DEFAULT)
            return layout.UI_SCALE_DEFAULT
        clamped = min(layout.UI_SCALE_MAX, max(layout.UI_SCALE_MIN, round(value, 2)))
        if clamped != value:
            log.warning("event=config_clamped key=ui_scale value=%s used=%s", value, clamped)
        return clamped

    @property
    def ui_scale(self) -> float:
        """整窗缩放系数(拖动时生效的那个,也是 ini 里 `ui_scale` 那个值)。"""
        return self._ui_scale

    def preview_ui_scale(self, scale: float) -> None:
        """拖动过程中即时生效,**不落盘、不记日志**(与透明度滑杆同一套分工)。"""
        scale = min(layout.UI_SCALE_MAX, max(layout.UI_SCALE_MIN, round(scale, 3)))
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
        pad, gap, row_h, width = layout.scaled_layout(scale, len(self._bars) or 1)
        self.setFixedSize(layout.layout_size(scale, len(self._bars) or 1))
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


def main() -> int:
    app = QApplication(sys.argv)
    # QStandardPaths 靠应用名决定配置目录,必须在创建窗口(也就是读配置)之前设置。
    # 日志目录同样由它算出,所以 setup_logging() 也必须排在这一行之后 ——
    # 实测:应用名没设时 AppConfigLocation 会退化成 %LOCALAPPDATA% 本身,日志就跟配置分家了。
    app.setApplicationName(paths.APP_NAME)
    level = logging_setup.setup_logging(paths.log_dir(), paths.APP_NAME)
    log.info(
        "event=start version=%s frozen=%s level=%s config=%s log=%s",
        paths.app_version(),  # 顺便把版本记在启动行:排查时"这是哪一版"最常被问
        bool(getattr(sys, "frozen", False)),
        logging.getLevelName(level),
        paths.settings_path(),
        paths.log_path(),
    )
    # 打包成 --noconsole 后 stderr 是 None:先记下来,省得以后怀疑日志缺了控制台那一半
    log.debug("event=start stderr=%s", "none" if sys.stderr is None else "available")

    # 单实例:同一份配置目录只允许一个进程 —— 两份会互相覆盖配置、共用一份日志
    # (启动时的"超限清空"会把对方的内容一起清掉),托盘还会多出一个图标让人分不清。
    # 再启动一次是**常见路径**:自启已经拉起过了,用户又双击了一次 exe。
    single = single_instance.SingleInstance(paths.config_dir())
    if not single.acquire():
        log.info("event=single_instance action=exit")  # 上面的通知已让已有实例显示小组件
        return 0

    # 有托盘图标,隐藏小组件后程序要继续活着,不能因为窗口不可见就退出
    app.setQuitOnLastWindowClosed(False)

    window = StatusBarWindow()
    window.show()
    log.info("event=window state=shown pos=%d,%d", window.x(), window.y())

    icon = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        icon = tray.TrayIcon(window)
        icon.show()
        log.info("event=tray state=created")
    else:
        # 托盘是隐藏 / 穿透之后唯一的恢复入口,没有它小组件藏起来就找不回来了
        log.warning("event=tray state=unavailable hint=隐藏后将无法从托盘恢复")

    single.activated(window.reveal)  # 第二个实例启动 → 把小组件叫出来
    app.aboutToQuit.connect(lambda: log.info("event=stop"))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
