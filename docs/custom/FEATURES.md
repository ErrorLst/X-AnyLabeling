# X-AnyLabeling fork 自研功能索引（FEATURES.md）

本文件给人读：职责、入口、挂载点、依赖的上游状态、行为级契约与已知坑。
与另外两份文档的分工：

- `docs/custom/contract.json` —— 机器可读的唯一事实源：路径、挂载点锚点 + 行号快照、
  上游依赖符号。只放能被脚本判定的硬数据。
- 本文件 —— 人读叙述。**不写行号**，不重复 contract.json 的硬数据；锚点原文与行号
  快照一律以 contract.json 为准（行号会漂移，锚点不会）。
- `docs/custom/MAP.md` —— 入口主链、热文件表与「想改 X 看哪里」定位表。

坐标系（三处 custom 一一对应）：

    anylabeling/custom/<feature>/   实现代码（自研代码只放这里）
    tests/custom/<feature>/         与实现一一对应的测试
    docs/custom/                    契约与人读文档（contract.json / FEATURES.md / MAP.md）

改动自研功能前后各跑一次 `tests/custom/test_fork_contract.py`（纯标准库、1 秒级）；
新增功能的完整步骤见 `.dsh/skills/xal-add-custom-feature/SKILL.md`。

## 总览

| 功能 | 一句话职责 | 代码 | 测试 | 挂载点 | 软挂载 |
|---|---|---|---|---|---|
| edit_extras | 普通滚轮 = 以光标为中心缩放 | `anylabeling/custom/edit_extras/` | `tests/custom/edit_extras/` | 1 个（1 行 import + 1 行调用） | 1 |
| ensure_label_file | 打开无标注图片时自动建同名空 json | `anylabeling/custom/ensure_label_file/` | `tests/custom/ensure_label_file/` | 1 个（1 行 import + 1 行调用） | 1 |
| model_validation | 模型验证子窗口（数据集上跑推理出报告；改标注在主窗口） | `anylabeling/custom/model_validation/` | `tests/custom/model_validation/` | 5 个（import、菜单 action 定义与挂载、方法定义、方法内调用） | 1 |
| smudge_tool | 涂抹修复：取别处纹理覆盖缺陷并撤销 | `anylabeling/custom/smudge_tool/` | `tests/custom/smudge_tool/` | 1 个（1 行 import + 1 行调用） | 5 |
| rename_tool | 按主分类批量重命名并打包成 zip（拖拽目录一键导出，源目录只读） | `anylabeling/custom/rename_tool/` | `tests/custom/rename_tool/` | 1 个（1 行 import + 1 行调用） | 1 |
| label_filter | 按标签分类过滤文件列表（Tool 菜单运行时追加，实例级包装 import_image_folder） | `anylabeling/custom/label_filter/` | `tests/custom/label_filter/` | 1 个（1 行 import + 1 行调用） | 2 |
| crash_log | 崩溃与运行日志落盘 `~/.xanylabeling/logs/xany-*.log`（faulthandler + 异常钩子 + Qt 钩子；异常退出检测） | `anylabeling/custom/crash_log/` | `tests/custom/crash_log/` | 1 个（1 行 import + 1 行调用） | 0 |

依赖分类的含义（下表每行都标一个）：

- `direct`：custom 代码直接读或写该上游状态，contract.json 已登记。
- `wrapped`：上游方法被实例级包装（软挂载），行为是「上游原实现 + 叠加」，不在上游 diff 里。
- `mirrored`：custom 里复刻了上游的分支条件，上游改了必须同步改 custom。
- `transitive`：只能间接得到的状态（例如磁盘路径由上游若干属性组合而来）。

## edit_extras

### 职责

普通滚轮 = 以光标为中心缩放（等价 Ctrl+滚轮）。Alt+滚轮微调选中矩形、Shift+滚轮拖动
对比分割线这两个上游手势保持原样；画笔模式下滚轮仍归画笔半径。

### 代码与体量

`anylabeling/custom/edit_extras/`（2 个文件 181 行）；测试 `tests/custom/edit_extras/`（2 个文件 496 行）。

### 入口符号

`anylabeling.custom.edit_extras.install_edit_extras`（幂等，装一次）、
`anylabeling.custom.edit_extras.wheel_zoom.install_wheel_zoom`。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/views/labeling/label_widget.py`：`from anylabeling.custom.edit_extras import install_edit_extras`
- `anylabeling/views/labeling/label_widget.py`（`LabelingWidget.__init__`）：`install_edit_extras(self)`
- 软挂载：`Canvas` 的事件过滤器，由 `WheelZoomFilter` 在
  `anylabeling/custom/edit_extras/wheel_zoom.py` 里安装，上游文件零改动。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.canvas` | direct | 事件过滤器的宿主 |
| `Canvas.zoom_request` | direct | 以光标位置发缩放请求 |
| `Canvas.editing`、`Canvas.selected_shapes` | direct | 判断 Alt+滚轮是否归画布 |
| `Canvas.enable_wheel_rectangle_editing`、`Canvas.auto_highlight_shape` | direct | 同上 |
| `Canvas.compare_pixmap` | direct | 判断 Shift+滚轮是否归画布 |
| `Canvas.is_brush_mode` | direct | 画笔模式让出滚轮 |
| `Canvas.wheelEvent` | mirrored | `_canvas_owns_wheel` 的分支条件照抄上游分支 |
| `Shape.shape_type`、`Shape.locked` | direct | 判断选中的是不是可微调的矩形 |

### 行为级契约（不可机器校验）

- `Canvas.wheelEvent` 里判断「哪些滚轮事件归画布」的分支被镜像到
  `_canvas_owns_wheel`：上游新增/调整分支时两边必须一起改，否则会出现
  「某手势既缩放又执行原动作」或「某手势彻底失效」。
- 纯水平滚轮（`angleDelta().y() == 0`）先被过滤器吃掉，避免上游把它当成向下滚轮。
- 安装是幂等的：重复调用复用同一个 `WheelZoomFilter`，不会叠加第二个过滤器。

### 测试

`python -m pytest -p no:cacheprovider tests/custom/edit_extras -v`（需 PyQt6）。
本机无 PyQt6，未能运行。

### 已知坑

- 过滤器装在 `canvas` 上，画布被替换（切换图片不会替换画布，重建窗口会）时必须重装。
- 事件过滤器只在 `obj is self._canvas` 时生效，子控件上的滚轮不走这里。
- 缩放请求的坐标是 `event.position().toPoint()`，与上游 Ctrl+滚轮一致。

## ensure_label_file

### 职责

打开一张没有标注文件的图片、且加载成功后，自动在同名 json 路径写一个空标注文件。
文件内容与落盘格式完全交给上游 `save_labels`，本功能只决定「什么时候需要建」。

### 代码与体量

`anylabeling/custom/ensure_label_file/`（2 个文件 173 行）；测试 `tests/custom/ensure_label_file/`（2 个文件 966 行）。

### 入口符号

`anylabeling.custom.ensure_label_file.install_ensure_label_file`（幂等安装）、
`anylabeling.custom.ensure_label_file.ensure_label_file`（判定 + 落盘）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/views/labeling/label_widget.py`：`from anylabeling.custom.ensure_label_file import install_ensure_label_file`
- `anylabeling/views/labeling/label_widget.py`（`LabelingWidget.__init__`）：`install_ensure_label_file(self)`
- 软挂载：`LabelingWidget.load_file` 被实例级包装，包装体在
  `anylabeling/custom/ensure_label_file/label_file.py`；只有上游返回成功才补建，异常一律吞掉。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.load_file` | wrapped | 包装点：成功后触发补建 |
| `LabelingWidget.save_labels` | direct | 唯一的落盘实现，写入空 json 并设置标注文件实例 |
| `LabelingWidget.get_label_file` | direct | 解析目标 json 路径 |
| `LabelingWidget.has_label_file` | direct | 建完文件后同步 delete 动作 |
| `LabelingWidget.filename` | direct | 判定当前是否加载了图片、是否为 json |
| `LabelingWidget.label_file` | direct | 非 None 说明上游已加载到标注文件 |
| `LabelingWidget.dirty` | direct | keep_prev 且未自动保存时会残留上一张的 shapes |
| `LabelingWidget.image` | direct | 图片确实解码成功 |
| `LabelingWidget.label_list` | direct | 必须为空，写出的才是空文件 |
| `LabelingWidget.canvas` | direct | 画布上没有残留 shape |
| `LabelingWidget.actions` | direct | 取 delete_file 动作 |
| `LabelingWidget.delete_file`（`actions` 字段） | direct | `set_clean` 在「没有标注文件」时禁用了它 |
| `LabelingWidget._config` | transitive | 上游 `save_labels` 内部读的配置 |
| `LabelingWidget.image_path`、`other_data`、`flag_widget`、`_annotation_checked` | transitive | 上游保存路径沿途会读写的状态 |

### 行为级契约（不可机器校验）

- 判定条件（`_missing_label_file`）：已加载图片且 `filename` 不是 json；`label_file`
  为 None；`dirty` 为假；`canvas.shapes` 为空；`image` 非空且未 null；
  `label_list` 为空；`get_label_file()` 解析出的路径**不存在**（`osp.lexists`）。
  全部满足才交给 `save_labels`。
- 落盘复用上游：json 的内容、缩进、`imagePath`/`imageData` 写法都随上游 `save_labels` 变，
  custom 不复制这套逻辑。
- 代价：`save_labels` 会覆盖目标文件，因此「检查存在」与「写盘」之间被别的进程
  抢先建出的 json 会被覆盖（接受的窄窗口）。
- 建完之后调 `actions.delete_file.setEnabled(has_label_file())`：上游 `set_clean` 在
  「没有标注文件」的分支里禁用了 delete 动作，那是文件存在之前做的决定，现在要补回来。
- 安装幂等：重复安装不会把 wrapper 套在 wrapper 上（否则一次加载会保存两次）。

### 测试

`python -m pytest -p no:cacheprovider tests/custom/ensure_label_file -v`（需 PyQt6）。
本机无 PyQt6，未能运行。

### 已知坑

- 只有 `load_file` 返回 `True`（上游成功语义）才补建；返回其它值一律不动。
- `label_list` 为空是必要条件：否则会把上一张图的标注写成新文件的初始内容。
- 上游若把 `get_label_file` 改成「无标注文件时返回 None」，这里会直接跳过（安全失败）。

## model_validation

### 职责

模型验证子窗口：在数据集上跑桌面端既有推理引擎，逐图出预测、人工判定、导出报告。
窗口本身**不带标注编辑器**：需要改标注时由主窗口**自动跟随**结果页的当前记录打开
（点选 / `A` / `D` / 过滤器切换 / 推理结束，200ms 去抖，无需按键），主窗口保存的结果
由文件监听同步回暂存标签，验证页只重新读盘。

### 代码与体量

`anylabeling/custom/model_validation/`（23 个文件 11961 行，含 `ui/` 子包）；
测试 `tests/custom/model_validation/`（44 个文件 19215 行）。（口径：目录内全部 `*.py`、
排除 `__pycache__`，行数取 `wc -l`。）「编辑搬到主窗口」那一轮新增
`main_window_bridge.py`（671 行：跳主窗口 + 保存回写）与 `async_scan.py`（189 行：
异步目录扫描），并删掉 `ui/` 下的 `label_dialog.py`（内置标签弹窗整个文件移除）；
本轮（固定 0.25 + 多标签 + 低分 NG）只改既有文件，新增测试
`test_mv_multilabel.py`（27 例）与 `test_mv_low_score.py`（10 例）。

### 入口符号

`anylabeling.custom.model_validation.launch_model_validation`（惰性启动、复用实例）、
`anylabeling.custom.model_validation.ui.dialog.ModelValidationDialog`（验证窗口本体）、
`anylabeling.custom.model_validation.main_window_bridge.MainWindowBridge`（`StagingSync`
回写）、`anylabeling.custom.model_validation.async_scan.DirectoryScanScheduler`（扫描调度）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

共 5 处，全部在 `anylabeling/views/labeling/label_widget.py`：

1. 模块级 import：`from anylabeling.custom.model_validation import launch_model_validation`
2. `LabelingWidget.__init__` 里的菜单动作定义：`model_validation = action(`（action 块本体）
3. `LabelingWidget.__init__` 里把动作挂进菜单：`model_validation,`
4. 上游文件里唯一新增的方法定义：`def open_model_validation(self):`
5. 该方法体内的一行调用：`launch_model_validation(self)`

本轮「编辑搬到主窗口」的重构**没有新增任何上游挂载点**：跳转、回写与异步扫描全部落在
`anylabeling/custom/model_validation/` 内（`label_widget.py` 保持这 5 处不变）。

软挂载：`widget._model_validation_dialog`（自研属性）由 `launch_model_validation` 持有，
窗口销毁时清空，因此反复点菜单只复用同一个窗口。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.menus` | direct | 菜单动作挂载点 |
| `LabelingWidget.error_message` | direct | 启动失败时的统一报错 |
| `LabelingWidget.load_file` | direct | 在主窗口打开一条记录的暂存图片（跳转的唯一入口） |
| `LabelingWidget.may_continue` | direct | 跳转前确认主窗口没有未保存的标注 |
| `LabelingWidget.output_dir` | direct | 非空时警告「编辑不会进入导出」并请用户确认 |
| `LabelingWidget.filename` | transitive | 不直接读该属性：`load_file` 装载的当前图片；跳转依赖其兄弟 json 约定 |
| `LabelingWidget.window()` | direct | `dialog._activate_target()` 取顶层窗口，装「主窗口被激活 → 抬升验证窗口」的过滤器；基类 API 按约定不进 contract.json |
| `anylabeling.config.current_config_file` | direct | 推理前确保全局 rc 路径可用 |
| `anylabeling.config.get_work_directory` | direct | 同上，兜底拼 `.xanylabelingrc` |
| `anylabeling.views.labeling.utils.opencv.qt_img_to_rgb_cv_img` | direct | 把 QImage 解码成 RGB 数组 |
| `anylabeling.services.auto_labeling.engines.OnnxBaseModel` | direct | 读输入形状、做元数据校验 |
| `anylabeling.services.auto_labeling.__base__.yolo.YOLO` | direct | 复用上游预处理/NMS/建 shape 的整条链；另依赖 v8 分支的 `multi_label` config 键（本工具经 `TASK_FAMILY_MAP` 只会走 v8 分支），契约登记见下节 |
| `anylabeling.app_info.__preferred_device__` | direct | 线程池预算会话选 CPU/GPU |

### 行为级契约（不可机器校验）

- **复用约定**：判定结果必须来自上游 `YOLO` 的同一套预处理、NMS 与 shape 构造，
  custom 只做编排（数据集分批、并发、判定、报告），不重写检测后处理。
- `ThreadedOnnxSession` 只镜像 `OnnxBaseModel` 被 `YOLO` 用到的那部分接口
  （`sess_opts`/`providers`/`ort_session`/`get_ort_inference`/`get_input_name`），
  并把两个线程池钉到单 worker 预算；上游改这些成员名时这里要跟着改。
- 推理前必须让 `anylabeling.config.current_config_file` 有值（上游模型基类会读全局配置），
  未设置时用 `get_work_directory()` 兜底。
- `classes.txt` 是类名权威：模型内嵌 names 与它不一致只提示不阻断，只有**类数量**不一致才报错。
- 启动器把重依赖（onnxruntime、albumentations、Qt 对话框）放在函数体内 import，
  保证应用启动只付一次 `import` 的成本。
- **编辑搬到主窗口 + 自动跟随当前记录**：验证页不再自带编辑器，也没有「在主窗口编辑
  当前记录」的按键——`E` 快捷键（`open_in_main_shortcut`）、`open_in_main_requested`
  信号与提示/气泡里的「E = 在主窗口编辑当前记录」文案一并删除，只剩 `A`/`D`。结果页
  把「当前记录真的换了」统一发成页面级信号 `current_record_changed(record_id)`：点选
  另一行、`A`/`D`、过滤器切换、一轮推理后首次填充、`record_changed` 同步刷新之后；
  同一条记录重复触发不发。dialog 用它接上既有的 `MainWindowBridge.open_record` 入口
  `_open_current_in_main_window`，中间是 **200ms 重启式防抖**（`FOLLOW_DEBOUNCE_MS`、
  `follow_timer`）：连续 `A`/`D` 或快速点选只打开最后停在的那一条。原来的
  `QTimer.singleShot(0, ...)` 自动跳转已删除，推理结束后的首屏由同一条跟随路径覆盖，
  不保留第二条调用。
- **跟随的三个前提与去重**：只在 `bridge.main_window is not None`、验证窗口可见
  （`isVisible()`）且结果页是当前页（`stack.currentWidget() is results_page`）时才跟随；
  检查发生在防抖计时器触发时，因此「推理结束 → 切到结果页」也在 200ms 之后照常跟随。
  `_followed_record_id` 记住上一次自动打开的记录 id，同一条记录重复触发不再调用
  `open_record`（过滤器切回同一条、同步刷新都不会重开，也不会多问一次 `may_continue`），
  被闸门拒绝的记录同样不重试；`start_validation` 开新一轮时清空跟随状态，本轮的第一条
  记录照常打开。`__main__` 独立启动（无主窗口）时静默不跟随，只在第一次写一行提示，
  之后切换不再刷状态行。
- **跳转前的安全闸门**：无主窗口 / 无记录 / 暂存图片缺失 / 该记录没有标签 /
  标签读不了或缺 `shapes`、`imagePath` / 主窗口 `output_dir` 非空（弹确认）/
  `may_continue` 为假 / sibling json 写不出来。任一道不过只写一行状态说明，
  绝不抛异常、绝不跳转。
- **跟随不抢焦点**：跳转只切换主窗口的当前文件。`MainWindowBridge.open_record()`
  既不抬升也不激活主窗口：原来的 `_activate` 方法连同它的调用已删除，
  `main_window_bridge.py` 全文对 `window()` / `raise_` / `activateWindow` /
  `showNormal` 的实际调用为 0（grep 一条也搜不到，只剩「不抬升、不夺焦点」的
  docstring 说明）。上游 `load_file` 结尾的 `canvas.setFocus()`（上游不可改）
  会把焦点交给主窗口里的画布，所以
  `anylabeling/custom/model_validation/ui/dialog.py` 的
  `_open_current_in_main_window()` 只在 `bridge.open_record(record)` 返回 True
  （成功）时才排一次 `QTimer.singleShot(0, self._restore_validation_focus)`；
  被拒绝的跟随不排、不抢焦点。`_restore_validation_focus()` 先看 `isVisible()` /
  `isMinimized()`，窗口不可见或已最小化就直接 return；否则 `self.activateWindow()`
  + `self.results_page.focus_results()`，焦点落到结果页的记录列表（`A` / `D` 仍
  只在结果页作用域生效）；整段 `RuntimeError` 兜底，窗口已销毁时静默。
- **桥接状态双写**：桥接的每一条状态/拒绝信息（跟随被拒、没有主窗口、暂存图片缺失、
  无标签项、`may_continue` 为假等）由 `_show_status_message` 同时写进配置页状态行与
  结果页汇总行。跟随只可能发生在结果页可见时，只写配置页会让用户以为「没反应」；
  结果页那一行会在下一次导出时交还给导出汇总。
- **兄弟 json 适配**：主窗口按「图片同名 json」找标注，而暂存把标签放在 `labels/`；
  跳转前把 canonical 暂存标签刷成图片旁的 sibling json（硬链接优先，失败降级 `copy2`），
  且以 canonical 为准（导出与结果页读的就是它）。
- **保存回写**：`StagingSync` 用 `QFileSystemWatcher` 盯暂存目录与 sibling，300ms
  去抖后把主窗口保存的 sibling 文本写回 canonical 标签（普通重写、不 replace，
  保住 inode 与硬链接）；坏 json 与半写文件直接丢弃，同一条记录每次去抖只发一次
  `record_changed`。
- **`record_changed` 只做幂等刷新**：`reload_record` + 导出汇总刷新，不重建列表、
  不从信号里再触发跳转（当前记录没变就不发 `current_record_changed`，因此也不会重开）；
  `attach(staging_root, records)` 在 `on_worker_finished` 装上，
  `start_validation` 与 `closeEvent` 时 `detach`。
- **内置编辑能力已拆除**：`image_view.py`（2200→1127 行）画布只读，不再有任何编辑
  信号/方法/常量（`set_edit_mode`、`editable_shapes`、`box_handles`、`hit_test`、
  `resize_points`、`_draw_edit_overlay` 等全删）；结果页删除编辑模式、编辑提示条与
  标签弹窗调用（`EDIT_*` 符号与 `__all__` 条目一并删）；`records.py` 删除
  `update_shape` / `update_shape_points` / `label_shape_types` 等写标注函数
  （保留 `read_staging_label` / `write_staging_label` 与 `edited` 字段）；
  `label_dialog.py` 整个文件删除。
- **数据集扫描异步化**：`async_scan.py` 的 `DirectoryScanScheduler` 只留最新请求、
  同一时刻最多一个 `DirectoryScanWorker(QThread)`；dialog 侧 400ms 防抖 + token 丢弃
  过期结果，预览行在前台显示「扫描中…」。UI 线程不再跑 `collect_pairs`，
  `_count_source_pairs` 只读缓存。
- **导出非模态**：导出的 `QProgressDialog` 由 `WindowModal` 改 `NonModal`，导出期间
  禁用导出按钮（`_exporting` 标志 + try/finally 防重入）；验证窗口本体本来就是非模态
  `Qt.Window`，导出不再把主窗口一起挡住。
- **导出进行中不能关窗**：`closeEvent` 在 `_exporting` 为真时直接 `event.ignore()`
  并提示「导出进行中，请稍候再关闭」。进度框改成非模态后，用户此前能在导出中途
  关掉窗口，让还在写盘的导出踩到已销毁的控件、拖垮整个应用；现在关窗请求被拒，
  窗口留到 zip 写完，导出收尾（关进度框、还原导出按钮与结果行）对已销毁控件也
  一律 `RuntimeError` 兜底。
- **主窗口激活时自动抬升**：验证窗口给主窗口装一个事件过滤器
  （`showEvent` → `_install_activate_filter`，幂等；`closeEvent` →
  `_remove_activate_filter`），收到 `WindowActivate` 后延后一拍
  `QTimer.singleShot(0, self.raise_)` 把自己抬到主窗口上方，用户在主窗口里编辑
  不再让验证窗口被压在下面。**不夺焦点**：全程不调 `activateWindow()`，画布继续
  可编辑；**不全局置顶**：不用 `WindowStaysOnTopHint`，不会浮到浏览器等其它应用
  之上；**尊重用户状态**：`isVisible()` 为假或 `isMinimized()` 为真时不抬；
  `__main__` 独立启动（`_main_window()` 为 None）时不装过滤器。
- **推理过滤固定 0.25**：推理期的置信度过滤只有 `app_config.INFERENCE_CONF_THRESHOLD`
  （= 0.25）一个来源。`ModelRunner` 与 `build_runner_pool` 都不再接收 `conf_threshold`
  形参，只把模块常量写进喂给上游 `YOLO` 的 config；报表快照
  `ValidationConfig.to_dict()` 记录 `inference_conf_threshold`（=0.25）与
  `inference_conf_threshold_fixed`（=True）。配置页那个分数阈值**不再是推理过滤阈值**，
  它只作 NG 判定规则（见下一条）。改这两个字段名或让页面值重新喂进推理，都是行为回归。
- **多标签：一个框多行标签**：上游按 multi-label NMS 为同一个框返回多条**共享坐标**的
  记录，`inference.merge_multilabel_predictions` 按
  `(shape_type, 坐标四舍五入到 1e-6)` 分组后合并：整组里最高分那条留作主
  `label`/`score`，**仅当行数 > 1** 时挂上平行的、按分数降序的 `labels`/`scores`
  （单标签 payload 与改动前**逐键相等**）。只对 **detect/segment** 开（`obb`/`pose`
  保持单标签，理由：本工具定位检测类框验证）；GT 侧保持单标签。画布
  `shape_label_rows` / `_stacked_glyphs` 逐行绘制整块标签，行距
  `LABEL_LINE_SPACING = 1.0` widget 像素（整块夹取与丢弃，**不逐行截半**）。
- **低分判 NG**：`judge.LOW_SCORE`，原因优先级
  `CLASS_MISMATCH → LOW_SCORE → IOU_BELOW → MISS_FP`；`judge_record(..., ng_score_threshold=None)`
  是可选**尾参**，不传 = 规则关闭（与旧行为逐键相等）。detail 记 `ng_score_threshold`、
  `low_score`（每项 `{index, label, score}`，`index` 与 `false_positives` /
  matched pair 的 `pred_index` 同坐标系）与每个 matched pair 的 `low_score` 布尔。
  画布上匹配对里低分的框为**青绿** `LOW_SCORE_COLOR`：`matched_pair_state` 的判定
  顺序是 `mismatch → low_score → iou_below → OK_PAIR`，**低分不论 IoU 是否达标都算**；
  未匹配的低分框仍是品红误报。图例第六项「低分」；配置页标题为「**NG 分数 score**」
  （属性名/默认 0.5/范围/步进不变），tooltip 说明「≤0.25 时不会有框低于它，
  LOW_SCORE 永不触发」。

### 上游改动（挂载点之外，需逐行审计）

本功能对上游文件只有 1 行功能性改动，逐字给出前后原文，供同步上游时核对：

文件：`anylabeling/services/auto_labeling/__base__/yolo.py`，第 388 行
（v8 分支的 `non_max_suppression_v8(...)` 调用点）

原：`                multi_label=False,`
新：`                multi_label=self.config.get("multi_label", False),`

说明：默认 `False` ⇒ 主窗口自动标注等既有调用方行为零变化；v5 分支（约 354 行）
保持 `multi_label=False` 不动。本工具经 `TASK_FAMILY_MAP` 只会走 v8 分支，
因此这条改动只影响本工具传入的 config。

### 测试

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p no:cacheprovider
tests/custom/model_validation -v`（需 PyQt6 + numpy，本工作区用仓库里的 `.venv`）。
本轮删掉 5 个编辑用例（`test_mv_edit_mode.py`、`test_mv_edit_drag.py`、
`test_mv_edit_persist.py`、`test_mv_edit_exit_on_switch.py`、`test_mv_label_dialog.py`），
新增 `test_mv_main_window_bridge.py`（22 例）、`test_mv_staging_sync.py`（14 例）、
`test_mv_async_scan.py`（6 例）、`test_mv_dialog_wiring.py`（21 例）、
`test_mv_export_nonmodal.py`（6 例）、`test_mv_raise_on_activate.py`（12 例）；
固定 0.25 + 多标签 + 低分 NG 这一轮又新增 `test_mv_multilabel.py`（27 例）与
`test_mv_low_score.py`（10 例），并改写 `test_mv_records_labelme.py`、
`test_mv_image_view.py`（22 例）、`test_mv_ui_dialog.py`、`test_mv_cancel.py`、
`test_mv_status_colors.py`、`test_mv_inference_runner.py`、`test_mv_infer_workers.py`。
每条例数都用 `--collect-only -q` 逐个文件核实过（口径见上）。

### 已知坑

- 上游文件里唯一新增的函数体是 `open_model_validation`；上游若在 `LabelingWidget`
  近邻新增同名方法或改菜单挂载写法，锚点会失配，先跑自检再动手。
- `__preferred_device__` 是 `anylabeling/app_info.py` 里 `__getattr__` 动态提供的名字，
  普通 IDE 跳转看不到定义。
- 会话只换线程预算，不改 provider 选择逻辑；GPU 仍由上游配置决定。
- 跳转依赖上游 `load_file` 的「图片同名 json」约定，以及 `may_continue` / `output_dir`
  这两个属性名：改名不会报错（`getattr` 兜底），但会静默降级成「不检查」；
  `window()` 是 `QWidget` 基类 API，按契约约定不进 contract.json。
- 结果页的 `edited` 标记来自 `records.edited`（主窗口保存回写时置位），
  不再由验证窗口自己的编辑动作产生。
- 抬升只在 **窗口管理器真的把 `WindowActivate` 发给顶层窗口** 时发生：X11 上
  点主窗口的标题栏/画布会把激活交给主窗口，抬升照常生效；但也有 WM 把点击当作
  「不改变激活窗口」的焦点翻转（focus-follows-mouse、部分平铺 WM 的
  raise-on-click 关闭时），主窗口本来就没被激活，验证窗口也就不会被压下去，
  这正是要的效果。抬升用 `raise_()` 而不是置顶，WM 仍可自行决定是否理会。
- 过滤器的目标是 `main_window.window()`（`LabelingWidget` 不是顶层窗口，
  `WindowActivate` 只发给顶层窗口）；`_main_window()` 为 None 时整条链路是空操作。
- **Wayland/WSLg 上回抢焦点可能失败**：`_restore_validation_focus()` 的
  `activateWindow()` 是应用自身的请求，Wayland 合成器可以忽略（WSLg 走的就是
  Wayland），上游 `canvas.setFocus()` 于是把键盘留在主窗口，跟随之后 `A` / `D`
  不再响应，要手动点回验证窗口；X11 下正常。这是平台限制，不是跟随逻辑的缺陷
  （本项目就在 WSLg 上运行，该平台差异已确认存在）。

## smudge_tool

### 职责

涂抹修复：右键取源点、左键拖框，用别处纹理覆盖缺陷区域，写回原图并支持 Ctrl+Z 撤销。

### 代码与体量

`anylabeling/custom/smudge_tool/`（4 个文件 2474 行：`texture_fill.py` 算法、
`operations.py` 读写/备份/几何、`smudge_filter.py` Qt 层、`__init__.py` 导出）；
测试 `tests/custom/smudge_tool/`（6 个文件 3382 行，含写回编码参数、TIFF
Orientation 与画布绘制态的回归）。

### 入口符号

`anylabeling.custom.smudge_tool.install_smudge_tool`（幂等安装）、
`anylabeling.custom.smudge_tool.SmudgeController`（按钮、事件过滤器、覆盖层、撤销栈）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/views/labeling/label_widget.py`：`from anylabeling.custom.smudge_tool import install_smudge_tool`
- `anylabeling/views/labeling/label_widget.py`（`LabelingWidget.__init__`）：`install_smudge_tool(self)`
- 软挂载（5 处，全在 `anylabeling/custom/smudge_tool/smudge_filter.py`）：
  - `LabelingWidget.populate_mode_actions` ← `_wrap_populate_mode_actions`
  - `LabelingWidget.import_image_folder` ← `_wrap_import_image_folder`
  - `Canvas.mouseMoveEvent` ← `_wrap_canvas_mouse_move`
  - `Canvas` 事件过滤器 ← `SmudgeController`
  - `Canvas.set_editing` ← `_wrap_canvas_set_editing`

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.populate_mode_actions` | wrapped | 工具栏重建后重新挂按钮 |
| `LabelingWidget.import_image_folder` | wrapped | 换目录时清空撤销历史 |
| `Canvas.set_editing` | wrapped | 任何画布模式切换都先退出涂抹模式（按钮回弹、覆盖层收回、恢复编辑态），再由上游切换 |
| `Canvas.mouseMoveEvent` | wrapped | 上游把它改回箭头光标后，包装体把模式十字放回去 |
| `LabelingWidget.canvas` | direct | 事件过滤器宿主与视图刷新 |
| `LabelingWidget.tools` | direct | 工具栏：按钮加进去、撑高 |
| `LabelingWidget.actions` | direct | 与上游动作共存 |
| `LabelingWidget.actions.edit_mode` | direct | 安全地请上游把画布切回编辑模式（触发它，而不是直接改 `canvas.mode`） |
| `LabelingWidget.set_edit_mode` | direct | 拿不到编辑动作时的回退：请上游把画布与工具栏切回编辑模式 |
| `LabelingWidget.actions` 的 11 个 `create_*` 绘制动作 | direct | 进入时连接其 `triggered`、退出时断开；保持可用，触发被接管（见行为级契约） |
| `LabelingWidget.filename`、`image_path` | direct | 解析当前图的磁盘路径 |
| `LabelingWidget.image_data` | transitive | 确认画布上确有图像 |
| `LabelingWidget.brightness_contrast_processor`、`brightness_contrast_values` | transitive | 刷新视图时不覆盖显示参数 |
| `LabelingWidget.status`、`statusBar`、`error_message` | direct | 状态与报错出口 |
| `Canvas.transform_pos`、`offset_to_center`、`scale`、`out_off_pixmap` | direct | 屏幕坐标与图像坐标互换 |
| `Canvas.load_pixmap`、`pixmap` | direct | 写回后重载画面 |
| `Canvas.override_cursor`、`restore_cursor` | direct | 模式光标；让路给绘制模式时不弹栈（见行为级契约） |
| `Canvas.editing` | direct | 退出前确认画布真的回到编辑态，没回到就补一次 `set_editing(True)` |
| `Canvas.is_loading` | direct | 上游加载中时不抢事件 |
| `Canvas.is_brush_mode`、`is_magic_wand_mode`、`drawing` | direct | 判断是否已切到别的绘制模式（拒绝叠加） |
| `Canvas.current` | direct | 是否有未收尾图形（有则拒绝切换） |
| `Canvas.is_auto_labeling` | direct | 是否自动标注会话（有则拒绝，编辑动作会清标记） |
| `anylabeling.views.labeling.utils.image.img_data_to_pil` | direct | 备份/读取路径上的图像转换 |

### 行为级契约（不可机器校验）

- **工具栏重建后按钮回来**：上游 `populate_mode_actions` 会先清空工具栏再重新添加动作，
  包装体在调用原实现之后重新 `addAction` 自己那个 already-created 的按钮；
  原方法体一行未改，包装只叠加，且只包装一次。
- **原图备份**：第一次改写某个文件前，把原图复制到
  `operations.default_backup_dir()`（系统临时目录下的 `dsh-smudge/<时间戳>-<pid>`），
  同名冲突加数字后缀；只复制、不移动、不删除，一个目录里同一文件只备份一次。
- **撤销**：历史在内存里按图片分组，记录 ROI、原像素块与磁盘路径；Ctrl+Z 把像素放回
  屏幕与磁盘。
  - **保留范围**：切图**不丢**——同一文件夹里的每张图各有一份自己的步骤记录，
    切走再切回这张图，之前的步骤仍在，Ctrl+Z 照常把像素放回屏幕与磁盘；
    换文件夹（打开另一个目录，或经 `LabelingWidget.import_image_folder` 打开
    另一个目录的文件）才把上一批图的步骤全部丢掉，进程结束也丢（磁盘上的备份
    一直保留）。「重新打开同一个文件夹」不算换文件夹：目录按规范路径比较
    （`_folder_changed` 对两边都做 `normcase + normpath + abspath`），命中同一个
    目录时历史留着。
  - **模型验证工作流**：验证窗口的「自动跟随」用 `LabelWidget.load_file(单张
    暂存图片)` 切图，主窗口的文件列表因此跟着导入**同一个**暂存图片目录；这属于
    「重新打开同一个文件夹」，不触发清空，所以涂完 A、跟到 B、再回到 A 时 A 的
    步骤仍在，Ctrl+Z 能撤销。
  - **只对当前图生效**：Ctrl+Z 撤销的是**当前显示**那张图的最后一步；别的图的步骤
    留在历史里、等切回那张图才能撤销（`_can_undo` 只看当前文件），状态栏的
    「已涂抹修复 · 可撤销 N 次」也只在模式开着时按当前图计数。
- **写回保留原编码参数（按格式分流）**：读图时从已打开的 PIL 对象取出参数，
  写回时只把属于该格式的参数交给 Pillow（门控见 `operations.JPEG_FORMATS`、
  `operations.ICC_FORMATS` 与 `operations.EXIF_FORMATS`）：
  - JPEG 与 MPO：量化表、色度抽样因子、ICC、EXIF。Pillow 对多图 JPEG 报
    `format == "MPO"`，它与 JPEG 同族，抽样因子同样保留；MPO 写回为单帧
    JPEG，多帧 MPO 的其余帧会丢失（与旧行为一致）。
  - PNG：ICC 与 eXIf。
  - TIFF 及其他格式：**仍走旧路径，一个参数都不传**，与旧行为逐字节一致。
    ICC 与 EXIF 一样按格式门控，但两者纳入的原因不同（见
    `operations.ICC_FORMATS`）：EXIF 是因为 TIFF 的 IFD 合并会覆盖宽高，
    ICC 是纯载荷、没有 tag 合并语义，门控只为守住「TIFF 一个参数都不传」
    的基线——writer 会把参数写成自己的 IFD tag 34675（源文件不带 profile
    时旧代码也不带该 tag），收集侧与写出侧都不认它，带 profile 的 TIFF
    写回后与旧路径逐字节一致。
    TIFF 的 `getexif()` 会把整个 IFD 读进来（含 256 ImageWidth / 257
    ImageLength / 278 RowsPerStrip / 284 PlanarConfiguration），回传后 writer
    会把这些 tag 合并进新 IFD 并**覆盖它自己刚设好的宽高**：Orientation 5~8 的
    TIFF 必然写坏（Pillow 读图时已按 Orientation 就地旋转像素并交换
    `image._size`，IFD 里还是旋转前的宽高），`PlanarConfiguration=2` 的文件
    则被标成 chunky。
  只改框内像素时整图不再按默认 q75/4:2:0 重编码（q95/4:4:4 由约 25 dB 提升到
  约 54 dB）。抽样因子的唯一来源是 `JpegImagePlugin.get_sampling(im)`（`im.info`
  里没有该键，传路径/句柄得 -1）；灰度图返回 -1，写回为 4:4:4（CMYK 与非标准
  sampling 元组同样返回 -1，CMYK 在读取时就被拒）。参数被写入器拒绝时回退到
  普通保存，绝不让整张图写不出去；回退后磁盘上是 Pillow 默认参数，而
  `_work_info` 仍是读入时的参数，下次写回会再试一次原参数（只多一次被拒的
  尝试，不影响像素）。
- **几何门槛**：ROI 小于 6 像素拒绝执行；源点必须先右键选；没有磁盘文件的图像拒绝写回。
- **小框填充（对齐贴片）**：ROI 两边都 <= 17 像素时它小于一个纹理块（块 24 像素），
  匹配窗口里没有任何已确定像素：这类块不再被跳过（跳过会留下缺陷像素），而是按
  「块在 ROI 内的相对偏移」从源窗口同位置取同尺寸贴片（源窗口与 ROI 同尺寸，所以
  小框等价于把源点处的同尺寸纹理 1:1 盖过来）。贴片同样记入「已确定像素」，后续块
  据此恢复正常的匹配路径。贴片落回 ROI 自身（源点就在框内、窗口与框重叠）、源窗口
  装不下对齐贴片、或根本没有源窗口时仍然跳过；整框仍可能报「未发生变化」。
- **源点标记**：右键当下就在覆盖层上画绿色十字（不依赖后续填充或画布重绘）；
  源窗口矩形只在尺寸已知时才画（拖框中或填充过一次之后）。
- **标记坐标**：覆盖层的十字与源窗口矩形都用画布局部坐标表示，`paintEvent`
  每次现算 `_canvas_origin()`（`mapFromGlobal(canvas.mapToGlobal(0,0))`）后平移
  画笔；覆盖层无论是画布的兄弟（滚动区布局）还是子控件（无父画布的单测）都对。
  `sync()` 的几何镜像仍要保留：它决定覆盖层在哪里，`_canvas_origin()` 只决定
  标记画在覆盖层的哪个位置。
- **模式光标**：进入模式设十字；上游 `Canvas.mouseMoveEvent` 在没有命中 shape 时
  改回箭头，包装体在**原实现之后**把十字放回去（事件过滤器在上游之前，压不住）；
  `_busy`（填充中，用 WaitCursor）或当前画布不可绘制时不动光标；退出模式
  `restore_cursor()` 归还。**唯一例外**是这次退出在为别的绘制模式让路
  （`_handing_over is False`，即画布正要切到 CREATE）：上游紧接着会在下一次
  移动时装上自己的绘制光标并覆盖这一条，此时弹栈只会有弹掉别人条目的风险，
  所以那一次不弹；这个标志只描述那一次退出，`_exit_mode` 在光标判定之后把它
  复位，之后的每一次退出都照常弹栈。
- **画布归属（进入即绘制态 + 工具接管）**：进入模式时由工具自己把画布切到 CREATE
  （`canvas.set_editing(False)`），**先**切换、后置 `_mode`：顺序反了的话，包装过的
  `set_editing` 会立刻把模式打回去。随后把上游 **11 个 `create_*`** 绘制动作
  （`DRAW_ACTION_NAMES`：`create_mode`、`create_brush_polygon_mode`、
  `create_magic_wand_mode`、`create_rectangle_mode`、`create_cuboid_mode`、
  `create_rotation_mode`、`create_quadrilateral_mode`、`create_circle_mode`、
  `create_line_mode`、`create_point_mode`、`create_line_strip_mode`）的 `triggered`
  信号逐个连到 `_on_draw_action`（`functools.partial` 带上动作本身，连接对象存进
  `_draw_connections`，退出时按这个字典精确 `disconnect`）。它们**保持可用**，只是
  触发被接管：见下一条与「快捷键」一条。`_mode_switched` 记住「画布欠一个编辑态」。
  动作名一律 `getattr` 取，缺动作或根本没有 `actions` 的替身（单测）全程不报错。
- **绘制类动作的接管（点按钮与按快捷键同一条路）**：涂抹模式开着时触发任一
  `DRAW_ACTION_NAMES` 动作，`_on_draw_action` 先 `_handing_over = False`（这是用户
  主动切模式，不是「让路」，与 `Canvas.set_editing` 那条让路语义区分开）→ 按钮
  `setChecked(False)` → `set_mode(False, use_action=False)`（走 `_exit_mode`：
  断连接、清标记、把画布切回 EDIT）→ **不再 `action.trigger()`**：用户自己的这次
  激活本来就会执行动作，接管槽又插在 `triggered` 上，再 trigger 一次就会让上游
  处理器跑第二遍。退出后由用户自己的激活路径执行该动作（一次操作一次副作用），
  画布照常切到它的模式（按 R 直接进矩形模式、工具栏高亮跟手）。槽位顺序两种都
  成立：接管槽在前时它先退出、上游处理器随后只跑一次；上游处理器在前时它先把
  画布切走（经包装过的 `set_editing` 让涂抹模式退出），接管槽再进来时 `_mode` 已
  是 False，直接 `return`（防递归，不重复退出、不重复执行）。
- **编辑动作不走接管**：`edit_mode` 与 `edit_brush_mode` **不在名单**——点
  「编辑对象」「画笔编辑」时工具没有连接它们的 `triggered`。上游处理器经
  `set_edit_mode` / `toggle_brush_mode` → `toggle_draw_mode(True)` → 包装过的
  `Canvas.set_editing` 让涂抹模式让路退出，天然只跑一次；对它们再挂一个接管槽
  没有意义——用户激活它们时画布本来就会经 `Canvas.set_editing` 让路退出一次。
- **菜单条目无需接管（有意为之）**：菜单条目就是 `self.actions.create_*` 的同一批
  `QAction`——`populate_mode_actions` 用 `utils.add_actions` 把它们塞进
  `menus.edit` 与 `canvas.menus[0]`——点条目等于 `QAction.trigger()` 等于发
  `triggered`，走的是与工具栏、快捷键完全相同的那条接管路，工具不需要给菜单
  装什么事件过滤器。两种槽位顺序的净结果都对：上游处理器先跑时它经包装过的
  `Canvas.set_editing(False)` 让路退出，接管槽再进来时 `_mode` 已是 False 直接
  `return`（防递归）；上游将来改成安装之后才连处理器时，接管槽先退出、处理器
  随后照跑。两种顺序都有真 `QAction` 用例钉住：处理器在前（真实窗口的顺序）
  是 `test_a_real_action_runs_once_in_the_order_of_the_widget`，接管槽在前是
  `test_a_real_action_runs_its_upstream_handler_exactly_once` 与
  `test_a_real_menu_click_runs_the_handler_exactly_once`。
- **退出即回编辑态**：`_exit_mode(use_action=True)` 先 `_disconnect_draw_actions()`
  （把 11 个 `create_*` 动作的 `triggered` 连接原样还回去）再清状态（拖框/源点/覆盖层/
  橡皮筋/工作数组），`_mode_switched` 为真时恢复编辑态：`use_action=True`（按钮、
  Esc 等工具自己发起的退出）优先 `actions.edit_mode.trigger()`，该动作缺失或
  **被禁用**（触发禁用的 `QAction` 是静默 no-op，不算已触发）时退到
  `widget.set_edit_mode()`，再退到 `canvas.set_editing(True)`；
  `use_action=False`（接管路径：用户正在激活某个绘制动作）只执行最后那一步
  `canvas.set_editing(True)`，不碰上游的编辑动作，也不碰 `widget.set_edit_mode()`
  ——两者都会再跑一遍上游处理器。**这个参数是刻意保留的**：让路路径若改用
  `use_action=True`，会在用户手势进行中再跑一次上游编辑动作（`set_edit_mode`
  → `toggle_draw_mode(True)`），把画布拉回 EDIT，结果按 R 之后停在 EDIT、矩形
  模式丢失。最后都确认 `canvas.editing()` 为真，否则补一次 `set_editing(True)`。
  退出幂等：重复调用只清已经空掉的状态。换图
  （`_adopt_file` → `_container_file`）**不退出模式**：只清本图的源点、拖框与
  工作数组，模式保持开启。
- **快捷键（全部放行，动作自己接管）**：上游任何模式切换都经包装过的
  `Canvas.set_editing`，它先让涂抹模式退出（`use_action=False`：**让路退出不代跑
  上游编辑动作**——簿记（按钮可用态、文本编辑态、自动标注清理）由发起切换的上游
  路径自己完成，本工具只保证画布交还到编辑态），把画布摆回 EDIT，然后执行上游
  自己的切换——数字键 `digit_shortcut_*` 与画笔、魔棒都走这条。字母快捷键同样
  有效：11 个 `create_*` 动作没有被置灰，Qt 的快捷键系统照常发出 `triggered`，
  由接管槽先退出涂抹模式再执行原动作（按 R 直接进矩形模式）。事件过滤器只保留
  `Ctrl+Z`（`ShortcutOverride`）与 `Esc`/`Ctrl+Z`（`KeyPress`）两条，其余
  `return False`——**不再比对 `action.shortcut()` 吃键**：那会把已经可用的快捷键
  再废一次。`Ctrl+Z` 与 `Esc` 的优先级不变。
- **让路退出为何 `use_action=False`（真实 `QAction` 探针）**：让路那次若改用
  `use_action=True`，会多跑一次上游编辑动作（`set_edit_mode()`，即
  `clear_auto_labeling_marks()` 与 `set_text_editing(True)` 各多一次），一次
  用户手势两个副作用。带选中形状切走再回来时，选中在两条路径下都由上游
  `Canvas.set_editing(False)` 自己的 `deselect_shape()` 丢掉，与该标志无关：
  `False` 挡掉的是重复的上游簿记，不是选中状态。
- **模式互斥（进入侧）**：涂抹模式与任何画布绘制/编辑模式不共存。上游每次
  `Canvas.set_editing`（九个绘制动作、数字键、画笔多边形、魔棒、编辑对象、
  画笔编辑，以及自动标注与画笔进入时画布自己的调用）都先退出涂抹模式——按钮
  回弹、覆盖层与橡皮筋隐藏——再执行原实现；反过来，画布处于 create
  模式（`drawing()` 为真）时点涂抹工具，只要**能安全切回**编辑模式就先触发上游
  编辑动作（`actions.edit_mode.trigger()`）自动切回，然后直接进入涂抹模式：不提示、
  也不需要用户多点一次「编辑对象」。只有**不安全**时才拒绝并提示「请先退出绘制模式，
  再使用涂抹工具」；不安全 = 正在画一个还没收尾的图形（`canvas.current` 非 None）、
  处于自动标注会话（`canvas.is_auto_labeling` 为真）、拿不到上游编辑动作或它被
  禁用、或触发之后画布仍在绘制模式（兜底，防上游行为变化）。走编辑动作而不是直接
  赋 `canvas.mode`：它经 `set_edit_mode → toggle_draw_mode(True) →
  canvas.set_editing(True)` 更新动作与工具栏簿记，而此时涂抹模式尚未进入，包装过的
  `set_editing` 是 no-op。
  `Canvas.mode_changed` 不能承担这件事：它只是设置里「自动切回编辑对象」的信号，
  而 `set_editing` 改了 `mode` 却什么都不发，故工具改从 `set_editing` 退出。
- 事件过滤器只在涂抹模式时消费事件，其余一律放行；鼠标手势三条：左键按下
  **无论落在图像内还是图像外都消费**（图像外 `_to_image` 返回 `None`、不开始框选，
  只在状态栏提示「请从图像内部开始框选」）——画布已处于 CREATE，放过去上游就会
  从图像外起手画出形状；右键按下取源点并消费；左右键双击（`MouseButtonDblClick`）
  同样消费，否则上游 `mouseDoubleClickEvent` 会在 CREATE 下把没收尾的图形收尾；
  其余按键（中键等）不碰。

### 测试

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p no:cacheprovider tests/custom/smudge_tool -v`
（需 PyQt6 + numpy + OpenCV；本工作区用仓库里的 `.venv`，全套 169 个用例：
`test_st_operations.py` 28 + `test_st_texture_fill.py` 39 + `test_st_filter.py` 49 +
`test_st_jpeg_metadata.py` 22（含 TIFF 逐字节 + Orientation 5~8 与 MPO）+
`test_st_draw_mode.py` 31（进入接管 / 按键与按钮切走 / 不递归 / 连接成对 /
退出回编辑态与三级 fallback（含禁用动作退到 widget 回退）/ 图像外按下不画形状 /
双击被吞 / 菜单条目经 `triggered` 接管、真菜单点击一次执行 / 编辑条目一次执行 /
接管不残留 `_handing_over` 标志、下次退出仍弹栈 / 让路退出不代跑上游编辑动作 /
真实 QAction 上下游处理器恰好执行一次：工具栏、快捷键、菜单、编辑动作，两种
槽位顺序各钉一遍，退出后真实动作上只剩上游的槽）；本轮复跑后两个文件共 80 个
用例通过）。

### 已知坑

- 所有软挂载都在实例上，不在类上：上游同步后必须逐条核对，contract.json 的
  `soft_mounts` 是这份清单的唯一事实源。
- 备份目录永不清理，长期使用会累积原图副本（有意为之：撤销与追溯优先）。
- 细长碎块（ROI 某边减去 12 后剩 1~5 像素，例如 29x29 的 5 像素条带、40x30 的
  4 像素条带）仍走 `MIN_SIDE=6` 的跳过分支，那条带会保留缺陷像素；对齐贴片机制
  本可覆盖它，但会改变 40x30 这类「正常尺寸」ROI 的结果，属独立决策。
- 按钮图标名是 `brush`，取自带 icon 的生成资源 `anylabeling/resources/resources.py`；
  换图标要同时改这里的名字。
- 上游 `Canvas.offset_to_center` 等几何方法改名时会静默失效（运行时才炸），
  靠 contract.json 的 upstream 清单在同步时兜住。
- 覆盖层标记**不能再用 `canvas.geometry().topLeft()` 做父坐标补偿**：那只在覆盖层
  自己 `pos()==(0,0)` 时成立；滚动区布局下 `overlay.geometry()==canvas.geometry()`，
  画布滚动后 `geometry().topLeft()` 为负，会多减一次，标记平移 `-canvas.pos()`。
- 实例级包装（`canvas.mouseMoveEvent = ...`）依赖 PyQt 让虚函数按实例属性派发，
  且必须在首次真正派发事件前安装；`label_widget.py` 的挂载点满足这一点，若上游
  改成在 C++ 侧转发或缓存绑定，包装会静默失效。
- 模式互斥靠包装 `Canvas.set_editing`：`Canvas.mode` 目前只在 `__init__` 与
  `set_editing` 里赋值；上游若换入口，包装静默失效，涂抹模式会再度赖着不走。
- 进入侧依赖上游 `LabelingWidget.actions.edit_mode` 存在且可用：缺失、被禁用或
  改名时退化为「拒绝并提示」，即 create 模式下再也进不去涂抹模式（安全失败）。
  被接管的 11 个 `create_*` 动作名取不到时逐个跳过（`getattr`），不报错，只是少
  接管一个按钮。这 11 个名字是 `LabelingWidget.__init__` 里 `action(...)` 造的
  局部动作、不是类成员：`self.actions.create_mode` 的 AST 值是 Attribute 而不是
  Name，`_class_member_ok` 找不到它们（已用自检脚本的函数逐个核实），故只在
  `contract.json` 的 `upstream` 里以 `LabelingWidget.actions` 与 `set_edit_mode`
  登记，11 个动作名本身不进 AST 清单。`edit_mode` 不进接管名单：它只作退出
  回编辑态（`actions.edit_mode.trigger()`，见上）与 create 模式下点涂抹按钮时的
  进入侧让路用；`edit_brush_mode` 工具从不读它，它的让路走上游
  `toggle_brush_mode → toggle_draw_mode(True)`。
- **菜单条目与工具栏共享同一 `QAction`，接管只靠 `triggered`**：上游一旦改成
  绕过 `QAction.trigger()` 直接切模式，或在菜单里放动作副本而不是动作本体，
  被接管的那一层就静默失效——模式仍由包装过的 `Canvas.set_editing` 让路退出
  （画布切换的兜底还在），但「先退出涂抹再跑原动作、一次手势一次副作用」的
  保证就只剩这条兜底；上游若连 `set_editing` 也绕开（直接改 `Canvas.mode`），
  涂抹模式就会赖在画布上。同步上游时按 `Canvas.set_editing` 的调用点清单核对
  （**需要真机验证**）。
- **为什么不用 `setEnabled(False)` 置灰**：Qt 把「动作被禁用」和「动作的快捷键」
  绑在一起——禁用 `QAction` 会连带停掉它的 `shortcut()`，实测（真实 `QAction` +
  画布事件过滤器，见本轮探针）按 R 时 `ShortcutOverride` 被过滤器接受、动作又被
  禁用，`triggered` 永远不发，按键根本到不了 `toggle_draw_mode`，直接违背「按
  矩形/多边形/魔棒的快捷键要能切走」这条需求。所以改用「连接 `triggered` 接管」：
  动作可用、快捷键有效，模式由接管槽先退出。**推论**：以后再想「临时停用某个上游
  动作」时，不要用 `setEnabled(False)`，否则会顺手废掉它的快捷键。
- 接管是连接在动作的 `triggered` 信号上的，不是包装上游方法，因此**不依赖**
  `LabelingWidget.toggle_draw_mode` 这个名字或它的方法体（`_draw_connections` 里
  存的是 `(signal, slot)`，退出时按 signal `disconnect(slot)` 精确断开，不会顺手
  断掉上游自己的槽）。**但槽位顺序会影响谁先跑**：真实窗口在构造函数里连好上游
  处理器、之后才 `install_smudge_tool(self)`，所以接管槽排在最后、上游处理器先跑
  （它经包装过的 `set_editing` 让涂抹模式退出）；接管槽在前（该动作的处理器在
  安装后才连上）时则接管槽先退出、上游处理器随后照跑。两种顺序下上游处理器都
  恰好执行一次，这正是「退出后不再 `action.trigger()`」要保证的（真实 `QAction`
  探针把两种顺序各测一遍）。上游若改用
  `QAction.setShortcutContext` 之外的派发路径（例如自己在 `Canvas` 里处理按键），
  接管仍然生效，但只覆盖动作本身被触发的那条路。

## rename_tool

### 职责

Tool 菜单里的「重命名」：按标注主分类把一份扁平数据集里的图片与同名 json 批量
改名，结果输出为一个 zip。**源目录严格只读**：不改名、不写入、不改 mtime、不建
临时文件，也不建任何暂存目录；需要改名的文件在 zip 里用新名字，其余条目用原名
原字节。源目录可以从文件对话框选择，也可以直接拖进窗口；界面上没有 zip 文件名
输入框、没有输出目录选择控件，也没有预览步骤，点一次「重命名」就串起扫描、
阻塞检查与打包，zip 名与落点都在执行时从源目录派生：`<源目录名>_renamed.zip`
写到源目录的上级目录（即数据集的同级目录），重名自动 `_2`、`_3`…，只产出那
一个 zip。结果是**只读文本显示器**：选择或拖入目录即解析、逐行打印汇总 / 忽略数 /
阻塞明细或待改名映射，有阻塞就禁用主按钮（不弹窗），写阶段实时打印被改名的条目，
**只打印真正改名的文件**，保持原名与原样镜像的只在结束汇总里计数。

### 代码与体量

`anylabeling/custom/rename_tool/`（4 个文件 1762 行：`rename_core.py` 规则与打包
913 行、`dialog.py` Qt 层 769 行、`launcher.py` 惰性启动、`__init__.py` 导出）；
测试 `tests/custom/rename_tool/`（8 个文件 2985 行）。

### 入口符号

`anylabeling.custom.rename_tool.install_rename_tool`（幂等，挂 Tool 菜单）、
`anylabeling.custom.rename_tool.launch_rename_tool`（惰性启动、复用实例）、
`anylabeling.custom.rename_tool.rename_core.plan_directory`（只读扫描出计划）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/views/labeling/label_widget.py`：`from anylabeling.custom.rename_tool import install_rename_tool`
- `anylabeling/views/labeling/label_widget.py`（`LabelingWidget.__init__`）：`install_rename_tool(self)`
- 软挂载：`widget._rename_tool_dialog`（自研属性）由 `launch_rename_tool` 持有，
  窗口销毁时清空，因此反复点菜单只复用同一个窗口。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.menus` | direct | Tool 菜单动作的挂载点；`menus` 或 `menus.tool` 缺失时安静返回 None |
| `anylabeling.views.labeling.utils.qt.new_action` | direct | 菜单动作工厂，与其它自定义菜单项一致 |
| `anylabeling.views.labeling.utils.qt.new_icon` | transitive | 由 `new_action(icon="convert")` 间接调用 |

### 行为级契约（不可机器校验）

- **R1 扫描与配对（不递归）**：只读源目录顶层（`os.listdir`）。图片扩展名
  `IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")`，大小写不敏感；
  `<stem><ext>` 与 `<stem>.json` 按 stem 精确同名配对（stem 区分大小写）；
  目录项按自然排序（`1.jpg` < `2.jpg` < `10.jpg`）。
- **R2 主标签与清洗**：`shapes` 里出现次数最多的 label（并列取先出现者）；
  `shapes` 为空或全部没有 label → `background`；标签先做 `sanitize_label`
  （连续非法字符 `\/:*?"<>|` 折成单个下划线 → 折叠连续下划线 → strip 两端
  的点/下划线/空格 → 空则 `unnamed`），**清洗之后再计数比较**。
- **R3 编号**：一般项目标 stem = `<label>_<n>`。先由「目标名已等于当前名」的
  项占位其编号，其余项按自然排序取该 label 下最小的空号；`n >= 1` 且是规范
  十进制（无前导零）。`person_0` / `person_01` / `person_1_extra` 都不算符合规范。
- **R4 符合命名规范**：某项算出的目标名与当前文件名完全相同（图片名与 json 名
  都相同）→ `already`（不改名、占编号）；否则 `rename`。
- **R5 `_aug` 项**：`SUFFIX = ^(?P<base>.+)_aug(?P<x>\d*)$`（贪婪，取最右一个
  `_aug` 之后为 x）；纯 stem 迭代剥离直到不再命中，后缀链按剥离顺序
  （`a_aug1_aug2` → 纯 stem `a`、后缀链 `("_aug1", "_aug2")`）；目标 stem =
  父项目标 stem + 后缀链原样拼接（x 不重算、不臆造）；`_aug` 项不占编号，只继承父项编号。
- **R6 孤儿 `_aug`**：纯 stem 在源目录没有对应图片 → `orphan`：不改名、不占编号、
  以当前名字原样镜像进 zip（它的 json 也不重写 `imagePath`），结果表里单列一类，不阻塞。
- **R7 阻塞条件**（任一存在即不导出（不写 zip、不碰 `.part`）；blockers 每项一句
  中文，带文件名）：B1 图片没有同名 json；B3 json 无法解析或顶层不是对象；B4 目录内
  有子目录（只处理顶层文件）；B5 同一 stem 有多张图片或多份 json。
- **R7b 忽略语义**：顶层 `.json`（大小写不敏感）且同 stem 没有图片 → **忽略**：不进
  `items` / `mirrored` / `entries()` / zip / `total_files()` / 镜像素数，不阻塞；
  只进 `plan.ignored`（自然排序），界面只报数量、不列名字。`RenamePlan.stats()` 给出
  `{total, images, json, ignored, other}`，满足 images+json+ignored+other==total 与
  total-ignored==total_files()；四个桶按**目录清单**归类（图片 / 有图的 json /
  无图的 json=ignored / 其余），因此同 stem 出现大小写变体 json（B5）时也没有文件落空；
  目录读不了时全 0（不抛异常）。
- **R8 条目名校验**：条目名非空、不含斜杠、不含 `..`、非绝对路径；每个源文件名在
  计划里恰好出现一次（`check_entry_names` 会重新列一遍源目录顶层文件，漏镜像、重复
  计入、幽灵文件、忽略集合与计划重叠都算问题；`plan.ignored` 是完整性守卫的唯一例外，
  既不被要求镜像、也不允许出现在计划里）；任何改名目标名不得等于任何「原样镜像」文件
  的名字（already / orphan / mirrored 三类），否则阻塞。`entry_name_ok` 拒绝任何含
  连续两个点的名字（`v1..2.txt` 即非法），比「不含 `../`」更严；`execute()` 会把
  `check_entry_names` 的结果并入阻塞项，非法条目名在解析阶段就阻塞（按钮禁用），
  而不是等到写 zip 才失败。
- **R9 只读源目录 + 产出 zip**：zip 是源目录顶层的镜像（每个未被忽略的顶层文件一个
  条目，含 `classes.txt`、隐藏文件、任意二进制文件，原字节）。`plan.mirrored` =
  「顶层里没有被任何 item 引用的文件」（B5 冲突的第二张图、二进制文件），`plan.ignored`
  里的 json 有意排除。先写 `<最终名>.part`，成功后 `os.replace` 到最终名；zip 最终名
  执行时由 `<源目录名>_renamed.zip` 派生、已存在则取 `_2`、`_3`…（界面没有文件名
  输入框）；`write_zip` 自身把关：`plan.blocked()` 或最终名已存在时 `raise RenameError`；
  返回 dict 含 `ignored` 计数。`progress(done, total, entry_name, source_name, changed)`
  **每个条目回调一次**（不节流），终止回调 `progress(total, total, "Done", "", False)`。
  失败时**不删除** `.part`，异常信息里带上它的完整路径；图片条目 `ZIP_STORED`，其余
  `ZIP_DEFLATED`，`allowZip64=True`，不写目录条目；输出 zip 不得落在源目录内。
- **R10 惰性单实例 + 主线程**：`launch_rename_tool(parent)` 内延迟 import 对话框，
  复用 `parent._rename_tool_dialog`；执行在主线程完成，`QCoreApplication.processEvents()`
  驱动进度、期间 `dialog.setEnabled(False)`，**不引入线程、不提供取消**。
- **R11 交互（只读显示器 + 选择即解析 + 一键导出）**：`setAcceptDrops(True)`；
  `dragEnterEvent` / `dragMoveEvent` 走同一辅助方法，只在拖入项里有文件夹时接受；
  `dropEvent` 取第一个文件夹（`os.path.abspath` 归一化），非文件夹项与多余文件夹
  在状态栏提示「已忽略: 名字（仅支持文件夹 / 仅取第一个文件夹）」，没有文件夹时提示
  「仅支持文件夹」；拖拽只设定目录、绝不自动导出。界面 = 源目录一行 + 只读提示行
  （「导出到数据集同级目录：<源目录名>_renamed.zip（重名自动加 _2、_3…）」）+ 按钮行 +
  **只读显示器** `QPlainTextEdit`（objectName `rename_log`、等宽、NoWrap、上限
  `DISPLAY_MAX_BLOCKS = 5000`）+ 进度条 + 状态栏一行；**没有表格 / 统计标签 / 阻塞
  标签 / `QProgressDialog`**，也没有输出目录控件（`output_button` / `output_label` /
  `pick_output_dir` / `set_output_dir` / `output_dir`）、预览按钮、zip 文件名控件与
  二次确认。`pick_source_dir()` 与 `dropEvent()` 同走 `set_source_dir(path)` =
  清显示器 → 记源 → 立即 `parse_source()`（主线程同步跑，进度条 + `processEvents`，
  `setEnabled(False)` 防重入、finally 恢复），解析完即刷新按钮。主按钮「重命名」
  （objectName `primary`）仅在**四条件**同时满足时启用：源目录非空、`export_dir(源)`
  非空、`self._plan is not None and not self._plan.blocked()`、不在忙碌中；
  `_refresh_actions()` 是唯一出口。显示器逐行输出（行顺序固定、不折叠、不列 ignored
  名字）：①汇总行「共 N 个文件：图片 i、成对 json j、其他 o、忽略 k（原样镜像 m）」；
  ②忽略行（只数量）；③阻塞时「发现 N 处阻塞，不能重命名，未写入任何文件（含 .part）：」
  + B1 明细「图片缺少同名 json（N 张）：」与每行一个文件名 + 「其他问题：」与每行一条；
  ④无阻塞时「待改名 N 个：」与每行一个 `a.jpg -> person_1.jpg`（N==0 打印「没有需要
  改名的文件」；目录为空打印「目录为空：可以重命名（会得到一个 0 条目的 zip）」）；
  ⑤解析失败打印「无法解析目录：<RenameError 文案>」（按钮禁用）。**阻塞不弹窗**：靠
  显示器首行 + 状态栏 + 禁用按钮提示。点击后：校验源目录（缺失则告警）；派生导出目录
  `os.path.dirname(os.path.normpath(os.path.abspath(源)))`，派生不出上级（根目录 / 上级
  规范化后等于源目录）则告警「源目录没有上级目录，无法导出」并中止；再校验 `self._plan`
  存在且未阻塞（防御性），否则不导出。写阶段只打印**真正被改名**的条目
  「a.jpg -> person_1.jpg    12/45」（i/N = 第 i 个改名文件 / 待改名文件总数，未改名者
  不打印，进度条也**只随改名的文件前进**、不因未改名条目回跳）；结束打印
  「已导出：<zip 绝对路径>」与「共 N 个文件：改名 X、保持原名 Y、原样镜像 Z、忽略 K」，
  **N = 归档条目数 + 忽略数**（即整个源目录，与解析汇总同口径），四个桶互斥且加起来
  就是 N；状态栏的「发现 N 处阻塞」与显示器同一口径（B1 按图片张数计，其余 blocker
  每条计 1）；失败**不追加成功行**，追加「重命名失败：<原因>」与（有则）
  「半成品已保留：<path>.part」，状态栏同步，**写失败弹窗保留**（裁决 1 只针对阻塞）。
- **例子**（`tests/custom/rename_tool/test_rt_plan.py` 逐条断言）：
  - E1 `a.jpg`+`a.json`(person) → `person_1.jpg` / `person_1.json`（rename）
  - E2 `person_1.jpg`+`person_1.json`(person) → 目标 == 当前 → already，占位 1
  - E3 E2 + `IMG_2.jpg`(person) → `IMG_2` → `person_2`
  - E4 `person_2.jpg`(already 占 2) + `a.jpg`(person) → `a` → `person_1`
  - E5 幂等：对 E1 执行后的结果目录再跑一遍 → 0 个 rename，全部 already
  - E6 `a` + `a_aug1` + `a_aug2`(person) → `person_1` / `person_1_aug1` / `person_1_aug2`
  - E7 `person_1` + `person_1_aug1` → 两者都 already
  - E8 `a_aug`（裸后缀）+ 父项 `a`(person) → `person_1` / `person_1_aug`（不补编号 1）
  - E9 孤儿 `a_aug1`（没有 `a`）→ 放行、保持原名、不占号
  - E10 同名陷阱：改名目标撞上原样镜像的名字 → 阻塞（见「已知坑」）

### 测试

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p no:cacheprovider tests/custom/rename_tool -v`
（需 PyQt6；本工作区 271 个用例全部通过）。

### 已知坑

- 源目录只读，所以源数据永远不会被规范化：要真正把名字落到数据上，解压 zip 之后
  再跑一次（第二次跑应该全是「符合规范」）。
- 空目录、或没有任何待改名项的目录（全部已符合规范）点一次也会导出一份镜像 zip：
  前者是 0 条目的 zip，后者条目全部原样镜像；源目录只读，绝不覆盖既有文件（zip
  重名时自动取 `_2`、`_3`…）。
- 缺 json、坏 json、子目录、同 stem 多文件都会**整体阻塞**，一项都不改；显示器给出
  原因（全部列出，界面上限 5000 行），主按钮禁用、状态栏同步提示，数据集同级目录里不会
  出现任何文件，包括 `.part`。**只有 json 没有图片不再阻塞**：忽略它，只报数量。
- 孤儿 `_aug` 放行且保持原名（它没有父项可继承编号），也不占号。
- 失败出口（扫描与写 zip 共两条分支）都在显示器里追加失败原因、不追加成功行，已经打印
  的写进度行保留；写失败同时回退按钮（`_refresh_actions()`）并弹窗，状态栏同步。
- 显示器开了 `setMaximumBlockCount(5000)`：Qt 到上限会丢行，`toPlainText()` 可能返回
  空串，读回一律用自有的 `plain_text()` / `log_lines()`（那一份不受上限影响）。
- 失败留下的 `.part` 要自行处理：工具绝不删除任何文件，下一次执行会被「临时文件
  已存在」挡下来，需要人工确认后处理。
- 图片条目 `ZIP_STORED`、其余条目 `ZIP_DEFLATED`：图片本来就不压缩，再套一层
  deflate 只会变慢；json 文本压得动。
- R8 第 3 条（改名目标不得等于已被占用的名字）分两种来源：
  ① 光看 `plan.mirrored` 在可解析的目录里撞不上——图片形状的名字只有在同 stem 已被
  另一张图片占用（那是 B5，已阻塞）时才会进 mirrored，`.json` 形状则只可能是 B5
  重复，所以扩展名形状互斥；白盒用例（手工往 `plan.mirrored` 里加
  撞名）钉的就是这个分支。
  ② 但 `plan.unchanged_names()` 还包含 already / orphan 两类 item 的当前名字，orphan
  的名字就是普通的 `<stem>_aug<x>.jpg` 形状，**真实目录里撞得上**：`b.jpg` +
  `b.json`(person) + `b_aug1.jpg`/`b_aug1.json` +
  `person_1_aug1.jpg`/`person_1_aug1.json` 时 `b -> person_1`、
  `b_aug1 -> person_1_aug1`，而 `person_1_aug1` 因纯 stem
  `person_1` 没有图片成为 orphan 保持原名，于是撞名阻塞。不拦就会写出两个同名 zip 条目，
  所以这是真实可达的阻塞，用例见 `test_rt_blockers.py` 的
  `test_target_collides_with_orphan_name_in_a_real_folder`。
- 扫描与打包都在主线程：几千个文件的大目录会在解析时短暂卡住界面，靠进度条 +
  `processEvents` 维持响应，不提供取消（取消会留下用户看不见的 `.part`）。
- 阻塞不弹窗（裁决 1）：只有显示器首行 + 状态栏 + 禁用按钮；写 zip 失败仍然弹窗。

## label_filter

### 职责

Tool 菜单里的「标签过滤」：按标签分类过滤文件列表。窗口列出当前目录的**分类**——
目录里 json 出现过的每个标签，外加「背景」（没有任何标注的图片）——勾选的分类就是
文件列表保留的图片；多选分类之间是「或」，分类名与标签精确匹配。

### 代码与体量

`anylabeling/custom/label_filter/`（5 个文件 1368 行：`core.py` 分类规则 305 行、
`installer.py` 挂载点与包装 510 行、`dialog.py` 选择窗口 478 行、`launcher.py`
惰性启动 43 行、`__init__.py` 导出 32 行）；测试 `tests/custom/label_filter/`
（5 个文件 1859 行）。

### 入口符号

`anylabeling.custom.label_filter.install_label_filter`（幂等：挂 Tool 菜单并装包装）、
`anylabeling.custom.label_filter.launch_label_filter`（惰性启动、复用实例）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/views/labeling/label_widget.py`：`from anylabeling.custom.label_filter import install_label_filter`
- `anylabeling/views/labeling/label_widget.py`（`LabelingWidget.__init__`）：`install_label_filter(self)`
- 软挂载 1：`LabelingWidget.import_image_folder` 由 `LabelFilterController` 做实例级
  包装（安装点在 `anylabeling/custom/label_filter/installer.py`）：上游方法整体照跑，
  包装只在它返回之后删掉不命中的行。
- 软挂载 2：`widget._label_filter_dialog`（自研属性）由 `launch_label_filter` 持有，
  窗口销毁时清空，反复点菜单只复用同一个窗口；复用分支在每次重新显示前调用
  `LabelFilterDialog.rescan()`，因此窗口始终列出当前 `last_open_dir` 的分类与计数，
  首次打开仍只由构造函数扫描一次。
- 行为级的第三条接管：包装体在调用上游前把 `may_continue` 探针临时装在实例上，
  调用一结束立刻还原（`_gate_probe`，见行为级契约与已知坑）。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.menus` | direct | Tool 菜单动作的挂载点；`menus` 或 `menus.tool` 缺失时安静返回 None |
| `LabelingWidget.import_image_folder` | wrapped | 唯一的过滤入口：上游重建列表后按选择删行 |
| `LabelingWidget.may_continue` | wrapped | 探针：判定上游这次调用是否真的跑到（取消即回滚） |
| `LabelingWidget.file_list_widget` | direct | 读行文本、删行 |
| `LabelingWidget.fn_to_index` | direct | 删行后重建；上游 `open_next_image` 与右键菜单都按它取行 |
| `LabelingWidget.open_next_image` | direct | 当前图被过滤掉时定位到第一张命中项（仍命中时不调用） |
| `LabelingWidget.filename` | direct | 包装前记录画布当前图，过滤后据此决定保留还是换图 |
| `LabelingWidget.last_open_dir` | direct | 分类扫描的目录来源 |
| `LabelingWidget.output_dir` | direct | 标注目录：json 在它下面，而不是图片旁边 |
| `LabelingWidget.statusBar` | direct | 状态栏提示（重置 / 清除 / 生效张数） |
| `anylabeling.views.labeling.utils.qt.new_action` | direct | 菜单动作工厂，与其它自定义菜单项一致 |
| `anylabeling.views.labeling.utils.qt.scan_all_images` | direct | `core.collect_files` 复用上游目录扫描（递归 + 自然排序），保证与文件列表同序 |

### 行为级契约（不可机器校验）

- **多选 = 或**：勾选的分类取并集，图片的任一标签命中即保留；**分类名精确匹配**
  （不做大小写折叠），因为候选名就是 json 里原样的标签。
- **无标注 = 真实分类「背景」**：json 缺失 / 读不了 / 坏 / 顶层不是对象 / 没有
  shapes、以及 shape 的 label 为空串，都归到同一个「背景」；数据集里本来就有同名标签
  时两者**合并为一行**（计数相加），绝不出现两行同名分类。
- **空勾选确认 = 清除过滤**：一个分类都不勾不是「过滤掉全部」，而是「不启用过滤」，
  文件列表恢复显示全部，用户不会得到一张空列表。
- **确认过滤后画布落在命中图片上**：应用前画布上的那张图若仍在过滤结果里，画布**保持
  不动**（不重新加载、不闪回第一张），文件列表当前行指向它（移动当前行时屏蔽列表
  信号，避免触发上游 `file_selection_changed` 的重新加载）；被过滤掉（或本来没有
  当前图）时加载结果里的**第一张**；结果为空（0 张）时不动画布、不加载任何文件
  （列表为空、`filename` 为 `None`，与上游空目录的表现一致）。这套接管只发生在
  调用方要求加载时（`load=True`，即确认过滤）；搜索框与「修改标注目录」等
  `load=False` 的调用仍按上游分工走（不选行、不加载），免得改标注目录后不再重载标注。
- **换目录自动重置并提示**：过滤属于它建立时所在的目录，上游方法收到另一个目录时先
  清掉过滤再照常打开，并在状态栏提示「已切换目录，标签过滤已重置」。
- **拖拽单文件不经过包装、始终可见**：只有目录调用走包装
  （`import_dropped_image_files` 不经包装），单张拖入的图片不受过滤影响。
- **取消「未保存标注」提示即回滚**：上游 `import_image_folder` 的第一个动作是问
  `may_continue`，答否时当场返回；探针据此判定「这次没真的跑」，把状态回滚成上一次
  的选择并提示「标签过滤未生效（操作已取消）」，过滤不生效。
- **不写任何配置键**：状态只存在 widget 实例属性上（`_label_filter_state` 等），
  不碰 `_config`、不落盘，重启后无残留。
- **扫描发生在对话框打开时、带进度且可取消**：窗口构造时就扫当前目录，主线程跑
  `QProgressDialog`、每 64 张 `processEvents` 一次；取消的扫描不产生结果，窗口不改
  变任何状态。
- **再次打开对话框回显生效勾选**：过滤正在生效时重新打开窗口，对应分类预先勾上，
  用户看到的就是文件列表当前保留的分类。
- **重新显示按当前目录重扫**：复用窗口在每次重新显示前 `rescan()`，行、计数、状态行
  与生效勾选都按当前 `last_open_dir` 重建；重扫被取消时窗口回到「从未扫描」状态
  （空列表 + 取消提示、确认按钮禁用），**不动**仍生效的过滤。
- **换目录被取消时不报「已重置」**：换目录时「是否保存标注」提示被取消，上游当场
  返回：过滤、状态栏与 tooltip 均保持不变（列表仍是旧过滤结果），不提示
  「已切换目录，标签过滤已重置」。
- **「清除过滤」与「空勾选确认」同走 reset()**：两者都调用 `controller.reset()`；
  取消时 `reset()` 返回 False，对话框保持打开并提示这次操作未生效，不提示「已清除」。
  **清除后画布保持当前图**：重建出的全量列表必然含它，恢复到同一张并选中其行，
  不重新加载（否则列表当前行会指向别的图，后续导航从错误的图片开始）。
- **回显只发生一次**：打开对话框时按当前生效过滤回显一次；之后搜索框触发的列表
  重建不再回显，用户手动取消的勾选不会被重新勾上。

### 测试

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p no:cacheprovider tests/custom/label_filter -v`
（需 PyQt6；本工作区 60 个用例全部通过，其中 `test_lf_reopen.py` 的 4 条覆盖复用窗口
重新打开时按当前目录重扫、回显生效过滤、首次只扫一次与重扫取消不动过滤，
`test_lf_filter_apply.py` 的 5 条新增覆盖确认 / 清除过滤后画布与列表当前行的表现，
其中 1 条守住 `load=False` 调用（搜索框、改标注目录）的上游分工）。

### 已知坑

- 包装链依赖「`may_continue` 是上游 `import_image_folder` 的**第一个动作**」：上游若
  把这声确认挪到后面或改掉属性名，探针就拿不到「这次没真的跑」的证据，取消保存提示时
  过滤会错误地留在列表上（失败方向是「不回滚」，不会崩）。
- 空目录（或目录里一张图片都没有）时分类表里仍会列出「背景（0 张）」，但**确认按钮
  禁用**：没有任何图片时过滤没有意义。
- 事后过滤是 `takeItem` 删除：删完必须重建 `fn_to_index`，否则 `open_next_image` 与
  右键菜单会按旧索引取到错行；已用 1 万条目用例兜底。
- 复用窗口的刷新依赖 launcher 复用分支调用 `rescan()`：删掉这个调用，同一会话里切目录
  后再打开窗口就会显示上一个目录的分类与计数（窗口构造只扫一次）。

## crash_log

### 职责

无控制台（打包版 `console=False`）时的崩溃取证：把 faulthandler、三个 Python 级异常钩子
（`sys.excepthook` / `threading.excepthook` / `sys.unraisablehook`）、应用日志器与
Qt message handler 的输出全部镜像到当天的 `xany-YYYYMMDD.log`，并用 session marker
区分正常退出与异常退出（崩溃、被强杀、断电）。只做记录，不改变任何原有行为。

### 代码与体量

`anylabeling/custom/crash_log/`（5 个文件 968 行：`handlers.py` 518 行、
`install.py` 160 行、`session.py` 139 行、`paths.py` 123 行、`__init__.py` 28 行）；测试
`tests/custom/crash_log/`（8 个文件 1040 行，46 个用例）。

### 入口符号

`anylabeling.custom.crash_log.install_crash_log`（幂等安装，永不抛）、
`anylabeling.custom.crash_log.uninstall_crash_log`（完整回滚，测试用）、
`anylabeling.custom.crash_log.get_log_directory`（本次会话解析出的目录，降级时为 None）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/app.py`（**模块顶层，位于 `LabelingWidget` 之前**）：
  `from anylabeling.custom.crash_log import install_crash_log  # fork 挂载点`
- `anylabeling/app.py`（模块顶层）：`install_crash_log()  # fork 挂载点`
- 软挂载：无（`soft_mounts: []`）。所有接管都发生在进程全局钩子与 `logging` 上，
  不包装任何上游方法，上游文件 diff 只有上面两行。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `anylabeling/app.py` 的导入顺序 | direct | 挂载点必须留在 `sys.path.append(...)` 之后、`import yaml` 之前：要早于 PyQt6 与上游包初始化，才能覆盖这些导入链上的异常 |
| `anylabeling.views.labeling.logger.logger` | direct | custom 不 import 该模块，只按硬编码名 `"X-AnyLabeling"` 取同名 logger 并挂 StreamHandler；上游改名即静默丢日志（已有测试钉住） |

### 行为级契约（不可机器校验）

- **挂载点不可挪**：`install_crash_log()` 在 `anylabeling/app.py` 模块顶层执行，必须位于
  `sys.path.append(...)` 之后（否则 custom 包 import 失败）、`import yaml` 与
  `from PyQt6 import ...` 之前（否则覆盖不到这些导入链上的异常）；挪进 `main()`
  就晚于 PyQt6 导入，也晚于上游 logger 的创建。
- **日志目录三级降级**：`XANY_LOG_DIR`（非空时按 `expanduser` + `abspath` 用）→
  `~/.xanylabeling/logs` → 系统临时目录下的 `xanylabeling-logs` → 都建不出来则目录为
  None（只镜像 stderr、不开 faulthandler）。与 `--work-dir` 无关：这里刻意不 import
  `anylabeling.config`，避免安装时刻引入会崩的依赖链。
- **只清理自有模式**：prune 只列、只删匹配 `^xany-\d{8}\.log$` 的文件名，同目录里
  他人的文件永不触碰；保留最新 14 份（`KEEP_LOGS`），删除失败静默。
- **`install_crash_log()` 永不抛**：目录解析、开流、faulthandler、三个 Python 钩子、
  app logger、session marker、Qt handler 每一步各自 try/except，任一步失败只降级、
  不影响启动；`XANY_LOG_DISABLE=1` 时零副作用（连 marker 都不写、目录都不建；
  已有测试覆盖，含目标目录未被创建）。
- **四钩子链式且不吞原行为**：三个 Python 钩子先写日志再调用安装时捕获的前一个钩子
  （前一个抛异常也吞掉，绝不把可恢复错误升级成致命错误）；Qt handler 有前一个就转交给它、
  没有才退回 stderr。`uninstall_crash_log()` 精确还原安装时捕获的对象（含「原本不存在」
  的 `sys.unraisablehook`），可反复安装 / 卸载。
- **跨天惰性轮转**：每次写日志时若日期变了，两个 append 句柄切到新文件、顺手 prune，
  并让 faulthandler 重新指向新文件描述符（faulthandler 直接写 fd，不切就会留在旧文件）。
- **worker 子进程不碰会话标记**：spawn / fork / frozen 三条路径（以及 frozen 下的
  `resource_tracker` 助手）都会重跑 `anylabeling/app.py` 的挂载点。`install_crash_log()`
  先用三层判据识别 worker（`multiprocessing.parent_process()`、「真的名为 `__mp_main__`
  的模块」、frozen 的 `spawn.is_forking(argv)`），`session.prepare_session_marker()`
  再兜一层：marker 的 pid == `os.getppid()` 说明父进程还活着 → 不报 stale、不覆写、
  不注册 atexit。worker 仍写自己的 SESSION START / faulthandler / 钩子，但绝不写、
  不删 marker——marker 是「该日志目录有一个存活会话」的单例事实，属于整个进程组。

### 测试

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p no:cacheprovider tests/custom/crash_log -v`
（需 PyQt6；本工作区 `--collect-only` 核实 8 个文件 46 个用例）。覆盖目录降级、prune 只删
自有文件、三个钩子的链式与回滚、Qt handler 的等级过滤、子进程里的真实 SIGSEGV / SIGABRT /
SIGKILL、真实 spawn worker 不抢父进程 marker 的回归、跨天轮转后 app logger 重绑定到新流、
`XANY_LOG_DISABLE` 零副作用（目标目录不被创建）。

### 已知坑

- **C++ 帧不符号化**：faulthandler 采到的 Qt / onnxruntime 原生栈只有地址没有函数名；
  要符号化得另配符号表，本功能不做。
- **SIGKILL / OOM / 断电只能靠会话标记**：这些情况下没有任何 Python 代码会执行，
  日志里只会有下一次启动写下的 `PREVIOUS SESSION DID NOT EXIT CLEANLY`；
  因此不能指望「日志最后一行」，要看这条标记。
- **spawn worker 的 SESSION START 是设计内的取证**：模块级挂载点会在 worker 里重跑，
  实测每次启动多出 2 条 SESSION START（worker 自己 + frozen 下的 `resource_tracker`
  助手）；它们不是噪音，不做过滤。
- **PID 复用只会推迟 stale 报告，不会丢标记**：`prepare_session_marker()` 用
  `pid == os.getppid()` 判定「marker 属于还活着的父进程」。崩溃进程的 pid 恰好等于
  当前父进程 pid 时这一次不报 stale；marker 仍在，下一次启动照样报出来。
- **`anylabeling` 包初始化链自身的导入崩溃不覆盖**：挂载点在 `anylabeling/app.py` 里
  执行，若崩在 `import anylabeling` 的包 `__init__` 阶段，日志里不会有记录。
- **Linux 上 PyInstaller 忽略 `--noconsole`**：Linux 下 `sys.stderr` 始终存在，
  `stderr is None` 的分支只在 Windows / macOS 的 windowed 打包里才走得到。

## 变更台账

新增或删除一个自研功能，必须同时改三处并跑自检：

1. `docs/custom/contract.json` 的 `features.<id>`（新增一节或删掉一节）；
2. 本文件（加/删一个 `## <id>` 小节，标题必须是功能 id）；
3. `docs/custom/MAP.md` 的定位表（当入口或目录约定变化时）。

然后跑 `python3 tests/custom/test_fork_contract.py`：它会检查锚点、上游符号、
路径存在性、文档覆盖与体积预算（单节 ≤2560B、全文件 ≤1024+2560×N）。

复制模板（把 `<id>` 换成功能 id，锚点换成真实行内原文，两处 `<真实行号>` 换成该锚点在
目标文件里的真实行号）：

    "<id>": {
      "summary": "一句话职责。",
      "code": ["anylabeling/custom/<id>/"],
      "tests": ["tests/custom/<id>/"],
      "enter": [{"module": "anylabeling.custom.<id>", "symbol": "install_<id>"}],
      "test_cmd": "python -m pytest -p no:cacheprovider tests/custom/<id> -v",
      "mounts": [
        {"file": "anylabeling/views/labeling/label_widget.py", "line": <真实行号>, "anchor": "from anylabeling.custom.<id> import install_<id>", "matcher": "line", "form": "import", "owner": "<module>"},
        {"file": "anylabeling/views/labeling/label_widget.py", "line": <真实行号>, "anchor": "install_<id>(self)", "matcher": "startswith", "form": "call", "owner": "LabelingWidget.__init__"}
      ],
      "soft_mounts": [],
      "upstream": {"anylabeling/views/labeling/label_widget.py:LabelingWidget": ["canvas"]}
    }

模板里两个「空 / 占位」值的写法：

- `"soft_mounts": []`：**没有实例级包装就留空数组**，自检允许为空（空数组 = 该功能没有
  软挂载）；有包装时按 `{"target": ..., "wrapped_by": ..., "install_site": ...}` 逐条登记。
  同一个功能的多条软挂载共用一个目录时，用同节的 `soft_mounts_prefix` 写一次前缀，
  `install_site` 只留文件名:行号（省体积，路径仍可拼全；见 `features.smudge_tool`）。
- `"line": <真实行号>`：必须填锚点在目标文件里的真实行号；留 0 或照抄别人的旧行号，
  非严格模式下是永久 `line drift` 告警，`XAL_CONTRACT_STRICT=1`（同步上游后）直接 FAIL。
