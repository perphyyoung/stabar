"""悬停明细卡:主窗口只有一寸大,装不下三行完整明细;Qt 自带的 tooltip 又弹在光标旁 ——
而光标就停在主界面上,弹出来必然盖住主界面。这里改成**自绘的小卡片**:悬停主界面片刻后
出现在窗口**下方**(下方放不下翻上方),与窗口矩形**绝不相交** —— "hover 文字不覆盖主界面"
这条要求由 `card_pos` 一处保证,并有纯函数用例钉着。

卡片对鼠标**完全透明**(`WindowTransparentForInput`):光标扫过它也不算任何窗口的事,
永远不抢主窗口的悬停态 —— 这是它的命门,别去掉;去掉就会出现
"卡片一出现 → 主窗口收到 Leave → 卡片藏 → 又 Enter"的闪烁循环。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

import diskbar
import layout

SHOW_DELAY_MS = 500  # 悬停多久才出现:系统 tooltip 约 700ms;太快晃眼,太慢像没反应
GAP = 4  # 卡片与窗口边缘的缝隙(上、下两种关系通用)
PANEL_PAD = 10  # 文字与卡片边缘的留白(基准像素)
PANEL_ALPHA = 225  # 底板不透明度:明细是"读"的信息,比尾巴要实
RADIUS = 6.0  # 圆角
FONT_PT = 9.0  # 与盘条同字号(见 diskbar.DiskBar 的 _font)
LINE_LEAD = 4  # 行间额外间距(基准像素)


def card_pos(window: QRect, card: QSize, work_area: QRect) -> QPoint:
    """卡片左上角:默认窗口**下方** `GAP` px、左对齐;下方放不下翻上方;x 夹进工作区。

    硬保证:**卡片矩形与窗口矩形不相交** —— "hover 文字不覆盖主界面"全靠这一条(用例钉着)。
    上下都放不下(窗口几乎和工作区一样高)时只保"整块在屏幕内"这条底线,不再强求不相交。
    坐标一律按"左上角 + 宽高"的**半开区间**算,绕开 `QRect.right()` 的闭区间歧义
    (项目里其它几何函数同款口径,见 `layout.snap_edge` 的说明)。
    """
    left, top = work_area.x(), work_area.y()
    right, bottom = left + work_area.width(), top + work_area.height()  # 半开
    x = min(max(window.x(), left), max(left, right - card.width()))
    below = window.y() + window.height() + GAP
    if below + card.height() <= bottom:
        return QPoint(x, below)
    above = window.y() - GAP - card.height()
    if above >= top:
        return QPoint(x, above)
    return QPoint(x, max(top, min(below, bottom - card.height())))


class HoverCard(QWidget):
    """明细卡窗口:深色底板 + 逐行明细,样式与盘条 / 尾巴同一套配色。"""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput,  # 命门:对鼠标完全透明,见模块说明
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._text = ""
        self._scale = layout.UI_SCALE_DEFAULT
        self._window_rect = QRect()  # present 时记下:可见期间刷新要按它们重算位置
        self._work_area = QRect()

    @property
    def text(self) -> str:
        """当前展示的明细(出处仍是 `diskbar.summary_text`,本模块不拼第二份)。"""
        return self._text

    def present(self, text: str, scale: float, window_rect: QRect, work_area: QRect) -> None:
        """换文字、量尺寸、按 `card_pos` 落位再显示 —— 一次到位,不在旧位置闪现。"""
        self._text = text
        self._scale = scale
        self._window_rect = QRect(window_rect)
        self._work_area = QRect(work_area)
        self._fit_to_text()
        self.move(card_pos(window_rect, self.frameGeometry().size(), work_area))
        self.show()
        self.raise_()

    def update_text(self, text: str, scale: float) -> None:
        """可见期间的刷新(`refresh` 调):内容更新,位置按**记下的窗口矩形**重算。

        要重算而不是沿用旧位置:明细变宽变高时,窗口贴着屏幕边的情况下不重算会压回主界面上。
        """
        self.present(text, scale, self._window_rect, self._work_area)

    def _fit_to_text(self) -> None:
        """按文字量出卡片尺寸:字号随整窗缩放(与盘条同一套"基准像素 × scale"的口径)。"""
        lines = self._text.splitlines() or [""]
        font = QFont(self.font())
        font.setPointSizeF(FONT_PT)
        metrics = QFontMetricsF(font)
        line_h = metrics.height() + LINE_LEAD
        width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
        self.setFixedSize(
            round((width + PANEL_PAD * 2) * self._scale),
            round((line_h * len(lines) - LINE_LEAD + PANEL_PAD * 2) * self._scale),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 与盘条同一套做法:按基准像素画,缩放交给画布变换(见 diskbar.paintEvent 的注释)
        painter.scale(self._scale, self._scale)
        w, h = self.width() / self._scale, self.height() / self._scale
        panel = QPainterPath()
        panel.addRoundedRect(0.5, 0.5, w - 1, h - 1, RADIUS, RADIUS)
        painter.fillPath(panel, layout.with_alpha(diskbar.OUTLINE_RGB, PANEL_ALPHA))
        font = QFont(self.font())
        font.setPointSizeF(FONT_PT)
        metrics = QFontMetricsF(font)
        line_h = metrics.height() + LINE_LEAD
        painter.setFont(font)
        painter.setPen(layout.with_alpha(diskbar.BASE_TEXT, 255))
        for index, line in enumerate(self._text.splitlines() or [""]):
            y = PANEL_PAD + metrics.ascent() + index * line_h
            painter.drawText(QPointF(PANEL_PAD, y), line)
