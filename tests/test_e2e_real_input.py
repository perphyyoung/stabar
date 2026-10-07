"""E2E(B 层):进程外用真鼠标 / 键盘驱动**打包后的 exe**,日志当观察点。

默认跳过,手动开启:

```powershell
uv run pyinstaller --noconfirm --clean stabar.spec   # 先打包
$env:STABAR_E2E = "1"                                 # 再开开关
uv run pytest tests/test_e2e_real_input.py -q
```

**会真的移动你的鼠标、抢焦点,所以必须有真实桌面会话**(headless CI 上跑不了)。
每个动作前都会检测"左键没被按下、光标没被挪走",检测到你在用鼠标就重试,全都无效则 skip ——
真失败和假失败必须分得清。

这一层存在的唯一理由,是它覆盖 A 层覆盖不到的三件事:隐形命中层是否真的接住鼠标、
操作系统把右键转成上下文菜单的那一层、以及真实退出路径。

日志按设计只有两份(打包后 / 其他):打包产物写 `%LOCALAPPDATA%\\stabar\\stabar.log`,而且
**不理会** `STABAR_CONFIG_DIR`(那个变量只隔离配置)。所以这里要按真实路径找日志,并按偏移量只读
本次启动之后写的部分。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from e2e_support import (
    VK_DOWN,
    VK_RETURN,
    InterferenceError,
    assert_in_order,
    drag,
    find_window,
    is_visible,
    read_events,
    rect_of,
    right_click,
    tap_keys,
    wait_for_event,
)

import logging_setup
import main

EXE = Path(__file__).resolve().parent.parent / "dist" / "stabar" / "stabar.exe"

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("STABAR_E2E") != "1",
        reason="真输入 E2E 需手动开启:先打包,再设 STABAR_E2E=1",
    ),
    pytest.mark.skipif(not EXE.exists(), reason=f"先打包,产物不存在:{EXE}"),
]


def packaged_log_path() -> Path:
    """打包后的日志位置:与打包后的 ini 同目录(%LOCALAPPDATA%\\stabar\\stabar.log)。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / main.APP_NAME / logging_setup.LOG_NAME


def _tail(path: Path, lines: int = 20) -> str:
    if not path.exists():
        return "(没有日志文件)"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


@pytest.fixture
def app(tmp_path):
    """启动打包后的 exe:配置隔离到 tmp,日志写打包后的那一份(与"只有两份"的设计一致)。"""
    env = {**os.environ, "STABAR_CONFIG_DIR": str(tmp_path), "STABAR_LOG_LEVEL": "DEBUG"}
    # conftest 给**本进程**设了 QT_QPA_PLATFORM=offscreen(离屏跑单测),但绝不能再透给子进程:
    # 打包按体积裁剪时把 qoffscreen.dll 这类插件剔掉了,子进程会直接启动失败并弹出
    # "no Qt platform plugin could be initialized. Available platform plugins are: windows."
    env.pop("QT_QPA_PLATFORM", None)

    log = packaged_log_path()
    since = log.stat().st_size if log.exists() else 0  # 日志是共享的:只读本次启动之后的部分
    proc = subprocess.Popen([str(EXE)], env=env)
    try:
        assert wait_for_event(log, "event=window state=shown", 15, since=since), (
            f"启动失败:子进程退出码={proc.poll()}(None 表示还活着)\n日志尾部:\n{_tail(log)}"
        )
        hwnd = find_window(proc.pid)
        assert hwnd, "没找到窗口句柄"
        yield SimpleNamespace(
            proc=proc,
            hwnd=hwnd,
            log=log,
            since=since,
            ini=tmp_path / "stabar.ini",
            wait=lambda pattern, timeout=6.0: wait_for_event(log, pattern, timeout, since=since),
            events=lambda: read_events(log, since),
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


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

    tap_keys(VK_DOWN)  # 菜单刚打开没有高亮,第一次按下键 = 第一项「隐藏」
    tap_keys(VK_RETURN)
    assert app.wait("event=window state=hidden", 3), "日志里没有 state=hidden"

    assert is_visible(app.hwnd) == 0  # 外部事实:窗口真的不可见了
    assert_in_order(app.events(), "event=menu item=隐藏", "event=window state=hidden")


def test_menu_quit_exits_process_gracefully(app) -> None:
    """真菜单选「退出」→ event=stop 且进程自己退出(退出码 0):验证优雅退出路径。"""
    guarded(lambda: right_click(app.hwnd))
    assert app.wait("event=menu open=context", 3), "右键没有弹出菜单"

    tap_keys(VK_DOWN, VK_DOWN, VK_DOWN)  # 隐藏 → 立即刷新 → 退出(分隔符会被跳过)
    tap_keys(VK_RETURN)
    assert app.wait("event=menu item=退出", 3), "没选到退出"
    assert app.wait("event=stop", 3), "没有 stop 事件,说明不是优雅退出"

    assert app.proc.wait(timeout=10) == 0  # 进程自己退出,不是被 kill 的
    assert_in_order(app.events(), "event=menu item=退出", "event=stop")
