---
name: xal-upstream-sync-audit
description: 同步上游后核对 fork 契约：跑 tests/custom/test_fork_contract.py（严格模式），逐条复核挂载点与 soft_mounts，优先在 anylabeling/custom/ 内适配，并把 mounts[].line 快照更新到新行号。
---

# 上游同步后的契约核对（xal-upstream-sync-audit）

## 生效范围

本 skill 在 `X-AnyLabeling/.dsh/skills/` 下，只有 cwd 位于 X-AnyLabeling/ 内时才会被自动
加载（项目根 = 从 cwd 向上找到的最近一个含 `.git` 的祖先）。cwd 在工作区根时请按显式路径打开。

## 时机

- merge / rebase 上游代码之后；
- 发版之前；
- 上游改了 `label_widget.py`、`widgets/canvas.py`、`custom/` 依赖的任何模块之后。

## 步骤

1. 跑严格模式自检：
   `XAL_CONTRACT_STRICT=1 python3 tests/custom/test_fork_contract.py`
   （严格模式下 WARN 也算失败，包括行号漂移）。
2. 按三类处置失败项：
   - `FAIL mount anchor missing` / `FAIL anchor not unique`：读上游 diff，找到挂载点的新位置
     重新挂上（仍保持 1~2 行形态），然后更新 `contract.json` 的锚点与行号。
   - `FAIL symbol missing`：上游改了 custom 依赖的内部符号，**优先在 `anylabeling/custom/`
     内适配**，不要回改上游。
   - `WARN line drift`：只更新 `docs/custom/contract.json` 里对应的 `mounts[].line` 快照。
3. 逐条核对 `soft_mounts`（实例级包装，不在上游 diff 里，自检抓不到）：
   `LabelingWidget.load_file`、`LabelingWidget.populate_mode_actions`、
   `LabelingWidget.import_image_folder`、`Canvas` 事件过滤器、`Canvas.mode_changed`、
   `widget._model_validation_dialog`。
4. 核对 `docs/custom/FEATURES.md` 里行为级契约指向的上游行，重点四条：
   `Canvas.wheelEvent` 的分支（镜像在 `anylabeling/custom/edit_extras/wheel_zoom.py`）、
   `save_labels` 的落盘行为、`set_clean` 里 `delete_file` 动作的启用/禁用分支、
   `populate_mode_actions` 的工具栏重建。
5. 跑受影响功能的最小测试（只跑对应目录，带 `-p no:cacheprovider`）。
6. 更新 `contract.json` 的 `verified_upstream_version` 与行号快照；报告里逐条列 `文件:行`。

## 变异演练（证明自检还有牙齿）

在 `/tmp` 建影子树（复制契约引用的所有文件 + `docs/custom/` + 自检脚本），然后：

    XAL_CONTRACT_ROOT=/tmp/xal-drill python3 /tmp/xal-drill/tests/custom/test_fork_contract.py

- 把 `install_edit_extras(self)` 改成 `install_edit_extras_v2(self)` => 必须非 0 退出并打印
  `FAIL mount anchor missing`；
- 把 `def offset_to_center(self):` 改成 `def offset_to_center_v2(self):` => 必须非 0 退出并打印
  `FAIL symbol missing`。

演练结束把影子树 `mv` 到 `${TMPDIR:-/tmp}/dsh-trash/<时间戳>-<名字>`，不要删除。

## 禁止

- 为了让自检通过而回改上游逻辑（应当适配 custom，或修正契约本身）。
- 跳过 `soft_mounts` 的逐条人工核对。
- 只跑自检就宣称同步完成（受影响功能的最小测试也要跑）。
