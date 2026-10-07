"""TrayIcon:菜单内容与顺序、勾选联动、显示切换、提示文字。"""

import pytest

import main


@pytest.fixture
def tray(window) -> main.TrayIcon:
    return main.TrayIcon(window)


def test_menu_items_and_order(tray) -> None:
    assert [action.text() for action in tray._menu.actions()] == [
        "隐藏",
        "立即刷新",
        "鼠标穿透",
        "开机自启",
        "",  # 分隔线
        "退出",
    ]


def test_hide_text_follows_visibility(window, tray) -> None:
    """菜单文字跟随窗口可见性;"真的隐藏了"由 `test_e2e_ui.py` 验证,这里只管文字。"""
    window.show()
    tray._sync_state()
    assert tray._visibility.text() == "隐藏"

    window.hide_widget()
    tray._sync_state()
    assert tray._visibility.text() == "显示"


def test_context_reason_does_not_toggle(window, tray) -> None:
    """右键托盘是弹菜单(由系统处理),不该走"切换显示"那条路。

    左键单击真的会切换显示 —— 那是"信号 → 槽 → 窗口状态"的链路,归 `test_e2e_ui.py`。
    """
    window.show()
    tray._on_activated(main.QSystemTrayIcon.ActivationReason.Context)
    assert window.isVisible()


def test_tooltip_follows_window_refresh(window, tray) -> None:
    """托盘悬停提示跟着刷新走,不用等 15 秒定时器。"""
    window.refresh()
    assert "已用" in tray.toolTip()


def test_autostart_checkbox_reflects_registry(window, tray, fake_registry) -> None:
    tray._sync_state()
    assert not tray._autostart.isChecked()

    tray._set_autostart(True)
    assert tray._autostart.isChecked()
    assert fake_registry.values[main.APP_NAME] == main.autostart_command()


def test_click_through_checkbox_reflects_window(window, tray) -> None:
    window.set_click_through(True)
    tray._sync_state()
    assert tray._click_through.isChecked()
