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
| smudge_tool | 涂抹修复：取别处纹理覆盖缺陷并撤销 | `anylabeling/custom/smudge_tool/` | `tests/custom/smudge_tool/` | 1 个（1 行 import + 1 行调用） | 5 |
| rename_tool | 按主分类批量重命名并打包成 zip（拖拽目录一键导出，源目录只读） | `anylabeling/custom/rename_tool/` | `tests/custom/rename_tool/` | 1 个（1 行 import + 1 行调用） | 1 |

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

`anylabeling/custom/smudge_tool/`（4 个文件 1945 行：`texture_fill.py` 算法、
`operations.py` 读写/备份/几何、`smudge_filter.py` Qt 层、`__init__.py` 导出）；
测试 `tests/custom/smudge_tool/`（4 个文件 1998 行）。

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
| `Canvas.set_editing` | wrapped | 任何画布模式切换都先退出涂抹模式（按钮/覆盖层/光标一并收回） |
| `Canvas.mouseMoveEvent` | wrapped | 上游把它改回箭头光标后，包装体把模式十字放回去 |
| `LabelingWidget.canvas` | direct | 事件过滤器宿主与视图刷新 |
| `LabelingWidget.tools` | direct | 工具栏：按钮加进去、撑高 |
| `LabelingWidget.actions` | direct | 与上游动作共存 |
| `LabelingWidget.actions.edit_mode` | direct | 安全地请上游把画布切回编辑模式（触发它，而不是直接改 `canvas.mode`） |
| `LabelingWidget.filename`、`image_path` | direct | 解析当前图的磁盘路径 |
| `LabelingWidget.image_data` | transitive | 确认画布上确有图像 |
| `LabelingWidget.brightness_contrast_processor`、`brightness_contrast_values` | transitive | 刷新视图时不覆盖显示参数 |
| `LabelingWidget.status`、`statusBar`、`error_message` | direct | 状态与报错出口 |
| `Canvas.transform_pos`、`offset_to_center`、`scale`、`out_off_pixmap` | direct | 屏幕坐标与图像坐标互换 |
| `Canvas.load_pixmap`、`pixmap` | direct | 写回后重载画面 |
| `Canvas.override_cursor`、`restore_cursor` | direct | 模式光标 |
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
  屏幕与磁盘。换目录、重新打开文件夹、进程结束都会丢掉历史（磁盘上的备份保留）。
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
  `restore_cursor()` 归还。
- **模式互斥（双向）**：涂抹模式与任何画布绘制/编辑模式不共存。上游每次
  `Canvas.set_editing`（九个绘制动作、数字键、画笔多边形、魔棒、编辑对象、
  画笔编辑，以及自动标注与画笔进入时画布自己的调用）都先退出涂抹模式——按钮
  回弹、覆盖层与橡皮筋隐藏、光标归还——再执行原实现；反过来，画布处于 create
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
- 事件过滤器只在涂抹模式且拿到左键/右键时消费事件，其余一律放行。

### 测试

`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -p no:cacheprovider tests/custom/smudge_tool -v`
（需 PyQt6 + numpy + OpenCV；本工作区用仓库里的 `.venv`，116 个用例全部通过：
`test_st_operations.py` 28 + `test_st_texture_fill.py` 39 + `test_st_filter.py` 49）。

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
  它是 `LabelingWidget.__init__` 里的局部动作、不是类成员，故只在
  `contract.json` 的 `upstream` 里以 `LabelingWidget.actions` 登记，
  `edit_mode` 本身不进 AST 清单。

## rename_tool

### 职责

Tool 菜单里的「重命名」：按标注主分类把一份扁平数据集里的图片与同名 json 批量
改名，结果输出为一个 zip。**源目录严格只读**：不改名、不写入、不改 mtime、不建
临时文件，也不建任何暂存目录；需要改名的文件在 zip 里用新名字，其余条目用原名
原字节。源目录可以从文件对话框选择，也可以直接拖进窗口；界面上没有 zip 文件名
输入框、没有输出目录选择控件，也没有预览步骤，点一次「重命名」就串起扫描、
阻塞检查与打包，zip 名与落点都在执行时从源目录派生：`<源目录名>_renamed.zip`
写到源目录的上级目录（即数据集的同级目录），重名自动 `_2`、`_3`…，只产出那
一个 zip。

### 代码与体量

`anylabeling/custom/rename_tool/`（4 个文件 1547 行：`rename_core.py` 规则与打包、
`dialog.py` Qt 层、`launcher.py` 惰性启动、`__init__.py` 导出）；
测试 `tests/custom/rename_tool/`（8 个文件 2471 行）。

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
  中文，带文件名）：B1 图片没有同名 json；B2 json 没有同名图片；B3 json 无法解析或
  顶层不是对象；B4 目录内有子目录（只处理顶层文件）；B5 同一 stem 有多张图片或多份 json。
- **R8 条目名校验**：条目名非空、不含斜杠、不含 `..`、非绝对路径；每个源文件名在
  计划里恰好出现一次（`check_entry_names` 会重新列一遍源目录顶层文件，漏镜像或重复
  计入都算问题）；任何改名目标名不得等于任何「原样镜像」文件的名字（already /
  orphan / mirrored 三类），否则阻塞。结果表显示的名字就是 zip 里的名字，执行期不补后缀。
  `entry_name_ok` 拒绝任何含连续两个点的名字（`v1..2.txt` 即非法），比「不含 `../`」
  更严；`execute()` 会把 `check_entry_names` 的结果并入阻塞项，因此非法条目名在扫描
  阶段就变成阻塞弹窗，而不是等到写 zip 才失败。
- **R9 只读源目录 + 产出 zip**：zip 是源目录顶层的完整镜像（每个顶层文件一个条目，
  含 `classes.txt`、隐藏文件、任意二进制文件，原字节）。`plan.mirrored` 的定义就是
  「顶层里没有被任何 item 的 image_name / json_name 引用的文件」，所以孤儿 json（B2）
  与同 stem 冲突里的第二张图（B5）也在这里，结果表因此能列出全部文件。先写
  `<最终名>.part`，成功后 `os.replace` 到最终名；zip 最终名在执行时由
  `<源目录名>_renamed.zip` 派生、已存在则自动取 `_2`、`_3`…（界面没有文件名输入框，
  完整路径只在成功弹窗与状态栏里给出）；`write_zip` 自身也把关：`plan.blocked()` 或
  最终名已存在时直接 `raise RenameError`（不依赖对话框）。失败时**不删除** `.part`，
  异常信息里带上它的完整路径；图片条目 `ZIP_STORED`，其余条目 `ZIP_DEFLATED`，
  `allowZip64=True`，不写目录条目；输出 zip 不得落在源目录内。
- **R10 惰性单实例 + 主线程**：`launch_rename_tool(parent)` 内延迟 import 对话框，
  复用 `parent._rename_tool_dialog`；执行在主线程完成，`QCoreApplication.processEvents()`
  驱动进度、期间 `dialog.setEnabled(False)`，**不引入线程、不提供取消**。
- **R11 交互（拖拽目录 + 一键导出到数据集同级目录）**：`setAcceptDrops(True)`；
  `dragEnterEvent` 与 `dragMoveEvent` 走同一个辅助方法，只在拖入项里有文件夹时接受、
  否则忽略，`dropEvent` 取第一个文件夹（`os.path.abspath` 归一化）为源
  目录，非文件夹项与多余文件夹在状态栏提示「已忽略: 名字（仅支持文件夹 /
  仅取第一个文件夹）」，一个文件夹都没有时提示「仅支持文件夹」且不改动源目录；
  拖拽只设定目录，绝不自动导出。界面只有源目录一行与一条只读提示行
  （「导出到数据集同级目录：<源目录名>_renamed.zip（重名自动加 _2、_3…）」），
  **没有输出目录选择控件**（没有 `output_button` / `output_label`，也没有
  `pick_output_dir` / `set_output_dir` / `output_dir`）。主按钮「重命名」
  （objectName `primary`）在源目录为空时禁用；没有「预览计划」按钮、没有 zip 文件名
  控件，也没有「确认执行」二次确认。点击后依次做：校验源目录（缺失则告警）；
  由源目录派生导出目录（`os.path.dirname(os.path.normpath(os.path.abspath(源)))`，
  即数据集同级目录），派生不出上级（源目录是文件系统根，或上级规范化后等于源目录
  本身）则告警「源目录没有上级目录，无法导出」并中止，**不写任何文件、不碰 `.part`**；
  主线程跑 `plan_directory` → `resolve_targets` →
  `check_entry_names` 并入 blockers（进度条 + `processEvents`，`setEnabled(False)`
  防重入、finally 恢复）；阻塞则 `QMessageBox.warning` 列前 8 条 + 总数、状态列写
  「阻塞，未导出」，**不写 zip、不碰 `.part`**；否则写 zip 成功后
  `QMessageBox.information` 给出完整路径与计数，状态列写「已改名 / 符合规范，保持原名 /
  孤儿增强文件，保持原名 / 原样镜像」。扫描失败（`plan_directory` 抛 `RenameError`，
  例如源目录被删或失去读权限）与写 zip 失败（例如上级目录不可写）共用同一条失败出口：
  先 `_discard_result()` 再 `_report_failure()`，结果表立刻退出成功外观（状态列全部
  改回「未导出」、统计回到「尚未重命名」，有意保留「新文件名」列），阻塞提示条改显示
  「导出失败：<原因>」，弹窗与状态栏给出「重命名失败」与半成品 `.part` 路径。
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
（需 PyQt6；本工作区 237 个用例全部通过）。

### 已知坑

- 源目录只读，所以源数据永远不会被规范化：要真正把名字落到数据上，解压 zip 之后
  再跑一次（第二次跑应该全是「符合规范」）。
- 空目录、或没有任何待改名项的目录（全部已符合规范）点一次也会导出一份镜像 zip：
  前者是 0 条目的 zip，后者条目全部原样镜像；源目录只读，绝不覆盖既有文件（zip
  重名时自动取 `_2`、`_3`…）。
- 缺 json、坏 json、只有 json 没图片、子目录、同 stem 多文件都会**整体阻塞**，
  一项都不改；点击「重命名」被阻塞时结果表仍列出顶层全部文件，但每一行的状态列都是
  「阻塞，未导出」（因为确实什么都没导出），弹窗与阻塞提示条给出原因（最多前 8 条 +
  总数），数据集同级目录里不会出现任何文件，包括 `.part`。
- 孤儿 `_aug` 放行且保持原名（它没有父项可继承编号），也不占号。
- 失败出口只回退「状态」列与统计，「新文件名」列保留本次扫描算出的结果，方便用户看到
  本会改成什么名字；此时阻塞提示条与状态栏写的是失败原因，而不是「未发现阻塞项」。
- 失败留下的 `.part` 要自行处理：工具绝不删除任何文件，下一次执行会被「临时文件
  已存在」挡下来，需要人工确认后处理。
- 图片条目 `ZIP_STORED`、其余条目 `ZIP_DEFLATED`：图片本来就不压缩，再套一层
  deflate 只会变慢；json 文本压得动。
- R8 第 3 条（改名目标不得等于已被占用的名字）分两种来源：
  ① 光看 `plan.mirrored` 在可解析的目录里撞不上——图片形状的名字只有在同 stem 已被
  另一张图片占用（那是 B5，已阻塞）时才会进 mirrored，`.json` 形状则只可能是孤儿
  json（B2）或 B5 重复，所以扩展名形状互斥；白盒用例（手工往 `plan.mirrored` 里加
  撞名）钉的就是这个分支。
  ② 但 `plan.unchanged_names()` 还包含 already / orphan 两类 item 的当前名字，orphan
  的名字就是普通的 `<stem>_aug<x>.jpg` 形状，**真实目录里撞得上**：`b.jpg` +
  `b.json`(person) + `b_aug1.jpg`/`b_aug1.json` +
  `person_1_aug1.jpg`/`person_1_aug1.json` 时 `b -> person_1`、
  `b_aug1 -> person_1_aug1`，而 `person_1_aug1` 因纯 stem
  `person_1` 没有图片成为 orphan 保持原名，于是撞名阻塞。不拦就会写出两个同名 zip 条目，
  所以这是真实可达的阻塞，用例见 `test_rt_blockers.py` 的
  `test_target_collides_with_orphan_name_in_a_real_folder`。
- 扫描与打包都在主线程：几千个文件的大目录会卡住界面，靠进度条 +
  `processEvents` 维持响应，不提供取消（取消会留下用户看不见的 `.part`）。

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
