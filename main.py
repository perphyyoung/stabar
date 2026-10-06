"""stabar —— Win11 磁盘占用桌面小组件(最小可用版本)。

显示 C:/D: 磁盘占用,无标题条、半透明背景、置顶显示,可拖动。
运行: uv run python main.py
"""

from __future__ import annotations

import shutil
import sys

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# ---- 可调参数 ----
DRIVES = ("C:/", "D:/")  # 要监控的盘符
BG_ALPHA = 10  # 背景透明度 0(全透明) ~ 255(不透明)
REFRESH_MS = 15_000  # 刷新间隔(毫秒)
WINDOW_W = 248  # 窗口宽度
ROW_H = 22  # 单行高度
PAD = 14  # 内边距
GAP = 6  # 行间距
EDGE_MARGIN = 24  # 距屏幕边缘距离

# ---- 配色 ----
COLOR_BG = QColor(28, 28, 30, BG_ALPHA)
COLOR_TRACK = QColor(255, 255, 255, 38)
COLOR_TEXT = QColor(235, 235, 240)
COLOR_DIM = QColor(158, 158, 168)
COLOR_ACCENT = QColor(78, 154, 241)
COLOR_WARN = QColor(233, 176, 78)
COLOR_DANGER = QColor(231, 92, 92)


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


class DiskBar(QWidget):
    """单行:盘符 + 占用进度条 + 百分比。"""

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._path = path
        self._label = path[0] + ":"
        self._stats = read_disk(path)
        # 让父窗口统一接收鼠标事件,简化拖动逻辑
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedHeight(ROW_H)
        self.reload()

    def reload(self) -> None:
        self._stats = read_disk(self._path)
        self.update()

    def detail_text(self) -> str:
        if self._stats is None:
            return f"{self._label}  不可用"
        used, total = self._stats
        ratio = used / total * 100 if total else 0.0
        return (
            f"{self._label}  已用 {human_size(used)} / {human_size(total)}"
            f"  可用 {human_size(total - used)}  ({ratio:.1f}%)"
        )

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        font = QFont(self.font())
        font.setPointSizeF(9.0)
        painter.setFont(font)

        w, h = self.width(), self.height()

        painter.setPen(COLOR_TEXT)
        painter.drawText(0, 0, 26, h, Qt.AlignVCenter | Qt.AlignLeft, self._label)

        percent_w = 44
        bar_x = 32
        bar_w = max(60, w - bar_x - percent_w - 8)
        bar_h = 8
        bar_y = (h - bar_h) // 2

        track = QPainterPath()
        track.addRoundedRect(bar_x, bar_y, bar_w, bar_h, bar_h / 2, bar_h / 2)
        painter.fillPath(track, COLOR_TRACK)

        if self._stats is None:
            painter.setPen(COLOR_DIM)
            painter.drawText(bar_x, 0, w - bar_x, h, Qt.AlignVCenter | Qt.AlignLeft, "不可用")
            return

        used, total = self._stats
        ratio = used / total if total else 0.0
        if ratio >= 0.9:
            color = COLOR_DANGER
        elif ratio >= 0.75:
            color = COLOR_WARN
        else:
            color = COLOR_ACCENT

        fill_w = bar_w * min(ratio, 1.0)
        if fill_w > 0:
            fill = QPainterPath()
            fill_w = max(fill_w, bar_h)
            fill.addRoundedRect(bar_x, bar_y, fill_w, bar_h, bar_h / 2, bar_h / 2)
            painter.fillPath(fill, color)

        painter.setPen(COLOR_DIM)
        painter.drawText(
            w - percent_w, 0, percent_w, h,
            Qt.AlignVCenter | Qt.AlignRight, f"{ratio * 100:.0f}%",
        )


class StatusBarWindow(QWidget):
    """无标题条、半透明的置顶窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(
            WINDOW_W,
            PAD * 2 + ROW_H * len(DRIVES) + GAP * (len(DRIVES) - 1),
        )

        self._drag_offset: QPoint | None = None

        self._bars: list[DiskBar] = []
        for i, path in enumerate(DRIVES):
            bar = DiskBar(path, self)
            bar.setGeometry(
                PAD, PAD + i * (ROW_H + GAP), WINDOW_W - PAD * 2, ROW_H
            )
            self._bars.append(bar)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

        self.refresh()
        self._move_to_corner()

    def refresh(self) -> None:
        for bar in self._bars:
            bar.reload()
        self.setToolTip("\n".join(bar.detail_text() for bar in self._bars))
        self.update()

    def _move_to_corner(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - EDGE_MARGIN,
            area.top() + EDGE_MARGIN,
        )

    # ---- 绘制 ----
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        radius = 12.0
        rect = QPainterPath()
        rect.addRoundedRect(0, 0, self.width(), self.height(), radius, radius)
        painter.fillPath(rect, COLOR_BG)

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None

    # ---- 右键菜单(无标题条,所以退出入口放这里) ----
    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("立即刷新", self.refresh)
        menu.addSeparator()
        menu.addAction("退出", QApplication.quit)
        menu.exec(event.globalPos())


def main() -> int:
    app = QApplication(sys.argv)
    window = StatusBarWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
