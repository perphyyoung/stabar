"""托盘:图标 + 右键菜单(小组件唯一的稳定入口 —— 它可以调到几乎看不见)。

菜单是**自绘控件行**拼出来的(滑杆 / 步进器 / 复选框 / 按钮),所以这里也放着菜单常量
(行缩进 / 滑杆宽度 / 三行阈值);`MenuRow` 让控件行的高亮与纯文字项一致。开机自启(写用户注册表
的 Run 键)是菜单里的一行,入口也在本模块。窗口只提供状态,菜单不碰它的内部。

开机自启的注册项名字用的是 `paths.APP_NAME`,命令是"当前运行方式的路径"(见 `autostart_command`)。
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionMenuItem,
    QSystemTrayIcon,
    QWidget,
    QWidgetAction,
)

import dialogs
import diskbar
import layout
import paths

if TYPE_CHECKING:  # 只为类型标注:运行时由 main 建好窗口再传进来
    from main import StatusBarWindow

log = logging.getLogger("stabar")


def make_tray_icon() -> QIcon:
    """托盘图标:直接读 stabar.ico,与 exe 的文件图标同一份文件。"""
    icon = QIcon(str(paths.icon_path()))
    if icon.isNull():
        # .ico 要靠 Qt 的 qico.dll 插件解码,打包时漏掉就会走到这里:
        # QIcon 不报错、只返回空图标,表现为托盘图标不出现,所以显式喊一声。
        # 这条必须是日志而不是 print:打包后 stderr 是 None,打印出去就没了。
        log.warning("event=icon_missing path=%s hint=检查打包是否漏了 qico.dll", paths.icon_path())
    return icon


# --- 托盘菜单 ---
# 控件行(滑杆 / 开关 / 按钮)没有菜单项那段缩进,要自己补上左边距才能与其它行文字左对齐。
# 实测(抓图 + 像素扫描):菜单里**没有可勾选项**之后,纯文字项从面板边缘 10~11px 处开始;
# 控件行自己补 8 的左边距正好落在同一列(量出来 11)。左右取同一个值,两侧留白也就相等了。
MENU_ITEM_INDENT = 8
SLIDER_WIDTH = 96  # 滑杆长度:够细调即可,太长会把整个菜单撑宽
# 三行阈值步进器:(行标签, action 名, hover 提示, 内部键)。这个顺序就是菜单里的顺序。
THRESHOLD_ROWS = (
    ("橙色阈值（%）", "橙色阈值", "占用到这条线变橙(只影响颜色)", "warn"),
    ("红色阈值（%）", "红色阈值", "占用到这条线变红;同时也会触发常亮", "danger"),
    ("常亮阈值（%）", "常亮阈值", "占用到这条线界面常亮(红色线也会触发)", "lit"),
)

# 托盘悬停提示的长度上限:Windows 的 NOTIFYICONDATA.szTip 是 128 个字符的缓冲,超出会被 shell
# 从中间截掉(实测:三块盘的完整明细约 140 字符,托盘里第三行只剩"可用 2.")。所以托盘用紧凑文字。
TRAY_TIP_MAX = 127

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command() -> str:
    """写进注册表的启动命令:打包后就是 exe 自身,开发时用 pythonw 跑 `main.py`。

    两头都给路径加引号,否则目录名带空格时会被系统当成多个参数切开。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    if not interpreter.exists():  # 少数环境没有 pythonw,退回 python(会带个控制台窗口)
        interpreter = Path(sys.executable)
    return f'"{interpreter}" "{paths.entry_script()}"'


def is_autostart_enabled() -> bool:
    """注册表里的命令和当前运行方式一致才算开启 —— 换过路径就不算。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _ = winreg.QueryValueEx(key, paths.APP_NAME)
    except OSError:
        return False
    return str(value).strip().casefold() == autostart_command().casefold()


def set_autostart(enabled: bool) -> None:
    """增删 Run 键;失败交给调用方处理(可能没有写权限或被组策略拦住)。"""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, paths.APP_NAME, 0, winreg.REG_SZ, autostart_command())
            return
        try:
            winreg.DeleteValue(key, paths.APP_NAME)
        except FileNotFoundError:
            pass


class MenuRow(QWidget):
    """托盘菜单里"控件行"的容器:自己画当前项底色。

    **实测**:菜单把当前项指到 `QWidgetAction` 那一行时,`QMenu` **不会**给它画高亮
    (纯文字项会:同一行左侧空白带的底色 30 → 43,而控件行纹丝不动)。所以想让控件行也高亮,
    只能自己画 —— 这里借用菜单项那套 style 控件来画,只画底色不画文字,于是换主题 / 换 DPI 时
    外观与纯文字项天然一致(而不是写死一个颜色)。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hovered = False

    def set_hovered(self, hovered: bool) -> None:
        """指针是否停在本行 —— 由 `TrayIcon` 的事件过滤器驱动:菜单自己收不到控件行的 move。"""
        if hovered == self._hovered:
            return
        self._hovered = hovered
        self.update()

    def paintEvent(self, event) -> None:
        if not self._hovered:
            return
        option = QStyleOptionMenuItem()
        option.initFrom(self)
        option.rect = self.rect()
        option.menuItemType = QStyleOptionMenuItem.MenuItemType.Normal
        option.checkType = QStyleOptionMenuItem.CheckType.NotCheckable
        option.menuHasCheckableItems = False
        option.maxIconWidth = 0
        option.text = ""  # 文字由行里的 QLabel 负责,这里只要那块底色
        option.state |= QStyle.StateFlag.State_Selected
        self.style().drawControl(QStyle.ControlElement.CE_MenuItem, option, QPainter(self), self)


class TrayIcon(QSystemTrayIcon):
    """托盘图标:小组件可以调到几乎看不见,这里是唯一稳定的入口。"""

    def __init__(self, window: StatusBarWindow) -> None:
        super().__init__(make_tray_icon())
        self._window = window
        self.setToolTip(paths.APP_NAME)

        self._menu = QMenu()
        # 菜单项默认**不**显示提示(QMenu::toolTipsVisible 默认 false),要显式打开;
        # 文字来自每个 QAction 自己的 setToolTip —— 没设的项,开了也没有提示可显示
        self._menu.setToolTipsVisible(True)

        # 控件行容器 / 行内控件 各自指向该行的 action(见 eventFilter、_release_row_focus)
        self._row_actions: dict[QWidget, QAction] = {}
        self._row_controls: dict[QWidget, QAction] = {}

        # 菜单顺序:版本号放最上,两个开关其次,四行控件居中,**常用项靠下**贴近「退出」。
        # 位置完全由下面几段的调用先后决定 —— 想调顺序就是调它们的先后。
        self._add_version_row()
        self._add_data_dir_row()  # 紧挨版本号:排查时要的就是这一对(哪一版 / 文件在哪)
        self._menu.addSeparator()  # 上面两行是"关于 / 自检",下面全是操作,隔一道
        #
        # 两个开关都是行内右侧复选框:菜单里一个"可勾选菜单项"都不留,Qt 才不会保留勾选列
        # (勾选列要花掉约 20px,数字与做法见 开发经验第 39 条)
        self._click_through = QCheckBox(self._menu)
        self._click_through.setToolTip("开启后窗口不再接收鼠标,只能从本菜单关掉")
        self._click_through.toggled.connect(window.set_click_through)
        self._add_row("鼠标穿透", self._click_through, "鼠标穿透", self._click_through.toolTip())

        self._autostart = QCheckBox(self._menu)
        self._autostart.setToolTip("开机后自动启动,可回本菜单取消")
        self._autostart.toggled.connect(self._set_autostart)
        self._add_row("开机自启", self._autostart, "开机自启", self._autostart.toolTip())

        self._edge_hide = QCheckBox(self._menu)
        self._edge_hide.setToolTip("贴到屏幕边就收起成一个小尾巴;鼠标蹭到它就长回来")
        self._edge_hide.toggled.connect(window.set_edge_hide)
        self._add_row("贴边隐藏", self._edge_hide, "贴边隐藏", self._edge_hide.toolTip())

        # 「还原配置」不常用:紧跟着两个开关放,不占贴近「退出」的那段常用区
        self._add_reset_row(window)

        # 四行控件(顺序固定):刷新频率、橙色阈值、红色阈值、常亮阈值、透明度
        self._add_refresh_row(window)
        self._threshold_spins: dict[str, QSpinBox] = {}
        for label, name, tooltip, key in THRESHOLD_ROWS:
            self._add_threshold_row(window, key=key, label=label, name=name, tooltip=tooltip)
        self._add_scale_row(window)  # 缩放与透明度都是"外观尺寸类",排在一起
        self._add_opacity_row(window)
        # 四个步进器统一宽度,按各自的 sizeHint 取最大(含最大位数与加减箭头,随字体/DPI 自适应)
        # —— 写死宽度会挤掉内容(实测 72px 时"85%"只显示出"85'");右对齐之后左边缘也跟着齐
        spins = (self._refresh_spin, *self._threshold_spins.values())
        widest = max(spin.sizeHint().width() for spin in spins)
        for spin in spins:
            spin.setFixedWidth(widest)
        self._sync_threshold_rows()

        # 常用项靠下:贴着「退出」,不必在菜单里长距离找
        self._visibility = self._menu.addAction("隐藏", self._toggle_visible)
        self._visibility.setToolTip("显示 / 隐藏小组件(隐藏后只能从托盘恢复)")
        self._menu.addAction("立即刷新", window.refresh).setToolTip("立刻读一次磁盘占用")

        self._menu.addSeparator()
        self._menu.addAction("退出", QApplication.quit).setToolTip("退出 stabar")
        self._menu.aboutToShow.connect(self._sync_state)
        # 收起时清掉控件行的焦点:留着它,再打开时 Qt 会把焦点恢复给上次那个步进器
        self._menu.aboutToHide.connect(self._release_row_focus)
        # 托盘菜单弹出的唯一留痕:真右键托盘图标时只有它能证明"点击真的到了图标上"
        self._menu.aboutToShow.connect(lambda: log.debug("event=menu open=tray"))
        # 同窗口右键菜单:记下选中的是哪一项
        self._menu.triggered.connect(lambda action: log.info("event=menu item=%s", action.text()))
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)
        window.updated.connect(self.setToolTip)
        # 窗口在托盘创建之前就 refresh 过一次,那次信号没人接,这里补上当前值(托盘用紧凑版)
        self.setToolTip(window.summary(compact=True))

    def _sync_state(self) -> None:
        """菜单弹出前,把文字和勾选对齐到窗口的真实状态。"""
        self._visibility.setText("隐藏" if self._window.isVisible() else "显示")
        # 滑杆对齐生效值;配置文件里可以写任意精度(如 0.33),滑杆取最近的一格,
        # 标签仍显示真实值 —— 不假装能表达它
        self._opacity_slider.blockSignals(True)  # 对齐不该反过来触发预览
        self._opacity_slider.setValue(layout.opacity_units(self._window.idle_opacity))
        self._opacity_slider.blockSignals(False)
        self._opacity_value.setText(layout.percent_text(self._window.idle_opacity))

        # 步进器同理:对齐到生效值,且不反过来触发预览
        self._refresh_spin.blockSignals(True)
        self._refresh_spin.setValue(self._window.refresh_seconds)
        self._refresh_spin.blockSignals(False)
        self._sync_threshold_rows()

        blocked = bool(self._window.windowFlags() & Qt.WindowTransparentForInput)
        self._click_through.blockSignals(True)  # 否则会回环触发 set_click_through
        self._click_through.setChecked(blocked)
        self._click_through.blockSignals(False)

        # 尺寸滑杆同理:对齐生效值,且不反过来触发预览
        self._scale_slider.blockSignals(True)
        self._scale_slider.setValue(layout.scale_units(self._window.ui_scale))
        self._scale_slider.blockSignals(False)
        self._scale_value.setText(layout.percent_text(self._window.ui_scale))

        self._autostart.blockSignals(True)  # 同上,避免回环
        self._autostart.setChecked(is_autostart_enabled())
        self._autostart.blockSignals(False)

        # 贴边隐藏:以窗口的实际状态为准(开穿透时它会被自动关掉,勾选要跟着变)
        self._edge_hide.blockSignals(True)
        self._edge_hide.setChecked(self._window.edge_hide)
        self._edge_hide.blockSignals(False)

    def _add_row(
        self,
        label: str,
        widget: QWidget,
        name: str,
        tooltip: str,
        trailing: QWidget | None = None,
    ) -> None:
        """在菜单里插一行「标签 + 控件(+ 尾随标签)」,滑杆与步进器共用。

        左边距必须是 `MENU_ITEM_INDENT`:控件行没有菜单项那段缩进,不补上会比其它行更靠左
        (实测:菜单项文字从菜单面板边缘 27px 处开始)。控件用官方机制 `QWidgetAction` +
        `setDefaultWidget` 放进菜单。

        提示文字同时挂在 action、行容器与控件上:菜单项提示要求 action 有 tooltip(且菜单开了
        `toolTipsVisible`),而指针落在行内的标签上时归属的是行容器 —— 标签与尾随标签都不接收
        鼠标事件,所以**整行任意位置**都是同一句提示。
        """
        holder = MenuRow(self._menu)  # 自己画当前项底色,见 MenuRow
        layout = QHBoxLayout(holder)
        # 左右取同一个值:左侧要跟上菜单项文字(见 MENU_ITEM_INDENT),右侧跟它相等才不显得偏
        layout.setContentsMargins(MENU_ITEM_INDENT, 4, MENU_ITEM_INDENT, 4)
        layout.setSpacing(8)

        title = QLabel(label, holder)
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(title)
        # 顶住:标签贴左、控件贴右 —— 各行宽度被 QWidgetAction 拉齐后,右边缘因此必然对齐
        layout.addStretch(1)
        widget.setToolTip(tooltip)
        layout.addWidget(widget)
        if trailing is not None:
            trailing.setAttribute(Qt.WA_TransparentForMouseEvents)
            layout.addWidget(trailing)

        holder.setToolTip(tooltip)
        # 控件行不参与 QMenu 的"当前项"跟踪(指针落在行里时事件被行内控件吃掉,菜单收不到 move),
        # 于是当前项会停在上一个纯文字项上;那时点本行空白处,事件冒泡到菜单 → 误触那一项
        # (**用户就是这样误开"鼠标穿透"的**)。两处兜住:
        #   1. 本行自己吃掉鼠标事件、不往上冒 —— 点空白处什么也不发生;
        #   2. 指针进入本行时把菜单当前项指到本行的 action —— 陈旧高亮立刻消失(见 eventFilter)。
        holder.setAttribute(Qt.WA_NoMousePropagation)

        action = QWidgetAction(self._menu)
        action.setText(name)  # 控件行本身没有文字,给 action 一个名字便于断言与调试
        action.setToolTip(tooltip)
        action.setDefaultWidget(holder)
        self._menu.addAction(action)  # 插在哪由 __init__ 里的调用点决定
        self._row_actions[holder] = action
        self._row_controls[widget] = action
        holder.installEventFilter(self)  # 进入 / 离开本行时同步菜单的当前项
        widget.installEventFilter(self)  # 滚轮不要改值(见 eventFilter)

    def eventFilter(self, obj, event) -> bool:
        """指针进出控件行时,把菜单的"当前项"跟着指过去。

        `QMenu` 只按自己的 `mouseMoveEvent` 跟踪当前项;指针在控件行里时事件被行内控件吃掉,
        菜单收不到 —— 当前项于是停在上一个纯文字项上(视觉上"高亮在鼠标穿透,而鼠标在别处"),
        点空白处还会误触它。这里补上这一段,行为就与纯文字项一致了。

        另:行内控件(步进器 / 滑杆)默认认滚轮,鼠标划过就改值 —— 菜单里这等于"手一抖就改配置",
        直接拦下(只在 DEBUG 留一行,便于回答"我滚了怎么没反应")。
        """
        action = self._row_actions.get(obj)
        if action is not None:
            if event.type() == QEvent.Type.Enter:
                if self._menu.isVisible():  # 菜单没弹时别去改它的当前项
                    self._menu.setActiveAction(action)
                obj.set_hovered(True)
            elif event.type() == QEvent.Type.Leave:
                self._menu.setActiveAction(None)
                obj.set_hovered(False)
        row = self._row_controls.get(obj)
        if row is not None and event.type() == QEvent.Type.Wheel:
            log.debug("event=wheel_ignored row=%s", row.text())
            return True
        return False

    def _release_row_focus(self) -> None:
        """菜单收起时把控件行的焦点交还出去(接线在 `__init__` 的 `aboutToHide`)。

        不清的话,那个步进器收起后仍是菜单窗口的焦点控件,**再打开时 Qt 会把焦点恢复给它**
        (实测:收起再打开,`hasFocus=True` 且文本仍是全选态),于是按 ↓ 想往下走菜单,实际改的是
        那个阈值 —— 按键先送到焦点控件,被它吃掉就不会进菜单。系统原生菜单收起后不留焦点,
        这里与它对齐。
        """
        for control in self._row_controls:
            control.clearFocus()
            line = getattr(control, "lineEdit", None)  # 步进器:顺带取消全选,免得看起来还"亮着"
            if line is not None:
                line().deselect()

    def _add_opacity_row(self, window: StatusBarWindow) -> None:
        """一行「透明度」滑杆(未报警时的不透明度)。

        **实测**:菜单 popup 里控件能收到 press / move / release,真鼠标按住拖动确实改值,
        菜单也不会被按下就关掉。拖动期间只预览,松手 / 菜单关闭时才提交落盘。
        """
        self._opacity_slider = QSlider(Qt.Horizontal, self._menu)
        # 一格 = layout.OPACITY_STEP(5%),量程就是配置允许的整个区间(5% ~ 100%)
        self._opacity_slider.setRange(1, layout.OPACITY_STEPS)
        self._opacity_slider.setValue(layout.opacity_units(window.idle_opacity))
        self._opacity_slider.setMinimumWidth(SLIDER_WIDTH)
        self._opacity_value = QLabel(layout.percent_text(window.idle_opacity), self._menu)
        self._opacity_value.setMinimumWidth(36)
        # 文字也贴右:否则数字左对齐,看着比步进器的右边缘短一截
        self._opacity_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self._add_row(
            "透明度", self._opacity_slider, "透明度", "未报警时的前景不透明度", self._opacity_value
        )

        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)
        self._opacity_slider.sliderReleased.connect(self._commit_opacity)
        self._menu.aboutToHide.connect(self._commit_opacity)  # 拖到一半直接点别处也算提交

    def _add_version_row(self) -> None:
        """最上面一行:左边「版本号」、右边版本值(与其它行同一套左右结构)。

        静态展示,点了不做任何事;提示只讲版本 —— 目录信息单独占一行(见 `_add_data_dir_row`),
        版本行的悬停不再夹带路径。
        """
        version = paths.app_version()
        self._version_label = QLabel(version, self._menu)
        self._version_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._add_row(
            "版本号",
            self._version_label,
            "版本号",
            f"当前运行的版本 {version}",  # 悬停只写用户能感知的事实;来源、路径之类不写进来
        )

    def _add_data_dir_row(self) -> None:
        """一行「配置和日志目录」:右边「打开」按钮,点了交给系统打开那个目录。

        目录取 `paths.config_dir()` —— 打包后它和日志同目录(%LOCALAPPDATA%\\stabar),源码运行是项目目录。
        完整路径直接写在提示里:不用点开就知道文件在哪。
        """
        self._open_dir_button = QPushButton("打开", self._menu)
        self._open_dir_button.setToolTip(f"打开 {paths.config_dir()}")
        self._open_dir_button.clicked.connect(self._open_data_dir)
        self._add_row(
            "配置和日志目录",
            self._open_dir_button,
            "配置和日志目录",
            self._open_dir_button.toolTip(),
        )

    def _open_data_dir(self) -> None:
        """点「打开」:先收起菜单再让系统打开 —— 这样资源管理器才会出现在前台、不被菜单挡着。"""
        self._menu.close()
        paths.open_path(paths.config_dir())

    def _add_scale_row(self, window: StatusBarWindow) -> None:
        """一行「调整尺寸」滑杆:一格 1%,量程 60% ~ 200%。

        与透明度滑杆同一套:拖动期间只预览(窗口立刻变大小),松手 / 菜单关闭才提交落盘。
        """
        self._scale_slider = QSlider(Qt.Horizontal, self._menu)
        self._scale_slider.setRange(
            layout.scale_units(layout.UI_SCALE_MIN), layout.scale_units(layout.UI_SCALE_MAX)
        )
        self._scale_slider.setValue(layout.scale_units(window.ui_scale))
        self._scale_slider.setMinimumWidth(SLIDER_WIDTH)
        self._scale_slider.setToolTip("小组件整体大小(60% ~ 200%)")
        self._scale_value = QLabel(layout.percent_text(window.ui_scale), self._menu)
        self._scale_value.setMinimumWidth(40)
        self._scale_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._add_row(
            "调整尺寸", self._scale_slider, "调整尺寸", "小组件整体大小", self._scale_value
        )

        self._scale_slider.valueChanged.connect(self._on_scale_changed)
        self._scale_slider.sliderReleased.connect(self._commit_scale)
        self._menu.aboutToHide.connect(self._commit_scale)  # 拖到一半直接点别处也算提交

    def _on_scale_changed(self, units: int) -> None:
        """拖动过程中:只预览,不落盘(拖一次会发几十个信号)。"""
        scale = layout.scale_from_units(units)
        self._scale_value.setText(layout.percent_text(scale))
        self._window.preview_ui_scale(scale)

    def _commit_scale(self) -> None:
        """松手 / 菜单关闭:提交(落盘 + 一条 `event=ui_scale`);同一个值重复提交会被去重。"""
        self._window.set_ui_scale(layout.scale_from_units(self._scale_slider.value()))

    def _add_reset_row(self, window: StatusBarWindow) -> None:
        """一行「还原配置」:标签在左、按钮在右(与上面几行同一套左右对齐)。

        托盘这层只管弹不弹、点了算不算;该还原哪些项、怎么生效都在窗口那边
        (见 `StatusBarWindow.resettable_diff` / `reset_settings`)。
        """
        button = QPushButton("还原", self._menu)
        button.setToolTip("把菜单里这些配置恢复成默认值(不含窗口位置)")
        button.clicked.connect(self._confirm_reset)
        self._add_row("还原配置", button, "还原配置", button.toolTip())

    def _confirm_reset(self) -> None:
        """点「还原」→ 列出有差异的项请人确认,确认了才真还原;没有差异就只说明一句。"""
        rows = self._window.resettable_diff()  # 空清单也照常弹,只是换成说明
        # 先收起菜单再弹模态框:popup 会请求键盘 / 鼠标抓取(离屏平台会打
        # "This plugin does not support grabbing the keyboard"),收起来最稳
        self._menu.close()
        if (
            dialogs.ResetDialog(rows, QColor(*diskbar.COLOR_DANGER)).exec()
            != QDialog.DialogCode.Accepted
        ):
            log.debug("event=config_reset_cancelled")
            return
        if not rows:
            log.debug("event=config_reset_skipped reason=all_default")  # 只是告知,没有可改的
            return
        self._window.reset_settings()  # 日志与落盘都在里面(event=config_reset)

    def _add_refresh_row(self, window: StatusBarWindow) -> None:
        """一行「刷新频率（秒）」步进器:1 ~ 60 秒、步进 1、可直接点进去敲数字。

        单位写在行标签上,步进器本身不带后缀 —— 免得同一个单位在菜单里出现两次。
        """
        self._refresh_spin = QSpinBox(self._menu)
        self._refresh_spin.setRange(layout.REFRESH_MIN_S, layout.REFRESH_MAX_S)
        self._refresh_spin.setSingleStep(1)
        # 敲 "30" 时别在敲到 "3" 的那一刻就把间隔改成 3 秒
        self._refresh_spin.setKeyboardTracking(False)
        self._refresh_spin.setValue(window.refresh_seconds)

        self._add_row("刷新频率（秒）", self._refresh_spin, "刷新频率", "每几秒读一次磁盘占用")

        self._refresh_spin.valueChanged.connect(self._on_refresh_changed)
        self._refresh_spin.editingFinished.connect(self._commit_refresh)
        self._menu.aboutToHide.connect(self._commit_refresh)

    def _add_threshold_row(
        self, window: StatusBarWindow, *, key: str, label: str, name: str, tooltip: str
    ) -> None:
        """一行阈值步进器(按百分比显示、步进 1%)。

        橙色与红色的量程互相顶住(橙色最高 `红色 - 1%`,红色最低 `橙色 + 1%`),配不出反序;
        常亮阈值不受红色约束 —— 比红色高就等于"红线即常亮"。
        """
        spin = QSpinBox(self._menu)
        spin.setRange(1, 100)
        spin.setSingleStep(round(layout.THRESHOLD_STEP * 100))
        spin.setKeyboardTracking(False)  # 敲 "85" 时别在敲到 "8" 的那一刻就改
        spin.setValue(round(window.thresholds[key] * 100))

        self._threshold_spins[key] = spin
        self._add_row(label, spin, name, tooltip)  # 提示文字由 _add_row 挂到三处

        spin.valueChanged.connect(lambda _value: self._preview_thresholds())
        spin.editingFinished.connect(self._commit_thresholds)
        self._menu.aboutToHide.connect(self._commit_thresholds)

    def _preview_thresholds(self) -> None:
        """改动过程中:只预览(重绘颜色 + 重算常亮),不落盘。

        顺手把三行的量程互相顶住 —— 只动边界、不动当前值,免得和用户正在敲的数字打架
        (值被顶到边界时 Qt 会自己夹取并再发一次信号,重入一次就收敛)。
        """
        percent = {key: spin.value() for key, spin in self._threshold_spins.items()}
        self._threshold_spins["warn"].setMaximum(percent["danger"] - 1)
        self._threshold_spins["danger"].setMinimum(percent["warn"] + 1)
        self._window.preview_thresholds(
            warn=percent["warn"] / 100,
            lit=percent["lit"] / 100,
            danger=percent["danger"] / 100,
        )

    def _commit_thresholds(self) -> None:
        """回车 / 菜单关闭:提交(落盘 + 一条 `event=thresholds`);同一组值会被去重。"""
        percent = {key: spin.value() for key, spin in self._threshold_spins.items()}
        self._window.set_thresholds(
            warn=percent["warn"] / 100,
            lit=percent["lit"] / 100,
            danger=percent["danger"] / 100,
        )

    def _sync_threshold_rows(self) -> None:
        """把三行对齐到生效值;橙色与红色的量程互相顶住,常亮阈值自由(1% ~ 100%)。"""
        percent = {key: round(value * 100) for key, value in self._window.thresholds.items()}
        for key, spin in self._threshold_spins.items():
            spin.blockSignals(True)  # 对齐不该反过来触发预览
            if key == "warn":
                spin.setRange(1, percent["danger"] - 1)
            elif key == "danger":
                spin.setRange(percent["warn"] + 1, 100)
            else:
                spin.setRange(1, 100)
            spin.setValue(percent[key])
            spin.blockSignals(False)

    def _on_opacity_changed(self, step: int) -> None:
        """拖动过程中:只预览,不落盘(拖一次会发几十个信号)。"""
        value = layout.opacity_from_step(step)
        self._opacity_value.setText(layout.percent_text(value))
        self._window.preview_idle_opacity(value)

    def _commit_opacity(self) -> None:
        """松手 / 菜单关闭:提交(落盘 + 一条 `event=idle_opacity`);同一个值重复提交会被去重。"""
        self._window.set_idle_opacity(layout.opacity_from_step(self._opacity_slider.value()))

    def _on_refresh_changed(self, seconds: int) -> None:
        """改动过程中:立刻生效(下次读盘按新间隔),不落盘、不记日志。"""
        self._window.preview_refresh_seconds(seconds)

    def _commit_refresh(self) -> None:
        """回车 / 菜单关闭:提交(落盘 + 一条 `event=refresh_interval`);同一个值会被去重。"""
        self._window.set_refresh_seconds(self._refresh_spin.value())

    def _set_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except OSError as error:
            # 不要用 print(file=sys.stderr):打包成 --noconsole 后它是 None,打印会静默消失
            log.warning("event=autostart enabled=%s ok=0 error=%s", enabled, error)
        else:
            log.info("event=autostart enabled=%s ok=1", enabled)
        self._sync_state()  # 以注册表的实际结果为准,别让勾选状态骗人

    def _toggle_visible(self) -> None:
        if self._window.isVisible():
            self._window.hide_widget()  # 内部会记 event=window state=hidden
        else:
            self._window.show()
            log.info("event=window state=shown")
        self._sync_state()

    def _on_activated(self, reason) -> None:
        # 左键单击切换显示;右键是菜单,由系统直接弹出,不走这里
        # 记下来路:短时间内第二次点击会被当成双击(DoubleClick),这正是"点了没反应"的常见原因
        log.debug("event=tray activated=%s", reason.name)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_visible()
