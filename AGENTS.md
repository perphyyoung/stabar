# AGENTS.md

本仓库的协作约定,面向在此工作的 AI 编码助手(以及人)。只写需要遵守的部分。

## 代码检查与格式化:改完按这个顺序跑

用**全局安装的 ruff**(当前 0.16.x)。三步顺序不能颠倒:

```powershell
ruff check --fix     # 1. 修可自动修的问题(含 import 排序)
ruff format          # 2. 统一格式
ruff check           # 3. 复核
uv run pytest -q     # 4. 跑测试
```

- `ruff format` 必须放在 `ruff check --fix` **之后**:自动修复可能重排代码,格式化要在最终
  形态上做,否则两者会互相推翻。
- 测试在 `tests/`(`uv run pytest -q`),改完必须全过。它跑在离屏 Qt 上
  (`QT_QPA_PLATFORM=offscreen`),不会弹真窗口;配置走 `tmp_path`、注册表用替身,
  **不碰真实 `stabar.ini` 和真实 Run 键**。
- 收尾标准:`ruff check` 输出 `All checks passed!`,`ruff format --check` 输出
  `already formatted`。
- **两层 E2E**,断言一律"日志事件序列 + 一条外部事实"配对(见 `日志使用说明.md` 第七节):
  - **A 层** `tests/test_e2e_ui.py`:进程内驱动真实事件,默认跑,不动鼠标;
  - **B 层** `tests/test_e2e_real_input.py`:进程外真鼠标驱动**打包后的 exe**,**默认跳过**。
    手动开启:`uv run pyinstaller --noconfirm --clean stabar.spec` → `$env:STABAR_E2E = "1"`。
    它会真的移动鼠标、抢焦点,必须有真实桌面会话;检测到你在用鼠标时会 **skip 而不是判失败**。
- 临时脚本一律以 `tmp-` 开头(已在 `.gitignore` 里),用完即删,不要留在仓库里。
- **不要配 format on save**:格式化只走命令行,避免编辑器与 agent 的改动互相打架。
- 配置都在 `pyproject.toml`:`line-length = 100`;`[tool.ruff.lint]` 的
  `extend-select = ["I", "N"]`(import 排序 + 命名规范)。用 `extend` 而不是 `select`,
  免得顶掉 ruff 的默认规则集。
- Qt 的重写方法名**必须是驼峰**(`paintEvent`、`enterEvent` 等),已在
  `[tool.ruff.lint.pep8-naming]` 的 `extend-ignore-names` 里集中豁免。
  **不要写行内 `# noqa`**;新增 Qt 重写方法就把名字加进那份白名单。

## 模块划分

- **`main.py` 只放应用装配与界面**(窗口、盘条、托盘、入口),基础设施不要往里塞。
  已经拆出去的:日志 → `logging_setup.py`。
- 后续同类需求(配置读写、资源测量……)同样独立成模块,并在 `README.md` 的目录结构里登记。
- 测试跟着模块走:测日志基础设施就在 `tests/test_logging.py` 里直接 `import logging_setup`,
  不要绕道 `main` 拿。

## 经验记录

- 踩过的坑、验证过的结论写进 **`开发经验.md`**(现象 → 原因 → 做法,并标注
  【实测】/【文档】/【项目内】),不要只留在对话里。
- 只写有证据的:实测要能贴出命令与输出,文档要能贴出链接。**不写没有复现路径的猜测。**
- 经验一旦变成"改了就会踩"的硬规则,同时补进 `README.md` 的必读清单。

## 提交信息:默认单行

- **默认单行**,一句话说清做了什么。非必要不写多行正文。
- 只在确有代码/文档改动时提交。纯讨论、给方案、只读排查的回合不要提交。

## 改代码前必读

`README.md` 有一节"改代码前先看这几条",其中三条最容易踩:

- 不要用 `setWindowOpacity` 做前景淡化:它会把整层像素相乘,连隐形命中层一起压成
  alpha 0,鼠标直接穿透,拖动和悬停全废。前景是"直接画 alpha"。
- `HIT_AREA_ALPHA = 1` 是功能代码,不是装饰,别改成 0。
- `REVEAL_START` 是独立常量,不要再写成 `WARN_THRESHOLD - 某个 margin`(那个隐式加减会让
  改警告线时连带挪动提亮起点)。

## 打包与图标

- 打包:`uv run pyinstaller --noconfirm --clean stabar.spec`;产物在 `dist\stabar\`,
  **必须整目录分发**(onedir),exe 单独拷出去跑不起来。
- **打包时机:dev 与打包产物出现差异时才打包**,不是"改了功能就打包"。判断标准是改动落在
  哪些面上:
  - **需要打包**:动到 `sys.frozen` 分支(`settings_path` / `autostart_command`)、
    `sys._MEIPASS` 路径解析、`stabar.spec` 裁剪规则、图标/资源文件、依赖版本。
  - **不必打包**:纯逻辑、配色、阈值、格式、注释、文档、测试 —— 这些 dev 与打包后行为一致,
    重新打包只是白花时间(打包也不会改变它们的行为)。
- 图标只有一份 `stabar.ico`,由 `scripts/make-icon.py` 生成:托盘运行时读它,打包时 exe 也
  嵌它。改了图案要重新生成再打包。
- **不要删 `imageformats/qico.dll`**:`QIcon` 读 `.ico` 靠它,删掉会静默返回空图标、托盘
  整个不出现(dev 下插件齐全,只在打包后暴露)。
- 资源测量工具在 `scripts/`,用法与检测结论见 `scripts/readme.md`。

## 语言

- 代码注释、文档、提交信息统一用中文。
