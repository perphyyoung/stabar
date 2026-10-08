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


def test_is_lit_only_at_the_lit_or_danger_line() -> None:
    """非悬停时亮度只有 0/1:到常亮阈值或红色阈值才常亮,橙色阈值不参与。"""
    assert not main.is_lit(0.84, lit=0.90, danger=0.90)
    assert not main.is_lit(0.86, lit=0.90, danger=0.90)  # 过了橙线但没到常亮线
    assert main.is_lit(0.90, lit=0.90, danger=0.90)  # 默认:常亮阈值与红色相同
    assert main.is_lit(0.95, lit=0.99, danger=0.90)  # 红线永远是兜底


def test_is_lit_uses_the_lower_of_the_two_lines() -> None:
    """常亮阈值低于红色时以它为准(于是可以"还蓝着但已经跳出来")。"""
    assert main.is_lit(0.60, lit=0.50, danger=0.90)
    assert not main.is_lit(0.49, lit=0.50, danger=0.90)


def test_read_disk_returns_none_when_query_fails(monkeypatch) -> None:
    """盘符不存在时不抛异常,交给界面显示"不可用"。"""

    def boom(_path: str):
        raise OSError("no such drive")

    monkeypatch.setattr(main.shutil, "disk_usage", boom)
    assert main.read_disk("Z:/") is None
