"""路径解析:配置与图标在不同运行方式下该落在哪。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import main


def test_settings_path_dev_stays_in_project(monkeypatch) -> None:
    """开发时留在项目目录,方便直接手改。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    expected = Path(main.__file__).with_name(main.SETTINGS_NAME)
    assert main.settings_path() == expected


def test_settings_path_frozen_uses_user_config(monkeypatch, tmp_path) -> None:
    """打包后必须换地方:onedir 的 __file__ 在 _internal 里,配置会藏进包内部、升级就丢。

    这里把 QStandardPaths 换成假的,免得测试真去用户 AppData 下建目录;
    被测的是"AppConfigLocation + 配置文件名"这个组合逻辑。
    """
    fake_paths = SimpleNamespace(
        AppConfigLocation="AppConfigLocation",
        writableLocation=lambda kind: str(tmp_path),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(main, "QStandardPaths", fake_paths)
    assert main.settings_path() == tmp_path / main.SETTINGS_NAME


def test_icon_path_dev_reads_project_file(monkeypatch) -> None:
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert main.icon_path() == Path(main.__file__).with_name(main.ICON_NAME)


def test_icon_path_frozen_reads_from_bundle(monkeypatch, tmp_path) -> None:
    """打包后图标从 PyInstaller 的解包目录读(spec 的 datas 负责把它带进去)。"""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert main.icon_path() == tmp_path / main.ICON_NAME
