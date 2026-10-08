"""第 2 层:单实例闸门 —— 进程内就够,不必真起进程。

这里能钉的是**认领语义**(同名的第二个认领不到、名字按配置目录派生、逃逸口);而"通知真的送到、
已有实例真的被唤醒"需要两边的事件循环同时在转 —— 单进程里做不到(feign 不出来),那条属于 B 层:
`tests/test_e2e_real_input.py::test_second_launch_wakes_the_running_instance`。

⚠️ 认领用的是 `QSharedMemory.create()`(原子)。**别改成 `QLocalServer.listen()`**:实测 Windows 上
同名 listen 两个进程都会成功(都会返回 True),拿它做互斥等于没做 —— 第一版就是这么写的,
下面第一条用例立刻抓到,随后用两个真进程复量确认(见 `single_instance.py` 开头的实测记录)。
"""

import single_instance


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
