# AGENTS.md

本仓库的协作约定,面向在此工作的 AI 编码助手(以及人)。只写需要遵守的部分。

## 代码检查与格式化:改完按这个顺序跑

用**全局安装的 ruff**(当前 0.16.x)。三步顺序不能颠倒:

```powershell
ruff check --fix     # 1. 修可自动修的问题(含 import 排序)
ruff format          # 2. 统一格式
ruff check           # 3. 复核
```

- `ruff format` 必须放在 `ruff check --fix` **之后**:自动修复可能重排代码,格式化要在最终
  形态上做,否则两者会互相推翻。
- 收尾标准:`ruff check` 输出 `All checks passed!`,`ruff format --check` 输出
  `already formatted`。
- **不要配 format on save**:格式化只走命令行,避免编辑器与 agent 的改动互相打架。
- 配置都在 `pyproject.toml`:`line-length = 100`;`[tool.ruff.lint]` 的
  `extend-select = ["I", "N"]`(import 排序 + 命名规范)。用 `extend` 而不是 `select`,
  免得顶掉 ruff 的默认规则集。
- Qt 的重写方法名**必须是驼峰**(`paintEvent`、`enterEvent` 等),已在
  `[tool.ruff.lint.pep8-naming]` 的 `extend-ignore-names` 里集中豁免。
  **不要写行内 `# noqa`**;新增 Qt 重写方法就把名字加进那份白名单。

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
