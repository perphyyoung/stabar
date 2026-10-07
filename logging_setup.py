"""日志:级别策略、事件行格式、落盘、Qt 消息桥接与异常钩子。

从 main.py 拆出来,以后日志相关的改动只改这个文件。三条要点:

1. **落盘是主线。** 打包成 `--noconsole` 后 `sys.stderr` 是 `None`(实测),
   `print(..., file=sys.stderr)` 会静默消失,文件是唯一的出口。
2. **级别**:环境变量 `STABAR_LOG_LEVEL` 优先,否则打包 WARNING、源码 INFO。
3. **超过上限是"清空"而不是轮转**:日志路径永远只有一份、永远是最新的,
   既不产生历史文件,也不会因为重命名被别的进程占着而失败。

事件行统一写成 `event=<名词> key=value ...`,一行一个事件,便于 grep 与断言;
完整的事件清单见 日志使用说明.md。
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from pathlib import Path

from PySide6.QtCore import QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QGuiApplication

# --- 级别 ---
LOG_LEVEL_ENV = "STABAR_LOG_LEVEL"  # 覆盖默认级别,大小写不敏感
LOG_LEVEL_FROZEN = logging.WARNING  # 打包后只记警告以上:日常使用日志文件通常是空的
LOG_LEVEL_DEV = logging.INFO  # 源码运行:记关键动作,不含每 15 秒一次的磁盘读数

# --- 文件 ---
LOG_NAME = "stabar.log"
LOG_MAX_BYTES = 1_000_000  # 超过就清空重写(不轮转、不留历史文件)
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(process)d] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

_logger: logging.Logger | None = None  # setup_logging 记住它,供两个钩子使用
_qt_bridge = None  # Qt 的消息回调必须被引用住,否则会被回收、回调悄悄失效


class TruncatingFileHandler(logging.FileHandler):
    """超过上限就清空重写。

    刻意不用 `RotatingFileHandler`:轮转会把当前文件改名成 `stabar.log.1`,于是
    `stabar.log` 出现历史断层,看日志的人会以为丢了内容;而且重命名在别的进程正读着
    (比如 `Get-Content -Wait`)时可能直接失败。清空只写文件、不改名字。
    """

    def __init__(self, filename, max_bytes: int, encoding: str = "utf-8") -> None:
        super().__init__(filename, mode="a", encoding=encoding, delay=False)
        self._max_bytes = max_bytes

    def emit(self, record: logging.LogRecord) -> None:
        self._clear_if_full(record.name)
        super().emit(record)

    def _clear_if_full(self, logger_name: str) -> None:
        if self._max_bytes <= 0:
            return
        try:
            if os.path.getsize(self.baseFilename) < self._max_bytes:
                return
        except OSError:  # 文件被外部删掉之类,后续交给 FileHandler 自己重建
            return

        self.acquire()
        try:
            self.close()  # 只关句柄,不删文件
            self.mode = "w"  # 以覆盖模式重开 = 清空
            self.stream = self._open()
            self.mode = "a"
        finally:
            self.release()
        # 清空后补一行标记,否则"日志怎么只有半截"会让人怀疑程序出了问题。
        # 这一条走 handle() 走完整流程;此时文件是空的,不会再触发清空。
        self.handle(
            logging.LogRecord(
                logger_name,
                logging.WARNING,
                __file__,
                0,
                "event=log_cleared max_bytes=%s",
                (self._max_bytes,),
                None,
            )
        )


def log_path(directory: Path) -> Path:
    """日志文件位置:与配置文件同目录,便于一起备份 / 清理。"""
    return directory / LOG_NAME


def resolve_level() -> int:
    """决定日志级别:环境变量优先,否则打包 WARNING、源码 INFO。

    自动化测试靠 `STABAR_LOG_LEVEL=DEBUG` 拿到可断言的事件流;名字写错时退回默认值,
    不因为一个环境变量把程序拦住。
    """
    levels = logging.getLevelNamesMapping()
    name = os.environ.get(LOG_LEVEL_ENV, "").strip().upper()
    if name in levels:
        return levels[name]
    return LOG_LEVEL_FROZEN if getattr(sys, "frozen", False) else LOG_LEVEL_DEV


# 记录的"来源标签":跑测试时由 tests/conftest.py 设成用例名,这样**共享一份日志**时也能看出
# 每行是谁写的。生产路径从不设置它,标签为空时输出与以前逐字符一致。
_source_label: ContextVar[str] = ContextVar("stabar_source_label", default="")


def set_source_label(label: str) -> None:
    """给之后的日志记录打上来源标签(测试用;传空串即取消)。"""
    _source_label.set(label)


class _LabelledFormatter(logging.Formatter):
    """把来源标签插进 logger 名后面:`stabar[test_menu_hide_really_hides]: event=...`。"""

    def format(self, record: logging.LogRecord) -> str:
        label = _source_label.get()
        if not label:
            return super().format(record)
        original = record.name
        record.name = f"{original}[{label}]"
        try:
            return super().format(record)
        finally:
            record.name = original  # 同一条记录还要给别的 handler 用,用完还原


def setup_logging(directory: Path, logger_name: str) -> int:
    """初始化日志,返回实际生效的级别。

    调用方要保证两件事:目录已经算好(打包模式下它依赖 Qt 的应用名,见
    `main.config_dir()`),且调用发生在 `setApplicationName()` 之后。
    """
    global _logger
    _logger = logging.getLogger(logger_name)

    level = resolve_level()
    formatter = _LabelledFormatter(LOG_FORMAT, LOG_DATEFMT)
    handlers: list[logging.Handler] = []
    problem = ""

    try:
        file_handler = TruncatingFileHandler(log_path(directory), LOG_MAX_BYTES)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as error:
        problem = f"{log_path(directory)}: {error}"  # 目录不可写也不能让程序起不来

    if sys.stderr is not None:
        # 打包成 --noconsole 时它是 None,加了会在每条日志上抛异常
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    if not handlers:
        handlers.append(logging.NullHandler())

    logging.basicConfig(level=level, handlers=handlers, force=True)

    if problem:
        _logger.warning("event=log_unavailable path=%s", problem)
    _install_qt_bridge()
    _install_excepthook()
    return level


def _install_qt_bridge() -> None:
    """把 Qt 自己发出的警告以上消息也收进日志。

    这类问题(加载不到平台插件、图标解码失败)恰恰只在打包后出现,而打包后没有控制台,
    不接进来就完全看不到。Qt 自身的 debug / info 太吵,丢掉;离屏平台的专属噪音也丢掉
    (见 `_OFFSCREEN_NOISE`)。
    """
    global _qt_bridge

    def bridge(mode, context, message) -> None:
        if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
            return
        text = " ".join(str(message).split())  # 折成单行,维持"一行一个事件"
        if _is_offscreen_noise(text):
            return
        level = {
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }.get(mode, logging.WARNING)
        logging.getLogger("qt").log(level, "event=qt %s", text)

    _qt_bridge = bridge
    qInstallMessageHandler(bridge)


# 离屏平台(跑测试时才用)的专属噪音:它不支持 raise() / 抓键盘,也没有字体目录,于是每次 show()、
# 每次弹菜单都刷一串警告。这些只说明"这个平台能力不全",不说明程序有问题,所以不记。
# **只在离屏平台过滤** —— dev 与打包用真平台,那时任何 Qt 警告都要留下。
_OFFSCREEN_NOISE = (
    "This plugin does not support",  # raise() / grabbing the keyboard
    "Cannot find font directory",
    "QFont::setPointSizeF: Point size <= 0",
)


def _is_offscreen_noise(message: str) -> bool:
    """这条 Qt 消息是不是离屏平台的专属噪音。"""
    if not any(pattern in message for pattern in _OFFSCREEN_NOISE):
        return False
    return QGuiApplication.platformName() == "offscreen"


def _install_excepthook() -> None:
    """未捕获异常(含 Qt 槽里抛出的)写进日志。

    不接管的话异常只走 stderr,而打包后 stderr 是 None —— 崩了什么都不剩。
    """

    def hook(exc_type, exc_value, traceback) -> None:
        logger = _logger or logging.getLogger(__name__)
        logger.critical("event=uncaught_exception", exc_info=(exc_type, exc_value, traceback))

    sys.excepthook = hook
