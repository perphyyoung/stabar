"""盘条:一行 = 盘符 + 占用进度条 + 百分比,以及悬停提示文字的拼装。

窗口只负责"把盘条摆好、下发不透明度与阈值";**怎么画、文字长什么样全在这里**:
- 颜色是这套界面唯一的"状态表达"(蓝 / 橙 / 红,带滞回),配色常量也在本模块;
- 悬停提示只有一处产出(`summary_text`):主界面用完整明细,托盘用紧凑版
  (托盘提示受 Windows 限制只有 128 个字符,见 `日志使用说明.md` / `README`)。

两条警戒线的值**不在**这里:它们是应用配置(托盘菜单可改、ini 里存着),由 `main` 传进来。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

import drives
import layout

# --- 文字描边:保证在浅色壁纸下也读得清 ---
OUTLINE_RGB = (12, 12, 14)
OUTLINE_WIDTH = 2.0  # 描边粗细(像素)
TRACK_RATIO = 0.15  # 进度条底槽不透明度 = 前景不透明度 × 该系数

# --- 色相 ---
BASE_TEXT = (238, 238, 242)
BASE_DIM = (196, 196, 204)
BASE_TRACK = (255, 255, 255)
COLOR_ACCENT = (78, 154, 241)
COLOR_WARN = (233, 176, 78)
COLOR_DANGER = (231, 92, 92)
COLOR_HYSTERESIS = 0.005  # 颜色切换滞回,避免卡在阈值上反复闪烁


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


class DiskBar(QWidget):
    """单行:盘符 + 占用进度条 + 百分比。不透明度由窗口统一下发。"""

    def __init__(
        self,
        path: str,
        parent: QWidget | None = None,
        *,
        warn: float,
        danger: float,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._label = path[0] + ":"
        self._stats = drives.read_disk(path)
        self._alpha = 255.0
        self._warned = False  # 警告色滞回标记
        # 两条警戒线由窗口传进来(可配、存在 ini 里),本模块不持有它们的默认值
        self._warn = warn
        self._danger = danger
        self._font = QFont(self.font())
        self._font.setPointSizeF(9.0)
        # 让父窗口统一接收鼠标事件,拖动和悬停都更简单
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedHeight(layout.ROW_H)
        self._scale = layout.UI_SCALE_DEFAULT  # 由窗口下发,见 set_scale

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
            f"{self._label}  已用 {layout.human_size(used)} / {layout.human_size(total)}"
            f"  可用 {layout.human_size(total - used)}  ({ratio:.1f}%)"
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
        return layout.with_alpha(self._bar_rgb(ratio), self._alpha)

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
        self.setFixedHeight(max(1, round(layout.ROW_H * scale)))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 绘制代码一律按**基准像素**写(26 / 32 / 8 那些),缩放由画布变换完成 —— 这样加一个
        # 像素常量不必再记得"要不要乘系数",也不会漏。字号同理:9pt 经变换就是 9·s 的效果,
        # 不必去 setPointSizeF(缩小时还会触发 QFont 的"点大小必须 > 0"警告)。
        painter.scale(self._scale, self._scale)
        w, h = self.width() / self._scale, self.height() / self._scale
        text_color = layout.with_alpha(BASE_TEXT, self._alpha)
        dim_color = layout.with_alpha(BASE_DIM, self._alpha)
        outline = layout.with_alpha(OUTLINE_RGB, self._alpha)

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
        painter.fillPath(track, layout.with_alpha(BASE_TRACK, self._alpha * TRACK_RATIO))

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

    托盘的提示有平台上限(Windows 的 `NOTIFYICONDATA.szTip` 只有 128 个字符),完整明细会被
    shell 从中间截掉;主界面的 tooltip 没有这个限制,所以两种长度各取所需,而文字始终由这里产出。
    """
    if compact:
        return "  ".join(bar.short_text() for bar in bars)
    return "\n".join(bar.detail_text() for bar in bars)
