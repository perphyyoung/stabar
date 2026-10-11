"""盘符:读占用、探测"已加载"的盘、决定显示哪些。

三件事分开:

- **读占用**:`read_disk()` —— 读不到返回 `None`(调用方据此显示"不可用");
- **探测**:`detect_drives()` —— 固定盘 + 可移动盘,**而且试读得到**。固定盘里就包含 USB 硬盘盒
  (它报的是 `DRIVE_FIXED` 而不是 `DRIVE_REMOVABLE`),所以"要含移动硬盘"靠的是两个类型一起收;
  试读那一步是决定性的:空光驱、读卡器无卡、拔掉的盘都会被它排除,主界面上不会出现一排永远
  "不可用"的行,也不必维护"哪些类型要排除"的白名单。映射的网络盘按类型排除 —— 它即使在线,
  `disk_usage` 也可能卡住几秒,而这一步跑在 UI 线程上(要看网络盘就把字母写进 ini 的 `drives`)。
- **显示集合**:`parse_drives()` / `visible_drives()` / `drives_text()` —— 纯函数,ini 里 `drives`
  默认 `auto`(全部已加载的),写 `C,E` 即白名单;白名单里当场读不到的盘不显示。

窗口怎么**用**这个集合(建盘条、盘集变了重建)在 `main.py`,不在本模块。
"""

from __future__ import annotations

import ctypes
import logging
import shutil
from ctypes import wintypes

log = logging.getLogger("stabar")

# 配置文件里的键:默认 auto(= 用探测结果),也可以写字母表白名单
DRIVES_KEY = "drives"
DRIVES_AUTO = "auto"
MAX_DRIVES = 8  # 显示上限:窗口高度 = 盘数 × 行高,再多就太占屏幕

# Win32 的驱动器类型(GetDriveTypeW 的返回值)。固定盘里包含 USB 硬盘盒,见模块说明
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
KEEP_DRIVE_TYPES = (DRIVE_FIXED, DRIVE_REMOVABLE)


def read_disk(path: str) -> tuple[int, int] | None:
    """返回 (已用字节, 总字节);盘符不存在时返回 None。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return usage.used, usage.total


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetLogicalDriveStringsW.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
_kernel32.GetLogicalDriveStringsW.restype = wintypes.DWORD
_kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
_kernel32.GetDriveTypeW.restype = wintypes.UINT


def drive_roots() -> list[str]:
    """系统里所有已分配盘符的根(形如 "C:/");拿不到就返回空表并留痕。"""
    size = _kernel32.GetLogicalDriveStringsW(0, None)  # 先问长度,再按长度取
    if not size:
        log.warning("event=drives error=GetLogicalDriveStringsW code=%s", ctypes.get_last_error())
        return []
    buffer = ctypes.create_unicode_buffer(size + 1)
    _kernel32.GetLogicalDriveStringsW(size, buffer)
    return [f"{part[0]}:/" for part in buffer[:size].split("\0") if part]


def detect_drives(include_removable: bool = True) -> list[str]:
    """**已加载**的盘符(含移动硬盘):固定盘 + 可移动盘,而且**试读得到**(见模块说明)。

    `include_removable=False` 只收**本地固定盘** —— 托盘「显示所有磁盘盘符」取消勾选时的口径
    (`KEEP_DRIVE_TYPES` 收可移动类型正是为了默认含移动硬盘)。⚠️ 系统口径的盲区:USB 硬盘盒、
    部分移动硬盘报的是 `DRIVE_FIXED`,`GetDriveType` 分不出"内置 vs USB 盒",它在两种模式下
    都会显示;能被这个开关分出去的只有"可移动"类型(U 盘、读卡器、部分移动硬盘)。
    """
    keep_types = KEEP_DRIVE_TYPES if include_removable else (DRIVE_FIXED,)
    usable = [
        root
        for root in drive_roots()
        if _kernel32.GetDriveTypeW(root) in keep_types and read_disk(root) is not None
    ]
    return sorted(usable)


def parse_drives(raw: object) -> tuple[list[str] | None, list[str]]:
    """解析 ini 里的 `drives` →(要显示的盘符 | None = 自动, 被丢掉的非法项)。

    `auto`(默认)或空 → (None, []);`C,E` → (["C:/", "E:/"], []);`C:`、`c:\\`、多余空格都认。
    写坏的项丢掉,全坏就当自动 —— 一个笔误不该让小组件变成空的。

    ⚠️ **两种形状都要认**:`QSettings` 会把带逗号的值读成**列表**(`drives=C,D` → `["C", "D"]`),
    只写字符串一种会在"写了两个盘"时静默退化成自动(实测踩到:E2E 里窗口多出一行、尾巴长度对不上)。
    """
    if isinstance(raw, (list, tuple)):
        text = ",".join(str(item) for item in raw).strip()
    else:
        text = str(raw).strip()
    if not text or text.lower() == DRIVES_AUTO:
        return None, []
    wanted: list[str] = []
    dropped: list[str] = []
    for raw_token in text.replace(";", ",").split(","):
        token = raw_token.strip()
        if not token:
            continue
        letter, tail = token[:1].upper(), token[1:]
        if "A" <= letter <= "Z" and tail in ("", ":", ":/", ":\\"):
            if letter not in wanted:
                wanted.append(letter)
        else:
            dropped.append(token)
    return ([f"{letter}:/" for letter in wanted] if wanted else None), dropped


def visible_drives(detected: list[str], wanted: list[str] | None) -> list[str]:
    """最终显示哪些盘:自动就用探测结果,白名单则**按写下的顺序**,两者都只取探测得到的。

    白名单里的盘当场读不到就不显示(不保留一行永远"不可用"的行);超过 `MAX_DRIVES` 取前几个。
    """
    if wanted is None:
        return sorted(detected)[:MAX_DRIVES]
    detected_set = set(detected)
    return [root for root in wanted if root in detected_set][:MAX_DRIVES]


def drives_text(wanted: list[str] | None) -> str:
    """把显示集合写成 ini 里的形式:`auto` 或 `C,D`。"""
    return DRIVES_AUTO if wanted is None else ",".join(root[0] for root in wanted)
