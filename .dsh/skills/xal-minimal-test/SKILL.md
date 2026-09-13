---
name: xal-minimal-test
description: X-AnyLabeling fork 的测试与操作约束：禁全量 pytest、禁 black/flake8、临时文件出仓库、删除改为移动到 dsh-trash、不用 git clean/reset --hard；含解释器探测与最小测试命令模板。
---

# 测试与操作约束（xal-minimal-test）

## 生效范围

本 skill 在 `X-AnyLabeling/.dsh/skills/` 下，只有 cwd 位于 X-AnyLabeling/ 内时才会被自动
加载（项目根 = 从 cwd 向上找到的最近一个含 `.git` 的祖先）。cwd 在工作区根时请按显式路径打开。

## 命令对照表

| 想做的事 | 命令 |
|---|---|
| 契约自检（任何环境都能跑） | `python3 tests/custom/test_fork_contract.py` |
| 契约自检（装了 pytest） | `python3 -m pytest -p no:cacheprovider tests/custom/test_fork_contract.py -v` |
| 同步上游后的严格自检 | `XAL_CONTRACT_STRICT=1 python3 tests/custom/test_fork_contract.py` |
| 单个功能的最小测试 | `python -m pytest -p no:cacheprovider tests/custom/<feature> -v` |
| 换一个仓库根跑自检（演练） | `XAL_CONTRACT_ROOT=/tmp/xal-drill python3 /tmp/xal-drill/tests/custom/test_fork_contract.py` |

## 解释器探测（Linux）

仓库 `AGENTS.md` §3 里的 `C:\Users\...` 路径是作者的 Windows 环境，在本工作区无效。
动手前先探测，以能跑通的解释器为准：

    python3 -c "import sys; print(sys.version)"
    python3 -m pytest --version
    python3 -c "import PyQt6; print('PyQt6 OK')"
    python3 -c "import numpy; print('numpy OK')"

本机实测：`/usr/bin/python3` = 3.14.4，无 pytest、无 PyQt6、无 numpy =>
**桌面测试跑不了（环境限制，不是交付缺陷）**，契约自检是刻意零依赖的，仍然必须能跑。

## 临时文件与「删除」

- 临时文件写到 `tempfile.gettempdir()`，**不要写进仓库**（`.diff` / `.log` / 临时脚本 /
  `.pytest_cache` 都算污染）。
- 需要「删除」时一律移动，不删除：
  `shutil.move(目标, osp.join(tempfile.gettempdir(), "dsh-trash", 时间戳 + "-" + 名字))`，
  并在回复里给出移动后的完整路径。
- 演练用的影子树建在 `/tmp` 下，演练结束同样 `mv` 到 `dsh-trash`。

## 禁止

- 跑全量 pytest：`pytest tests`、`pytest tests/<大目录>` 这类目录级批量同样算全量。
- 跑 black / flake8（新增代码靠人工保证：行长 ≤79 列、4 空格缩进、导入顺序与相邻代码一致）。
- 在仓库里留下任何临时/中间文件。
- `git clean` / `git checkout --` / `git reset --hard` / `git stash`。
- 用 `rm` / `rmdir` / `find -delete` 删除文件或目录。

## 坑

- 桌面测试需要 `QT_QPA_PLATFORM=offscreen`：`tests/custom/edit_extras/`、
  `tests/custom/ensure_label_file/`、`tests/custom/smudge_tool/` 有 conftest 设置；
  `tests/custom/model_validation/` 的 conftest 不设该变量（只管 scratch 目录 /
  dsh-trash 回收 fixtures），offscreen 由各测试文件开头的
  `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`（如
  `test_mv_app_config.py`）。
- 缺 PyQt6 时 `import` 阶段就会失败，这类失败不要误判成业务回归。
- 自检脚本只允许 import 标准库（`PyQt6` 等一切第三方库都禁止）：它必须能在裸 python3
  上跑（实测 import 仅 `ast` / `functools` / `json` / `os` / `re` / `sys` / `pathlib`）。
