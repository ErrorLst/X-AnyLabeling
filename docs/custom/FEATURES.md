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
| model_validation | 模型验证子窗口（数据集上跑推理出报告） | `anylabeling/custom/model_validation/` | `tests/custom/model_validation/` | 5 个（import、菜单 action 定义与挂载、方法定义、方法内调用） | 1 |
| smudge_tool | 涂抹修复：取别处纹理覆盖缺陷并撤销 | `anylabeling/custom/smudge_tool/` | `tests/custom/smudge_tool/` | 1 个（1 行 import + 1 行调用） | 4 |

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

### 代码与体量

`anylabeling/custom/model_validation/`（22 个文件 12113 行，含 `ui/` 子包）；
测试 `tests/custom/model_validation/`（41 个文件 18752 行）。

### 入口符号

`anylabeling.custom.model_validation.launch_model_validation`（惰性启动、复用实例）、
`anylabeling.custom.model_validation.ui.dialog.ModelValidationDialog`（主窗口）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

共 5 处，全部在 `anylabeling/views/labeling/label_widget.py`：

1. 模块级 import：`from anylabeling.custom.model_validation import launch_model_validation`
2. `LabelingWidget.__init__` 里的菜单动作定义：`model_validation = action(`（action 块本体）
3. `LabelingWidget.__init__` 里把动作挂进菜单：`model_validation,`
4. 上游文件里唯一新增的方法定义：`def open_model_validation(self):`
5. 该方法体内的一行调用：`launch_model_validation(self)`

软挂载：`widget._model_validation_dialog`（自研属性）由 `launch_model_validation` 持有，
窗口销毁时清空，因此反复点菜单只复用同一个窗口。

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.menus` | direct | 菜单动作挂载点 |
| `LabelingWidget.error_message` | direct | 启动失败时的统一报错 |
| `anylabeling.config.current_config_file` | direct | 推理前确保全局 rc 路径可用 |
| `anylabeling.config.get_work_directory` | direct | 同上，兜底拼 `.xanylabelingrc` |
| `anylabeling.views.labeling.utils.opencv.qt_img_to_rgb_cv_img` | direct | 把 QImage 解码成 RGB 数组 |
| `anylabeling.services.auto_labeling.engines.OnnxBaseModel` | direct | 读输入形状、做元数据校验 |
| `anylabeling.services.auto_labeling.__base__.yolo.YOLO` | direct | 复用上游预处理/NMS/建 shape 的整条链 |
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

### 测试

`python -m pytest -p no:cacheprovider tests/custom/model_validation -v`（需 PyQt6 + numpy）。
本机无 PyQt6/numpy，未能运行。

### 已知坑

- 上游文件里唯一新增的函数体是 `open_model_validation`；上游若在 `LabelingWidget`
  近邻新增同名方法或改菜单挂载写法，锚点会失配，先跑自检再动手。
- `__preferred_device__` 是 `anylabeling/app_info.py` 里 `__getattr__` 动态提供的名字，
  普通 IDE 跳转看不到定义。
- 会话只换线程预算，不改 provider 选择逻辑；GPU 仍由上游配置决定。

## smudge_tool

### 职责

涂抹修复：右键取源点、左键拖框，用别处纹理覆盖缺陷区域，写回原图并支持 Ctrl+Z 撤销。

### 代码与体量

`anylabeling/custom/smudge_tool/`（4 个文件 1726 行：`texture_fill.py` 算法、
`operations.py` 读写/备份/几何、`smudge_filter.py` Qt 层、`__init__.py` 导出）；
测试 `tests/custom/smudge_tool/`（4 个文件 1414 行）。

### 入口符号

`anylabeling.custom.smudge_tool.install_smudge_tool`（幂等安装）、
`anylabeling.custom.smudge_tool.SmudgeController`（按钮、事件过滤器、覆盖层、撤销栈）。

### 挂载点（锚点原文，行号见 contract.json）与软挂载

- `anylabeling/views/labeling/label_widget.py`：`from anylabeling.custom.smudge_tool import install_smudge_tool`
- `anylabeling/views/labeling/label_widget.py`（`LabelingWidget.__init__`）：`install_smudge_tool(self)`
- 软挂载（4 处，全在 `anylabeling/custom/smudge_tool/smudge_filter.py`）：
  - `LabelingWidget.populate_mode_actions` ← `_wrap_populate_mode_actions`
  - `LabelingWidget.import_image_folder` ← `_wrap_import_image_folder`
  - `Canvas` 事件过滤器 ← `SmudgeController`
  - `Canvas.mode_changed` ← `_on_canvas_mode_changed`

### 依赖的上游状态

| 上游 | 分类 | 用途 |
|---|---|---|
| `LabelingWidget.populate_mode_actions` | wrapped | 工具栏重建后重新挂按钮 |
| `LabelingWidget.import_image_folder` | wrapped | 换目录时清空撤销历史 |
| `Canvas.mode_changed` | wrapped | 切到画笔/魔棒时自动退出涂抹模式 |
| `LabelingWidget.canvas` | direct | 事件过滤器宿主与视图刷新 |
| `LabelingWidget.tools` | direct | 工具栏：按钮加进去、撑高 |
| `LabelingWidget.actions` | direct | 与上游动作共存 |
| `LabelingWidget.filename`、`image_path` | direct | 解析当前图的磁盘路径 |
| `LabelingWidget.image_data` | transitive | 确认画布上确有图像 |
| `LabelingWidget.brightness_contrast_processor`、`brightness_contrast_values` | transitive | 刷新视图时不覆盖显示参数 |
| `LabelingWidget.status`、`statusBar`、`error_message` | direct | 状态与报错出口 |
| `Canvas.transform_pos`、`offset_to_center`、`scale`、`out_off_pixmap` | direct | 屏幕坐标与图像坐标互换 |
| `Canvas.load_pixmap`、`pixmap` | direct | 写回后重载画面 |
| `Canvas.override_cursor`、`restore_cursor` | direct | 模式光标 |
| `Canvas.is_loading` | direct | 上游加载中时不抢事件 |
| `Canvas.is_brush_mode`、`is_magic_wand_mode`、`drawing` | direct | 判断是否已切到别的绘制模式 |
| `anylabeling.views.labeling.utils.image.img_data_to_pil` | direct | 备份/读取路径上的图像转换 |

### 行为级契约（不可机器校验）

- **工具栏重建后按钮回来**：上游 `populate_mode_actions` 会先清空工具栏再重新添加动作，
  包装体在调用原实现之后重新 `addAction` 自己那个 already-created 的按钮；
  原方法体一行未改，包装只叠加，且只包装一次。
- **原图备份**：第一次改写某个文件前，把原图复制到
  `operations.default_backup_dir()`（系统临时目录下的 `dsh-smudge/<时间戳>-<pid>`），
  同名冲突加数字后缀；只复制、不移动、不删除，一个目录里同一文件只备份一次。
- **撤销**：历史在内存里按图片分组，记录 ROI、原像素块与磁盘路径；Ctrl+Z 把像素放回
  屏幕与磁盘。换目录、重新打开文件夹、进程结束都会丢掉历史（磁盘上的备份保留）。
- **几何门槛**：ROI 小于 6 像素拒绝执行；源点必须先右键选；没有磁盘文件的图像拒绝写回。
- **模式互斥**：进入画笔/魔棒模式时自动退出涂抹模式，按钮状态跟着回弹。
- 事件过滤器只在涂抹模式且拿到左键/右键时消费事件，其余一律放行。

### 测试

`python -m pytest -p no:cacheprovider tests/custom/smudge_tool -v`（需 PyQt6 + numpy + OpenCV）。
本机无 PyQt6/numpy，未能运行。

### 已知坑

- 所有软挂载都在实例上，不在类上：上游同步后必须逐条核对，contract.json 的
  `soft_mounts` 是这份清单的唯一事实源。
- 备份目录永不清理，长期使用会累积原图副本（有意为之：撤销与追溯优先）。
- 按钮图标名是 `brush`，取自带 icon 的生成资源 `anylabeling/resources/resources.py`；
  换图标要同时改这里的名字。
- 上游 `Canvas.offset_to_center` 等几何方法改名时会静默失效（运行时才炸），
  靠 contract.json 的 upstream 清单在同步时兜住。

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
- `"line": <真实行号>`：必须填锚点在目标文件里的真实行号；留 0 或照抄别人的旧行号，
  非严格模式下是永久 `line drift` 告警，`XAL_CONTRACT_STRICT=1`（同步上游后）直接 FAIL。
