"""TrayIcon:菜单内容与顺序、透明度滑杆、刷新步进器、勾选联动、显示切换、提示文字。"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel

import main


@pytest.fixture
def tray(window) -> main.TrayIcon:
    return main.TrayIcon(window)


def saved_line(tmp_path, key: str) -> str:
    """配置文件里以 `key` 开头的那一行(真读文件,不读 QSettings 的缓存)。"""
    text = (tmp_path / main.SETTINGS_NAME).read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith(key))


def row_band(menu, action) -> tuple[int, int]:
    """菜单项的 y 带(避开边框一两像素)。"""
    rect = menu.actionGeometry(action)
    return rect.top() + 2, rect.bottom() - 2


def strip_mean(image, top: int, bottom: int, x0: int = 8, x1: int = 25) -> tuple[int, ...]:
    """行左侧空白带的平均底色(不含文字与控件)—— 用来看"有没有被画上当前项高亮"。"""
    total = [0, 0, 0]
    count = 0
    for x in range(x0, x1):
        for y in range(top, bottom):
            color = QColor(image.pixel(x, y))
            total[0] += color.red()
            total[1] += color.green()
            total[2] += color.blue()
            count += 1
    return tuple(round(value / count) for value in total)


def wheel_event() -> QWheelEvent:
    """一次向下滚轮(Qt 6 的构造签名很长,这里只填必需字段)。"""
    return QWheelEvent(
        QPointF(10, 10),
        QPointF(100, 100),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_widget_row_follows_the_menu_highlight(window, tray) -> None:
    """控件行也要跟菜单的"当前项":进入时高亮 + 当前项指过去,离开时都清掉。

    背景(实测):`QMenu` **不**为 `QWidgetAction` 行画高亮(纯文字项会),而控件行收不到菜单的
    move —— 当前项于是停在上一个纯文字项上,点行的空白处就会误触那一项(用户就是这么误开
    "鼠标穿透"的)。修法见 `main.MenuRow` 与 `TrayIcon.eventFilter`。

    底色是用菜单项那套 style 控件画的,所以外观与纯文字项一致(实测真平台两者都是 `(43,43,43)`);
    离屏平台 Qt 自己不画菜单项高亮,没法在这里对比颜色,因此本用例只断言"进入时变、离开时回"。
    """
    menu = tray.contextMenu()
    holder = tray._opacity_slider.parentWidget()
    assert holder.testAttribute(Qt.WA_NoMousePropagation)  # 点空白处不冒泡到菜单

    menu.popup(QPoint(400, 300))
    try:

        def background(action=None) -> tuple[int, ...]:
            # `update()` 是异步的:不先把事件跑完就抓图,会抓到改动之前的画面
            QApplication.processEvents()
            target = action or tray._row_actions[holder]
            return strip_mean(menu.grab().toImage(), *row_band(menu, target))

        plain = background()
        QApplication.sendEvent(holder, QEvent(QEvent.Type.Enter))
        assert menu.activeAction() is tray._row_actions[holder]  # 当前项被指到本行
        hovered = background()
        assert hovered != plain  # 真的画上了底色

        QApplication.sendEvent(holder, QEvent(QEvent.Type.Leave))
        assert menu.activeAction() is None  # 陈旧高亮不会留下
        assert background() == plain  # 底色也清干净

        # 颜色与纯文字项一致这件事由**构造**保证(同一套 style 控件画底色),这里不跨环境比对;
        # 真平台的实测值(两边都是 43)记在 docstring 与 `开发经验.md` 第 34 条。
        QApplication.sendEvent(holder, QEvent(QEvent.Type.Enter))
        assert background() == hovered  # 再次进入:底色可复现
    finally:
        menu.close()


def test_menu_reopens_without_row_focus(window, tray) -> None:
    """收起菜单再打开,不该还停在上次改的那一格。

    曾经的现象(实测):收起再打开,那个步进器仍是焦点控件、文本仍是全选态 —— 于是按 ↓ 想往下走
    菜单,实际改的是阈值(按键先送到焦点控件,被它吃掉就不会进菜单)。修法见 `_release_row_focus`。
    """
    menu = tray.contextMenu()
    spin = tray._threshold_spins["warn"]
    first = menu.actions()[0]

    menu.popup(QPoint(400, 300))
    try:
        spin.setFocus()  # 鼠标点它的等价物
        spin.lineEdit().selectAll()
        assert menu.focusWidget() is spin

        menu.hide()
        menu.popup(QPoint(400, 300))
        assert menu.focusWidget() is None  # 焦点不再残留
        assert spin.lineEdit().selectedText() == ""  # 也不再"看起来还选中"

        # 菜单能正常走:↓ 让当前项往下挪 —— 按键没被残留焦点吃掉
        menu.setActiveAction(first)
        QApplication.sendEvent(
            menu, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        )
        assert menu.activeAction() is not first
    finally:
        menu.close()


def test_wheel_over_rows_does_not_change_values(window, tray) -> None:
    """鼠标划过控件行滚轮不该改值 —— 步进器与滑杆默认都认滚轮,菜单里那是"手一抖就改配置"。"""
    menu = tray.contextMenu()
    menu.popup(QPoint(400, 300))
    try:
        for control in (tray._threshold_spins["warn"], tray._opacity_slider):
            # 放到量程中间:挨着边界时"没变"可能只是到头了,那就白测了
            control.setValue((control.minimum() + control.maximum()) // 2)
            before = control.value()
            # 直接问过滤器本身 —— 不经过投递,免得测成"Qt 在这个平台怎么送滚轮"
            assert tray.eventFilter(control, wheel_event()) is True
            assert control.value() == before
    finally:
        menu.close()


def test_menu_items_and_order(tray) -> None:
    assert [action.text() for action in tray._menu.actions()] == [
        "鼠标穿透",  # 勾选项放最上:不容易误触
        "开机自启",
        "刷新频率",  # 步进器那一行(QWidgetAction;控件本身在屏幕上另有文字)
        "橙色阈值",  # 步进器,同上
        "红色阈值",  # 步进器,同上
        "常亮阈值",  # 步进器,同上
        "透明度",  # 滑杆那一行,同上
        "隐藏",  # 常用项靠下,贴着"退出"
        "立即刷新",
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


def test_every_item_shows_a_hover_tooltip(tray) -> None:
    """整个右键菜单每一项都有 hover 提示。

    Qt 的 `QMenu` **默认不显示**菜单项提示(`toolTipsVisible` 默认 false),文字又必须挂在
    `QAction` 上 —— 所以控件行之外的那些项以前是没有提示可显示的。
    """
    assert tray._menu.toolTipsVisible()
    assert [a.text() for a in tray._menu.actions() if a.text() and not a.toolTip()] == []

    # 控件行:提示同时挂在 action / 行容器 / 控件上,行内标签又不拦鼠标 → 整行任意位置都有提示
    for widget in (tray._refresh_spin, tray._opacity_slider):
        row = widget.parentWidget()
        assert widget.toolTip() and row.toolTip() == widget.toolTip()
        assert all(
            label.testAttribute(Qt.WA_TransparentForMouseEvents)
            for label in row.findChildren(QLabel)
        )
    action = next(a for a in tray._menu.actions() if a.text() == "刷新频率")
    assert action.toolTip() == tray._refresh_spin.toolTip()


def test_threshold_spins_default_and_coupled(tray, window) -> None:
    """三条阈值:默认 85% / 90% / 90%,按百分比、步进 1%;橙色与红色的量程互相顶住。"""
    spins = tray._threshold_spins
    assert {key: spin.value() for key, spin in spins.items()} == {
        "warn": 85,
        "danger": 90,
        "lit": 90,
    }
    assert all(spin.suffix() == "" for spin in spins.values())  # 单位写在行标签上
    assert all(spin.singleStep() == 1 for spin in spins.values())
    assert [label.text() for label in spins["warn"].parentWidget().findChildren(QLabel)] == [
        "橙色阈值（%）"
    ]

    # 橙色最多到"红色 - 1%",红色至少从"橙色 + 1%"起;常亮阈值自由(1% ~ 100%)
    assert spins["warn"].maximum() == 89
    assert spins["danger"].minimum() == 86
    assert (spins["lit"].minimum(), spins["lit"].maximum()) == (1, 100)

    spins["danger"].setValue(95)  # 抬高红线,橙色上限跟着放宽
    assert spins["warn"].maximum() == 94
    spins["warn"].setValue(99)  # 想越过红线也不行,被上限顶住
    assert spins["warn"].value() == 94
    assert window.thresholds["warn"] == pytest.approx(0.94)

    spins["lit"].setValue(50)  # 常亮阈值不受红色约束:压到橙线以下也允许
    assert window.thresholds["lit"] == pytest.approx(0.50)


def test_threshold_change_applies_then_persists(window, tray, tmp_path, events) -> None:
    """改阈值:盘条立刻换线、常亮立刻重算,提交后写 ini 并只记一条日志。"""
    tray._sync_state()
    bar = window._bars[0]
    assert bar._warn == pytest.approx(main.WARN_THRESHOLD)

    tray._threshold_spins["warn"].setValue(70)
    assert window.thresholds["warn"] == pytest.approx(0.70)
    assert bar._warn == pytest.approx(0.70)  # 立刻生效:下一次绘制就用新线
    assert saved_line(tmp_path, "warn_threshold") == "warn_threshold=0.85"  # 还没落盘

    tray._threshold_spins["lit"].setValue(50)
    assert window.thresholds["lit"] == pytest.approx(0.50)

    tray._commit_thresholds()
    assert saved_line(tmp_path, "warn_threshold") == "warn_threshold=0.7"
    assert saved_line(tmp_path, "lit_threshold") == "lit_threshold=0.5"
    assert saved_line(tmp_path, "danger_threshold") == "danger_threshold=0.9"

    tray._commit_thresholds()  # 菜单关闭时还会来一次
    assert [e for e in events() if "event=thresholds" in e] == [
        "event=thresholds warn=0.70 lit=0.50 danger=0.90"
    ]
