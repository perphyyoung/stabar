"""路径与版本:配置 / 日志 / 图标落在哪,以及本程序的版本号。

三种运行形态的落点不同(源码运行留在项目目录、打包后落到用户目录、自动化测试用环境变量引走),
判断集中在这里 —— 别处不要再自己算 `__file__` / `sys.frozen` / `_MEIPASS`。
"""

from __future__ import annotations

import importlib.metadata
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices

import logging_setup

log = logging.getLogger("stabar")

APP_NAME = "stabar"  # 决定打包后配置目录的名字;也是日志、单实例、注册表项的名字
ICON_NAME = "stabar.ico"  # 托盘图标与 exe 文件图标共用这一份
SETTINGS_NAME = "stabar.ini"  # 实际位置见 settings_path(),开发与打包后不同
# 覆盖配置与日志目录(日志实现见 logging_setup.py),自动化测试靠它做隔离
CONFIG_DIR_ENV = "STABAR_CONFIG_DIR"


def _app_config_dir() -> Path:
    """打包后的用户目录(%LOCALAPPDATA%\\stabar)。

    依赖 Qt 的应用名,所以调用方必须等 `setApplicationName(APP_NAME)` 之后再用 ——
    应用名没设时 AppConfigLocation 会退化成 %LOCALAPPDATA% 本身(实测)。
    """
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    directory = Path(base) if base else Path.home() / f".{APP_NAME}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_dir() -> Path:
    """**配置文件**所在目录,优先级从高到低:

    1. 环境变量 `STABAR_CONFIG_DIR` —— 自动化测试用:把 ini 引到临时目录,不碰你真实的配置;
    2. 打包运行 -> %LOCALAPPDATA%\\stabar;
    3. 源码运行 -> 项目目录,方便直接手改。

    开发时留在项目目录是为了方便直接手改;打包后不能再写 exe 旁边,原因有两个:
    一是 onedir 的 __file__ 指向 _internal 子目录,配置会藏进包内部、升级就丢;
    二是装到 Program Files 之类的路径下可能根本没有写权限。
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        directory = Path(override)
    elif getattr(sys, "frozen", False):  # PyInstaller 打包运行时的标志
        directory = _app_config_dir()
    else:
        directory = Path(__file__).parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def log_dir() -> Path:
    """**日志**所在目录,**只有两种**(见 `日志使用说明.md`):

    - 打包运行 -> 用户配置目录(%LOCALAPPDATA%\\stabar),和打包后的 ini 同目录;
    - 其他情况(源码运行、跑测试)-> 项目目录。

    刻意**不理会** `STABAR_CONFIG_DIR`:那个变量只用来隔离**配置**。日志就两份,测试直接写
    项目目录这份,跑完能立刻看;代价是多个用例共用一个文件,所以读日志必须按偏移量切
    (见 `tests/e2e_support.read_since`)。
    """
    if getattr(sys, "frozen", False):
        return _app_config_dir()
    return Path(__file__).parent


def app_version() -> str:
    """版本号:**唯一来源是 `pyproject.toml` 的 `[project] version`**,这里只负责把它读出来。

    走标准库的 `importlib.metadata` —— 它读的是"已安装分发"的元数据:源码形态靠 `uv sync` 把本工程
    装成可编辑包,打包形态靠 `stabar.spec` 里的 `copy_metadata`(PyInstaller 官方做法)把那小份
    `dist-info` 带进 `_internal/`。**不必把 `pyproject.toml` 塞进包里**,代码里也不另抄一份。
    没有元数据(比如直接 `python main.py`、没同步过环境)就返回 `unknown` 并留一条日志。
    """
    try:
        return importlib.metadata.version(APP_NAME)
    except importlib.metadata.PackageNotFoundError:
        log.warning("event=version_unavailable hint=环境里没有本工程的分发,先 uv sync")
        return "unknown"


def settings_path() -> Path:
    """配置文件位置。"""
    return config_dir() / SETTINGS_NAME


def log_path() -> Path:
    """日志文件位置。"""
    return logging_setup.log_path(log_dir())


def icon_path() -> Path:
    """图标文件位置:开发时在项目目录,打包后在 PyInstaller 的解包目录。

    打包后靠 stabar.spec 的 datas 把 .ico 带进 _internal,所以这里用 sys._MEIPASS 定位。
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / ICON_NAME
    return Path(__file__).with_name(ICON_NAME)


def entry_script() -> Path:
    """开发形态下自启要跑的那个脚本:项目目录里的 `main.py`(打包后不用它)。

    单独抽出来是因为写注册表的那段代码在 `tray.py` —— 如果那里用 `Path(__file__)`,
    自启会去启动 `tray.py`,那不是入口。
    """
    return Path(__file__).with_name("main.py")


def open_path(path: Path) -> None:
    """用系统默认程序打开一个路径(这里只用来开目录)。

    单独一层是为了**能替换**:测试里换成替身,免得真弹资源管理器(与 `winreg` 那套注入同一个思路)。
    目录不存在就先建出来 —— 首次运行就点开时(还没写过任何文件)它可能真的还不存在。
    """
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    log.debug("event=open_dir path=%s", directory)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
