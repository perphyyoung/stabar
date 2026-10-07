"""日志:级别策略、落盘、事件行约定、超限清空、Qt 消息桥接与异常钩子。

这些是"打包后没有控制台"时的唯一排查手段,也是自动化测试的观察点,所以要当成功能测,
而不是当装饰。实现都在 logging_setup.py。

落点约定(见 `日志使用说明.md`):日志只有两份,跑测试写**项目目录**那一份。
本文件里**只验证日志系统本身**的用例改用 `scratch_log` —— 它们的事件(`event=demo`、
`ValueError: boom` 之类)是合成出来的,写进项目日志只会变成没有诊断价值的噪音。
"""

import logging
import sys
from types import SimpleNamespace

from PySide6.QtCore import QtMsgType

import logging_setup
import main


def _reinit(directory, monkeypatch, level: str) -> None:
    """按指定级别重新初始化日志(同一个文件,追加写)。"""
    monkeypatch.setenv(logging_setup.LOG_LEVEL_ENV, level)
    logging_setup.setup_logging(directory, main.APP_NAME)


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


def test_config_override_isolates_ini_but_not_log(tmp_path, monkeypatch) -> None:
    """`STABAR_CONFIG_DIR` 只隔离配置文件;日志固定"打包后 / 其他"两份,不跟着走。"""
    monkeypatch.setenv(main.CONFIG_DIR_ENV, str(tmp_path))
    assert main.settings_path() == tmp_path / main.SETTINGS_NAME
    assert main.log_dir() != tmp_path
    assert main.log_path() == main.log_dir() / logging_setup.LOG_NAME


def test_log_dir_frozen_is_user_config_dir(tmp_path, monkeypatch) -> None:
    """打包后日志与打包后的 ini 同目录(用假的 QStandardPaths,不碰真实 AppData)。"""
    fake = SimpleNamespace(
        AppConfigLocation="AppConfigLocation",
        writableLocation=lambda kind: str(tmp_path),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(main, "QStandardPaths", fake)
    assert main.log_dir() == tmp_path
    assert main.log_path() == tmp_path / logging_setup.LOG_NAME


def test_events_land_in_file_as_single_lines(scratch_log) -> None:
    """一行一个事件,且立刻可见(文件句柄逐条 flush),自动化测试才能边跑边读。"""
    main.log.info("event=demo state=ok")
    assert "event=demo state=ok" in scratch_log.text()


def test_level_filters_lower_severity(scratch_log, monkeypatch) -> None:
    """打包后默认 WARNING:INFO 不进文件,否则日志会天天被 15 秒一次的刷新灌满。"""
    _reinit(scratch_log.root, monkeypatch, "WARNING")
    main.log.info("event=dropped")
    main.log.warning("event=kept")

    text = scratch_log.text()
    assert "event=dropped" not in text
    assert "event=kept" in text


def test_oversized_log_is_cleared_not_rotated(scratch_log, monkeypatch) -> None:
    """超过上限是清空而不是轮转:路径永远只有一份,不产生历史文件、也不删文件。

    必须把文件推到上限才会触发清空,所以这条用例也写在 scratch 里 —— 在共享的项目日志上做,
    会把同一次运行里别的用例(包括**失败用例**)的证据一起清掉。
    """
    monkeypatch.setattr(logging_setup, "LOG_MAX_BYTES", 200)  # 不必真写满 1 MB
    _reinit(scratch_log.root, monkeypatch, "DEBUG")

    for i in range(20):
        main.log.info("event=filler seq=%d %s", i, "x" * 60)

    text = scratch_log.text()
    assert "event=log_cleared" in text  # 清空后留了标记
    assert "event=filler seq=19" in text  # 清空之后照常继续写
    assert scratch_log.path.exists()  # 文件还在,没有被删除
    assert not list(scratch_log.root.glob(f"{logging_setup.LOG_NAME}.*"))  # 没有 .1 历史文件


def test_qt_messages_are_bridged_and_folded(scratch_log) -> None:
    """Qt 的警告以上要收进来(打包后它们原本无处可去),debug/info 丢掉,多行折成一行。"""
    logging_setup._qt_bridge(QtMsgType.QtWarningMsg, None, "第一行\n第二行")
    logging_setup._qt_bridge(QtMsgType.QtDebugMsg, None, "太吵了")

    text = scratch_log.text()
    assert "event=qt 第一行 第二行" in text
    assert "太吵了" not in text


def test_offscreen_platform_noise_is_dropped(scratch_log) -> None:
    """离屏平台的专属噪音不进日志:它只说明平台能力不全,不说明程序有问题。

    测试跑在 `QT_QPA_PLATFORM=offscreen` 上(见 conftest),所以这几条会被丢掉;
    dev 与打包用真平台,同样的消息不会过滤。
    """
    logging_setup._qt_bridge(QtMsgType.QtWarningMsg, None, "This plugin does not support raise()")
    logging_setup._qt_bridge(QtMsgType.QtWarningMsg, None, "QFont::setPointSizeF: Point size <= 0")
    logging_setup._qt_bridge(QtMsgType.QtWarningMsg, None, "event=qt 这条是真警告,要留下")

    text = scratch_log.text()
    assert "does not support raise" not in text
    assert "setPointSizeF" not in text
    assert "这条是真警告,要留下" in text


def test_source_label_marks_the_writing_case(scratch_log, request) -> None:
    """跑测试时每行都带用例标签 —— 日志只有一份,没标签就分不清哪行是谁写的。

    生产路径不设标签,所以那时输出与以前完全一致(下面第二段就是验证这一点)。
    """
    main.log.info("event=labelled")
    assert f"[{request.node.name}]" in scratch_log.text()

    logging_setup.set_source_label("")
    main.log.info("event=unlabelled")
    labelled_line = next(
        line for line in scratch_log.text().splitlines() if "event=unlabelled" in line
    )
    assert "stabar: event=unlabelled" in labelled_line  # 没有方括号


def test_uncaught_exception_is_recorded(scratch_log, monkeypatch) -> None:
    """打包后 stderr 是 None,异常只能靠这一条留在日志里。"""
    _reinit(scratch_log.root, monkeypatch, "WARNING")
    sys.excepthook(ValueError, ValueError("boom"), None)

    text = scratch_log.text()
    assert "event=uncaught_exception" in text
    assert "boom" in text


def test_clamped_opacity_is_logged(app_log, scratch_log, qtbot, monkeypatch, low_usage) -> None:
    """夹取会静默改写你写的值,是"调了却不生效"的经典来源,必须在 WARNING 档留痕。

    这里同时验证 main 的告警确实落进了日志 —— 断言对象是"日志契约",所以留在这一层。
    """
    main.settings_path().write_text("[General]\nidle_opacity=0\n", encoding="utf-8")
    _reinit(scratch_log.root, monkeypatch, "WARNING")

    window = main.StatusBarWindow()
    qtbot.addWidget(window)

    assert f"event=config_clamped key=idle_opacity value=0.0 used={main.MIN_OPACITY}" in (
        scratch_log.text()
    )
