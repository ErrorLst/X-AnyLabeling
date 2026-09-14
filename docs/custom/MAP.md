# X-AnyLabeling 架构与热路径地图（MAP.md）

给人读的导航图：从入口到画布的主链、数据怎么流、哪些文件最热、想改一件事该看哪。
配合 `docs/custom/FEATURES.md`（自研功能索引）与 `docs/custom/contract.json`（机器可读契约）使用。

坐标系（三处 custom 一一对应）：

    anylabeling/custom/<feature>/   实现代码（自研代码只放这里）
    tests/custom/<feature>/         与实现一一对应的测试
    docs/custom/                    契约与人读文档（contract.json / FEATURES.md / MAP.md）

三条阅读约定：

1. **本文件不写具体行号**（除了 `class X` 这类结构锚点）：行号会漂移，用 `glob` 找文件、
   用符号名定位代码；挂载点的行号快照只存在于 `docs/custom/contract.json`。
2. `anylabeling/resources/resources.py` 是生成物（约 10.9 万行）：**禁止打开、禁止扫描、
   禁止参与任何全树搜索**。
3. 找文件用 `glob` 工具的「工作区相对路径」模式（如 `**/canvas*.py`），不要做全树递归扫描。

## 入口与主调用链

    anylabeling/app.py            def main()
      -> anylabeling/views/mainwindow.py            class MainWindow
        -> anylabeling/views/labeling/label_wrapper.py     class LabelingWrapper
          -> anylabeling/views/labeling/label_widget.py    class LabelingWidget
               self.canvas = self.label_list.canvas = Canvas(...)
            -> anylabeling/views/labeling/widgets/canvas.py      class Canvas
            -> anylabeling/views/labeling/widgets/auto_labeling/auto_labeling.py
                 class AutoLabelingWidget
                   self.model_manager = ModelManager()
              -> anylabeling/services/auto_labeling/model_manager.py  class ModelManager
                -> 本地模型：anylabeling/services/auto_labeling/<model>.py
                -> 远端模型：anylabeling/services/auto_labeling/remote_server.py
                     class RemoteServer（HTTP 到 X-AnyLabeling-Server）

自研功能的挂载点绝大多数落在这条链的两个地方：`LabelingWidget.__init__`（1 行 import + 1 行调用）
与 `Canvas` 的实例级包装（事件过滤器 / 方法包装）；例外有五处：模型验证的菜单动作与
方法定义、重命名工具的 Tool 菜单动作追加、标签过滤的 Tool 菜单运行时追加
（`menus.tool.addAction`）与 `LabelingWidget.import_image_folder` 的实例级包装，
换图复位视图的 `load_file` 实例级包装，
以及崩溃日志的第五处——`anylabeling/app.py` **模块顶层**挂载（1 行 import + 1 行调用，
发生在 `LabelingWidget` 之前，是整个进程里最早安装的自研功能）。
逐条清单见 `docs/custom/contract.json` 的 `features.<id>.mounts`。

## 数据流

**① 打开图片到落盘**：`anylabeling/views/labeling/label_widget.py` 的 `load_file`
-> `anylabeling/views/labeling/label_file.py` 的 `LabelFile` 读 json
-> 形状进 `label_list` / `canvas.shapes` -> 用户编辑 -> `save_labels` 写回 json。
自研的 `ensure_label_file` 就挂在这个链条的两端：包装 `load_file`，缺文件时复用 `save_labels` 落盘。
同一链条上还挂着 `reset_view_on_switch`（包装 `load_file`，换图复位视图）。

**② 自动标注**：`AutoLabelingWidget` 面板 -> `ModelManager.load_model`
-> `Model.predict_shapes`（本地）或 `RemoteServer`（远端）-> 结果作为 marks 回到 `canvas`。
远端子链固定打在 Server 的 `/v1/models`、`/v1/predict`（视频系列走 `/v1/video/*`），
URL 在 `anylabeling/services/auto_labeling/remote_server.py` 里由 `server_url` 拼出。

## 热文件表（快照行数 = 2026-09-14）

| 文件 | 行数 | 说明 |
|---|---|---|
| `anylabeling/views/labeling/label_widget.py` | 7308 | 主窗口逻辑 + 全部挂载点 |
| `anylabeling/views/labeling/widgets/canvas.py` | 5541 | 画布：绘制、缩放、编辑、滚轮 |
| `anylabeling/views/labeling/ppocr/editors.py` | 4801 | PPOCR 编辑面板 |
| `anylabeling/services/auto_labeling/model_manager.py` | 2716 | 模型注册与加载 |
| `anylabeling/views/labeling/label_converter.py` | 2359 | 标注格式互转 |
| `anylabeling/views/labeling/settings/dialog.py` | 2175 | 设置对话框 |
| `anylabeling/views/labeling/utils/export.py` | 1953 | 导出实现 |
| `anylabeling/views/labeling/widgets/auto_labeling/auto_labeling.py` | 1857 | 自动标注面板 |
| `anylabeling/services/auto_labeling/remote_server.py` | 878 | 远端模型客户端 |
| `anylabeling/resources/resources.py` | 109255 | **生成物：禁止打开 / 扫描 / 全树搜索** |

## 目录清单（快照 2026-09-14）

- `anylabeling/views/labeling/`：111 个 .py / 77325 行，含 8 个子包
  （`chatbot`、`classifier`、`ppocr`、`settings`、`utils`、`video_classifier`、`vqa`、`widgets`）。
- `anylabeling/services/auto_labeling/`：101 个顶层 .py + 8 个子包；一个模型一个文件。
- `anylabeling/services/auto_training/`：11 个 .py（ultralytics 训练链）。
- `anylabeling/custom/`：69 个 .py / 37449 行（目录内全部 .py；FEATURES.md 登记其中
  8 个自研功能，另有未登记的 `remote_training`）。
- `anylabeling/custom/model_validation/`：24 个 .py / 12543 行（关键文件：`ui/` 下的
  `dialog.py` 1188、`results_page.py` 2008、`image_view.py` 1280，以及
  `main_window_bridge.py` 689、`async_scan.py` 189、`multilabel.py` 360）；本轮新增
  `multilabel.py`（360 行：整图类无关 NMS 的合并、每类一行的展开与 IoU），
  `inference.py`（508 行）只做接线；「编辑搬到主窗口」那一轮新增
  `main_window_bridge.py`（现 689 行，跳主窗口 + 保存回写）与 `async_scan.py`（189 行，
  异步目录扫描），删除 `label_dialog.py`（内置标签弹窗）。
- `tests/custom/`：85 个 .py / 41068 行（含契约自检脚本 `tests/custom/test_fork_contract.py`）。

## 想改 X 该看哪里

| 想改什么 | 看哪里 |
|---|---|
| 新增自研功能 | `anylabeling/custom/` + `docs/custom/contract.json` 的 features 一节（步骤见 `.dsh/skills/xal-add-custom-feature/SKILL.md`） |
| 画布交互与滚轮缩放 | `anylabeling/views/labeling/widgets/canvas.py`；普通滚轮缩放由 `anylabeling/custom/edit_extras/` 以事件过滤器接管 |
| 切换图片时画布缩放/滚动回到默认 | `anylabeling/custom/reset_view_on_switch/`（实例级包装 `load_file`，换图复位成首图初始态、同文件重载不复位、有意压过 `keep_prev_scale`；软挂载见 contract.json） |
| 数据集按主分类批量重命名 | `anylabeling/custom/rename_tool/`（Tool 菜单「重命名」，拖拽目录一键导出，源目录只读，结果输出 zip） |
| 按标签分类过滤文件列表 | `anylabeling/custom/label_filter/`（Tool 菜单「标签过滤」运行时追加，实例级包装 `import_image_folder` 做二道过滤） |
| 崩溃 / 运行日志（无控制台取证） | `anylabeling/custom/crash_log/`（挂载 `anylabeling/app.py:23-24`；日志 `~/.xanylabeling/logs/xany-YYYYMMDD.log`，保留 14 份；`XANY_LOG_DIR` 改目录、`XANY_LOG_DISABLE=1` 关闭） |
| 快捷键 | `anylabeling/views/labeling/label_widget.py` 的动作定义（快捷键表取自 `self._config["shortcuts"]`，即 `.xanylabelingrc`） |
| 标注文件读写（json / LabelFile） | `anylabeling/views/labeling/label_file.py`；保存入口是 `label_widget.py` 的 `save_labels` |
| 导入导出格式 | `anylabeling/views/labeling/label_converter.py` 与 `anylabeling/views/labeling/utils/export.py` |
| 设置项 | `anylabeling/views/labeling/settings/dialog.py`（配套 `schema.py` / `controller.py` / `runtime_applier.py`） |
| 自动标注模型下拉与面板 | `anylabeling/views/labeling/widgets/auto_labeling/auto_labeling.py`；模型清单与加载在 `anylabeling/services/auto_labeling/model_manager.py` |
| 远端子服务（HTTP 契约） | `anylabeling/services/auto_labeling/remote_server.py`；服务端在 X-AnyLabeling-Server 仓库（端点契约见其 AGENTS.md） |
| PPOCR 相关 | `anylabeling/views/labeling/ppocr/editors.py`（同目录 `pipeline.py` / `dialogs.py`） |
| 训练 | `anylabeling/services/auto_training/ultralytics/trainer.py` |
| 帮助菜单与动作 | `anylabeling/views/labeling/label_widget.py`（动作统一用 `action(...)` 工厂创建） |
| 图标资源 | 生成物 `anylabeling/resources/resources.py`（**禁扫**）；使用处用 `QIcon(":/images/images/<name>.png")` |
| 主题与样式 | `anylabeling/views/labeling/utils/theme.py` 与 `anylabeling/views/labeling/utils/style.py` |
| 国际化字符串 | 调用点写 `self.tr(...)`（如 `anylabeling/views/labeling/label_widget.py`）；加载在 `anylabeling/app.py`，读 `:/languages/translations/<lang>.qm` |
| 主窗口与菜单栏结构 | `anylabeling/views/mainwindow.py` |
| 应用启动与命令行 | `anylabeling/app.py` |
| 自研功能的挂载点 / 上游依赖符号 | `docs/custom/contract.json`（唯一事实源）与 `docs/custom/FEATURES.md` |

## 禁止与陷阱

- 不要用 `find` / 全树递归扫描定位文件：仓库里有 10 万行的 `anylabeling/resources/resources.py`。
  用 `glob` 的工作区相对模式（如 `**/settings/*.py`）。
- 不要跑全量 pytest：只跑单文件或单个功能目录，并且一律带 `-p no:cacheprovider`
  （例：`python -m pytest -p no:cacheprovider tests/custom/smudge_tool -v`）。
- 桌面测试需要 `QT_QPA_PLATFORM=offscreen`（`tests/custom/*/conftest.py` 已设置）。
- 临时文件写到系统临时目录（`tempfile.gettempdir()`），**不要写进仓库**；
  需要「删除」时移动到 `${TMPDIR:-/tmp}/dsh-trash/<时间戳>-<名字>`，不要用 `rm`。
- 不用 `git clean` / `git checkout --` / `git reset --hard` / `git stash`。
- 契约自检零第三方依赖（不 import PyQt6 / numpy / pytest）：
  `python3 tests/custom/test_fork_contract.py`。
