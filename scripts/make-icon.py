"""生成 stabar.ico —— 托盘图标与 exe 文件图标共用这一份。

用法: uv run python .\\scripts\\make-icon.py

图案设计就写在本脚本里,是整个项目图标的唯一来源:
  - 程序运行时由 main.py 的 make_tray_icon() 直接读 stabar.ico 给托盘用
  - 打包时 stabar.spec 通过 EXE(icon=...) 把同一个文件嵌进 exe
改了图案要重新跑一次本脚本,然后重新打包。

为什么不直接 QImage.save("stabar.ico"):Qt 的 ICO 写入只支持单尺寸,
而 Windows 在资源管理器列表、任务栏、属性页等处会用到 16/32/48/256 等不同档位,
只塞一张图会糊。这里手写 ICO 容器,每一档放一张 PNG(Vista 起支持 PNG 条目)。
"""

import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap, Qt

ICON_NAME = "stabar.ico"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT_PATH = Path(__file__).resolve().parent.parent / ICON_NAME

# 与 main.py 里小组件的配色保持一致
PLATE_RGB = (28, 28, 30)
TRACK_RGB = (255, 255, 255)
ACCENT_RGB = (78, 154, 241)
BAR_FILLS = (0.78, 0.55)  # 两条进度条的填充比例,纯装饰


def draw_icon_pixmap(size: int) -> QPixmap:
    """按 64px 设计稿等比缩放,画一枚"深色底板 + 两条占用条"。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)

    unit = size / 64  # 以 64px 为基准等比缩放
    margin = round(4 * unit)
    side = size - margin * 2
    radius = 14 * unit
    painter.setBrush(QColor(*PLATE_RGB, 235))  # 深色底板
    painter.drawRoundedRect(margin, margin, side, side, radius, radius)

    bar_x = round(13 * unit)
    bar_w = round(38 * unit)
    bar_h = max(2, round(9 * unit))
    bar_radius = bar_h / 2
    for index, used in enumerate(BAR_FILLS):
        y = round((21 + index * 15) * unit)
        painter.setBrush(QColor(*TRACK_RGB, 60))  # 底槽
        painter.drawRoundedRect(bar_x, y, bar_w, bar_h, bar_radius, bar_radius)
        painter.setBrush(QColor(*ACCENT_RGB))  # 已用部分
        filled = max(bar_h, round(bar_w * used))
        painter.drawRoundedRect(bar_x, y, filled, bar_h, bar_radius, bar_radius)

    painter.end()
    return pixmap


def png_bytes(size: int) -> bytes:
    """把指定尺寸的图标渲染成 PNG 字节。"""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    draw_icon_pixmap(size).save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def build_ico(sizes: tuple[int, ...]) -> bytes:
    """按 ICO 格式拼装:6 字节头 + 每档 16 字节目录项 + 各档图像数据。"""
    images = [(size, png_bytes(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))  # 保留位, 类型=图标, 图像数

    entries = b""
    offset = 6 + 16 * len(images)
    payload = b""
    for size, blob in images:
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 宽(256 用 0 表示)
            size if size < 256 else 0,  # 高
            0,  # 调色板色数,真彩色填 0
            0,  # 保留位
            1,  # 色彩平面数
            32,  # 位深
            len(blob),
            offset,
        )
        offset += len(blob)
        payload += blob
    return header + entries + payload


def generate() -> int:
    _app = QGuiApplication([])  # 只为了能创建 QPixmap,不必进事件循环
    blob = build_ico(ICON_SIZES)
    OUT_PATH.write_bytes(blob)
    sizes = "/".join(str(size) for size in ICON_SIZES)
    print(f"已生成 {OUT_PATH}  {len(blob) / 1024:.1f} KB  ({len(ICON_SIZES)} 档:{sizes})")
    return 0


if __name__ == "__main__":
    raise SystemExit(generate())
