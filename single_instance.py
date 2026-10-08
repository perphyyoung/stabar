"""单实例:同一份配置目录只允许一个进程;第二次启动去唤醒已有实例,然后自己退出。

两件事分开做,各用最合适的那一个:

- **认领(互斥)**:`QSharedMemory.create()` —— 同名只能成功一次,是**原子**的,不存在"两个同时
  启动都成功"的窗口。
  ⚠️ **这里不能用 `QLocalServer.listen()`**:实测 Windows 上同名 listen **不会失败** ——
  两个进程都能 listen(先起的、后起的都返回 True),Qt 会再建一个管道实例。
  第一版就是这么写的,用例一跑就抓到了,随后用两个进程复量确认:
  ```
  host   listen: True
  client listen: True      ← 这就是坑:listen 成功 ≠ 抢到了
  client connect: True     ← 能不能连上,才是"已有实例在跑"的可靠信号
  ```
- **唤醒**:`QLocalServer` / `QLocalSocket`(命名管道)—— 给已认领的那个进程送一条 "activate",
  让它把小组件显示出来。进程一结束管道就被系统回收,不像文件锁那样留下要手动清理的残留。

名字(共享内存 key 与管道名)都**按配置目录派生**(见 `instance_name`):E2E 与临时探测脚本都用
`STABAR_CONFIG_DIR` 指到临时目录,于是它们天然处在另一个命名域,不会跟用户正在跑的实例互锁 ——
我们的测试经常并行起实例。

两个必须知道的点:
1. **必须在 `setApplicationName()` 之后用**:配置目录是从应用名算出来的(见 `main._app_config_dir`);
2. 它用到 `QtNetwork`(本地命名管道,不支持也不需要使用 TLS),而 `stabar.spec` 以前把 QtNetwork
   整个裁掉了 —— 加了它就别再裁 `qt6network.dll`(体积多约 1 MB;OpenSSL/TLS 那几项仍然照裁)。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSharedMemory
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger("stabar")

ACTIVATE_MESSAGE = b"activate"
SOCKET_TIMEOUT_MS = 500  # 连接/发送最多等这么久:别为了"叫醒已有实例"卡住启动
ALLOW_MULTIPLE_ENV = (
    "STABAR_ALLOW_MULTIPLE"  # 置 1 可故意开多份(排查 / 对比用;也是被锁住时的逃生口)
)


def _digest(config_dir: Path) -> str:
    """按配置目录派生名字 —— 同一份配置只允许一个进程。"""
    key = str(config_dir.resolve()).casefold().encode("utf-8")
    return hashlib.sha256(key).hexdigest()[:16]


def claim_key(config_dir: Path) -> str:
    """共享内存的 key(互斥用)。"""
    return f"stabar-claim-{_digest(config_dir)}"


def instance_name(config_dir: Path) -> str:
    """命名管道的名字(唤醒用)。"""
    return f"stabar-wake-{_digest(config_dir)}"


class SingleInstance:
    """单实例闸门。

    用法(见 `main()`)::

        single = SingleInstance(config_dir())
        if not single.acquire():        # 已有实例:已经通知它显示小组件
            return 0                    # 自己安静退出
        ...
        single.activated(window.reveal)  # 建好窗口之后再挂回调
    """

    def __init__(self, config_dir: Path) -> None:
        self.claim_key = claim_key(config_dir)
        self.name = instance_name(config_dir)
        # 一直持有它:共享内存活到最后一个持有者退出(引用被回收就会提前释放)
        self._claim = QSharedMemory(self.claim_key)
        self._server = QLocalServer()
        self._callback: Callable[[], None] | None = None

    def acquire(self) -> bool:
        """认领这个名字:认领成功就是唯一实例;否则通知已有实例并返回 False。"""
        if os.environ.get(ALLOW_MULTIPLE_ENV) == "1":
            log.debug("event=single_instance role=primary reason=allow_multiple")
            return True
        if self._claim.create(1):  # 原子:同名只会成功一次
            self._listen()  # 只有认领到的那个才监听唤醒管道
            log.debug("event=single_instance role=primary name=%s", self.name)
            return True
        self._notify()  # 已有实例:叫它一声,然后调用方自己退出
        return False

    def activated(self, callback: Callable[[], None]) -> None:
        """注册"第二个实例来了"时要做什么(建好窗口之后再挂)。"""
        self._callback = callback

    def _listen(self) -> None:
        if not self._server.listen(self.name):
            # 认领到了却监听不上:唤醒功能失效,但不该因此不让程序启动
            log.warning(
                "event=single_instance role=primary reason=listen_failed name=%s", self.name
            )
            return
        self._server.newConnection.connect(self._on_connection)

    def _notify(self) -> None:
        """告诉已有实例"又被启动了一次" —— 尽力而为:送不到只记日志,不挡自己退出。"""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(SOCKET_TIMEOUT_MS):
            # 对方还没开始转事件循环(比如它正在启动)时收不到:不该因此卡住,更不该去抢名字
            log.warning("event=single_instance role=secondary notified=0 name=%s", self.name)
            return
        socket.write(ACTIVATE_MESSAGE)
        if socket.waitForBytesWritten(SOCKET_TIMEOUT_MS):
            log.debug("event=single_instance role=secondary notified=1")
        else:
            log.warning("event=single_instance role=secondary notified=0 reason=write_timeout")
        socket.disconnectFromServer()

    def _on_connection(self) -> None:
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda: self._on_message(socket))

    def _on_message(self, socket: QLocalSocket) -> None:
        if bytes(socket.readAll().data()) != ACTIVATE_MESSAGE:
            return
        log.info("event=single_instance action=activate")
        if self._callback is not None:
            self._callback()
        socket.disconnectFromServer()
