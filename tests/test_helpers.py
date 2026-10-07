"""纯函数:不需要 Qt 运行环境(不创建 `QApplication`、不跑事件循环),最便宜也最稳的一层。

`with_alpha` 会返回 `QColor` —— 那是 Qt 的值类型,不需要事件循环,仍属于这一层
(判据见 AGENTS.md 的「测试分层」)。
"""

import pytest

import main


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (1024**2, "1 MB"),
        (3 * 1024**3, "3.0 GB"),
        (5 * 1024**4, "5.0 TB"),
    ],
)
def test_human_size(value: float, expected: str) -> None:
    """B/KB/MB 取整,GB/TB 保留一位小数。"""
    assert main.human_size(value) == expected


def test_lerp_interpolates() -> None:
    assert main.lerp(0, 10, 0.25) == 2.5
    assert main.lerp(2, 2, 0.9) == 2


@pytest.mark.parametrize(
    ("alpha", "expected"),
    [(-1, 0), (0, 0), (127.6, 128), (255, 255), (300, 255)],
)
def test_with_alpha_clamps_and_rounds(alpha: float, expected: int) -> None:
    """alpha 被夹到 0~255,所以传 -1 或 300 都不会画出奇怪的像素。"""
    assert main.with_alpha((10, 20, 30), alpha).alpha() == expected


def test_reveal_level_below_start_never_brightens() -> None:
    """低于提亮起点就完全不提亮 —— 这是"平时尽量透明"的保证。"""
    assert main.reveal_level(main.REVEAL_START) == 0.0
    assert main.reveal_level(0.0) == 0.0


def test_reveal_level_at_or_above_warn_is_full() -> None:
    assert main.reveal_level(main.WARN_THRESHOLD) == 1.0
    assert main.reveal_level(1.0) == 1.0


def test_reveal_level_is_linear_in_between() -> None:
    """起点到警告线之间是线性过渡,不是一步跳到全亮。"""
    midpoint = (main.REVEAL_START + main.WARN_THRESHOLD) / 2
    assert main.reveal_level(midpoint) == pytest.approx(0.5)


def test_read_disk_returns_none_when_query_fails(monkeypatch) -> None:
    """盘符不存在时不抛异常,交给界面显示"不可用"。"""

    def boom(_path: str):
        raise OSError("no such drive")

    monkeypatch.setattr(main.shutil, "disk_usage", boom)
    assert main.read_disk("Z:/") is None
