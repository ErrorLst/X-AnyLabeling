---
name: xal-add-custom-feature
description: 在 X-AnyLabeling fork 中新增自研功能：代码只放 anylabeling/custom/<feature>/，上游文件只加 1~2 行挂载点，测试放 tests/custom/<feature>/，并同步 docs/custom/contract.json 与 docs/custom/FEATURES.md。
---

# 新增自研功能（xal-add-custom-feature）

## 生效范围

本 skill 在 `X-AnyLabeling/.dsh/skills/` 下，加载条件是 **cwd 位于 X-AnyLabeling/ 内**
（项目根 = 从 cwd 向上找到的最近一个含 `.git` 的祖先目录）。cwd 停在工作区根
（`/home/zhoujin/xany`）时不会被自动加载，需要按显式路径打开本文件。

## 铁律

1. **上游零逻辑改动**：`anylabeling/views/**`、`anylabeling/services/**` 等上游文件里
   只允许出现「1 行 import + 1 行调用」形态的挂载点，禁止新增函数体、禁止改上游逻辑、
   禁止搬移上游代码。
2. 优先级：**事件过滤器 / 信号槽 / 实例级包装**（完全不改上游）> 上游文件的 1~2 行挂载点 >
   万不得已才改上游逻辑，且必须在报告与提交信息里写明理由。
3. 每一处上游改动都要能审计：报告里逐条列 `文件:行`，并登记进
   `docs/custom/contract.json` 的 `features.<id>.mounts`。

## 步骤

1. 建实现包 `anylabeling/custom/<feature>/`，在 `__init__.py` 里导出幂等的
   `install_<feature>(widget)`：重复调用必须复用已有包装/过滤器，不能叠加第二个。
   包内不要 import `label_widget` 或画布模块，widget 由参数传入（保持导入图无环、可单测）。
2. 在 `anylabeling/views/labeling/label_widget.py` 加挂载点：模块级 1 行 import +
   `LabelingWidget.__init__` 里 1 行 `install_<feature>(self)`，行内写中文注释。
   菜单类功能才需要额外的 action 定义/挂载（见 `features.model_validation.mounts`）。
3. 建测试 `tests/custom/<feature>/`（与实现一一对应）：加 `conftest.py` 设
   `QT_QPA_PLATFORM=offscreen`；临时文件用 `tempfile.gettempdir()`；结束用
   `shutil.move` 移到 `${TMPDIR:-/tmp}/dsh-trash/<时间戳>-<名字>`，不要直接删。
4. 在 `docs/custom/contract.json` 的 `features` 里加一节：`mounts` 每项带
   `file/line/anchor/matcher/form/owner`，`anchor` 必须是行内代码原文；
   `upstream` 按 `<文件>:<类>` 或 `<文件>:@module` 聚合，AST 查不到的成员用
   `{name, anchor, matcher}`。新增一节后单节必须 ≤2560B。
5. 更新 `docs/custom/FEATURES.md`：加一个标题为 `## <feature>` 的小节（职责 / 代码与体量 /
   入口符号 / 挂载点 + 软挂载 / 依赖的上游状态 / 行为级契约 / 测试 / 已知坑），**不写行号**。
6. 必要时更新 `docs/custom/MAP.md`（入口链或定位表变化时）。
7. 跑验证：`python3 tests/custom/test_fork_contract.py`（零依赖入口，1 秒级），
   再跑该功能的最小测试 `python -m pytest -p no:cacheprovider tests/custom/<feature> -v`。
8. 报告里逐条列 `文件:行` + 改动内容。

## 完成定义

- [ ] `anylabeling/custom/<feature>/` 存在且导出幂等的 `install_<feature>`。
- [ ] 上游文件只多了 import + 调用（+ 必要的菜单 action），`git diff` 里没有逻辑改动。
- [ ] `tests/custom/<feature>/` 有最小测试，临时文件出仓库。
- [ ] `contract.json` 有 `features.<feature>`，锚点命中恰好 1 行。
- [ ] `FEATURES.md` 有 `## <feature>` 小节，`MAP.md` 已按需更新。
- [ ] `python3 tests/custom/test_fork_contract.py` 退出码 0。

## 禁止

- 改上游逻辑、在上游文件里新增函数体或搬移代码。
- 跑全量 pytest（目录级批量同样算全量）、跑 black / flake8。
- 在仓库里创建临时/中间文件（`.diff`、`.log`、临时脚本、`.pytest_cache`）。
- 直接删除文件（一律 `shutil.move` 到 `dsh-trash` 并报告完整路径）。
