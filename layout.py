"""纯计算与可调量程:尺寸与贴边的几何、文字与配色的换算,以及各项允许的取值范围。

这些函数**不依赖 Qt 的窗口/事件循环**(只用 `QSize` / `QColor` 这类值类型),所以第 1 层用例
直接调用它们(见 `tests/test_helpers.py`);窗口与托盘只是它们的消费方。

搬进这里的原因只有一个:主界面代码太长,而这些算式与界面无关 —— 改尺寸常量时,重排(`main`)、
动画目标、用例断言三处必须同步;取值范围则被"窗口读配置 / 托盘给滑杆量程"两处共用。都放在这里,
才不会各推一遍。
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor

# 首次运行的不透明度(之后以配置文件里的值为准:程序启动会把生效值回写)
FIRST_RUN_IDLE_OPACITY = 0.30

# --- 尺寸与屏幕边距 ---
UI_SCALE_DEFAULT = 1.0  # 整窗缩放的首次运行值(系数,不是像素)
WINDOW_W = 248
ROW_H = 22
PAD = 6
GAP = 6
EDGE_MARGIN = 24  # 首次运行时窗口离屏幕边多远(之后就由拖动决定)

# --- 贴边隐藏(收起成"一条断线")---
# 收起 = 折叠成贴在屏幕边上的一条断线:线只有一条,断成几段,一段表示一个盘。
# 每段**长度固定**(只表示"这个盘在这儿"),状态只靠颜色;整条线**与屏幕边平行**
# (贴左 / 贴右是竖的,贴上 / 贴下是横的,见 `tail_layout`)。
# 尺寸是**固定的设备像素**,不随主界面缩放变(「调整尺寸」只管展开形态)。
TAIL_SEGMENT = 6  # 每段(每块盘)的长度
TAIL_LINE_H = 2  # 线宽
TAIL_DASH_GAP = 2  # 两段之间的断开间隙(断开就靠它)
TAIL_MARGIN = 3  # 线到尾巴边缘的留白(尾巴同时是命中区:线只有 2px 粗,留点余量才好蹭)

# 贴边判定的阈值:取**最近**那条边,它的"内缩量"不超过这个值就算贴上。
# 内缩量是**带符号**的:窗口还在屏幕内为正,压出屏幕为负 —— 所以"拖出一半"这种也判得上,
# 不必为"压边"另立一条规则(见 `snap_edge`)。
EDGE_SNAP_PX = 8


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def clamp(value: int, low: int, high: int) -> int:
    """把 value 夹在 [low, high] 内(high 比 low 小时以 low 为准 —— 窗口比工作区还大时的兜底)。"""
    return max(low, min(high, value))


def snap_edge(
    x: int, y: int, width: int, height: int, area: tuple[int, int, int, int]
) -> str | None:
    """窗口贴在哪条屏幕边上:取**最近**那条边,它的内缩量 ≤ `EDGE_SNAP_PX` 就算贴上,否则 None。

    `area` 是**左下开区间**的矩形 (left, top, right, bottom)(Qt 的 `QRect.right()` 是闭区间,
    调用方要先 +1 换成这个口径)。

    内缩量**带符号**:窗口还在屏幕内时为正(离这条边还有多远),压出屏幕时为负(越出去多少)。
    取的是**最小**的那个 —— 都在屏幕内时它就等于"离得最近的那条",而压出去的那条是负数、
    自然排在最前。于是"贴住边"和"拖出去一半"是同一条规则,不必为后者再立一条。

    ⚠️ **只对展开状态用**:收起成尾巴时窗口只有十几像素,判定结果没有意义(尾巴本来就贴在那条边上)。
    贴边状态一旦定下来就保持,只有拖动结束、开关变化、缩放时才重新判定。
    """
    left, top, right, bottom = area
    inset = {
        "left": x - left,
        "right": right - (x + width),
        "top": y - top,
        "bottom": bottom - (y + height),
    }
    edge = min(inset, key=lambda name: inset[name])
    return edge if inset[edge] <= EDGE_SNAP_PX else None


def tail_pos(
    edge: str,
    x: int,
    y: int,
    width: int,
    height: int,
    tail_width: int,
    tail_height: int,
    area: tuple[int, int, int, int],
) -> tuple[int, int]:
    """收起成尾巴后尾巴的左上角:贴着那条边,沿边方向对齐展开时的中心。

    `x/y/width/height` 传**展开时**的矩形(锚点那份):尾巴落在它的中间,展开才不会横向跳。
    位置一律夹在工作区内 —— 窗口停在角落时,尾巴不能因为"对齐中心"而露到屏幕外(露出去就点不到了)。
    """
    left, top, right, bottom = area
    center_x = clamp(x + width // 2 - tail_width // 2, left, right - tail_width)
    center_y = clamp(y + height // 2 - tail_height // 2, top, bottom - tail_height)
    if edge == "left":
        return left, center_y
    if edge == "right":
        return right - tail_width, center_y
    if edge == "top":
        return center_x, top
    return center_x, bottom - tail_height  # bottom


def scaled_layout(scale: float, rows: int) -> tuple[int, int, int, int]:
    """整窗缩放后的 (边距 pad, 行距 gap, 行高 row_h, 宽度 width)。

    抽成纯函数是因为有两个消费方:窗口重排(`StatusBarWindow.apply_scale`)和"展开尺寸"的推算
    (收起 / 展开动画的目标、用例断言)—— 各推一遍迟早推歪。
    """
    return (
        round(PAD * scale),
        round(GAP * scale),
        max(1, round(ROW_H * scale)),
        max(1, round(WINDOW_W * scale)),
    )


def layout_size(scale: float, rows: int) -> QSize:
    """展开状态下整窗的尺寸(与 `apply_scale` 同一套算式,不另抄一份)。"""
    pad, gap, row_h, width = scaled_layout(scale, rows)
    return QSize(width, pad * 2 + row_h * rows + gap * (rows - 1))


def tail_layout(edge: str, rows: int) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    """尾巴的尺寸与每块盘的线段矩形(设备像素,左上角为原点)—— 尾巴长什么样全在这一处。

    **一条线,断成几段**:所有线段排在同一条直线上(同一个 y),段间留 `TAIL_DASH_GAP` 的空隙,
    一段表示一个盘;每段长度固定(`TAIL_SEGMENT`),状态是颜色的事(见 `DiskBar.status_rgb`)。
    整条线**与屏幕边平行** —— 贴左 / 贴右把矩形转 90°(x↔y、宽高互换),线就是竖的。

    尺寸**不随主界面缩放变**:「调整尺寸」管的是展开形态,贴边后这个标记不该跟着变粗变长。
    只有盘数会影响线的长度(要装得下每块盘一段)。

    返回 (宽, 高, [线段矩形…]),矩形顺序与盘序一致 —— 调用方拿着直接画,不必再判断方向。
    """
    length = TAIL_MARGIN * 2 + rows * TAIL_SEGMENT + (rows - 1) * TAIL_DASH_GAP
    thick = TAIL_MARGIN * 2 + TAIL_LINE_H
    lines = [
        # 先按"线沿 x 轴"排;竖线把矩形转 90°(x↔y、宽高互换)即可,段的长度不变
        (
            TAIL_MARGIN + index * (TAIL_SEGMENT + TAIL_DASH_GAP),
            TAIL_MARGIN,
            TAIL_SEGMENT,
            TAIL_LINE_H,
        )
        for index in range(rows)
    ]
    if edge in ("left", "right"):
        return thick, length, [(y, x, h, w) for x, y, w, h in lines]
    return length, thick, lines


def with_alpha(rgb: tuple[int, int, int], alpha: float) -> QColor:
    """给颜色换一个 alpha。**alpha 是 0~255,不是 0~1** —— 写 0.55 会被取整成 1(几乎看不见)。"""
    color = QColor(*rgb)
    color.setAlpha(max(0, min(255, round(alpha))))
    return color


def is_lit(max_ratio: float, lit: float, danger: float) -> bool:
    """占用率是否该常亮:到「常亮阈值」或「红色阈值」都算 —— 取两者中**更低**的那条。

    红色线永远是"最迟必须亮"的兜底:就算把常亮阈值设得比它高(配置文件手写),到红线照样常亮。
    两条线都没到就**完全不提亮**,严格按配置文件的不透明度显示(没有渐亮斜坡 —— 那会让
    "平时隐身"变得不可预期)。
    """
    return max_ratio >= min(lit, danger)


def percent_text(value: float) -> str:
    """把 0.85 这样的值写成给人看的百分比(滑杆右侧标签、还原框里的阈值共用)。"""
    return f"{round(value * 100)}%"


# --- 各项的取值量程:窗口读配置时夹取、托盘给滑杆 / 步进器定量程,两头共用 ---
REFRESH_MIN_S = 1  # 刷新间隔(秒)的量程
REFRESH_MAX_S = 60
LIVE_OPACITY = 1.00  # 鼠标悬停 / 自动提亮时的不透明度
MIN_OPACITY = 0.05  # 不透明度下限(再小就真的看不见了;滑杆的最小格也是它)
OPACITY_STEP = 0.05  # 滑杆步进(5%):一格一档,拖动与键盘都是这个粒度
OPACITY_STEPS = round(1.0 / OPACITY_STEP)  # 滑杆格数(1 格 = 5%,共 20 格)
THRESHOLD_MIN = 0.01  # 三条阈值:托盘按百分比显示,存盘是 1% 粒度的小数
THRESHOLD_MAX = 1.00
THRESHOLD_STEP = 0.01  # 一格 = 1%
THRESHOLD_GAP = 0.01  # 橙色与常亮都必须比红色至少低一格(橙色与常亮之间不设约束)
UI_SCALE_MIN = 0.6  # 整窗缩放的下限:再小就看不清了
UI_SCALE_MAX = 2.0  # 上限:再大就占满屏
SCALE_UNIT = 0.01  # 缩放滑杆一格 = 1%


def scale_units(scale: float) -> int:
    """缩放系数 -> 滑杆格数(一格 1%,所以格数就是百分数)。"""
    return min(
        round(UI_SCALE_MAX / SCALE_UNIT),
        max(round(UI_SCALE_MIN / SCALE_UNIT), round(scale / SCALE_UNIT)),
    )


def scale_from_units(units: int) -> float:
    """滑杆格数 -> 缩放系数。"""
    return min(UI_SCALE_MAX, max(UI_SCALE_MIN, units * SCALE_UNIT))


def opacity_units(value: float) -> int:
    """不透明度 -> 滑杆格数。

    配置文件里可以写任意精度(如 0.33),滑杆表达不了就取最近的一格;标签仍显示真实值。
    """
    return min(OPACITY_STEPS, max(1, round(value * OPACITY_STEPS)))


def opacity_from_step(step: int) -> float:
    """滑杆格数 -> 不透明度。

    用"格数 / 总格数"而不是"格数 × 0.05":后者带浮点尾巴(14 × 0.05 = 0.7000000000000001),
    写进配置文件就成了难看的 `idle_opacity=0.7000000000000001`。
    """
    return step / OPACITY_STEPS


def human_size(n: float) -> str:
    """字节数写成给人看的大小:B/KB/MB 取整,GB/TB 保留一位小数。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
