"""纯函数:不需要 Qt 运行环境(不创建 `QApplication`、不跑事件循环),最便宜也最稳的一层。

`layout.with_alpha` 会返回 `QColor` —— 那是 Qt 的值类型,不需要事件循环,仍属于这一层
(判据见 AGENTS.md 的「测试分层」)。
"""

from collections import namedtuple
from itertools import pairwise

import pytest
from e2e_support import center_of_rect, screen_bounds, visible_size
from PySide6.QtCore import QPoint, QRect, QSize

import drives
import hovercard
import layout
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
    assert layout.human_size(value) == expected


def test_lerp_interpolates() -> None:
    assert layout.lerp(0, 10, 0.25) == 2.5
    assert layout.lerp(2, 2, 0.9) == 2


def test_snap_edge_only_when_close_enough() -> None:
    """贴边判定:取最近那条边,它的内缩量 ≤ layout.EDGE_SNAP_PX 才算贴上,否则就当普通摆放。"""
    area = (0, 0, 1920, 1040)  # 左下开区间(调用方会把 QRect 的右/下 +1)

    assert layout.snap_edge(0, 300, 248, 60, area) == "left"  # 正好齐平
    assert layout.snap_edge(layout.EDGE_SNAP_PX, 300, 248, 60, area) == "left"  # 差一格也算
    assert layout.snap_edge(layout.EDGE_SNAP_PX + 1, 300, 248, 60, area) is None  # 差两格就不认
    assert layout.snap_edge(1920 - 248, 300, 248, 60, area) == "right"
    assert layout.snap_edge(300, 1040 - 60, 248, 60, area) == "bottom"
    assert layout.snap_edge(600, 300, 248, 60, area) is None  # 离哪条都远


def test_snap_edge_counts_windows_pushed_out_of_screen() -> None:
    """压出屏幕也算贴上:**内缩量为负**,同一条"≤ 阈值"的规则就盖住了"拖出去一半"。

    贴边隐藏默认开着,"拖出去一半松手 → 拉回边上摆正 → 收起成尾巴"靠的就是这一条。
    """
    area = (0, 0, 1920, 1040)
    assert layout.snap_edge(-3, 300, 248, 60, area) == "left"  # 手抖拖过头 3 像素
    assert layout.snap_edge(-124, 300, 248, 60, area) == "left"  # 一半在屏幕外
    assert layout.snap_edge(1920 - 124, 300, 248, 60, area) == "right"  # 右边同理
    assert layout.snap_edge(300, -30, 248, 60, area) == "top"
    # 关键的一条:压出左边 124px,但离**上**边只有 100px。要按带符号的值取最小才算得上左边;
    # 若按绝对值取最近,最近的会是上边(100 < 124),而它够不着阈值 —— 于是谁也不认,
    # 窗口就丢在半屏外了。
    assert layout.snap_edge(-124, 100, 248, 60, area) == "left"
    # 整块都压出去了(拖得太狠):最近的那条边仍然是它压过的那条
    assert layout.snap_edge(-248, 300, 248, 60, area) == "left"


@pytest.mark.parametrize(
    ("edge", "expected"),
    [
        ("left", (0, 325)),  # 贴左:y 对齐展开矩形的中心 (300 + 30 - 5)
        ("right", (1920 - 14, 325)),
        ("top", (417, 0)),  # x 对齐中心 (300 + 124 - 7)
        ("bottom", (417, 1040 - 10)),
    ],
)
def test_tail_pos_is_flush_and_centered(edge: str, expected: tuple[int, int]) -> None:
    """尾巴落点:贴着那条边,沿边方向对齐展开时的中心 —— 展开 / 收起才不会横向跳。

    强调"整块留在工作区内"是因为它是**命中**要求:尾巴得在屏幕内、且有 alpha ≥ 1 的像素,
    否则鼠标蹭不到,收起来就再也长不回来了(见 README 必读第 2 条)。
    """
    area = (0, 0, 1920, 1040)
    assert layout.tail_pos(edge, 300, 300, 248, 60, 14, 10, area) == expected


def test_tail_pos_clamps_inside_the_work_area() -> None:
    """窗口停在角落时,尾巴不能因为"对齐中心"而露到屏幕外(露出去就点不到了)。"""
    area = (0, 0, 1920, 1040)
    assert layout.tail_pos("top", 1900, 0, 248, 60, 14, 10, area) == (1920 - 14, 0)  # 右界夹住
    assert layout.tail_pos("left", 0, -60, 248, 60, 14, 10, area) == (0, 0)  # 上界夹住


def test_tail_layout_is_one_broken_line() -> None:
    """**一条线断成几段**:所有线段在同一条直线上(同一个 y),段间留出固定间隙,一段一个盘。"""
    rows = 3
    width, height, lines = layout.tail_layout("top", rows)

    assert len(lines) == rows
    assert len({y for _x, y, _w, _h in lines}) == 1  # 同一条线:横坐标变、纵坐标不变
    assert len({h for _x, _y, _w, h in lines}) == 1  # 线宽一致
    assert {w for _x, _y, w, _h in lines} == {layout.TAIL_SEGMENT}  # 每段**长度固定**
    for (x, _y, w, _h), (next_x, *_rest) in pairwise(lines):
        assert next_x - (x + w) == layout.TAIL_DASH_GAP  # 断开处就是那条间隙
    for x, y, w, h in lines:  # 都落在尾巴范围内(尾巴同时是命中区)
        assert x + w <= width and y + h <= height


def test_tail_layout_is_parallel_to_the_edge() -> None:
    """整条线与屏幕边**平行**:贴上/下是横的,贴左/右是竖的 —— 同一份布局转 90°。"""
    rows = 3
    horizontal_w, horizontal_h, horizontal = layout.tail_layout("top", rows)
    vertical_w, vertical_h, vertical = layout.tail_layout("left", rows)

    assert (horizontal_w, horizontal_h) == (vertical_w, vertical_h)[::-1]  # 转 90°:宽高互换
    assert len({y for _x, y, _w, _h in horizontal}) == 1  # 横线:同一行
    assert len({x for x, _y, _w, _h in vertical}) == 1  # 竖线:同一列
    assert horizontal_w > horizontal_h and vertical_h > vertical_w
    for x, y, w, h in vertical:
        assert x + w <= vertical_w and y + h <= vertical_h


def test_tail_layout_extends_the_line_with_more_rows() -> None:
    """盘多时线变**长**(多一段),线宽不变:一段一个盘是硬要求(`DRIVES` 是可改的)。"""
    short_w, short_h, two = layout.tail_layout("top", 2)
    long_w, long_h, four = layout.tail_layout("top", 4)
    assert long_h == short_h  # 线宽不变:还是一根线
    assert long_w > short_w
    assert len(two) == 2 and len(four) == 4
    for x, y, w, h in four:
        assert x + w <= long_w and y + h <= long_h


def test_parse_drives_accepts_letters_and_falls_back_to_auto() -> None:
    """ini 的 `drives`:auto / 空 → 自动;字母表 → 白名单;写坏的项丢掉,全坏回自动。"""
    assert drives.parse_drives(drives.DRIVES_AUTO) == (None, [])
    assert drives.parse_drives("") == (None, [])
    assert drives.parse_drives("   ") == (None, [])
    assert drives.parse_drives("C,E") == (["C:/", "E:/"], [])
    # 冒号、反斜杠、空格、分号都认;重复的并成一项
    assert drives.parse_drives("c: ; D:\\ ; C") == (["C:/", "D:/"], [])
    # 写坏的项丢掉,好的仍然生效
    assert drives.parse_drives("C,ZZ,9") == (["C:/"], ["ZZ", "9"])
    # 全坏 → 回自动(一个笔误不该让小组件变成空的)
    assert drives.parse_drives("ZZ") == (None, ["ZZ"])
    # QSettings 会把带逗号的值读成**列表**:两种形状都要认
    assert drives.parse_drives(["C", "D"]) == (["C:/", "D:/"], [])
    assert drives.parse_drives(["C", "ZZ"]) == (["C:/"], ["ZZ"])


def test_visible_drives_keeps_the_written_order_and_caps_the_count() -> None:
    """最终显示哪些盘:自动按字母序;白名单按写下的顺序,而且只在探测得到的里面挑。"""
    detected = ["D:/", "C:/", "F:/"]
    assert drives.visible_drives(detected, None) == ["C:/", "D:/", "F:/"]
    assert drives.visible_drives(detected, ["F:/", "C:/"]) == ["F:/", "C:/"]
    assert drives.visible_drives(detected, ["C:/", "Z:/"]) == ["C:/"]  # 当场读不到的盘不显示
    many = [f"{chr(ord('C') + i)}:/" for i in range(drives.MAX_DRIVES + 3)]
    assert len(drives.visible_drives(many, None)) == drives.MAX_DRIVES
    assert len(drives.visible_drives(many, list(reversed(many)))) == drives.MAX_DRIVES


def test_drives_text_round_trips() -> None:
    """写回 ini 的形式:`auto` 或 `C,D`(与 `parse_drives` 互为逆运算)。"""
    assert drives.drives_text(None) == drives.DRIVES_AUTO
    assert drives.drives_text(["C:/", "F:/"]) == "C,F"
    assert drives.parse_drives(drives.drives_text(["C:/", "F:/"]))[0] == ["C:/", "F:/"]


def test_layout_size_is_one_formula_for_window_and_morph() -> None:
    """展开尺寸只有一处算式:窗口 `setFixedSize` 与收起 / 展开的目标都从它来。"""
    pad, gap, row_h, width = layout.scaled_layout(1.0, 2)
    assert (pad, gap, row_h, width) == (layout.PAD, layout.GAP, layout.ROW_H, layout.WINDOW_W)
    assert layout.layout_size(1.0, 2) == main.QSize(
        layout.WINDOW_W, layout.PAD * 2 + layout.ROW_H * 2 + layout.GAP
    )
    assert layout.layout_size(2.0, 2).width() == layout.WINDOW_W * 2  # 跟着缩放一起长


def test_center_of_rect_stays_on_screen() -> None:
    """矩形有一部分在屏幕外时,取点必须落在**屏幕内** —— 几何中心可能是个不存在的点。

    实测:任务栏自动隐藏时,shell 报的托盘图标矩形底部到 1126,而屏幕只有 1080;几何中心因此
    落到屏幕外,`SetCursorPos` 被系统夹回边缘、读回来差 20+ 像素,被误判成"真人鼠标在动"。
    """
    left, top, _, bottom = screen_bounds()
    off_bottom = (left + 100, bottom - 2, left + 132, bottom + 46)  # 实测形态:底部超出屏幕 46 像素

    x, y = center_of_rect(off_bottom)
    assert top <= y < bottom  # 落在屏幕内
    assert off_bottom[0] <= x < off_bottom[2]
    assert visible_size(off_bottom) == (32, 2)  # 可见的只剩贴着屏幕边缘的两像素

    # 完全在屏幕外:退回几何中心,由上层(托盘那两条用例)据此跳过
    outside = (left + 100, bottom + 10, left + 132, bottom + 40)
    assert center_of_rect(outside) == (left + 116, bottom + 25)


@pytest.mark.parametrize(
    ("alpha", "expected"),
    [(-1, 0), (0, 0), (127.6, 128), (255, 255), (300, 255)],
)
def test_with_alpha_clamps_and_rounds(alpha: float, expected: int) -> None:
    """alpha 被夹到 0~255,所以传 -1 或 300 都不会画出奇怪的像素。"""
    assert layout.with_alpha((10, 20, 30), alpha).alpha() == expected


def test_is_lit_only_at_the_lit_or_danger_line() -> None:
    """非悬停时亮度只有 0/1:到常亮阈值或红色阈值才常亮,橙色阈值不参与。"""
    assert not layout.is_lit(0.84, lit=0.90, danger=0.90)
    assert not layout.is_lit(0.86, lit=0.90, danger=0.90)  # 过了橙线但没到常亮线
    assert layout.is_lit(0.90, lit=0.90, danger=0.90)  # 默认:常亮阈值与红色相同
    assert layout.is_lit(0.95, lit=0.99, danger=0.90)  # 红线永远是兜底


def test_is_lit_uses_the_lower_of_the_two_lines() -> None:
    """常亮阈值低于红色时以它为准(于是可以"还蓝着但已经跳出来")。"""
    assert layout.is_lit(0.60, lit=0.50, danger=0.90)
    assert not layout.is_lit(0.49, lit=0.50, danger=0.90)


def test_read_disk_returns_none_when_query_fails(monkeypatch) -> None:
    """盘符不存在时不抛异常,交给界面显示"不可用"。"""

    def boom(_path: str):
        raise OSError("no such drive")

    monkeypatch.setattr(drives.shutil, "disk_usage", boom)
    assert drives.read_disk("Z:/") is None


def test_detect_drives_can_exclude_removable(monkeypatch) -> None:
    """「显示所有磁盘盘符」取消后,探测只收**本地固定盘**;可移动类型(U 盘 / 读卡器)被排掉。

    CD-ROM(类型 4)本来就收不进来,两种口径一致;试读那一步照旧起作用(见模块说明)。
    """
    monkeypatch.setattr(drives, "drive_roots", lambda: ["C:/", "D:/", "E:/", "F:/"])
    types = {
        "C:/": drives.DRIVE_FIXED,
        "D:/": drives.DRIVE_FIXED,
        "E:/": drives.DRIVE_REMOVABLE,
        "F:/": 4,  # DRIVE_CDROM
    }
    monkeypatch.setattr(drives._kernel32, "GetDriveTypeW", lambda root: types[root])
    usage = namedtuple("usage", "used total")(1, 2)  # read_disk 只用 .used / .total
    monkeypatch.setattr(drives.shutil, "disk_usage", lambda root: usage)  # 全部试读得到

    assert drives.detect_drives() == ["C:/", "D:/", "E:/"]  # 默认:固定 + 可移动(现状)
    assert drives.detect_drives(include_removable=False) == ["C:/", "D:/"]  # 取消勾选的口径


def test_card_pos_sits_below_the_window_by_default() -> None:
    """默认在窗口**下方** 4px、左对齐 —— 稳定不晃,不跟随光标。"""
    window = QRect(100, 100, 248, 62)
    card = QSize(200, 40)
    area = QRect(0, 0, 1920, 1040)

    pos = hovercard.card_pos(window, card, area)

    assert pos == QPoint(100, 100 + 62 + hovercard.GAP)


def test_card_pos_never_intersects_the_window() -> None:
    """核心要求:hover 文字不覆盖主界面 —— 各方位、各种放不下都要与窗口矩形**不相交**。"""
    area = QRect(0, 0, 1920, 1040)
    card = QSize(200, 40)
    cases = [
        QRect(100, 100, 248, 62),  # 中间:下方
        QRect(100, 1040 - 40, 248, 40),  # 贴底:翻上方
        QRect(1700, 100, 200, 62),  # 右缘:x 被夹进屏幕
        QRect(0, 0, 248, 62),  # 左上角
        QRect(100, 960, 248, 70),  # 底部且下方翻不上时的夹取路径
    ]
    for window in cases:
        pos = hovercard.card_pos(window, card, area)
        card_rect = QRect(pos, card)
        assert not card_rect.intersects(window), f"卡片盖住了窗口:window={window}"
        assert area.contains(card_rect), f"卡片跑到屏幕外:window={window}"
