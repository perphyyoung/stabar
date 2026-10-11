"""E2E 共用工具:日志事件解析 + 真输入(ctypes SendInput)。

- A 层(`tests/test_e2e_ui.py`,进程内)只用"日志事件"那半部分;
- B 层(`tests/test_e2e_real_input.py`,进程外真输入)两半都用。

三条设计要点都是从踩坑里来的(详见 `开发经验.md` 第 15、16 条):

1. `SendInput` 返回"成功插入的事件数",**必须校验** —— 结构体字段写错时它会静默不生效;
2. 动作前必须确认左键没被按下、光标没被外部挪走:你在用鼠标时 `SetCursorPos` 会被真实移动
   覆盖,那样测出来的失败是**假失败**,要重试或跳过,不能记成失败;
3. 无论如何都要补上左键抬起,绝不让被测机器的鼠标卡在"按下"状态。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

import uiautomation as auto  # 只有真输入 E2E 用得到(见文件末尾),uv 会一并带上它依赖的 comtypes
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent

# ======================================================================
# 日志事件:两层共用的观察点
# ======================================================================


def read_since(path: Path, since: int = 0) -> str:
    """读日志里"字节偏移量 since 之后"的内容。

    日志只有两份(打包后 / 其他),测试与 dev 运行**共用**同一份(见 `日志使用说明.md`),
    所以断言必须切掉之前的内容 —— 否则会读到别的用例或你自己的 dev 运行写下的事件,造成假通过。
    文件被"超限清空"过(长度反而变小)时退回从头读。
    """
    if not path.exists():
        return ""
    if path.stat().st_size < since:
        since = 0
    with path.open("rb") as handle:
        handle.seek(since)
        return handle.read().decode("utf-8", errors="replace")


def read_events(path: Path, since: int = 0) -> list[str]:
    """把日志读成事件行列表(只保留 `event=` 之后的部分)。"""
    events = []
    for line in read_since(path, since).splitlines():
        marker = line.find("event=")
        if marker >= 0:
            events.append(line[marker:].rstrip())
    return events


def assert_in_order(events: list[str], *needles: str) -> None:
    """断言这些子串按给定顺序出现 —— 对事件流来说,顺序本身就是结论。"""
    position = -1
    for needle in needles:
        found = next(
            (i for i, event in enumerate(events) if needle in event and i > position), None
        )
        assert found is not None, f"按顺序没等到 {needle!r};实际事件:\n" + "\n".join(events)
        position = found


def wait_for_event(path: Path, pattern: str, timeout: float = 6.0, since: int = 0) -> bool:
    """轮询日志等某个事件出现(B 层用;A 层同步执行,不需要等)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(pattern in event for event in read_events(path, since)):
            return True
        time.sleep(0.05)
    return False


# ======================================================================
# 真输入:Windows / ctypes
# ======================================================================

VK_LBUTTON = 0x01
VK_DOWN, VK_RETURN = 0x28, 0x0D  # 菜单里:下移一项 / 确认(HOME、END 在 QMenu 里无效,见 tap_keys)
LEFTDOWN, LEFTUP = 0x0002, 0x0004
RIGHTDOWN, RIGHTUP = 0x0008, 0x0010
KEYUP = 0x0002

# 拖动偏移:默认小一点(只验证"拖得动");要拖到屏幕外就显式传 dx(光标到屏幕边缘会被系统夹住,
# 所以"压出屏幕多少像素"取决于按下时光标离边缘多远 —— 见 `drag`)
DRAG_DX, DRAG_DY = 36, 24
DRAG_STEPS = 6

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),  # ULONG_PTR
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _InputUnion)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short


class InterferenceError(RuntimeError):
    """检测到真人鼠标在动 —— 本轮数据不可信,应重试或跳过,不要当成失败。"""


def _send(*items: INPUT) -> None:
    count = len(items)
    array = (INPUT * count)(*items)
    sent = _user32.SendInput(count, array, ctypes.sizeof(INPUT))
    if sent != count:
        raise OSError(f"SendInput 只送出 {sent}/{count},last_error={ctypes.get_last_error()}")


def _mouse(flags: int) -> INPUT:
    item = INPUT()
    item.type = INPUT_MOUSE
    item.mi.dwFlags = flags
    return item


def _key(vk: int, up: bool = False) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki.wVk = vk
    item.ki.dwFlags = KEYUP if up else 0
    return item


def cursor() -> tuple[int, int]:
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def left_button_down() -> bool:
    """左键是否已经被按着(说明你正在用鼠标)。"""
    return bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def set_cursor(x: int, y: int) -> None:
    if not _user32.SetCursorPos(int(x), int(y)):
        raise OSError("SetCursorPos 失败")


def place(x: int, y: int) -> None:
    """把光标放稳;200ms 内被外部挪走就抛 InterferenceError。"""
    set_cursor(x, y)
    time.sleep(0.2)
    got = cursor()
    if abs(got[0] - x) > 3 or abs(got[1] - y) > 3:
        raise InterferenceError(f"光标被外部挪走:期望 ({x},{y}) 实际 {got}")


@ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
def _enum_callback(hwnd, _lparam):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == _enum_callback.target and _user32.IsWindowVisible(hwnd):
        _enum_callback.found = hwnd
        return False
    return True


def find_window(pid: int) -> int | None:
    """按进程号找它的可见顶层窗口(我们的窗口是 Qt.Tool,不进任务栏但仍是顶层窗口)。"""
    _enum_callback.target = pid
    _enum_callback.found = None
    _user32.EnumWindows(_enum_callback, 0)
    return _enum_callback.found


def rect_of(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def is_visible(hwnd: int) -> int:
    """外部事实:窗口是否真的可见(日志说 hidden 不算数)。"""
    return _user32.IsWindowVisible(hwnd)


def center_of(hwnd: int) -> tuple[int, int]:
    left, top, right, bottom = rect_of(hwnd)
    return (left + right) // 2, (top + bottom) // 2


def drag(hwnd: int, dx: int = DRAG_DX, dy: int = DRAG_DY) -> None:
    """真拖动:按下 → 分步移动 → 抬起。位置先放稳,不稳就抛 InterferenceError。

    `dx` 是光标要走的位移;窗口跟的是**光标的位移**,而光标到屏幕边缘会被系统夹住 —— 所以想让
    窗口压出屏幕,得让光标**从远处**往边上走(压出多少 = 按下时光标离那条边多远)。
    """
    if left_button_down():
        raise InterferenceError("左键在动作前已被按下")
    x, y = center_of(hwnd)
    place(x, y)
    _send(_mouse(LEFTDOWN))
    try:
        step_x, step_y = dx // DRAG_STEPS, dy // DRAG_STEPS
        for step in range(1, DRAG_STEPS + 1):
            set_cursor(x + step_x * step, y + step_y * step)
            time.sleep(0.06)
    finally:
        _send(_mouse(LEFTUP))


def right_click(hwnd: int) -> None:
    """真右键(按下 + 抬起):Windows 在抬起时把它转成上下文菜单事件。"""
    if left_button_down():
        raise InterferenceError("左键在动作前已被按下")
    x, y = center_of(hwnd)
    place(x, y)
    _send(_mouse(RIGHTDOWN))
    time.sleep(0.05)
    _send(_mouse(RIGHTUP))


def mouse_event(kind, local: QPoint, global_pos: QPoint) -> QMouseEvent:
    """造一个左键鼠标事件(在**进程内**验证"按下 → 移动 → 松开"这类链路时用)。

    给的是局部坐标与屏幕坐标两套:窗口移动/缩放那类逻辑同时要看这两者。
    注意它只把事件交给目标控件,不走系统投递 —— 要验证"谁来收这个事件",得用真实输入。
    """
    return QMouseEvent(
        kind,
        QPointF(local),
        QPointF(global_pos),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def tap_keys(*keys: int, gap: float = 0.12) -> None:
    """依次敲键(按下 + 抬起)。

    实测:Qt 的 `QMenu` **不认 HOME / END**(按了没反应),只认上下键;而且菜单刚打开时
    没有高亮项,第一次按下键会选到**第一项**。所以要选第 N 项就按 N 次 DOWN。
    """
    for vk in keys:
        _send(_key(vk))
        _send(_key(vk, up=True))
        time.sleep(gap)


# ======================================================================
# 托盘图标:定位与点击(只有 B 层用)
#
# 实测(Qt 6.11 / Win11):托盘图标挂在被测进程里一个**不可见**的顶层窗口上,类名含
# `TrayIconMessageWindow`、`uID = 0`;拿它问 `Shell_NotifyIconGetRect` 就能得到图标在
# 屏幕上的矩形,而该点落在 explorer 的 `TrayNotifyWnd` 里 —— 于是"真点托盘图标"可以
# 由进程外的测试完成。图标被收进溢出、还没展开时矩形为空,上层应据此跳过。
# ======================================================================

TRAY_CLASS_HINT = "TrayIconMessageWindow"
VK_ESCAPE = 0x1B  # 关掉弹出的菜单

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


_shell32.Shell_NotifyIconGetRect.argtypes = (
    ctypes.POINTER(NOTIFYICONIDENTIFIER),
    ctypes.POINTER(wintypes.RECT),
)
_shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long


@ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
def _enum_all(hwnd, _lparam):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == _enum_all.target:
        _enum_all.found.append(hwnd)
    return True


def windows_of(pid: int) -> list[int]:
    """该进程的**全部**顶层窗口,含不可见的 —— 托盘图标那个窗口就是不可见的。

    (`find_window()` 只找可见窗口,是给小组件本身用的。)
    """
    _enum_all.target = pid
    _enum_all.found = []
    _user32.EnumWindows(_enum_all, 0)
    return list(_enum_all.found)


def class_of(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def tray_icon_rect(pid: int, timeout: float = 5.0) -> tuple[int, int, int, int] | None:
    """托盘图标的屏幕矩形;超时拿不到(被收进未展开的溢出、或根本没建)返回 None。

    **必须轮询**:图标是进程起来之后才注册到 shell 的,启动瞬间就问会失败
    (实测:一次性判定会让用例整条跳过)。
    """
    deadline = time.monotonic() + timeout
    while True:
        rect = _query_tray_rect(pid)
        if rect is not None:
            return rect
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def _query_tray_rect(pid: int) -> tuple[int, int, int, int] | None:
    for hwnd in windows_of(pid):
        if TRAY_CLASS_HINT not in class_of(hwnd):
            continue
        for uid in (0, 1, 2, 3):  # uID 实测是 0,留几个备选免得 Qt 改实现
            guid = (ctypes.c_byte * 16)()
            ident = NOTIFYICONIDENTIFIER(ctypes.sizeof(NOTIFYICONIDENTIFIER), hwnd, uid, guid)
            rect = wintypes.RECT()
            if _shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) != 0:
                continue
            if (rect.left, rect.top, rect.right, rect.bottom) != (0, 0, 0, 0):
                return rect.left, rect.top, rect.right, rect.bottom
    return None


SM_CXSCREEN, SM_CYSCREEN = 0, 1  # 主屏整屏尺寸
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SPI_GETWORKAREA = 0x0030

_user32.SystemParametersInfoW.argtypes = (
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    wintypes.UINT,
)
_user32.SystemParametersInfoW.restype = wintypes.BOOL


def work_area() -> tuple[int, int, int, int]:
    """主显示器的**工作区**(已排除任务栏)的 (left, top, right, bottom)。

    测试进程自己跑在离屏平台上(`QT_QPA_PLATFORM=offscreen`),问 Qt 只会拿到假屏幕,所以直接
    问系统:给 E2E 预置窗口位置时要用真实工作区(顺便也避开任务栏那一条)。
    """
    rect = wintypes.RECT()
    _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return rect.left, rect.top, rect.right, rect.bottom


def primary_bounds() -> tuple[int, int, int, int]:
    """主显示器整屏的 (left, top, right, bottom)—— 主屏原点就是 (0, 0)(任务栏在主屏上)。"""
    return 0, 0, _user32.GetSystemMetrics(SM_CXSCREEN), _user32.GetSystemMetrics(SM_CYSCREEN)


def taskbar_nudge_point(rect: tuple[int, int, int, int] | None = None) -> tuple[int, int]:
    """把鼠标推到"任务栏所在那一侧"的屏幕边缘 —— 自动隐藏的任务栏会因此滑出来。

    哪一侧**由图标矩形自己说了算**:任务栏自动隐藏时,shell 报的图标矩形会往那一侧超出屏幕
    (实测:矩形底部到 1126 而屏幕只有 1080 → 任务栏在下方)。这样就不必假设任务栏在哪一侧、
    是不是自动隐藏;矩形拿不到时退回去推下缘(Win11 的默认位置)。
    """
    left, top, right, bottom = primary_bounds()
    if rect is not None:
        if rect[3] > bottom:
            return (left + right) // 2, bottom - 1
        if rect[2] > right:
            return right - 1, (top + bottom) // 2
        if rect[0] < left:
            return left, (top + bottom) // 2
        if rect[1] < top:
            return (left + right) // 2, top
    return (left + right) // 2, bottom - 1


def screen_bounds() -> tuple[int, int, int, int]:
    """整个虚拟桌面(多显示器时是并集)的 (left, top, right, bottom)。"""
    left = _user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = _user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    right = left + _user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    bottom = top + _user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return left, top, right, bottom


def clamp_to_screen(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """矩形与屏幕的交集(屏幕外的部分被切掉;完全在外面时返回空矩形)。"""
    left, top, right, bottom = rect
    screen_left, screen_top, screen_right, screen_bottom = screen_bounds()
    return (
        max(left, screen_left),
        max(top, screen_top),
        min(right, screen_right),
        min(bottom, screen_bottom),
    )


def visible_size(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """矩形在屏幕内的高宽(0 表示有一边完全在屏幕外)。"""
    left, top, right, bottom = clamp_to_screen(rect)
    return max(0, right - left), max(0, bottom - top)


def center_of_rect(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """矩形与**屏幕**交集的中心 —— 不能直接取几何中心。

    实测:托盘图标的矩形会有一部分落在屏幕外(矩形底部到 1126,而屏幕只有 1080 —— 任务栏
    自动隐藏时,shell 报的是"隐藏位置"的矩形)。那时几何中心是个**不存在**的点:
    `SetCursorPos` 被系统夹回屏幕边缘,读回来差 20+ 像素,于是被 `place()` 判成"真人鼠标在动",
    整条用例白白跳过(而这台机器上根本没人碰鼠标)。完全在屏幕外的矩形退回几何中心,由上层跳过。
    """
    left, top, right, bottom = clamp_to_screen(rect)
    if right <= left or bottom <= top:
        left, top, right, bottom = rect
    return (left + right) // 2, (top + bottom) // 2


def click_at(x: int, y: int, *, right: bool = False) -> None:
    """在屏幕坐标处真点一下;光标被外部挪走就抛 InterferenceError。"""
    if left_button_down():
        raise InterferenceError("左键在动作前已被按下")
    place(x, y)
    down, up = (RIGHTDOWN, RIGHTUP) if right else (LEFTDOWN, LEFTUP)
    _send(_mouse(down))
    time.sleep(0.05)
    _send(_mouse(up))


# ======================================================================
# 按名称选菜单项:UI Automation(只有 B 层用)
#
# 选哪一项按**名称**定位,不按"第几项" —— 位置一变(菜单重排、增删项)旧写法就失效。
#
# 凭据是 Windows 给辅助技术与自动化测试的正式接口 —— UI Automation。Qt 把菜单暴露得很完整
# (进程外实测,宿主与查探是两个进程):
#
#     Class='QMenu'   ← 菜单窗口
#       ├ Name='隐藏'     Class='QAction'       Type=MenuItemControl   ← 名字就是 QAction.text()
#       ├ Name='透明度行' Class='QWidgetAction' Type=MenuItemControl   ← 控件行同样有名字
#       └ Name=''        Class='QAction'       Type=SeparatorControl
#
# 于是"选哪一项"按键名定位,与它在菜单里的顺序**无关**。两条实测注意:
#   1. Qt **不**提供 UIA 的 Invoke 模式(取到的是 None),所以只能 `Click()` —— 它走真实鼠标
#      (SetCursorPos + SendInput),恰好正是这一层要的;
#   2. 菜单里有的文字会变(隐藏 / 显示),所以按**正则**匹配。
#
# 官方依据:Qt《Accessibility》—— Qt 通过平台 API 把 GUI 语义暴露给辅助技术,菜单项的名字取自
# `QAction::text()`,且**不需要开关**(辅助技术发 WM_GETOBJECT 时按需创建)。
# ======================================================================

MENU_CLASS = "QMenu"


def visible_menu():
    """当前可见的菜单窗口;没有就返回 None。

    只认**可见**的:程序里另一个菜单(小组件自己的右键菜单)同类名,没弹出时也可能在树里,
    那份坐标是陈旧的,点下去会点到空处。
    """
    for window in auto.GetRootControl().GetChildren():
        if window.ClassName == MENU_CLASS and not window.IsOffscreen:
            return window
    return None


def visible_menu_names() -> list[str]:
    """当前可见菜单里的项名(找不到项时打进断言消息,便于一步定位)。"""
    menu = visible_menu()
    return [child.Name for child in menu.GetChildren()] if menu else []


def click_menu_item(pattern: str, timeout: float = 5.0, *, closes_menu: bool = True) -> str:
    """在**当前可见的菜单**里按名字找到一项、真点它,返回点到的名字。

    `pattern` 是正则,但按**整名匹配**(自动包成 `^(?:…)$`)—— 非锚定的子串匹配会误点:
    实测踩到,「显示所有磁盘盘符」加入菜单后,`隐藏|显示` 先匹配到它(它排在「隐藏」前面、
    名字里就含"显示"),而它是复选框行、点了不关菜单,于是被判成"点空了",重试 3 次(≈7 秒)
    后 skip —— 看起来就像"真人鼠标干扰"。

    找不到则断言失败(附上当时菜单里的项名);点完菜单没关说明多半被真人鼠标干扰 —— 抛
    `InterferenceError`,交给调用方的 `guarded()` 重试或跳过,而不是记成失败。

    `closes_menu=False` 用于"点完程序就退出"的项(如「退出」):那时**菜单窗口还在**(进程正在退),
    不能拿"菜单没关"当点空了 —— 那会被判成"被真人鼠标干扰",重试时程序已经没了。
    """
    if left_button_down():
        raise InterferenceError("左键在动作前已被按下")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        menu = visible_menu()
        if menu is not None:
            item = menu.MenuItemControl(RegexName=f"^(?:{pattern})$", searchDepth=1)
            if item.Exists(0.5, 0.1):
                name = str(item.Name)
                item.Click()  # 真鼠标
                if closes_menu and item.Exists(0.5, 0.1):  # 菜单没关 = 点空了
                    raise InterferenceError(f"点完「{name}」菜单还在,可能被真人鼠标干扰")
                return name
        time.sleep(0.1)
    raise AssertionError(f"当前菜单里没有匹配 {pattern!r} 的项:{visible_menu_names()}")
