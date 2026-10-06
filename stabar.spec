# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格:onedir 打包 + 裁剪未使用的 Qt 组件。

用法: uv run pyinstaller --noconfirm --clean stabar.spec

为什么需要这个文件: PySide6 的官方钩子会把 PySide6 包目录里的 DLL 成批收进来,
其中大部分(Quick/Qml/Pdf/Network/OpenGL/Svg、软件 OpenGL 回退、Qt 翻译、
图片格式与网络相关插件)我们一个都用不到。本组件只用 QtCore / QtGui / QtWidgets
加 Qt 自绘,所以这里把它们显式剔掉,体积可以从 111MB 降到 50MB 量级。
"""

import os

# --- 用不到的 PySide6 模块:在导入分析阶段就排除 ---
PYSIDE_EXCLUDES = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtXml",
    "PySide6.QtDBus",
    "PySide6.QtConcurrent",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtDesigner",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtCharts",
    "PySide6.QtMultimedia",
    "PySide6.QtStateMachine",
    "PySide6.QtScxml",
    "PySide6.QtSerialPort",
    "PySide6.QtPositioning",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtTextToSpeech",
    "PySide6.QtRemoteObjects",
    "PySide6.QtSensors",
    "PySide6.QtSpatialAudio",
]

# --- 用不到的标准库,顺手减一点 ---
STDLIB_EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc",
    "lib2to3",
    "distutils",
    "setuptools",
    "pip",
    "sqlite3",
    "curses",
    "xmlrpc",
]

# --- 钩子会无条件收集的二进制 / 数据文件,按文件名剔除 ---
DROP_BASENAMES = {
    # 用不到的 Qt 模块 DLL
    "qt6quick.dll",
    "qt6qml.dll",
    "qt6qmlmodels.dll",
    "qt6qmlmeta.dll",
    "qt6qmlworkerscript.dll",
    "qt6pdf.dll",
    "qt6network.dll",
    "qt6opengl.dll",
    "qt6svg.dll",
    "qt6virtualkeyboard.dll",
    "qtnetwork.pyd",
    # Mesa 软件 OpenGL 回退:本组件只用 QPainter 光栅绘制,不碰 OpenGL
    "opengl32sw.dll",
    # 只被 Qt6Network 拉进来的 OpenSSL
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    # 平台插件:只保留 qwindows.dll
    "qdirect2d.dll",
    "qoffscreen.dll",
    "qminimal.dll",
    # 图片格式插件:界面没有任何图片,所以只留 qico.dll ——
    # 托盘图标是 .ico,Qt 靠它解码;删掉的话 QIcon 会静默返回空图标,托盘直接不出现
    "qgif.dll",
    "qicns.dll",
    "qjpeg.dll",
    "qpdf.dll",
    "qsvg.dll",
    "qtga.dll",
    "qtiff.dll",
    "qwbmp.dll",
    "qwebp.dll",
    # 图标引擎 / 触屏 / 网络信息 / 虚拟键盘 / TLS:依赖上面已剔除的模块
    "qsvgicon.dll",
    "qtuiotouchplugin.dll",
    "qnetworklistmanager.dll",
    "qtvirtualkeyboardplugin.dll",
    "qcertonlybackend.dll",
    "qopensslbackend.dll",
    "qschannelbackend.dll",
}
# 保留: qwindows.dll(Qt 唯一需要的平台插件)、qmodernwindowsstyle.dll(默认样式)


def _drop(name: str) -> bool:
    """按归一化后的路径判断某个产物是否要剔除。"""
    norm = name.replace("\\", "/").lower()
    if os.path.basename(norm) in DROP_BASENAMES:
        return True
    # Qt 翻译文件(.qm),我们所有文字都是 QPainter 自绘,不用 Qt 标准对话框
    return "/pyside6/translations/" in f"/{norm}"


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # 托盘图标运行时读这个文件,所以要一起打进包里(_internal/stabar.ico)
    datas=[(os.path.join(SPECPATH, "stabar.ico"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=PYSIDE_EXCLUDES + STDLIB_EXCLUDES,
    noarchive=False,
)

_before = len(a.binaries) + len(a.datas)
a.binaries = [b for b in a.binaries if not _drop(b[0])]
a.datas = [d for d in a.datas if not _drop(d[0])]
print(
    f"[stabar.spec] 裁剪产物: {_before} -> "
    f"{len(a.binaries) + len(a.datas)} 项"
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="stabar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(SPECPATH, "stabar.ico"),  # 由 scripts/make-icon.py 生成
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="stabar",
)
