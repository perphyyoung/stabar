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

import layout
import main
import tray


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

    # 这是**小组件自己**的右键菜单(隐藏 / 立即刷新 / 退出),与托盘的菜单是两个:
    # 托盘那份含控件行、顺序见 `test_tray.py::test_menu_items_and_order`
    assert items == ["隐藏", "立即刷新", "", "退出"]  # 分隔线在这里是空字符串
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
    """左键单击托盘 = 切换显示:发的是 `activated` 信号,覆盖"信号 → 槽"的连线。

    进程内只能走到信号这一层:"系统把点击送到托盘图标"发生在 shell 那一侧,进程内看不见。
    """
    tray_icon = tray.TrayIcon(window)
    try:
        window.show()

        tray_icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        assert not window.isVisible()
        assert_in_order(events(), "event=window state=hidden")

        tray_icon.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        assert window.isVisible()
        assert_in_order(events(), "event=window state=hidden", "event=window state=shown")
    finally:
        tray_icon.hide()


def test_tray_menu_pops_and_sets_idle_opacity(window, events, qtbot) -> None:
    """托盘菜单真的弹出来,并改到"未报警时的透明度"。

    这是 Qt 官方推荐的托盘自动化姿势(`geometry()` + `contextMenu().popup()`,见官方
    知识库 "Automating Tray Icon"):不点屏幕坐标、不依赖 shell。**真点托盘图标**那件事
    官方明确说坐标会因其他程序增删图标而漂移,归 B 层(`test_e2e_real_input.py`)。
    """
    tray_icon = tray.TrayIcon(window)
    try:
        menu = tray_icon.contextMenu()
        assert menu is not None

        def drag_slider() -> None:
            tray_icon._opacity_slider.setValue(10)  # 拖到第 10 格 = 50%
            tray_icon._opacity_slider.sliderReleased.emit()  # 松手 → 提交
            menu.close()  # 收起菜单(还会触发一次提交,应被去重)

        QTimer.singleShot(120, drag_slider)
        menu.popup(tray_icon.geometry().center())  # 离屏平台下可能是空矩形,popup 不挑坐标

        qtbot.waitUntil(
            lambda: any("event=idle_opacity value=0.50" in e for e in events()), timeout=2000
        )
        assert_in_order(events(), "event=menu open=tray", "event=idle_opacity value=0.50")
        assert window.idle_opacity == pytest.approx(0.50)
        assert window._bars[0]._alpha == pytest.approx(0.50 * 255)  # 立刻生效
        assert len([e for e in events() if "event=idle_opacity" in e]) == 1  # 松手 + 关菜单只记一条
    finally:
        tray_icon.hide()


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

    tray_icon = tray.TrayIcon(window)
    try:
        tray_icon._on_activated(QSystemTrayIcon.ActivationReason.Trigger)
        assert window.isVisible()

        idle_alpha = layout.FIRST_RUN_IDLE_OPACITY * 255
        assert window._bars[0]._alpha == pytest.approx(idle_alpha), (
            f"从托盘显示后应回到最暗档 {idle_alpha:.1f},"
            f"实际 {window._bars[0]._alpha:.1f}\n事件流尾部:\n" + "\n".join(events()[-6:])
        )

        # 也不是"下次刷新就自愈":刷新读的还是那个没被清掉的 _hovered
        window.refresh()
        qtbot.waitUntil(lambda: window._bars[0]._alpha == pytest.approx(idle_alpha), timeout=2000)
    finally:
        tray_icon.hide()
