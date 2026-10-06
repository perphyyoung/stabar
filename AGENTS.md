# AGENTS.md

本仓库的协作约定,面向在此工作的 AI 编码助手(以及人)。只写需要遵守的部分。

## 代码检查:改完必须跑

- 改动任何 Python 代码后,必须运行 `ruff check`(用**全局安装的 ruff**,当前 0.16.x),
  **必须全绿**才能收尾。
- 规则配置在 `pyproject.toml` 的 `[tool.ruff.lint]`:`extend-select = ["N"]`,已启用命名规则。
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
- 图标只有一份 `stabar.ico`,由 `scripts/make-icon.py` 生成:托盘运行时读它,打包时 exe 也
  嵌它。改了图案要重新生成再打包。
- **不要删 `imageformats/qico.dll`**:`QIcon` 读 `.ico` 靠它,删掉会静默返回空图标、托盘
  整个不出现(dev 下插件齐全,只在打包后暴露)。
- 资源测量工具在 `scripts/`,用法与检测结论见 `scripts/readme.md`。

## 语言

- 代码注释、文档、提交信息统一用中文。
