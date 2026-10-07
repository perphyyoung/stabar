"""DiskBar:数据换算、警告色滞回、透明度的实际落点。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage

import main


def make_bar(monkeypatch, used: int = 500, total: int = 1000) -> main.DiskBar:
    monkeypatch.setattr(main, "read_disk", lambda path: (used, total))
    bar = main.DiskBar("C:/")
    # 关键:不加这个属性,render() 会先铺一层不透明底色,量出来的 alpha 恒为 255
    bar.setAttribute(Qt.WA_TranslucentBackground)
    bar.resize(240, main.ROW_H)
    return bar


def painted_alpha(bar: main.DiskBar, x: int, y: int) -> int:
    """把 bar 画进一张自己掌控 alpha 的透明图,取指定像素的 alpha。"""
    image = QImage(240, main.ROW_H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    bar.render(image)
    return image.pixelColor(x, y).alpha()


def test_ratio_none_when_drive_missing(monkeypatch, qapp) -> None:
    monkeypatch.setattr(main, "read_disk", lambda path: None)
    assert main.DiskBar("Z:/").ratio() is None


def test_ratio_and_detail_text(monkeypatch, qapp) -> None:
    bar = make_bar(monkeypatch, used=250, total=1000)
    assert bar.ratio() == pytest.approx(0.25)
    assert "已用" in bar.detail_text()
    assert "25.0%" in bar.detail_text()


def test_bar_color_hysteresis(monkeypatch, qapp) -> None:
    """卡在警告线上时颜色不该来回闪:跌回阈值下方一点点仍保持警示色。"""
    bar = make_bar(monkeypatch)
    warn = QColor(*main.COLOR_WARN).name()
    accent = QColor(*main.COLOR_ACCENT).name()
    danger = QColor(*main.COLOR_DANGER).name()

    assert bar._bar_color(main.WARN_THRESHOLD).name() == warn
    slightly_below = main.WARN_THRESHOLD - main.COLOR_HYSTERESIS / 2
    assert bar._bar_color(slightly_below).name() == warn
    clearly_below = main.WARN_THRESHOLD - main.COLOR_HYSTERESIS * 2
    assert bar._bar_color(clearly_below).name() == accent
    assert bar._bar_color(main.DANGER_THRESHOLD).name() == danger


def test_alpha_controls_painted_opacity(monkeypatch, qapp) -> None:
    """下发多少 alpha 就画多少 —— 前景淡化靠"直接画 alpha",不是让整窗透明。

    取盘符文字所在的那一像素:它被完整覆盖,所以 alpha 正好等于下发的值。
    """
    bar = make_bar(monkeypatch)
    text_pixel = (5, main.ROW_H // 2)

    bar.set_alpha(255)
    assert painted_alpha(bar, *text_pixel) == 255
    bar.set_alpha(64)
    assert painted_alpha(bar, *text_pixel) == 64


def test_percent_label_has_one_decimal(monkeypatch, qapp) -> None:
    """主界面上的占用率带 1 位小数(80.2% 这种),不四舍五入成整数。"""
    assert make_bar(monkeypatch, used=250, total=1000).percent_label() == "25.0%"
    assert make_bar(monkeypatch, used=802, total=1000).percent_label() == "80.2%"
    assert make_bar(monkeypatch, used=1000, total=1000).percent_label() == "100.0%"

    monkeypatch.setattr(main, "read_disk", lambda path: None)
    assert main.DiskBar("Z:/").percent_label() == "0.0%"  # 读不到数据也不能崩
