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
    window.show()
    tray._sync_state()
    assert tray._visibility.text() == "隐藏"

    tray._toggle_visible()
    assert not window.isVisible()
    assert tray._visibility.text() == "显示"


def test_left_click_toggles_but_right_click_does_not(window, tray) -> None:
    window.show()
    tray._on_activated(main.QSystemTrayIcon.ActivationReason.Trigger)
    assert not window.isVisible()

    tray._on_activated(main.QSystemTrayIcon.ActivationReason.Context)
    assert not window.isVisible()


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
