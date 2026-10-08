"""E2E(B 层):进程外用真鼠标 / 键盘驱动被测程序,日志当观察点。

默认跳过,手动开启:

```powershell
uv run pyinstaller --noconfirm --clean stabar.spec   # 先打包(或用下面的 dev 形态免打包)
$env:STABAR_E2E = "1"                                 # 再开开关
uv run pytest tests/test_e2e_real_input.py -q
$env:STABAR_E2E_TARGET = "dev"                        # 可选:改跑源码形态,不必打包
```

**会真的移动你的鼠标、抢焦点,所以必须有真实桌面会话**(headless CI 上跑不了)。
每个动作前都会检测"左键没被按下、光标没被挪走",检测到你在用鼠标就重试,全都无效则 skip ——
真失败和假失败必须分得清。

**为什么可以跑源码形态**:这一层的不变量(命中层、OS 把右键转成菜单、shell 把点击转给托盘图标)
取决于 Windows 与代码路径,和"是不是 PyInstaller 打包的"无关 —— 同一个 shell、同一份代码。
打包形态自身的差异(frozen 路径、插件裁剪、无控制台)不属于这一层。所以改动落在**纯逻辑**上时,
`STABAR_E2E_TARGET=dev` 就能免去每次打包(见 `AGENTS.md` 的「打包时机」)。

这一层存在的理由,是它覆盖 A 层覆盖不到的四件事:隐形命中层是否真的接住鼠标、
操作系统把右键转成上下文菜单的那一层、**系统托盘那一侧**(点击真的送到了图标上)、
以及真实退出路径。

日志按设计只有两份(打包后 / 其他):打包产物写 `%LOCALAPPDATA%\\stabar\\stabar.log`,而且
**不理会** `STABAR_CONFIG_DIR`(那个变量只隔离配置)。所以这里要按真实路径找日志,并按偏移量只读
本次启动之后写的部分。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from e2e_support import (
    VK_DOWN,
    VK_RETURN,
    InterferenceError,
    assert_in_order,
    center_of_rect,
    class_of,
    click_at,
    drag,
    find_window,
    is_visible,
    read_events,
    read_since,
    rect_of,
    right_click,
    tap_keys,
    tray_icon_rect,
    wait_for_event,
    windows_of,
)

import logging_setup
import main

BASE = Path(__file__).resolve().parent.parent
EXE = BASE / "dist" / "stabar" / "stabar.exe"
TARGET = os.environ.get("STABAR_E2E_TARGET", "exe")  # dev = 跑源码形态,不必打包

# 键盘落到第几项:菜单刚打开时没有高亮项,按 N 次 ↓ 走到第 N 项(分隔线跳过;按多了会绕回开头)。
# **两个菜单不是同一个**:小组件自己那份只有三项;托盘那份含控件行,顺序见 `TrayIcon.__init__`,
# 由第 2 层的 `test_menu_items_and_order` 钉着 —— 改托盘顺序就要改 ITEM_TRAY_HIDE。
ITEM_WINDOW_HIDE = 1  # 小组件菜单:隐藏 / 立即刷新 / ─── / 退出
ITEM_WINDOW_QUIT = 3
ITEM_TRAY_HIDE = 8  # 托盘菜单:鼠标穿透 / 开机自启 / 刷新频率 / 橙红常亮三阈值 / 透明度 / 隐藏

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("STABAR_E2E") != "1",
        reason="真输入 E2E 需手动开启:先打包(或设 STABAR_E2E_TARGET=dev),再设 STABAR_E2E=1",
    ),
    pytest.mark.skipif(
        TARGET != "dev" and not EXE.exists(),
        reason=f"先打包,产物不存在:{EXE}(或设 STABAR_E2E_TARGET=dev 跑源码形态)",
    ),
]


def target_log_path() -> Path:
    """被测进程的日志落点:打包后是 %LOCALAPPDATA%\\stabar,源码形态是项目目录。"""
    if TARGET == "dev":
        return BASE / logging_setup.LOG_NAME
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / main.APP_NAME / logging_setup.LOG_NAME


def launch_command() -> list[str]:
    """启动命令:`STABAR_E2E_TARGET=dev` 跑源码形态,默认跑打包产物。"""
    if TARGET == "dev":
        return [sys.executable, str(BASE / "main.py")]
    return [str(EXE)]


def _tail(path: Path, lines: int = 20) -> str:
    if not path.exists():
        return "(没有日志文件)"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _pid_from_log(path: Path, since: int) -> int | None:
    """从日志里取被测进程自己的 pid(记录格式里的 `[pid]`)。

    源码形态下 `Popen` 拿到的是**转发器**的 pid,真正的解释器是它的子进程(实测:venv 的
    python.exe 会再起一个)。日志是进程自己写的,里面的 pid 才指向被测进程。
    """
    matched = re.search(r"\[(\d+)\].*event=start", read_since(path, since))
    return int(matched.group(1)) if matched else None


def _stop(proc) -> None:
    """收尾:按**进程树**结束被测进程。

    源码形态下必须带 `/T`:转发器被终止时不会带走子进程,留下一个还在跑的窗口会污染后续用例。
    """
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False
    )
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def app(tmp_path):
    """启动被测进程:配置隔离到 tmp,日志按"只有两份"的落点读。"""
    env = {**os.environ, "STABAR_CONFIG_DIR": str(tmp_path), "STABAR_LOG_LEVEL": "DEBUG"}
    # conftest 给**本进程**设了 QT_QPA_PLATFORM=offscreen(离屏跑单测),但绝不能再透给子进程:
    # 打包按体积裁剪时把 qoffscreen.dll 这类插件剔掉了,子进程会直接启动失败并弹出
    # "no Qt platform plugin could be initialized. Available platform plugins are: windows."
    env.pop("QT_QPA_PLATFORM", None)

    log = target_log_path()
    since = log.stat().st_size if log.exists() else 0  # 日志是共享的:只读本次启动之后的部分
    proc = subprocess.Popen(launch_command(), env=env, cwd=str(BASE))
    try:
        assert wait_for_event(log, "event=window state=shown", 15, since=since), (
            f"启动失败:子进程退出码={proc.poll()}(None 表示还活着)\n日志尾部:\n{_tail(log)}"
        )
        pid = _pid_from_log(log, since) or proc.pid  # dev 形态下两者不同,见 _pid_from_log
        hwnd = find_window(pid)
        assert hwnd, f"没找到窗口句柄(pid={pid})"
        yield SimpleNamespace(
            proc=proc,
            pid=pid,
            hwnd=hwnd,
            log=log,
            since=since,
            ini=tmp_path / "stabar.ini",
            wait=lambda pattern, timeout=6.0: wait_for_event(log, pattern, timeout, since=since),
            events=lambda: read_events(log, since),
        )
    finally:
        _stop(proc)


def guarded(action, rounds: int = 3):
    """执行真输入动作;遇到真人鼠标干扰就重试,全都无效则跳过(而不是判失败)。"""
    for _ in range(rounds):
        try:
            return action()
        except InterferenceError:
            time.sleep(1.5)
    pytest.skip("检测到真人鼠标干扰(光标被挪走 / 左键被按着),本轮数据不可信")


def test_drag_moves_window_and_saves_position(app) -> None:
    """拖得动 = 隐形命中层真的接收鼠标。这是 A 层唯一覆盖不到的不变量。"""
    before = rect_of(app.hwnd)
    guarded(lambda: drag(app.hwnd))
    assert app.wait("event=position_saved", 3), "日志里没有 position_saved"

    after = rect_of(app.hwnd)
    assert (after[0], after[1]) != (before[0], before[1]), "日志说保存了位置,窗口却没动"

    text = app.ini.read_text(encoding="utf-8")
    assert f"pos_x={after[0]}" in text and f"pos_y={after[1]}" in text  # 外部事实:真的落盘了


def test_real_right_click_hides_window(app) -> None:
    """真右键 → 菜单 → 键盘选「隐藏」:覆盖"OS 把右键转成上下文菜单"的那一层。"""
    guarded(lambda: right_click(app.hwnd))
    assert app.wait("event=menu open=context", 3), "右键没有弹出菜单"

    tap_keys(*[VK_DOWN] * ITEM_WINDOW_HIDE)  # 一个 ↓ 就到「隐藏」
    tap_keys(VK_RETURN)
    assert app.wait("event=window state=hidden", 3), "日志里没有 state=hidden"

    assert is_visible(app.hwnd) == 0  # 外部事实:窗口真的不可见了
    assert_in_order(app.events(), "event=menu item=隐藏", "event=window state=hidden")


def test_menu_quit_exits_process_gracefully(app) -> None:
    """真菜单选「退出」→ event=stop 且进程自己退出(退出码 0):验证优雅退出路径。"""
    guarded(lambda: right_click(app.hwnd))
    assert app.wait("event=menu open=context", 3), "右键没有弹出菜单"

    tap_keys(*[VK_DOWN] * ITEM_WINDOW_QUIT)  # 隐藏 → 立即刷新 → 退出(分隔线跳过)
    tap_keys(VK_RETURN)
    assert app.wait("event=menu item=退出", 3), "没选到退出"
    assert app.wait("event=stop", 3), "没有 stop 事件,说明不是优雅退出"

    assert app.proc.wait(timeout=10) == 0  # 进程自己退出,不是被 kill 的
    assert_in_order(app.events(), "event=menu item=退出", "event=stop")


def tray_icon_or_skip(app):
    """托盘图标的屏幕矩形;拿不到就跳过 —— 图标在未展开的溢出区里时是点不到的。"""
    rect = tray_icon_rect(app.pid)  # 必须用被测进程自己的 pid:dev 形态下 Popen 的 pid 是转发器
    if rect is None:
        seen = sorted({class_of(hwnd) for hwnd in windows_of(app.pid)})
        pytest.skip(f"拿不到托盘图标矩形(图标可能在未展开的溢出区);进程窗口类:{seen}")
    return rect


def test_real_tray_click_toggles_visibility(app) -> None:
    """真左键点托盘图标 = 显示 / 隐藏:覆盖"点击真的送到了托盘图标上"。

    A 层只能发 `activated` 信号,shell 把点击转给图标这一段进程内看不见。
    """
    rect = tray_icon_or_skip(app)

    guarded(lambda: click_at(*center_of_rect(rect)))
    assert app.wait("event=window state=hidden", 3), "点了托盘图标,窗口没藏起来"
    assert is_visible(app.hwnd) == 0  # 外部事实

    # 两次点击靠得太近时第二次不生效(实测:间隔约 0.3 秒时窗口藏了就不再回来,1 秒以上正常)——
    # 短间隔内 Qt 报的是 DoubleClick 而我们只响应 Trigger,`event=tray activated=` 就是为这个留的
    time.sleep(1.0)
    guarded(lambda: click_at(*center_of_rect(rect)))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and is_visible(app.hwnd) == 0:
        time.sleep(0.1)  # 启动时已经有过一次 shown,不能拿它当"回来了"
    assert is_visible(app.hwnd) == 1, "再点一下,窗口没回来"
    assert_in_order(app.events(), "event=window state=hidden", "event=window state=shown")


def test_real_tray_menu_hides_window(app) -> None:
    """真右键托盘图标 → 菜单 → 键盘选「隐藏」:托盘那一侧的端到端链路。

    与 `test_real_right_click_hides_window` 的区别:那个是小组件自己的菜单(右键窗口),
    这个走系统托盘 —— 图标由 explorer 画,菜单是 Qt 收到 shell 转发后才弹的。
    """
    rect = tray_icon_or_skip(app)

    guarded(lambda: click_at(*center_of_rect(rect), right=True))
    assert app.wait("event=menu open=tray", 3), "右键托盘图标没有弹出菜单"

    tap_keys(*[VK_DOWN] * ITEM_TRAY_HIDE)  # 托盘菜单长:一路 ↓ 到「隐藏」(见 ITEM_TRAY_HIDE)
    tap_keys(VK_RETURN)
    assert app.wait("event=window state=hidden", 3), "日志里没有 state=hidden"

    assert is_visible(app.hwnd) == 0  # 外部事实
    assert_in_order(app.events(), "event=menu open=tray", "event=menu item=隐藏")
