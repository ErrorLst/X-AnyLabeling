# 仓库约定（Fork 维护规则）

本仓库是 **X-AnyLabeling 的 fork**。上游会持续演进，我们需要长期跟进同步（merge / rebase 上游代码），
因此本仓库的一切改动都以「**同步成本最小**」为第一原则。

## 1. 最小化修改原始代码

- **新增功能不要写进上游文件。** 自研模块统一放在 `anylabeling/custom/` 下，一个功能一个子包：
  - `anylabeling/custom/model_validation/` —— 模型验证子窗口
  - `anylabeling/custom/edit_extras/` —— 滚轮缩放
- 上游文件（`anylabeling/views/**`、`anylabeling/services/**` 等）**只允许加「挂载点」**，典型形态是 1 行 import + 1 行调用：

  ```python
  from anylabeling.custom.<feature> import install_<feature>   # 挂载点 1
  ...
  install_<feature>(self)                                      # 挂载点 2
  ```

  禁止在上游文件里新增函数体、修改上游逻辑、搬移上游代码。
- 能通过**外部手段**实现的，就不要改上游文件。优先顺序：
  1. 事件过滤器 / 信号槽 / 包装（完全不改上游）
  2. 上游文件的 1~2 行挂载点
  3. 万不得已才改上游逻辑，且必须在报告与提交信息里说明理由
- **每一处上游改动都要可审计**：报告里逐条列出 `文件:行` + 改动内容，升级上游时按这份清单核对挂载点是否仍然存在。

## 2. 测试：禁止全量测试

- **禁止运行全量测试**（`pytest tests`、`pytest tests/<大目录>` 这类目录级批量同样算全量）。全量太慢，而且收益很低：本仓库既有的 UI 测试几乎都是 `SimpleNamespace` 假对象 + 未绑定方法调用，跑全量并不会覆盖到 fork 新增的挂载点。
- 只做**针对改动部分的最小化测试**：
  - 自研模块的测试放 `tests/custom/<feature>/`，与 `anylabeling/custom/<feature>/` 一一对应
  - 只跑与本次改动直接相关的测试文件，例如：
    `python -m pytest -p no:cacheprovider tests/custom/edit_extras -v`
  - 只有当某个既有测试文件**确实会执行到**改动路径时，才额外跑那一个文件
- **不做静态检查**：不要跑 `black` / `flake8`（太耗时，收益低）。新增/修改的 Python 文件靠人工保证格式：行长 ≤ 79 列、缩进 4 空格、import 顺序与相邻代码一致。
- 跨模块回归风险靠 **diff 评审 + 手工冒烟** 兜底，不靠全量测试。

## 3. 环境与操作习惯

- 解释器：`C:\Users\zhoujin\miniconda3\envs\xal-cpu\python.exe`
- pytest 一律带 `-p no:cacheprovider`
- **查找文件用 `glob` 工具，不要做全树递归扫描**：`Get-ChildItem -Recurse` / `find` 在本仓库一次要 30–40 秒
  （仓库里有 10 万行的 `resources.py`）。glob 的模式按「工作区相对路径」匹配，例如 `**/custom/*/__init__.py`、`**/canvas*.py`。
- 代码风格沿用仓库既有约定：`line-length = 79`、4 空格缩进（见 `pyproject.toml`），但**不用工具校验**
- **不要在仓库内创建任何临时/中间文件**（`.diff`、`.log`、临时脚本等）；需要临时文件时写到系统 TEMP
- **不要直接删除文件**：需要移除时移动到 `%TEMP%\dsh-trash\<时间戳>-<名字>`，并在回复里给出目标完整路径
- 不使用 `git clean` / `git checkout -- ` / `git reset --hard` / `git stash`

## 4. 与上游同步时

- 上游升级后重点核对：`anylabeling/custom/**` 是否仍能导入、各挂载点是否仍存在、custom 依赖的上游内部状态（属性名、信号名、方法签名）是否变化。
- 上游改动破坏了挂载点或 custom 依赖的内部状态时，**优先在 `custom/` 内做适配**，而不是回改上游。

## 5. 快速索引（追加，不改上文条款）

- 自研功能索引（人读）：`docs/custom/FEATURES.md`；架构与热文件地图：`docs/custom/MAP.md`；
  机器可读契约（挂载点 / 上游依赖符号的唯一事实源，单节 ≈1.7KB）：`docs/custom/contract.json`。
- 改动自研功能前后各跑一次契约自检（纯标准库、不导入 PyQt6、1~2 秒）：
  `python3 tests/custom/test_fork_contract.py`（装了 pytest 时：`python3 -m pytest -p no:cacheprovider tests/custom/test_fork_contract.py -v`）；
  同步上游后用严格模式：`XAL_CONTRACT_STRICT=1 python3 tests/custom/test_fork_contract.py`。
- `anylabeling/resources/resources.py` 约 10.9 万行，是生成物：**禁止打开、禁止扫描、禁止参与任何全树搜索**。
- §3 里 `C:\Users\...` 的解释器路径是作者 Windows 环境；本工作区是 Linux。先探测
  `python3 -c "import PyQt6"` / `python3 -m pytest --version`，以能跑通的解释器为准；契约自检不依赖 PyQt6。
- 本地 skill 在 `.dsh/skills/`（连同目录：`xal-add-custom-feature`、`xal-upstream-sync-audit`、`xal-minimal-test`）；
  仅当 cwd 位于本仓库内时才会被自动加载。
- **验证降级**（哪个阶段跑多少测试、探针写在哪）：见 §6。

## 6. 验证降级（实现阶段与主会话的分工）

子代理的每一个往返都要 5~30 分钟，所以**验证分两层，不要在每个实现轮里重复跑全套**：

- **实现阶段（子代理/实现轮）只跑与本轮改动直接相关的测试文件**，例如
  `python -m pytest -p no:cacheprovider tests/custom/smudge_tool/test_st_draw_mode.py -q`；
  **不要**在每个实现轮里重复跑整个功能目录的测试，也不要重复跑契约自检。
- **主会话在实现返回后统一跑一次**：该功能目录的完整测试
  （`pytest -p no:cacheprovider tests/custom/<feature> -q`）+ 契约自检
  （`python3 tests/custom/test_fork_contract.py`，同步上游后用 `XAL_CONTRACT_STRICT=1`），
  失败时把失败用例与最小复现交回实现轮，而不是让实现轮自己反复跑全目录。
- 这两条不违反 §2 的"禁止全量测试"：§2 禁止的是 `pytest tests` 这类整仓库目录级批量；
  这里说的仍是单个 feature 目录。
- **探针（真机实证脚本）优先写在主会话里先跑**：结论（数值、现象、失败用例）作为任务书
  的一部分交给实现轮，实现轮不必自己重跑一遍探针；只有实现轮到"必须证明自己的改动"时才再跑。
  这样一次发现的问题能在最早的一轮暴露，而不是第 3、4 轮才由实现轮探出来。


