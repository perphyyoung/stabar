"""自绘的小对话框:无边框 + 自绘标题栏。

为什么要自绘标题栏:原生标题栏(**连标题文字的位置**)整行都是 Windows 按系统主题画的,Qt 只把字符串
交过去(`setWindowTitle`),没有设置对齐的接口 —— 想让标题居中就只能自己画。代价是没有系统阴影 / 圆角,
拖动也得自己接(见 `TitleBar`)。

拆成独立模块的理由与 `logging_setup.py` 一样:它是纯界面零件,不属于"应用装配"(见 AGENTS.md)。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

TITLE = "还原配置"  # 窗口名(任务栏 / Alt-Tab 用的)与自绘标题栏上那行字是同一个


class TitleBar(QWidget):
    """自绘标题栏:按住它就能拖动所在窗口。

    对话框去掉原生框架(`FramelessWindowHint`)之后,系统不再提供拖动 —— 这一层得自己接。
    标题文字用带 `WA_TransparentForMouseEvents` 的 QLabel,于是指针落在文字上时事件**直接到本控件**,
    不依赖"标签忽略 → 父控件冒泡"那条链路(`sendEvent` 直投也测不出那条链路,见 开发经验第 35 条)。
    """

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._window = window
        self._offset: QPoint | None = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            # 记下"按下时光标相对窗口左上角"的偏移,移动时保持这个偏移不变
            self._offset = event.globalPosition().toPoint() - self._window.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._window.move(event.globalPosition().toPoint() - self._offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._offset = None
        super().mouseReleaseEvent(event)


class ResetDialog(QDialog):
    """确认框:逐行列出要改的项(当前值 → 默认值);没有要改的项就只说一句。

    自建而不是 `QMessageBox`,理由有三个:列表要**直接可见**(`QMessageBox` 的 `setDetailedText`
    默认藏在"Show Details"按钮后面)、破坏性按钮要标红、标题位置要由我们定(见模块说明)。

    父窗口有意留空(顶层窗口):调用方的小组件可能正处于隐藏状态,挂在隐藏窗口下的对话框可能不显示。
    """

    def __init__(self, rows: list[tuple[str, str, str]], danger_color: QColor) -> None:
        """`rows` 空即"没有要改的项":内容换成一句说明 + 居中的单颗「好」,不再显示「还原」。"""
        super().__init__(None)
        self.setWindowTitle(TITLE)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        frame = QFrame(self)  # 没有原生框架之后得自己给个边框,否则深色背景下看不出窗口边界
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)  # 让边框贴着窗口,四周不留缝
        outer.addWidget(frame)

        column = QVBoxLayout(frame)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self._build_title_bar())
        divider = QFrame(frame)
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFrameShadow(QFrame.Shadow.Sunken)
        column.addWidget(divider)
        column.addLayout(self._build_body(rows, danger_color))

    def _build_title_bar(self) -> QWidget:
        """标题栏:一行居中的粗体标题(没有 ✕ —— 「取消」就是出口)。"""
        self._title_bar = TitleBar(self)
        self._title_bar.setToolTip("按住这里可以拖动窗口")
        bar = QHBoxLayout(self._title_bar)
        bar.setContentsMargins(12, 8, 12, 8)
        self.title_label = QLabel(TITLE, self._title_bar)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)  # 居中:可断言,不靠眼睛
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents)  # 点击穿透到标题栏
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        bar.addWidget(self.title_label)
        return self._title_bar

    def _build_body(self, rows: list[tuple[str, str, str]], danger_color: QColor) -> QVBoxLayout:
        """正文:说明 + 清单 + 按钮组,整块左右居中(两侧留白出自同一份 margins,天然对称)。"""
        layout = QVBoxLayout()
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(12)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)  # 左留白
        if rows:
            layout.addWidget(
                QLabel("要还原以下配置吗？窗口位置不动。", self), 0, Qt.AlignmentFlag.AlignCenter
            )
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            for index, (name, current, default) in enumerate(rows):
                grid.addWidget(QLabel(name, self), index, 0)
                grid.addWidget(QLabel(f"{current} → {default}", self), index, 1)
            row_box = QHBoxLayout()  # 清单整块居中,内部两列仍各自对齐
            row_box.addStretch(1)
            row_box.addLayout(grid)
            row_box.addStretch(1)
            layout.addLayout(row_box)

            self.cancel_button = QPushButton("取消", self)
            self.cancel_button.setDefault(True)  # 回车不误触:默认就是"取消"
            self.reset_button = QPushButton("还原", self)
            self.reset_button.setStyleSheet(f"color: {danger_color.name()};")  # 破坏性:标红
            self.cancel_button.clicked.connect(self.reject)
            self.reset_button.clicked.connect(self.accept)
            pair: tuple[QPushButton, ...] = (self.cancel_button, self.reset_button)
        else:
            layout.addWidget(
                QLabel("所有配置都已经是默认值，没有需要还原的项。", self),
                0,
                Qt.AlignmentFlag.AlignCenter,
            )
            self.ok_button = QPushButton("好", self)
            self.ok_button.setDefault(True)
            self.ok_button.clicked.connect(self.accept)
            pair = (self.ok_button,)

        widest = max(button.sizeHint().width() for button in pair)
        for button in pair:
            button.setFixedWidth(widest)  # 等宽才齐(按字体/DPI 自适应取最宽的那个)
            buttons.addWidget(button)
        buttons.addStretch(1)  # 右留白:与左边同一份,两侧间距才一样
        layout.addLayout(buttons)
        return layout
