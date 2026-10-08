"""E2E(B 层):进程外用真鼠标 / 键盘驱动被测程序,日志当观察点。

默认跳过,手动开启:

```powershell
# 源码形态(常规):免打包;多条环境变量之间用 ;(赋值不能跟在 && 后面)
$env:STABAR_E2E = "1"; $env:STABAR_E2E_TARGET = "dev"; uv run pytest tests/test_e2e_real_input.py -q

# 打包形态:只有要验证打包自身时才跑;不设目标时它本来就是默认目标
uv run pyinstaller --noconfirm --clean stabar.spec
$env:STABAR_E2E = "1"; uv run pytest tests/test_e2e_real_input.py -q
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

**选菜单项一律按名称**:用 UI Automation 找到那一项再真点(`e2e_support.click_menu_item`),
与它在菜单里的位置**无关**。以前是"按 N 次 ↓ 到第 N 项",那个 N 得手工数、菜单一重排就失效
(而且真数错过一次:写 10,而「隐藏」在第 11 项)。

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
    InterferenceError,
    assert_in_order,
    center_of_rect,
    class_of,
    click_at,
    click_menu_item,
    drag,
    find_window,
    is_visible,
    read_events,
    read_since,
    rect_of,
    right_click,
    set_cursor,
    taskbar_nudge_point,
    tray_icon_rect,
    visible_size,
    wait_for_event,
    windows_of,
)

import logging_setup
import main

BASE = Path(__file__).resolve().parent.parent
EXE = BASE / "dist" / "stabar" / "stabar.exe"
TARGET = os.environ.get("STABAR_E2E_TARGET", "exe")  # dev = 跑源码形态,不必打包

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("STABAR_E2E") != "1",
        reason=(
            "真输入 E2E 需手动开启:设 STABAR_E2E=1 再跑 —— 常规跑源码形态"
            "(再加 STABAR_E2E_TARGET=dev),验证打包形态就先打包(见 README 的「测试」)"
        ),
    ),
    pytest.mark.skipif(
        TARGET != "dev" and not EXE.exists(),
        reason=f"没打包,产物不存在:{EXE} —— 要么先打包,要么设 STABAR_E2E_TARGET=dev 跑源码形态",
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
    """执行真输入动作;遇到真人鼠标干扰就重试,全都无效则跳过(而不是判失败)。

    跳过时把**最后一次的原始报错**也带上:只说"检测到干扰"没法判断是真人动了鼠标,还是
    我们自己的取点有问题(实测踩过:托盘矩形有一部分在屏幕外 → 取点落到屏幕外 → 被夹回 →
    看起来和"有人动鼠标"一模一样)。
    """
    last: InterferenceError | None = None
    for _ in range(rounds):
        try:
            return action()
        except InterferenceError as error:
            last = error
            time.sleep(1.5)
    pytest.skip(f"检测到真人鼠标干扰(光标被挪走 / 左键被按着),本轮数据不可信:最后一次 {last}")


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
    """真右键 → 菜单 → 按名称点「隐藏」:覆盖"OS 把右键转成上下文菜单"的那一层。"""
    guarded(lambda: right_click(app.hwnd))
    assert app.wait("event=menu open=context", 3), "右键没有弹出菜单"

    guarded(lambda: click_menu_item("隐藏|显示"))  # 按名称点,不用数「第几项」
    assert app.wait("event=window state=hidden", 3), "日志里没有 state=hidden"

    assert is_visible(app.hwnd) == 0  # 外部事实:窗口真的不可见了
    assert_in_order(app.events(), "event=menu item=隐藏", "event=window state=hidden")


def test_menu_quit_exits_process_gracefully(app) -> None:
    """真菜单选「退出」→ event=stop 且进程自己退出(退出码 0):验证优雅退出路径。"""
    guarded(lambda: right_click(app.hwnd))
    assert app.wait("event=menu open=context", 3), "右键没有弹出菜单"

    guarded(lambda: click_menu_item("退出", closes_menu=False))  # 点完进程就开始退,菜单还在
    assert app.wait("event=menu item=退出", 3), "没选到退出"
    assert app.wait("event=stop", 3), "没有 stop 事件,说明不是优雅退出"

    assert app.proc.wait(timeout=10) == 0  # 进程自己退出,不是被 kill 的
    assert_in_order(app.events(), "event=menu item=退出", "event=stop")


MIN_TRAY_VISIBLE = 8  # 图标可见高度小于这个数就别点了:点不准(实测自动隐藏时只剩 2 像素)
TASKBAR_REVEAL_TIMEOUT = 3.0  # 唤出任务栏最多等这么久


def tray_icon_or_skip(app):
    """托盘图标的屏幕矩形;**先想办法让图标真的可见**,确实做不到才跳过。

    任务栏自动隐藏时,shell 报的图标矩形会有一部分落在屏幕外(实测底部到 1126,而屏幕只有
    1080)—— 光靠"把坐标夹进屏幕"只能点到贴着边缘的两像素,点不中。所以先把鼠标推到任务栏
    那一侧的屏幕边缘把它唤出来(`taskbar_nudge_point` 按矩形往哪边超出判断在哪一侧,不假设
    位置、也不假设是不是自动隐藏),等图标整个露出来再继续。**必须用被测进程自己的 pid**:
    dev 形态下 Popen 的 pid 是转发器。
    """
    rect = tray_icon_rect(app.pid, timeout=1.5)
    deadline = time.monotonic() + TASKBAR_REVEAL_TIMEOUT
    while not (rect is not None and visible_size(rect)[1] >= MIN_TRAY_VISIBLE):
        if time.monotonic() >= deadline:
            break
        set_cursor(*taskbar_nudge_point(rect))  # 推到那一侧边缘,任务栏会滑出来
        time.sleep(0.25)
        rect = tray_icon_rect(app.pid, timeout=1.0)

    if rect is None:
        seen = sorted({class_of(hwnd) for hwnd in windows_of(app.pid)})
        pytest.skip(f"拿不到托盘图标矩形(图标可能在未展开的溢出区);进程窗口类:{seen}")
    _, height = visible_size(rect)
    if height < MIN_TRAY_VISIBLE:
        pytest.skip(
            f"把鼠标推到任务栏那一侧边缘后,图标仍只有 {height} 像素可见(矩形 {rect})—— "
            "可能在未展开的溢出区,点不准"
        )
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

    guarded(lambda: click_menu_item("隐藏|显示"))  # 托盘菜单长,按名称点省去数项
    assert app.wait("event=window state=hidden", 3), "日志里没有 state=hidden"

    assert is_visible(app.hwnd) == 0  # 外部事实
    assert_in_order(app.events(), "event=menu open=tray", "event=menu item=隐藏")
