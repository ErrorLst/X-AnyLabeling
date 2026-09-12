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
- 静态检查同样只针对改动文件：`black --check <changed files>`、`flake8 --jobs 1 <changed files>`
- 跨模块回归风险靠 **diff 评审 + 手工冒烟** 兜底，不靠全量测试。

## 3. 环境与操作习惯

- 解释器：`C:\Users\zhoujin\miniconda3\envs\xal-cpu\python.exe`
- pytest 一律带 `-p no:cacheprovider`；flake8 一律带 `--jobs 1`
- black `line-length = 79`、flake8 `max-line-length = 79`（见 `pyproject.toml`）
- **不要在仓库内创建任何临时/中间文件**（`.diff`、`.log`、临时脚本等）；需要临时文件时写到系统 TEMP
- **不要直接删除文件**：需要移除时移动到 `%TEMP%\dsh-trash\<时间戳>-<名字>`，并在回复里给出目标完整路径
- 不使用 `git clean` / `git checkout -- ` / `git reset --hard` / `git stash`

## 4. 与上游同步时

- 上游升级后重点核对：`anylabeling/custom/**` 是否仍能导入、各挂载点是否仍存在、custom 依赖的上游内部状态（属性名、信号名、方法签名）是否变化。
- 上游改动破坏了挂载点或 custom 依赖的内部状态时，**优先在 `custom/` 内做适配**，而不是回改上游。
