"""TrayIcon:菜单内容与顺序、透明度滑杆、刷新步进器、勾选联动、显示切换、提示文字。"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton

import dialogs
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


def test_reset_diff_lists_only_changed_items(window) -> None:
    """差异清单只列**有差异**的项,并给出"当前 → 默认"(还原框与执行都以它为准)。"""
    assert window.resettable_diff() == []  # 全新配置本来就都是默认值

    window.set_refresh_seconds(30)
    window.set_thresholds(warn=0.80, lit=0.95, danger=0.92)

    assert window.resettable_diff() == [
        ("刷新频率", "30 秒", "15 秒"),
        ("橙色阈值", "80%", "85%"),
        ("红色阈值", "92%", "90%"),
        ("常亮阈值", "95%", "90%"),
    ]  # 未动的透明度不在其中


def test_reset_keeps_position_and_restores_others(window, tmp_path) -> None:
    """还原只动那 5 项:ini 里的**窗口位置必须原样留着**(定点要求)。"""
    window.set_idle_opacity(0.55)
    window.move(321, 123)
    window._save_position()
    window._settings.sync()  # 位置是攒着写的(靠进程退出冲刷),这里要真落盘才能查
    assert "pos_x=321" in (tmp_path / main.SETTINGS_NAME).read_text(encoding="utf-8")

    changed = window.reset_settings()

    assert changed == ["未报警透明度"]
    assert window.idle_opacity == pytest.approx(main.FIRST_RUN_IDLE_OPACITY)
    text = (tmp_path / main.SETTINGS_NAME).read_text(encoding="utf-8")
    assert "pos_x=321" in text and "pos_y=123" in text  # 位置没被碰
    assert "idle_opacity=0.3" in text  # 这一项回到了默认


def test_reset_row_asks_before_resetting(window, tray) -> None:
    """点「还原」先弹确认框:只列有差异的项、默认按钮是取消、还原标红;确认后才真还原。"""
    window.set_idle_opacity(0.50)
    window.set_thresholds(warn=0.80, lit=0.90, danger=0.90)

    menu = tray.contextMenu()
    menu.popup(QPoint(400, 300))
    listed: list[str] = []
    try:

        def confirm() -> None:
            dialog = QApplication.activeModalWidget()  # exec() 的嵌套循环里才拿得到
            assert isinstance(dialog, dialogs.ResetDialog)
            listed.extend(label.text() for label in dialog.findChildren(QLabel))
            assert dialog.cancel_button.isDefault()  # 回车不误触
            assert dialog.reset_button.styleSheet()  # 红色提醒
            assert dialog.cancel_button.width() == dialog.reset_button.width()  # 等宽才齐
            dialog.reset_button.click()

        QTimer.singleShot(120, confirm)
        tray._confirm_reset()  # 与点按钮同一条路

        assert "80% → 85%" in listed and "50% → 30%" in listed  # 有差异的都在
        assert "15 秒" not in listed  # 没差异的不列
        assert window.idle_opacity == pytest.approx(main.FIRST_RUN_IDLE_OPACITY)
        assert window.thresholds["warn"] == pytest.approx(main.WARN_THRESHOLD)
    finally:
        menu.close()


def test_reset_row_reports_when_nothing_to_reset(window, tray) -> None:
    """没有可还原项时,点了要给一句说明(而不是毫无反应),且什么都不改。"""
    assert window.resettable_diff() == []  # 前提:本来就都是默认值

    menu = tray.contextMenu()
    menu.popup(QPoint(400, 300))
    seen: dict[str, object] = {}
    try:

        def read_and_close() -> None:
            dialog = QApplication.activeModalWidget()
            assert isinstance(dialog, dialogs.ResetDialog)
            seen["文字"] = [label.text() for label in dialog.findChildren(QLabel)]
            seen["有还原按钮"] = hasattr(dialog, "reset_button")
            button = dialog.ok_button
            left = button.mapTo(dialog, QPoint(0, 0)).x()
            seen["好按钮左右留白"] = (left, dialog.width() - (left + button.width()))
            button.click()  # 只有一颗「好」

        QTimer.singleShot(120, read_and_close)
        tray._confirm_reset()

        assert any("已经是默认值" in str(text) for text in seen["文字"])
        assert seen["有还原按钮"] is False
        left, right = seen["好按钮左右留白"]
        assert left == right  # 没有可还原项时也只留一颗「好」,且居中
        assert window.idle_opacity == pytest.approx(main.FIRST_RUN_IDLE_OPACITY)  # 什么都没动
    finally:
        menu.close()


def mouse_event(kind, local: QPoint, global_pos: QPoint) -> QMouseEvent:
    """造一个左键鼠标事件(拖动用例用):局部坐标与屏幕坐标都给全。"""
    return QMouseEvent(
        kind,
        QPointF(local),
        QPointF(global_pos),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_reset_dialog_draws_its_own_title_bar() -> None:
    """自绘标题栏:无边框、标题左右居中、没有多余的 ✕,按住标题栏还能拖动。

    原生标题栏整行都是 Windows 按主题画的,Qt 没有设置对齐的接口 —— 想让标题居中只能自己画
    (开发经验第 38 条)。去掉原生框架的代价就是拖动也得自己接,所以这条一并钉住。
    """
    dialog = dialogs.ResetDialog([("橙色阈值", "80%", "85%")], QColor(*main.COLOR_DANGER))
    dialog.show()
    try:
        assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint  # 原生标题栏已关掉

        bar, label = dialog._title_bar, dialog.title_label
        left = label.mapTo(bar, QPoint(0, 0)).x()
        right = bar.width() - (left + label.width())
        assert left == right  # 标题在标题栏里左右居中(数值断言,不靠眼睛)

        assert [b.text() for b in dialog.findChildren(QPushButton)] == ["取消", "还原"]  # 没有 ✕

        before = dialog.pos()
        start = QPoint(bar.width() // 2, bar.height() // 2)
        drag = QPoint(24, 18)
        for kind, offset in (
            (QEvent.Type.MouseButtonPress, QPoint(0, 0)),
            (QEvent.Type.MouseMove, drag),
            (QEvent.Type.MouseButtonRelease, drag),
        ):
            QApplication.sendEvent(bar, mouse_event(kind, start + offset, before + start + offset))
        assert dialog.pos() == before + drag  # 拖多远就走多远(标题标签是点击穿透的,事件直达标题栏)
    finally:
        dialog.close()


def test_menu_items_and_order(tray) -> None:
    assert [action.text() for action in tray._menu.actions()] == [
        "鼠标穿透",  # 勾选项放最上:不容易误触
        "开机自启",
        "还原配置",  # 不常用:紧跟着勾选项,不占贴近"退出"的常用区(按钮那一行)
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


def test_switches_are_checkbox_rows_not_checkable_items(tray) -> None:
    """两个开关是"行内右侧复选框",不是可勾选菜单项。

    可勾选菜单项会让 Qt 给**整张**菜单保留一条 ~20px 的勾选列:实测菜单文字起点 10~11 → 27px、
    菜单宽度 207 → 228px。所以这条钉住"菜单里一个可勾选项都没有",免得哪天被改回去。
    """
    assert isinstance(tray._click_through, QCheckBox)
    assert isinstance(tray._autostart, QCheckBox)
    assert [action.text() for action in tray._menu.actions() if action.isCheckable()] == []

    for box, name in ((tray._click_through, "鼠标穿透"), (tray._autostart, "开机自启")):
        assert tray._row_controls[box].text() == name  # 是控件行,不是菜单项
        margins = box.parentWidget().layout().contentsMargins()
        assert margins.left() == margins.right() == main.MENU_ITEM_INDENT  # 左右留白相等


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
