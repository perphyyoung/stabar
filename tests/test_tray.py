"""TrayIcon:菜单内容与顺序、透明度滑杆、刷新步进器、勾选联动、显示切换、提示文字。"""

import pytest

import main


@pytest.fixture
def tray(window) -> main.TrayIcon:
    return main.TrayIcon(window)


def saved_line(tmp_path, key: str) -> str:
    """配置文件里以 `key` 开头的那一行(真读文件,不读 QSettings 的缓存)。"""
    text = (tmp_path / main.SETTINGS_NAME).read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith(key))


def test_menu_items_and_order(tray) -> None:
    assert [action.text() for action in tray._menu.actions()] == [
        "隐藏",
        "立即刷新",
        "刷新频率",  # 步进器那一行(QWidgetAction;控件本身在屏幕上另有文字)
        "透明度",  # 滑杆那一行,同上
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
    """托盘悬停提示跟着刷新走,不用等定时器。"""
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


def test_opacity_slider_matches_window_value(window, tray) -> None:
    """滑杆与标签跟着生效值走;一格 = OPACITY_STEP,量程覆盖配置允许的整个区间。"""
    assert tray._opacity_slider.minimum() * main.OPACITY_STEP == pytest.approx(main.MIN_OPACITY)
    assert tray._opacity_slider.maximum() * main.OPACITY_STEP == pytest.approx(main.LIVE_OPACITY)

    tray._sync_state()
    assert tray._opacity_slider.value() == 6  # 首跑默认 0.30 = 第 6 格
    assert tray._opacity_value.text() == "30%"

    window.set_idle_opacity(0.50)
    tray._sync_state()
    assert tray._opacity_slider.value() == 10
    assert tray._opacity_value.text() == "50%"


def test_hand_edited_value_rounds_into_slider(window, tray) -> None:
    """配置文件里可以写任意精度(0.333):滑杆取最近的一格(35%),标签仍显示真实值(33%)。"""
    window.set_idle_opacity(0.333)
    tray._sync_state()
    assert tray._opacity_slider.value() == 7  # 7 × 5% = 35%,是离 33% 最近的一格
    assert tray._opacity_value.text() == "33%"


def test_drag_previews_then_commit_persists(window, tray, tmp_path, events) -> None:
    """拖动只预览(不落盘),松手才提交;同一个值重复提交只记一条日志。"""
    tray._sync_state()
    assert saved_line(tmp_path, "idle_opacity") == "idle_opacity=0.3"

    tray._opacity_slider.setValue(14)  # 相当于把滑杆拖到第 14 格 = 70%
    assert tray._opacity_value.text() == "70%"
    assert window._bars[0]._alpha == pytest.approx(0.70 * 255)  # 立刻看得到
    assert saved_line(tmp_path, "idle_opacity") == "idle_opacity=0.3"  # 但还没落盘

    tray._opacity_slider.sliderReleased.emit()  # 松手
    assert saved_line(tmp_path, "idle_opacity") == "idle_opacity=0.7"  # 落盘的值没有浮点尾巴

    tray._commit_opacity()  # 菜单关闭时还会来一次
    assert [e for e in events() if "event=idle_opacity" in e] == ["event=idle_opacity value=0.70"]


def test_refresh_spin_range_default_and_editable(tray, window) -> None:
    """步进器:1 ~ 60 秒、步进 1、默认与历史行为一致(15 秒),并且可以直接编辑。"""
    assert tray._refresh_spin.minimum() == main.REFRESH_MIN_S == 1
    assert tray._refresh_spin.maximum() == main.REFRESH_MAX_S == 60
    assert tray._refresh_spin.singleStep() == 1
    assert tray._refresh_spin.value() == main.REFRESH_DEFAULT_S == 15
    assert not tray._refresh_spin.isReadOnly()

    window.set_refresh_seconds(30)
    tray._sync_state()
    assert tray._refresh_spin.value() == 30


def test_refresh_spin_applies_then_persists(window, tray, tmp_path, events) -> None:
    """改步进器:定时器立刻换间隔(不落盘),提交后写 ini 并记一条日志。"""
    tray._sync_state()
    assert window._timer.interval() == 15_000

    tray._refresh_spin.setValue(30)
    assert window.refresh_seconds == 30
    assert window._timer.interval() == 30_000  # 立刻生效:下次读盘就按新间隔
    assert saved_line(tmp_path, "refresh_seconds") == "refresh_seconds=15"  # 但还没落盘

    tray._commit_refresh()
    assert saved_line(tmp_path, "refresh_seconds") == "refresh_seconds=30"

    tray._commit_refresh()  # 菜单关闭时还会来一次
    assert [e for e in events() if "event=refresh_interval" in e] == [
        "event=refresh_interval seconds=30"
    ]
