"""E2E(A 层):**必须真的把事件 / 信号发出去**才能验证的链路,断言日志事件序列。

A 层只管跨对象的交互链路:右键事件 → QMenu → 选中项 → 窗口状态;托盘信号 → 槽 → 显示切换。
**能直接调一个方法就验证的,属于第 2 层**(组件测试),不要往这里放 —— 反过来,第 4 层只补
A 层原理上覆盖不到的(判据见 AGENTS.md 的「测试分层」)。

断言一律"日志(程序声明)+ 一条外部事实(真实状态)"配对,免得退化成"验证代码和日志犯同一个错"。

A 层覆盖不了、留给 B 层(`tests/test_e2e_real_input.py`)的三条:
1. Windows 分层窗口的逐像素命中测试(隐形命中层是否真的接住鼠标);
2. 操作系统把右键转成 `contextMenuEvent` 的那一层;
3. 系统托盘里的真实点击 —— 以及真实退出路径。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from e2e_support import assert_in_order
from PySide6.QtCore import QTimer
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


def test_tray_show_after_menu_hide_returns_to_idle(window, events, qtbot, monkeypatch) -> None:
    """右键隐藏、再从托盘显示,应该回到平时的最暗档(回归:曾一直全亮)。

    曾经的真 bug:`contextMenuEvent` 收尾按**光标实际位置**重判悬停,而点"隐藏"时光标正停在
    菜单项上(菜单就弹在窗口里,该点仍在窗口矩形内)→ `_hovered` 被置回 True、亮度被拉到 1.0,
    可窗口此刻已经隐藏;此后不会再有 Enter / Leave 来清这个标记,所以从托盘显示出来就是全亮的。
    它也不是"下一次刷新会自愈"的:15 秒后的刷新读的也是这个 `_hovered`,照样拉满。
    修法是收尾时先看窗口是否还在(见 `main.contextMenuEvent`)。
    """
    # 光标停在窗口内,等价于"刚点完菜单项"的那一瞬间(收尾逻辑读的是真实光标位置)
    monkeypatch.setattr(
        main, "QCursor", SimpleNamespace(pos=lambda: window.mapToGlobal(window.rect().center()))
    )

    window.show()
    assert open_context_menu(window, choose="隐藏")[0] == "隐藏"
    assert not window.isVisible()

    tray = main.TrayIcon(window)
    try:
        tray._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert window.isVisible()

        idle_alpha = main.FIRST_RUN_IDLE_OPACITY * 255
        assert window._bars[0]._alpha == pytest.approx(idle_alpha), (
            f"从托盘显示后应回到最暗档 {idle_alpha:.1f},"
            f"实际 {window._bars[0]._alpha:.1f}\n事件流尾部:\n" + "\n".join(events()[-6:])
        )

        # 也不是"下次刷新就自愈":刷新读的还是那个没被清掉的 _hovered
        window.refresh()
        qtbot.waitUntil(lambda: window._bars[0]._alpha == pytest.approx(idle_alpha), timeout=2000)
    finally:
        tray.hide()
