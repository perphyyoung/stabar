"""E2E(A 层):进程内驱动真实事件,断言日志事件序列。

A 层覆盖:菜单项触发、隐藏 / 显示、穿透开关、悬停提亮 —— 断言的是**事件序列**,不只是终态。
断言一律"日志(程序声明)+ 一条外部事实(真实状态)"配对,免得退化成"验证代码和日志犯同一个错"。

A 层覆盖不了、留给 B 层(`tests/test_e2e_real_input.py`)的三条:
1. Windows 分层窗口的逐像素命中测试(隐形命中层是否真的接住鼠标);
2. 操作系统把右键转成 `contextMenuEvent` 的那一层;
3. 系统托盘里的真实点击。
"""

from __future__ import annotations

from e2e_support import assert_in_order
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import main


def open_context_menu(window, choose: str | None = None) -> list[str]:
    """真的发一个右键菜单事件;可选在菜单里选一项。返回菜单项文字列表。

    不能用 `QTest.mouseClick(window, Qt.RightButton)` —— 实测它**不会**触发
    `contextMenuEvent`:鼠标→菜单的转换发生在平台窗口层,而 QTest 把鼠标事件直接发给
    widget,跳过了那一层(见 `开发经验.md` 第 8 条)。
    """
    captured: list[str] = []

    def act() -> None:
        popup = QApplication.activePopupWidget()  # 菜单 exec() 的嵌套循环里才拿得到
        if not isinstance(popup, QMenu):
            return
        captured.extend(action.text() for action in popup.actions())
        if choose is not None:
            next(action for action in popup.actions() if action.text() == choose).trigger()
        popup.close()  # 让 exec() 返回

    QTimer.singleShot(120, act)
    center = window.rect().center()
    QApplication.sendEvent(
        window,
        QContextMenuEvent(QContextMenuEvent.Reason.Mouse, center, window.mapToGlobal(center)),
    )
    return captured


def test_context_menu_lists_items(window, events) -> None:
    window.show()
    items = open_context_menu(window)

    assert items[0] == "隐藏"
    assert "立即刷新" in items
    assert items[-1] == "退出"
    assert_in_order(events(), "event=menu open=context")


def test_menu_hide_really_hides(window, events) -> None:
    """右键 → 隐藏:事件顺序正确,且窗口真的不可见。"""
    window.show()
    assert open_context_menu(window, choose="隐藏")[0] == "隐藏"

    assert_in_order(
        events(),
        "event=menu open=context",
        "event=menu item=隐藏",
        "event=window state=hidden",
    )
    assert not window.isVisible()  # 外部事实

    # 菜单关掉后按光标真实位置重判悬停:否则会一直停在最亮(曾是真实 bug)
    assert not window._hovered


def test_tray_left_click_toggles_visibility(window, events) -> None:
    """左键单击托盘 = 切换显示:走真实的 activated 槽,不是直接调切换函数。"""
    tray = main.TrayIcon(window)
    try:
        window.show()

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert not window.isVisible()
        assert_in_order(events(), "event=window state=hidden")

        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert window.isVisible()
        assert_in_order(events(), "event=window state=hidden", "event=window state=shown")
    finally:
        tray.hide()


def test_click_through_logs_and_flags(window, events) -> None:
    """穿透是"开了只能回托盘关",所以日志必须留痕。"""
    window.set_click_through(True)
    assert bool(window.windowFlags() & main.Qt.WindowTransparentForInput)
    assert_in_order(events(), "event=click_through enabled=True")

    window.set_click_through(False)
    assert not bool(window.windowFlags() & main.Qt.WindowTransparentForInput)
    assert_in_order(
        events(), "event=click_through enabled=True", "event=click_through enabled=False"
    )


def test_hover_reveals_then_reverts(window, events) -> None:
    """悬停提亮:用真的 Enter / Leave 事件派发,而不是直接调处理方法。"""
    before = window._bars[0]._alpha
    assert before < 255  # 低占用下应该是最暗档

    QApplication.sendEvent(window, QEvent(QEvent.Type.Enter))
    assert window._bars[0]._alpha == 255  # 硬切,同一 tick 到位
    assert_in_order(
        events(), "event=hover state=enter", "event=reveal target=1.00 from=0.00 reason=hover"
    )

    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
    assert window._bars[0]._alpha == before
    assert_in_order(
        events(),
        "event=hover state=enter",
        "event=hover state=leave",
        "event=reveal target=0.00 from=1.00 reason=usage",
    )
