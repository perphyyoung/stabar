"""开机自启:命令拼装与注册表分支。全部用替身,不碰真实注册表。"""

import sys

import main
import paths


def test_command_dev_quotes_interpreter_and_script(monkeypatch) -> None:
    """开发时靠 pythonw 跑脚本(不弹控制台);两段路径都要带引号,否则含空格的目录会被切开。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = main.autostart_command()
    assert command.count('"') == 4
    assert command.startswith('"')
    assert "python" in command.lower()
    assert "main.py" in command


def test_command_frozen_is_the_exe_itself(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "stabar.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert main.autostart_command() == f'"{exe}"'


def test_registry_round_trip(monkeypatch, fake_registry) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    main.set_autostart(True)
    assert main.is_autostart_enabled()
    main.set_autostart(False)
    assert not main.is_autostart_enabled()
    main.set_autostart(False)  # 重复关闭不该报错(条目可能已被手动删掉)
    assert fake_registry.values == {}


def test_stale_path_is_not_treated_as_enabled(monkeypatch, fake_registry) -> None:
    """挪过目录之后,注册表里的旧路径不该算"已开启",否则勾选状态会骗人。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_registry.values[paths.APP_NAME] = '"D:\\old\\stabar.exe"'
    assert not main.is_autostart_enabled()
