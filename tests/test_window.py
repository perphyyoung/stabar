"""StatusBarWindow:提亮状态机、鼠标穿透、隐藏、命中层、配置回写与位置夹取。

组件层验证:构造窗口 + 直接调用 / 派发事件就能验证的都放这里;
必须跨对象走事件与信号链路的放 `test_e2e_ui.py`(见 AGENTS.md 的测试分层)。
"""

import pytest
from e2e_support import (
    assert_in_order,  # 只借日志解析那半部分(见该模块说明)
)
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import main


def test_low_usage_keeps_configured_opacity(window) -> None:
    """低占用时严格按配置的不透明度显示,提亮完全不介入。"""
    assert window._level == 0.0
    assert window._bars[0]._alpha == pytest.approx(main.FIRST_RUN_IDLE_OPACITY * 255)


def test_hover_details_cover_the_whole_window(window) -> None:
    """整块区域悬停都能出详情:窗口开 `WA_AlwaysShowToolTips`,盘条不接收鼠标事件。

    真人实测的"详情时有时无"来自两处:tooltip 默认只对活动窗口显示(本窗口置顶、从不被激活),
    以及盘条铺满窗口挡住了挂在窗口上的详情。
    """
    assert window.testAttribute(Qt.WA_AlwaysShowToolTips)
    assert window.toolTip()  # 详情文字挂在窗口上
    assert window._bars
    assert all(bar.testAttribute(Qt.WA_TransparentForMouseEvents) for bar in window._bars)


def test_hover_text_has_one_source_and_two_lengths(window) -> None:
    """悬停提示只有一处实现:窗口用完整明细,托盘用紧凑版(平台只给 128 个字符)。"""
    assert window.toolTip() == window.summary()  # 窗口上挂的就是完整版
    assert window.summary() == main.summary_text(window._bars)  # 出处只有 summary_text
    assert window.summary(compact=True) == main.summary_text(window._bars, compact=True)

    full, compact = window.summary(), window.summary(compact=True)
    assert "\n" in full and "\n" not in compact  # 完整版多行、紧凑版一行
    assert all(bar.short_text() in compact for bar in window._bars)
    assert len(compact) <= main.TRAY_TIP_MAX


def test_brightness_only_from_hover_or_thresholds(window, monkeypatch) -> None:
    """非悬停时亮度只有 0/1:橙色阈值只改颜色;到常亮阈值或红色阈值才常亮。"""
    bar = window._bars[0]

    monkeypatch.setattr(main, "read_disk", lambda path: (860, 1000))  # 86%:过橙线、未到常亮
    window.refresh()
    assert window._target == 0.0  # 橙线不提亮
    assert bar._bar_color(0.86).name() == QColor(*main.COLOR_WARN).name()  # 但它变橙了

    monkeypatch.setattr(main, "read_disk", lambda path: (910, 1000))  # 91%:过红线
    window.refresh()
    assert window._target == 1.0

    # 常亮阈值压到 50%:60% 就常亮,而颜色还是蓝的(橙线仍在 85%)
    window.set_thresholds(warn=0.85, lit=0.50, danger=0.90)
    monkeypatch.setattr(main, "read_disk", lambda path: (600, 1000))
    window.refresh()
    assert window._target == 1.0
    assert bar._bar_color(0.60).name() == QColor(*main.COLOR_ACCENT).name()


def test_threshold_config_clamped_and_order_enforced(window, events) -> None:
    """阈值读入:越界夹取、反序拉回调(橙色至少比红色低 1%),都留 WARNING。"""
    window._settings.setValue("danger_threshold", 1.5)
    assert window._read_threshold("danger_threshold", main.DANGER_THRESHOLD) == pytest.approx(
        main.THRESHOLD_MAX
    )
    window._settings.setValue("warn_threshold", "abc")
    assert window._read_threshold("warn_threshold", main.WARN_THRESHOLD) == pytest.approx(
        main.WARN_THRESHOLD
    )

    window._warn_threshold, window._danger_threshold = 0.99, 0.50  # 反序
    window._enforce_threshold_order()
    assert window.thresholds == {
        "warn": pytest.approx(0.49),
        "danger": pytest.approx(0.50),
        "lit": pytest.approx(main.LIT_THRESHOLD),  # 常亮阈值不受红色约束
    }

    assert any("event=config_clamped key=danger_threshold" in e for e in events())
    assert any("event=config_clamped key=warn_threshold" in e for e in events())
    assert any("event=config_invalid key=warn_threshold" in e for e in events())


def test_refresh_seconds_config_is_clamped(window, events) -> None:
    """配置里的刷新秒数生效;超出量程就夹取到步进器的范围并留痕(与 idle_opacity 同一套规则)。"""
    window._settings.setValue("refresh_seconds", 600)
    window._settings.sync()
    assert window._read_refresh_seconds() == main.REFRESH_MAX_S

    window._settings.setValue("refresh_seconds", "abc")
    assert window._read_refresh_seconds() == main.REFRESH_DEFAULT_S

    assert any("event=config_clamped key=refresh_seconds" in e for e in events())
    assert any("event=config_invalid key=refresh_seconds" in e for e in events())


def test_hover_is_instant_and_reverts_instantly(window, events) -> None:
    """悬停是硬切:同一 tick 就到位,移开立即回落,不经过动画。

    用真的 Enter / Leave 事件派发(不是直接调处理方法),顺带断言事件顺序 ——
    这仍是"组件级"验证,所以留在本层;跨对象链路见 `test_e2e_ui.py`。
    """
    QApplication.sendEvent(window, QEvent(QEvent.Type.Enter))
    assert window._bars[0]._alpha == pytest.approx(255)
    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
    assert window._bars[0]._alpha == pytest.approx(main.FIRST_RUN_IDLE_OPACITY * 255)

    assert_in_order(
        events(),
        "event=hover state=enter",
        "event=reveal target=1.00 from=0.00 reason=hover",
        "event=hover state=leave",
        "event=reveal target=0.00 from=1.00 reason=usage",
    )


def test_high_usage_brightens_to_full(window, qtbot, monkeypatch) -> None:
    """越过警告线后自动提亮到 100%;这条走动画,所以等它真正跑完。"""
    monkeypatch.setattr(main, "read_disk", lambda path: (950, 1000))
    window.refresh()
    qtbot.waitUntil(lambda: window._bars[0]._alpha == pytest.approx(255), timeout=3000)


def test_click_through_toggles_flag_and_logs(window, events) -> None:
    """穿透开关:标志和日志都要有 —— 开启后只能回托盘关,所以日志必须留痕。"""
    assert not bool(window.windowFlags() & main.Qt.WindowTransparentForInput)

    window.set_click_through(True)
    assert bool(window.windowFlags() & main.Qt.WindowTransparentForInput)

    window.set_click_through(False)
    assert not bool(window.windowFlags() & main.Qt.WindowTransparentForInput)

    assert_in_order(
        events(), "event=click_through enabled=True", "event=click_through enabled=False"
    )


def test_ui_scale_resizes_window_bars_and_persists(window, tmp_path) -> None:
    """缩放系数:窗口与每个盘条一起变;落盘写进 ini(下次启动由它还原)。"""
    base = window.size()
    window.set_ui_scale(1.5)

    assert window.size().width() == round(base.width() * 1.5)
    assert abs(window.size().height() - round(base.height() * 1.5)) <= 2  # 各段分别取整
    assert window._bars[0].height() == round(main.ROW_H * 1.5)
    assert "ui_scale=1.5" in (tmp_path / main.SETTINGS_NAME).read_text(encoding="utf-8")
    # 与窗口位置同理:它算几何不算配置,所以「还原配置」不管它
    assert "ui_scale" not in {key for key, *_ in main.RESETTABLE_SETTINGS}


def test_ui_scale_is_clamped(window) -> None:
    """拖到极限就停住:下限防"小到看不见",上限防"占满屏"。"""
    window.set_ui_scale(9.0)
    assert window.ui_scale == pytest.approx(main.UI_SCALE_MAX)
    window.set_ui_scale(0.01)
    assert window.ui_scale == pytest.approx(main.UI_SCALE_MIN)


def test_hide_widget_clears_hover_state(window) -> None:
    """隐藏时顺手清掉悬停态,否则下次从托盘显示出来会停在全亮。"""
    window.show()
    window._hovered = True
    window.hide_widget()
    assert not window.isVisible()
    assert not window._hovered


def test_drive_set_follows_the_detector(window, monkeypatch) -> None:
    """盘集变了就重建盘条,窗口高度跟着走 —— 插上移动硬盘,它就出现在列表里。"""
    assert [bar.path for bar in window._bars] == ["C:/", "D:/"]

    monkeypatch.setattr(main, "detect_drives", lambda: ["C:/", "D:/", "F:/"])
    window.refresh()

    assert [bar.path for bar in window._bars] == ["C:/", "D:/", "F:/"]
    assert window.full_size() == main.layout_size(window.ui_scale, 3)


def test_drives_config_limits_what_is_shown(
    qtbot, tmp_path, monkeypatch, low_usage, fake_registry
) -> None:
    """ini 写 `drives=C,D` 就只显示这两个、顺序照写;`auto` 回自动。

    带逗号的值 `QSettings` 会读成**列表**,只认字符串的实现会静默退化成自动(F: 就冒出来了)。
    """
    ini = tmp_path / "stabar.ini"
    ini.write_text("[General]\ndrives=C,D\n", encoding="utf-8")
    monkeypatch.setattr(main, "settings_path", lambda: ini)
    monkeypatch.setattr(main, "detect_drives", lambda: ["C:/", "D:/", "F:/"])

    window = main.StatusBarWindow()
    qtbot.addWidget(window)

    assert window.drives == ["C:/", "D:/"]
    assert [bar.path for bar in window._bars] == ["C:/", "D:/"]  # 没写 F:,不显示

    window.set_drives(["D:/", "C:/"])  # 顺序按写的来
    assert [bar.path for bar in window._bars] == ["D:/", "C:/"]

    # 落盘的形式由 QSettings 决定(带逗号的值它会加引号),要钉的是**读回来还是这两个、顺序不变**
    reopened = main.StatusBarWindow()
    qtbot.addWidget(reopened)
    assert reopened.drives == ["D:/", "C:/"]

    window.set_drives(None)  # 回自动
    assert [bar.path for bar in window._bars] == ["C:/", "D:/", "F:/"]
    assert "drives=auto" in ini.read_text(encoding="utf-8")


def test_drive_change_while_collapsed_re_arms_the_collapse(window, qtbot, monkeypatch) -> None:
    """正收着的时候盘集变了(插上移动硬盘):重建之后要重新判断收起,不能一直摊开着。"""
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left(), area.top() + 200)
    window.set_edge_hide(True)
    window._hide_to_edge()
    qtbot.waitUntil(lambda: window.size() == window.tail_size(), timeout=1000)

    monkeypatch.setattr(main, "detect_drives", lambda: ["C:/", "D:/", "F:/"])
    window.refresh()

    assert len(window._bars) == 3  # 盘条已经是新的
    qtbot.waitUntil(lambda: window.edge_hidden, timeout=main.EDGE_HIDE_DELAY_MS + 2000)


def test_hit_layer_covers_whole_rect(window) -> None:
    """命中层必须是 alpha=1:改成 0 鼠标就会穿透,拖动和悬停全废。"""
    image = window.grab().toImage()
    assert image.pixelColor(120, 31).alpha() == main.HIT_AREA_ALPHA
    assert image.pixelColor(3, 31).alpha() == main.HIT_AREA_ALPHA


def test_edge_hide_snaps_collapses_to_a_tail_and_grows_back(window, qtbot) -> None:
    """贴边隐藏:吸附齐平 → 指针离开折叠成小尾巴 → 蹭到尾巴再长回来。

    收起与展开都是**窗口矩形**的事(位置 + 尺寸),所以断言的落点是窗口矩形这个外部事实,不是日志。
    """
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left() + main.EDGE_SNAP_PX - 1, area.top() + 120)  # 离左边一格以内
    window.set_edge_hide(True)

    assert window.edge == "left"
    assert window.x() == area.left()  # 吸附成与屏幕边齐平
    assert window.edge_hidden is False
    assert window.size() == window.full_size()  # 此刻是展开形态

    # 指针离开 → 延迟之后折叠成尾巴(必须真派发 Leave:收起是由它启动的定时器触发的)
    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
    qtbot.waitUntil(lambda: window.edge_hidden, timeout=main.EDGE_HIDE_DELAY_MS + 1000)
    tail = window.tail_size()
    qtbot.waitUntil(lambda: window.size() == tail, timeout=1000)
    assert window.x() == area.left()  # 尾巴整块留在屏幕内(命中规则见 README 必读第 2 条)
    assert area.top() <= window.y() and window.y() + tail.height() <= area.bottom() + 1

    QApplication.sendEvent(window, QEvent(QEvent.Type.Enter))  # 蹭到尾巴
    qtbot.waitUntil(lambda: window.size() == window.full_size(), timeout=1000)
    assert window.x() == area.left()  # 长回锚点
    assert window.edge_hidden is False

    window.set_edge_hide(False)
    assert window.edge is None  # 关掉开关就退出贴边


def test_edge_hide_grabs_a_half_off_screen_drag(window, qtbot) -> None:
    """拖出去一半(压出屏幕)也算贴上这条边:松手拉回边上摆正,并照常收起成尾巴。"""
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left() - window.width() // 2, area.top() + 100)  # 一半在屏幕外
    window.set_edge_hide(True)

    assert window.edge == "left"
    assert window.x() == area.left()  # 拉回屏幕内、与边齐平
    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
    qtbot.waitUntil(lambda: window.edge_hidden, timeout=main.EDGE_HIDE_DELAY_MS + 1000)
    qtbot.waitUntil(lambda: window.size() == window.tail_size(), timeout=1000)
    assert window.x() == area.left()  # 尾巴在屏幕内:压出去的窗口不会被拖丢


def test_tail_is_taller_sideways_and_flatter_top_bottom(window, qtbot) -> None:
    """线段朝向的可见后果:贴左/右时尾巴是**竖**的(高 > 宽),贴上/下时是横的(宽 > 高)。

    方向本身由 `main.tail_layout` 决定(第 1 层钉着),这里钉的是窗口真的按那条边取对了尺寸。
    """
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()

    window.move(area.left(), area.top() + 200)  # 贴左
    window.set_edge_hide(True)
    assert window.edge == "left"
    sideways = window.tail_size()
    assert sideways.height() > sideways.width()  # 竖线:尾巴竖着

    window.set_edge_hide(False)
    window.move(area.left() + 200, area.top())  # 贴上
    window.set_edge_hide(True)
    assert window.edge == "top"
    flat = window.tail_size()
    assert flat.width() > flat.height()  # 横线:尾巴横着
    assert sideways.width() == flat.height()  # 就是把同一条线转 90°


def test_tail_size_is_fixed_and_ignores_the_ui_scale(window, qtbot) -> None:
    """贴边后是**固定大小**:「调整尺寸」管的是展开形态,不该让贴边的标记跟着变粗变长。"""
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left(), area.top() + 200)
    window.set_edge_hide(True)

    before = window.tail_size()
    window.set_ui_scale(2.0)
    assert window.tail_size() == before
    window.set_ui_scale(main.UI_SCALE_MIN)
    assert window.tail_size() == before
    # 展开形态仍然跟着缩放走(它才是"调整尺寸"的作用对象)
    assert window.full_size().width() == round(main.WINDOW_W * main.UI_SCALE_MIN)


def test_committing_ui_scale_re_arms_the_collapse(window, qtbot) -> None:
    """托盘「调整尺寸」:拖动期间窗口**暂时展开**,提交(松手 / 关菜单)之后要重新判断该不该收起。

    没有这一步,收着的窗口被调过一次大小就会一直展开着:菜单弹出期间它收不到鼠标事件,
    没人会再判一次。
    """
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left(), area.top() + 200)
    window.set_edge_hide(True)
    window._hide_to_edge()
    qtbot.waitUntil(lambda: window.size() == window.tail_size(), timeout=1000)

    window.preview_ui_scale(1.5)  # 拖动中:暂时展开(看得见才好对齐)
    assert window.edge_hidden is False
    assert window.size() == window.full_size()
    assert not window._edge_timer.isActive()  # 拖的时候不该收起

    window.set_ui_scale(1.5)  # 松手提交 = 调整结束
    assert window._edge_timer.isActive()  # 光标不在窗口上 → 重新排上收起
    qtbot.waitUntil(
        lambda: window.size() == window.tail_size(), timeout=main.EDGE_HIDE_DELAY_MS + 2000
    )
    assert window.edge_hidden is True


def test_tail_keeps_a_hittable_layer(window, qtbot) -> None:
    """尾巴必须整块有 alpha ≥ 1:否则鼠标穿透到桌面,收起来之后再也蹭不回来(README 必读第 2 条)。"""
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left(), area.top() + 100)
    window.set_edge_hide(True)
    window._hide_to_edge()  # 直接收起:绕过 250ms 的延迟
    qtbot.waitUntil(lambda: window.size() == window.tail_size(), timeout=1000)

    image = window.grab().toImage()  # 尾巴里没有底板:整块可见像素就靠这层隐形矩形撑着
    assert image.pixelColor(image.width() // 2, image.height() // 2).alpha() >= main.HIT_AREA_ALPHA


def test_leaving_while_morphing_still_collapses(window, qtbot) -> None:
    """展开动画还没跑完就把鼠标移开:形变结束时必须按光标**实际位置**补判一次。

    形变期间的 Leave 是被跳过的(窗口自己在动,Qt 会补发假的 Leave);跳掉的那个若是真 Leave,
    之后就再没有事件可来,窗口会一直展开着。
    """
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left(), area.bottom() - window.height() - 50)  # 右下区域:离离屏光标很远
    window.set_edge_hide(True)
    window._hide_to_edge()
    qtbot.waitUntil(lambda: window.size() == window.tail_size(), timeout=1000)

    QApplication.sendEvent(window, QEvent(QEvent.Type.Enter))  # 蹭到尾巴 → 开始展开
    QApplication.sendEvent(window, QEvent(QEvent.Type.Leave))  # 动画还没停就把鼠标移开
    qtbot.waitUntil(lambda: window.edge_hidden, timeout=main.EDGE_HIDE_DELAY_MS + 2000)


def test_pressing_the_tail_expands_it_for_dragging(window) -> None:
    """按尾巴 = 要挪动它:立刻长回展开形态(不补间),否则会拖着一条小线段跑。"""
    area = main.QApplication.primaryScreen().availableGeometry()
    window.show()
    window.move(area.left(), area.top() + 100)
    window.set_edge_hide(True)
    window._hide_to_edge()
    assert window.edge_hidden is True

    QTest.mousePress(window, Qt.LeftButton, pos=main.QPoint(2, 2))
    try:
        assert window.edge_hidden is False
        assert window.size() == window.full_size()  # 一次到位,不是补间到一半
    finally:
        QTest.mouseRelease(window, Qt.LeftButton, pos=main.QPoint(2, 2))


def test_edge_hide_is_on_by_default_and_yields_to_click_through(window, tmp_path) -> None:
    """默认开(贴到边就收起);开鼠标穿透时会被自动关掉(穿透后收不到任何鼠标,**物理上**互斥)。"""
    assert window.edge_hide is True
    assert window.edge is None  # 默认位置离屏幕边还有一段距离:不贴边

    window.set_edge_hide(False)
    assert "edge_hide=false" in (tmp_path / main.SETTINGS_NAME).read_text(encoding="utf-8")

    window.set_edge_hide(True)
    window.set_click_through(True)
    assert window.edge_hide is False  # 穿透把它关了
    assert window.edge is None


def test_config_is_written_back_on_first_run(window) -> None:
    """启动回写让配置文件自解释:没有这个键时补上生效值。"""
    path = main.settings_path()
    assert path.exists()
    assert f"idle_opacity={main.FIRST_RUN_IDLE_OPACITY}" in path.read_text(encoding="utf-8")


def test_out_of_screen_position_is_clamped(
    qtbot, tmp_path, monkeypatch, low_usage, fake_registry, fake_drives
) -> None:
    """换过显示器后越界的坐标要被夹回屏幕内,否则窗口会跑到看不见的地方。

    贴边隐藏默认开着,所以夹到右下角之后它还会顺手吸附成与边齐平(比夹取的位置更贴一格)——
    这里要钉的是"整块都在工作区内"这个不变量。
    """
    ini = tmp_path / "stabar.ini"
    ini.write_text("[General]\npos_x=999999\npos_y=999999\n", encoding="utf-8")
    monkeypatch.setattr(main, "settings_path", lambda: ini)

    window = main.StatusBarWindow()
    qtbot.addWidget(window)

    area = main.QApplication.primaryScreen().availableGeometry()
    assert area.left() <= window.x() and window.x() + window.width() <= area.right() + 1
    assert area.top() <= window.y() and window.y() + window.height() <= area.bottom() + 1
    assert window.edge in ("right", "bottom")  # 落在角落:贴着其中一条边
