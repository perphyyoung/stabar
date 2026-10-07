"""StatusBarWindow:提亮状态机、鼠标穿透、隐藏、命中层、配置回写与位置夹取。

组件层验证:构造窗口 + 直接调用 / 派发事件就能验证的都放这里;
必须跨对象走事件与信号链路的放 `test_e2e_ui.py`(见 AGENTS.md 的测试分层)。
"""

import pytest
from e2e_support import assert_in_order  # 只借日志解析那半部分(见该模块说明)
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

import main


def test_low_usage_keeps_configured_opacity(window) -> None:
    """低占用时严格按配置的不透明度显示,提亮完全不介入。"""
    assert window._level == 0.0
    assert window._bars[0]._alpha == pytest.approx(main.FIRST_RUN_IDLE_OPACITY * 255)


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
