"""第 2 层:单实例闸门 —— 进程内就够,不必真起进程。

这里能钉的是**认领语义**(同名的第二个认领不到、名字按配置目录派生、逃逸口)与**唤醒消息的
读取方式**(同步 + 有界超时,不依赖 `readyRead`;实测记录见 `single_instance._read_activation`)。
而"通知真的送到、已有实例真的被唤醒"需要两边的事件循环同时在转 —— 单进程里做不到,那条属于
B 层:`tests/test_e2e_real_input.py::test_second_launch_wakes_the_running_instance`。

⚠️ 认领用的是 `QSharedMemory.create()`(原子)。**别改成 `QLocalServer.listen()`**:实测 Windows 上
同名 listen 两个进程都会成功(都会返回 True),拿它做互斥等于没做 —— 第一版就是这么写的,
下面第一条用例立刻抓到,随后用两个真进程复量确认(见 `single_instance.py` 开头的实测记录)。
"""

import logging

from PySide6.QtCore import QByteArray

import single_instance


class _FakeSocket:
    """假 socket:按 `_read_activation` 用到的接口造,可摆出三种数据到达时序。"""

    def __init__(self, buffered: int, arrives_after_wait: bool) -> None:
        self._buffered = buffered
        self._arrives_after_wait = arrives_after_wait
        self.waits: list[int] = []
        self.disconnected = 0
        self.deleted = 0

    # 以下方法名照 Qt 的驼峰(测试故意的),对齐 `_read_activation` 的调用面
    def bytesAvailable(self) -> int:  # noqa: N802
        return self._buffered

    def readAll(self) -> QByteArray:  # noqa: N802
        if self._buffered:
            self._buffered = 0
            return QByteArray(single_instance.ACTIVATE_MESSAGE)
        return QByteArray(b"")

    def waitForReadyRead(self, msecs: int) -> bool:  # noqa: N802
        self.waits.append(msecs)
        if self._arrives_after_wait:
            self._buffered = len(single_instance.ACTIVATE_MESSAGE)
            self._arrives_after_wait = False
            return True
        return False

    def disconnectFromServer(self) -> None:  # noqa: N802
        self.disconnected += 1

    def deleteLater(self) -> None:  # noqa: N802
        self.deleted += 1


class _FakeServer:
    """假 server:让 `_on_connection` 的"清空队列"循环可以拿假 socket。"""

    def __init__(self, sockets: list[_FakeSocket]) -> None:
        self._queue = list(sockets)

    def hasPendingConnections(self) -> bool:  # noqa: N802
        return bool(self._queue)

    def nextPendingConnection(self) -> _FakeSocket:  # noqa: N802
        return self._queue.pop(0)


def _gate(tmp_path) -> tuple[single_instance.SingleInstance, list[bool]]:
    gate = single_instance.SingleInstance(tmp_path)
    woken: list[bool] = []
    gate.activated(lambda: woken.append(True))
    return gate, woken


def test_activation_already_buffered_is_read_immediately(tmp_path) -> None:
    """对端写得快、数据在拿到 socket 时已经躺好:**当场读出**并回调,不靠任何信号。"""
    gate, woken = _gate(tmp_path)

    socket = _FakeSocket(buffered=len(single_instance.ACTIVATE_MESSAGE), arrives_after_wait=False)
    gate._read_activation(socket)

    assert woken == [True]
    assert socket.waits == []  # 数据已在:一次等待都不需要
    assert (socket.disconnected, socket.deleted) == (1, 1)


def test_activation_arriving_later_is_waited_for(tmp_path) -> None:
    """数据晚到:同步等(有界)再读 —— `readyRead` 可能永远不来,不能指望它。"""
    gate, woken = _gate(tmp_path)

    socket = _FakeSocket(buffered=0, arrives_after_wait=True)
    gate._read_activation(socket)

    assert woken == [True]
    assert socket.waits  # 等过至少一次
    assert (socket.disconnected, socket.deleted) == (1, 1)


def test_activation_never_arriving_logs_a_warning(tmp_path, caplog) -> None:
    """消息等不到:**记警告**(不再静默丢),回调不触发,socket 收尾干净。"""
    gate, woken = _gate(tmp_path)

    socket = _FakeSocket(buffered=0, arrives_after_wait=False)
    with caplog.at_level(logging.WARNING, logger="stabar"):
        gate._read_activation(socket)

    assert woken == []
    assert any("state=unreadable" in record.message for record in caplog.records)
    assert socket.disconnected == 1
    assert socket.deleted == 1


def test_pending_connections_are_drained(tmp_path) -> None:
    """队列里有几条就处理几条:只取一条的话,残留的连接在某些平台不再发 `newConnection`。"""
    gate, woken = _gate(tmp_path)

    sockets = [
        _FakeSocket(buffered=len(single_instance.ACTIVATE_MESSAGE), arrives_after_wait=False)
        for _ in range(2)
    ]
    gate._server = _FakeServer(sockets)  # type: ignore[assignment]
    gate._on_connection()

    assert woken == [True, True]  # 两条连接都被读出
    assert sockets[0].disconnected == 1 and sockets[1].disconnected == 1


def test_second_instance_cannot_claim(qtbot, tmp_path) -> None:
    """同一个配置目录:第一个认领成功,第二个认领不到(于是它会通知并退出)。"""
    first = single_instance.SingleInstance(tmp_path)
    assert first.acquire() is True

    second = single_instance.SingleInstance(tmp_path)
    assert second.acquire() is False  # 不是唯一实例 —— 返回值与"通知是否送达"无关


def test_name_is_per_config_dir(tmp_path) -> None:
    """不同配置目录互不干扰 —— E2E 与探测脚本靠这条与用户正在跑的实例隔离。"""
    other = tmp_path / "other"
    other.mkdir()
    assert single_instance.instance_name(tmp_path) != single_instance.instance_name(other)
    assert single_instance.claim_key(tmp_path) != single_instance.claim_key(other)
    assert single_instance.instance_name(tmp_path) == single_instance.instance_name(tmp_path / ".")


def test_allow_multiple_escape_hatch(qtbot, tmp_path, monkeypatch) -> None:
    """`STABAR_ALLOW_MULTIPLE=1` 时不做互斥(排查 / 对比两份实例用,也是被锁住时的逃生口)。"""
    assert single_instance.SingleInstance(tmp_path).acquire() is True
    monkeypatch.setenv(single_instance.ALLOW_MULTIPLE_ENV, "1")
    assert single_instance.SingleInstance(tmp_path).acquire() is True
