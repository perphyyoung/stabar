"""日志:级别策略、落盘、事件行约定、超限清空、Qt 消息桥接与异常钩子。

这些是"打包后没有控制台"时的唯一排查手段,也是自动化测试的观察点,所以要当成功能测,
而不是当装饰。实现都在 logging_setup.py。
"""

import logging
import sys

import pytest
from PySide6.QtCore import QtMsgType

import logging_setup
import main


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """把配置与日志目录引到 tmp,并在结束后还原 logging 状态。

    还原是必须的:setup_logging() 用的是 basicConfig(force=True),会顶掉 pytest 自己的
    处理器;文件句柄也要关掉,否则 Windows 上 tmp 目录删不掉。
    """
    monkeypatch.setenv(main.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # 让被改写的钩子自动回滚
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield tmp_path / logging_setup.LOG_NAME
    for handler in root.handlers:
        if handler not in saved_handlers:
            handler.close()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    logging_setup.qInstallMessageHandler(None)
    logging_setup._qt_bridge = None


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def test_default_level_follows_running_mode(monkeypatch) -> None:
    """没设环境变量时:源码运行 INFO,打包后 WARNING。"""
    monkeypatch.delenv(logging_setup.LOG_LEVEL_ENV, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert logging_setup.resolve_level() == logging_setup.LOG_LEVEL_DEV

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert logging_setup.resolve_level() == logging_setup.LOG_LEVEL_FROZEN


def test_env_overrides_level_ignoring_case(monkeypatch) -> None:
    """自动化测试靠 STABAR_LOG_LEVEL=DEBUG 拿到可断言的事件流。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "debug")
    assert logging_setup.resolve_level() == logging.DEBUG

    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, " warning ")
    assert logging_setup.resolve_level() == logging.WARNING


def test_invalid_level_falls_back_instead_of_crashing(monkeypatch) -> None:
    """级别名写错不能把程序拦住,退回默认值即可。"""
    monkeypatch.delenv(logging_setup.LOG_LEVEL_ENV, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "tracing")
    assert logging_setup.resolve_level() == logging_setup.LOG_LEVEL_DEV


def test_config_dir_override_moves_both_files(tmp_path, monkeypatch) -> None:
    """E2E 的隔离手段:配置与日志一起搬走,不碰真实文件;打包后也要能被覆盖。"""
    monkeypatch.setenv(main.CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert main.settings_path() == tmp_path / main.SETTINGS_NAME
    assert main.log_path() == tmp_path / logging_setup.LOG_NAME


def test_events_land_in_file_as_single_lines(log_file, monkeypatch) -> None:
    """一行一个事件,且立刻可见(文件句柄逐条 flush),自动化测试才能边跑边读。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")
    logging_setup.setup_logging(log_file.parent, main.APP_NAME)
    main.log.info("event=demo state=ok")

    text = _read(log_file)
    assert "event=demo state=ok" in text
    assert "event=start" not in text  # 只有 main() 才记启动,测试里不该凭空出现


def test_level_filters_lower_severity(log_file, monkeypatch) -> None:
    """打包后默认 WARNING:INFO 不进文件,否则日志会天天被 15 秒一次的刷新灌满。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "WARNING")
    logging_setup.setup_logging(log_file.parent, main.APP_NAME)
    main.log.info("event=dropped")
    main.log.warning("event=kept")

    text = _read(log_file)
    assert "event=dropped" not in text
    assert "event=kept" in text


def test_oversized_log_is_cleared_not_rotated(log_file, monkeypatch) -> None:
    """超过上限是清空而不是轮转:路径永远只有一份,不产生历史文件、也不删文件。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")
    monkeypatch.setattr(logging_setup, "LOG_MAX_BYTES", 200)  # 不必真写满 1 MB
    logging_setup.setup_logging(log_file.parent, main.APP_NAME)

    for i in range(20):
        main.log.info("event=filler seq=%d %s", i, "x" * 60)

    text = _read(log_file)
    assert "event=log_cleared" in text  # 清空后留了标记
    assert "event=filler seq=19" in text  # 清空之后照常继续写
    assert log_file.exists()  # 文件还在,没有被删除
    assert not list(log_file.parent.glob(f"{logging_setup.LOG_NAME}.*"))  # 没有 .1 之类的历史文件


def test_qt_messages_are_bridged_and_folded(log_file, monkeypatch) -> None:
    """Qt 的警告以上要收进来(打包后它们原本无处可去),debug/info 丢掉,多行折成一行。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "DEBUG")
    logging_setup.setup_logging(log_file.parent, main.APP_NAME)
    logging_setup._qt_bridge(QtMsgType.QtWarningMsg, None, "第一行\n第二行")
    logging_setup._qt_bridge(QtMsgType.QtDebugMsg, None, "太吵了")

    text = _read(log_file)
    assert "event=qt 第一行 第二行" in text
    assert "太吵了" not in text


def test_uncaught_exception_is_recorded(log_file, monkeypatch) -> None:
    """打包后 stderr 是 None,异常只能靠这一条留在日志里。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "WARNING")
    logging_setup.setup_logging(log_file.parent, main.APP_NAME)
    sys.excepthook(ValueError, ValueError("boom"), None)

    text = _read(log_file)
    assert "event=uncaught_exception" in text
    assert "boom" in text


def test_clamped_opacity_is_logged(log_file, qtbot, monkeypatch, low_usage) -> None:
    """夹取会静默改写你写的值,是"调了却不生效"的经典来源,必须在 WARNING 档留痕。"""
    main.settings_path().write_text("[General]\nidle_opacity=0\n", encoding="utf-8")
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, "WARNING")
    logging_setup.setup_logging(log_file.parent, main.APP_NAME)

    window = main.StatusBarWindow()
    qtbot.addWidget(window)

    assert f"event=config_clamped key=idle_opacity value=0.0 used={main.MIN_OPACITY}" in _read(
        log_file
    )
