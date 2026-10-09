"""pytest 公共设施:离屏跑 Qt,并且绝不碰真实配置与真实注册表。"""

import logging
import os
import sys

import pytest

# 必须在 Qt 加载之前设置:用离屏平台就不会在测试时弹出真窗口,CI 上也能跑。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _log_source_label(request):
    """给日志行打上用例标签:日志是共享一份的,这样一眼能看出每行是谁写的。

    生产路径从不设置标签,所以 dev / 打包的输出格式不变(见 `日志使用说明.md` 第四节)。
    """
    import logging_setup

    logging_setup.set_source_label(request.node.name)
    yield
    logging_setup.set_source_label("")


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
    import drives

    monkeypatch.setattr(drives, "read_disk", lambda path: (100, 1000))


@pytest.fixture
def fake_drives(monkeypatch) -> list[str]:
    """把盘符探测钉成 `C:/` `D:/` —— "这台机器插了几个盘"不该决定用例的结果。"""
    import drives

    roots = ["C:/", "D:/"]
    monkeypatch.setattr(drives, "detect_drives", lambda: list(roots))
    return roots


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch, low_usage, fake_registry, fake_drives):
    """构造窗口:配置引到 tmp,磁盘数据、注册表、盘符探测都是假的。"""
    import main

    monkeypatch.setattr(main, "settings_path", lambda: tmp_path / "stabar.ini")
    window = main.StatusBarWindow()
    qtbot.addWidget(window)
    return window


def _log_test_failure(item, report) -> None:
    """把失败的用例记进日志(**ERROR** 级)。

    失败这件事不属于被测程序 —— app 根本不知道自己在被测,所以只能由测试框架来记。
    级别用 ERROR:任何档位(DEBUG / INFO / WARNING)都留得住,搜 `event=test_failed` 就能找到;
    只有日志别的行(事件流上下文)才受级别影响。
    """
    import logging_setup
    import main

    root = logging.getLogger()
    if not any(isinstance(handler, logging.FileHandler) for handler in root.handlers):
        logging_setup.setup_logging(main.log_dir(), main.APP_NAME)  # 本用例没配日志时补上

    crash = getattr(report.longrepr, "reprcrash", None)
    detail = crash.message if crash is not None else str(report.longrepr).splitlines()[-1]
    logging.getLogger(main.APP_NAME).error(
        "event=test_failed nodeid=%s phase=%s detail=%s",
        item.nodeid,
        report.when,
        " ".join(detail.split()),  # 折成一行,维持"一行一个事件"
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """pytest 的结果钩子:失败就写一条 ERROR(见 `_log_test_failure`)。"""
    report = yield
    if report.failed:
        _log_test_failure(item, report)
    return report


@pytest.fixture
def app_log(tmp_path, monkeypatch):
    """初始化日志,并把**配置**目录隔离到 tmp。

    日志**不隔离**:按设计只有两份(打包后 / 其他),跑测试就写项目目录那份,跑完能直接看。
    代价是多个用例共用一个文件,所以断言一律用 `log_text()` / `events()` 按偏移量切 ——
    直接读整个文件会读到别的用例或你自己的 dev 运行写下的事件,造成假通过。

    还原 logging 状态是必须的:`setup_logging()` 用 `basicConfig(force=True)`,会顶掉 pytest
    自己的处理器;文件句柄也要关掉。
    """
    import logging_setup
    import main

    monkeypatch.setenv(main.CONFIG_DIR_ENV, str(tmp_path))  # 只影响 ini,日志不理会它
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")  # 测试默认看全部事件
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # 让被改写的钩子自动回滚

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    logging_setup.setup_logging(main.log_dir(), main.APP_NAME)
    yield main.log_path()

    for handler in root.handlers:
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    logging_setup.qInstallMessageHandler(None)
    logging_setup._qt_bridge = None


@pytest.fixture
def log_text(app_log):
    """读取"本用例开始之后"写入的日志内容(共用一个文件,必须按偏移量切)。"""
    from e2e_support import read_since

    since = app_log.stat().st_size if app_log.exists() else 0
    return lambda: read_since(app_log, since)


@pytest.fixture
def events(app_log):
    """事件行列表,同样只含本用例开始之后的;每次调用重新读,便于取最新那段。"""
    from e2e_support import read_events

    since = app_log.stat().st_size if app_log.exists() else 0
    return lambda: read_events(app_log, since)


@pytest.fixture
def scratch_log(app_log, tmp_path, monkeypatch):
    """给"只验证日志系统本身"的用例用:事件是**合成**出来的,写 scratch,不混进项目日志。

    项目那份只该留**真实事件**与失败记录,而 `event=demo`、`ValueError: boom` 这类行只是用例的
    副产品,没有诊断价值。真实行为用例(如 `test_e2e_ui.py`)仍写项目那一份。
    """
    from types import SimpleNamespace

    from e2e_support import read_since

    import logging_setup
    import main

    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")
    logging_setup.setup_logging(tmp_path, main.APP_NAME)  # 日志状态由 app_log 负责还原
    path = tmp_path / logging_setup.LOG_NAME
    return SimpleNamespace(path=path, text=lambda: read_since(path), root=tmp_path)
