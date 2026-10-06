"""临时基准脚本:拆开统计刷新成本。用完即删。"""

import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

# 脚本位于 scripts/ 下,要 import 项目根目录的 main.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main

app = QApplication(sys.argv)
window = main.StatusBarWindow()
window.show()
app.processEvents()


def bench(label: str, fn, n: int = 30) -> None:
    fn()  # 预热
    t0 = time.process_time()
    for _ in range(n):
        fn()
    cpu = (time.process_time() - t0) * 1000
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    wall = (time.perf_counter() - t0) * 1000
    print(f"{label}: CPU {cpu / n:.3f} ms/次, 墙钟 {wall / n:.3f} ms/次")


bench("只读磁盘数据(两个盘符)", lambda: (main.read_disk("C:/"), main.read_disk("D:/")))
bench("refresh() 本身", window.refresh)
bench("refresh() + 事件循环重绘", lambda: (window.refresh(), app.processEvents()))
print("BENCH_OK")
