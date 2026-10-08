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


def test_hit_layer_covers_whole_rect(window) -> None:
    """命中层必须是 alpha=1:改成 0 鼠标就会穿透,拖动和悬停全废。"""
    image = window.grab().toImage()
    assert image.pixelColor(120, 31).alpha() == main.HIT_AREA_ALPHA
    assert image.pixelColor(3, 31).alpha() == main.HIT_AREA_ALPHA


def test_config_is_written_back_on_first_run(window) -> None:
    """启动回写让配置文件自解释:没有这个键时补上生效值。"""
    path = main.settings_path()
    assert path.exists()
    assert f"idle_opacity={main.FIRST_RUN_IDLE_OPACITY}" in path.read_text(encoding="utf-8")


def test_out_of_screen_position_is_clamped(
    qtbot, tmp_path, monkeypatch, low_usage, fake_registry
) -> None:
    """换过显示器后越界的坐标要被夹回屏幕内,否则窗口会跑到看不见的地方。"""
    ini = tmp_path / "stabar.ini"
    ini.write_text("[General]\npos_x=999999\npos_y=999999\n", encoding="utf-8")
    monkeypatch.setattr(main, "settings_path", lambda: ini)

    window = main.StatusBarWindow()
    qtbot.addWidget(window)

    area = main.QApplication.primaryScreen().availableGeometry()
    assert window.x() == area.right() - window.width()
    assert window.y() == area.bottom() - window.height()
