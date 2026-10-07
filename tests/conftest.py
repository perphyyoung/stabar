"""pytest 公共设施:离屏跑 Qt,并且绝不碰真实配置与真实注册表。"""

import logging
import os
import sys

import pytest

# 必须在 Qt 加载之前设置:用离屏平台就不会在测试时弹出真窗口,CI 上也能跑。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _FakeKey:
    """够用的上下文管理器:只包住一个字典。"""

    def __init__(self, store: dict) -> None:
        self._store = store

    def __enter__(self) -> dict:
        return self._store

    def __exit__(self, *exc) -> None:
        return None


class FakeWinreg:
    """最小可用的 winreg 替身,只实现 main.py 用到的那几个入口。

    方法名必须和 winreg 逐字一致(已在 pyproject 的白名单里豁免命名检查),
    否则注入进去 main.py 就找不到。行为上要和真实 API 对齐:查不到就抛
    FileNotFoundError(它是 OSError 的子类,正好命中 main.py 的 except)。
    """

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 1
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def CreateKeyEx(self, root, path, reserved, access) -> _FakeKey:
        return _FakeKey(self.values)

    def OpenKey(self, root, path) -> _FakeKey:
        if not self.values:
            raise FileNotFoundError(path)
        return _FakeKey(self.values)

    def QueryValueEx(self, key, name) -> tuple[str, int]:
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value) -> None:
        key[name] = value

    def DeleteValue(self, key, name) -> None:
        if name not in key:
            raise FileNotFoundError(name)
        del key[name]


@pytest.fixture
def fake_registry(monkeypatch) -> FakeWinreg:
    """注入假的 winreg:测注册表逻辑,但一个字节都不写真实注册表。"""
    import main

    fake = FakeWinreg()
    monkeypatch.setattr(main, "winreg", fake)
    return fake


@pytest.fixture
def low_usage(monkeypatch) -> None:
    """把磁盘占用固定在 10%:低于提亮起点,得到确定的最暗档。"""
    import main

    monkeypatch.setattr(main, "read_disk", lambda path: (100, 1000))


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, low_usage, fake_registry):
    """构造窗口:配置引到 tmp,磁盘数据与注册表都是假的。"""
    import main

    monkeypatch.setattr(main, "settings_path", lambda: tmp_path / "stabar.ini")
    window = main.StatusBarWindow()
    qtbot.addWidget(window)
    return window


@pytest.fixture
def app_log(tmp_path, monkeypatch):
    """把配置目录与日志都落到 tmp,并在结束后还原 logging 状态。

    还原是必须的:`setup_logging()` 用的是 `basicConfig(force=True)`,会顶掉 pytest 自己的
    处理器;文件句柄也要关掉,否则 Windows 上 tmp 目录删不掉。
    """
    import logging_setup
    import main

    monkeypatch.setenv(main.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")  # 测试默认看全部事件
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # 让被改写的钩子自动回滚

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    logging_setup.setup_logging(tmp_path, main.APP_NAME)
    yield tmp_path / logging_setup.LOG_NAME

    for handler in root.handlers:
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    logging_setup.qInstallMessageHandler(None)
    logging_setup._qt_bridge = None


@pytest.fixture
def events(app_log):
    """把日志读成事件行列表;每次调用重新读,便于在动作之间取最新的那一段。"""
    from e2e_support import read_events

    return lambda: read_events(app_log)
