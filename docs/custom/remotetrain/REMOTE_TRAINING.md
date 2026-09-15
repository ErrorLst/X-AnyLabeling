# X-AnyLabeling 远程训练 · 设计规格

> **本文件是「远程训练」功能的唯一设计规格**：两个 fork 仓库（桌面标注器与服务端）的实现、联调与验收都以本文件为唯一依据。

## §0 文档说明与阅读顺序 · 术语 · 示例值≠契约

### §0.1 文档地位、场景前提与阅读顺序

**文档地位**：唯一设计规格；服务端与客户端共享同一套契约，任何冲突以本文件为准。

**场景前提（只在此声明一次，全文不再重复）**：

| 前提 | 取值 |
| --- | --- |
| 使用规模 | **内部小规模**：约 **10 个用户**在同一内网共享一台自有服务端、共用一个 `work_dir`、**共用同一把 `api_key`**；训练跑在 detached 子进程，web 层流量约 1–3 req/s |
| 服务端平台 | **仅 Linux** 的部署与测试（systemd / `/proc` 探活 / `fcntl.flock`） |
| 进程模型 | 单进程单 worker（`uvicorn app.custom.server:app`，不带 `--workers`） |
| fork 约定 | 自研代码只放新增目录（`custom/`）；上游文件只允许极少量挂载点，服务端为零改动上游 |
| 首版任务类型 | Ultralytics `detect` 与 `segment` |

**阅读顺序**：

| 目标 | 章节 |
| --- | --- |
| 实现服务端 | §2 + §3 + §4 |
| 实现客户端 | §2 + §3 + §5 |
| 联调与排障 | §3 + §6 |

**引用写法（全文唯一两种）**：同文件写 `§x.y`；跨篇写 `服务端篇 §x.y` / `客户端篇 §x.y`。不使用文件路径互引。

### §0.2 术语表

| 术语 | 含义（一句话） |
| --- | --- |
| `stem` | 图片文件名去掉原扩展名后的主干名，是标签文件 `labels/<split>/<stem>.txt` 的命名依据 |
| `plan` / `upload` | 两阶段上传的两个路由：`plan` 声明清单并换取 `upload_token`，`upload` 提交 zip 并落库 |
| 两阶段上传 | 先 plan 后 upload 的提交顺序：plan 只做清单登记、schema 校验与容量**预检（advisory，非准入依据）**；**容量准入以提交期临界区复检为准**（§4.1.3）；upload 才写 blob 与数据集目录 |
| manifest | plan 阶段提交的数据集清单（逐文件 `stem` / `split` / `sha256` 与标注信息），服务端据此校验、物化与复核 |
| `file_id` | 产物清单 `files[]` 条目的不可变文件标识；下载接口只用它寻址，不拼相对路径 |
| 部分结果 | 取消 / 失败 / 中断任务保留在 `artifacts/<job_id>/partial/` 的结果，清单以 `partial: true` 标注 |
| `capabilities` | 服务端下发的能力快照（设备、显存表、参数面、常量、warnings），客户端据此决定能否提交与如何展示 |
| `is_terminal` | 服务端下发的权威终态字段，定义 `is_terminal := (finished_at != null)` |
| preset 家族 | 按模型家族下发的 optimizer 预设组（如 `yolo11` / `yolo26`）；`auto` 是取值哨兵（客户端可选、服务端默认也可选），两种来源都**不注入超参** |
| `attempt` | 一次训练尝试；同一恢复周期内的两次 attempt 靠「自动重排入队前归档上一 attempt 的终态证据」区分（§4.3.1） |
| 单 worker | 服务端只跑一个进程（不带 `--workers`），因此进程内全局锁与内存态才是唯一权威 |
| 软删除 | 一切删除先移入 `.trash/`，按 `trash_ttl_hours` 到期物理回收；**唯一例外**见 §4.1.7（超配额路径下一轮即清除）；禁止直接删 |

### §0.3 示例值≠契约

**口径**：文中 JSON / YAML 示例里的 `sha256` / `mtime` / `size` / 计数型数值（条数、字节数、序号）属**示例省略**（可截断、可缩短、可用占位值），一律以字段表与规则段为准；示例本身不构成契约。

**三处例外不得臆造**：

| 例外 | 要求 |
| --- | --- |
| `file_id` | 示例值必须能**按 §3 的规则复算**出来，不得随手编造 |
| `state.json` / `queue.json` 字段名 | 服务端磁盘契约：字段名逐字为准，不得改名、不得省略 |
| 错误码字符串 | 必须与 §3 的错误码表逐字一致，不得自造近义词 |

---

## §1 范围与决策台账

### §1.1 目标链路（六步）

| 步 | 动作 | 关键契约 |
| --- | --- | --- |
| 1 | **客户端选择**：数据集本地目录 + 类别表 `classes.txt` + 模型 + 训练参数 | 任务类型限 `detect` / `segment`；参数面见 §3 |
| 2 | **客户端本地处理**：扫描图片与同名 `.json` → 标注转换 → 按类别独立分层划分 train/val（可配随机种子）→ 打包 zip | 客户端篇 §5.2 |
| 3 | **两阶段增量上传**：plan 声明清单并取 `upload_token` → upload 提交 zip | 图片按 sha256 内容寻址去重；标注文件每次全量、绝不缓存；见 §4.1 |
| 4 | **服务端排队与调度**：blob 入库 → 数据集物化 → 任务严格 FCFS 入队 → 多卡显存账本调度 | §4.1、§4.2 |
| 5 | **GPU 训练与观测**：训练进程脱离服务端进程运行；客户端按 `job_id` 轮询状态与 `?after=<seq>` 增量事件，可取消 | §3、§4.3、客户端篇 §5.5 |
| 6 | **恢复与取回**：非正常结束（`failed` / `interrupted` / `cancelled`）可手动恢复；结束后客户端自行下载结果（含部分结果） | §4.3、客户端篇 §5.6 |

### §1.2 首个版本范围

| 维度 | 结论 |
| --- | --- |
| 任务类型 | 仅 Ultralytics `detect` 与 `segment`；请求体保留 `task` 字段，可用取值由 `configs/custom/training.yaml` 的 `tasks` 决定，未来可扩展 |
| 数据集来源 | 客户端**本地目录**（非当前标注项目）：图片 + 同名 `.json`，类别表 `classes.txt` |
| 服务器形态 | 单服务器、单进程单 worker；服务端**仅 Linux** |
| 鉴权 | 沿用上游 `APIKeyMiddleware`（header `Token`）；**不做用户体系、不做 owner 隔离是内部信任模型下的显式决策**（用户已拍板）：持 key 者可查任意任务，实践上各客户端按本地台账 `ids` 查询、各看各的（§1.3） |
| 上传 | **两阶段增量上传**（plan → upload）：图片按 sha256 内容寻址增量去重，标注文件每次全量上传；**不做断点续传**（中断即重传，已入库 blob 仍命中） |
| 队列 | 严格 FCFS（先到先服务，不插队）；提交时做可行性预检 |
| 实时性 | 客户端**轮询**（HTTP 短请求）：任务状态 + `?after=<seq>` 增量事件；**不做** SSE / WebSocket |
| 训练环境 | 单一环境（不建独立训练 venv）+ fork 自有 `requirements/custom/training.txt`；`python_executable` 作为逃生舱 |
| 恢复 | 手动恢复仅限 `failed` / `interrupted` / `cancelled`；**`completed` 不可恢复** |
| 结果消费 | 客户端自行下载结果，**含部分结果**（取消 / 失败保留的 `partial/`）；**不做**发布为服务端推理模型 |
| 客户端台账 | 本地**永久记录**全部训练任务 ID（客户端职责，客户端篇 §5.3）；服务端保证按 `job_id` 查询稳定 |
| 部署与测试 | **仅 Linux**：systemd 部署与测试；不提供 Windows 支持 |

### §1.3 非目标（首个版本明确不做）

- **多租户 / 用户体系 / 按用户配额 / 任务 owner 鉴权**（**有意不做**，不是遗漏）：【用户已拍板，内部信任模型】不引入用户体系与按用户隔离；同一把 `api_key` 的使用者可见彼此的任务，但客户端按本地 `tasks.json` 台账的 `ids` 查询（`GET /jobs?ids=`，§3.2.4），因此**实践上各看各的**。
- SSE / WebSocket 推送（实时性只做轮询）。
- OBB / Pose / Classify 训练。
- 把训练产物发布为服务端推理模型（`GET /v1/models` 行为不变）。
- 数据集整包级去重与整包校验（仅逐图片 sha256 校验）。
- Windows 部署与测试。
- 上传与单文件下载的断点续传（下载侧只做单文件 `Range` 取用，不做跨会话续传）。
- `systemd-run` 每任务独立 cgroup（保留 `KillMode=process`；见 §7）。

### §1.4 fork 约定与部署前提

- 两个仓库均为 fork：`origin=ErrorLst/*`、`upstream=CVHub520/*`。**任何对上游文件的修改都会让后续 `git merge upstream/main` 产生冲突**，因此一切改动以「同步成本最小」为第一原则。
- 自研代码只放**新增目录**；上游文件只允许极少量「挂载点」（客户端侧为 1~2 行 import + 调用；服务端为**零改动上游**）。
- 服务端部署形态：`uvicorn app.custom.server:app`（组合 app），由部署脚本 / systemd `ExecStart` 指定；训练节点不再以 `app.main:app` 为入口，但 `app.main:app` 本身保持可用、未被修改。
- 服务端运行前提：Linux（`/proc` 探活、`os.killpg`、`fcntl.flock`）+ systemd `KillMode=process`（只杀主进程，训练子进程存活）。
- 上游默认配置**没有鉴权**，而训练端点含上传 / 训练 / 取消 / 恢复 / 删除数据集 / 下载产物，因此服务端启动自检是 fail-closed 的（§2.4）。
- **部署形态（约 10 个用户）**：约 10 个用户在同一内网、共享一台训练服务器、一个 `work_dir` 与一把 `api_key`；并发由 GPU 决定（`max_concurrent_jobs` 默认 **2**、每卡 **1**，§3.11 / §4.2.1），**用户数只影响排队长度，不影响 web 层**——训练跑在 detached 子进程里、web 进程不做 GPU 工作。排队可观测性已具备：job 对象的 `queue_position` / `eta_seconds`（§3.4.4）与 `queued_reason`（§4.2.1）。**单进程单 worker 因此是本前提的直接推论、必须保留**：全部并发保护都是进程内的，多 worker 会让它们**静默失效**（§3.11、§4.4.5）。


### §1.5 决策台账

除 §7 的「未决项」列出的条目外，全部决策已确认，实现与文档逐条对齐。`D9` 已被 `N4` 推翻，以 `N4` 为准。

**基础决策（D）**

| 编号 | 决策 | 落地要点 |
| --- | --- | --- |
| D1 | 内部使用（约 10 人共用一把 key），不做用户体系 | 沿用上游 `app/core/middleware.py` 的 `APIKeyMiddleware`，header 名取自 `settings.security.api_key_header`（默认 `Token`）；**不做 owner 隔离 = 内部信任模型下的显式决策**（用户已拍板）：持 key 者可查任意任务，客户端按本地台账 `ids` 查询故实践上各看各的（§2.4） |
| D2 | 数据集来源是本地目录（非当前标注项目） | 客户端 UI 参考「模型验证」：只读路径框 + 浏览按钮；数据集 = 图片 + 同名 `.json`，类别表 = `classes.txt`（客户端篇 §5.2） |
| D3 | 实时状态用轮询 | 所有客户端凭任务 ID 查询；接口幂等、无会话状态（§3、客户端篇 §5.5） |
| D4 | 多卡；训练与推理不区分卡 | 必须显存账本 + 每卡预留（`gpu_reserve_mb`），防止训练打爆推理（§4.2） |
| D5 | 显存静态估算表可接受 | 表可被配置文件覆盖，并随 `capabilities` 下发（§3、§4.2） |
| D6 | 任务可恢复 | 队列持久化（`queue.json`）+ 训练进程脱离服务端进程（`start_new_session=True`）（§4.3） |
| D7 | 不做「发布为服务端推理模型」 | 结果只落 `artifacts/<job_id>/`，客户端自行下载（§4.3） |
| D8 | 客户端本地记录所有训练任务 ID | 客户端职责（客户端篇 §5.3）；服务端保证按 `job_id` 查询稳定 |
| D9 | **已被 N4 推翻，以 N4 为准** | 允许对「非正常结束」的任务手动恢复 |
| D10 | **仅 Linux 部署与测试** | 服务端的部署、测试、进程与终止分支只实现 Linux 路径（`Popen(..., start_new_session=True)` / `os.killpg` / `fcntl.flock`）；不提供 Windows 支持（§2.6、§4.4） |

**数据与协议决策（C）**

| 编号 | 决策 | 落地要点 |
| --- | --- | --- |
| C1 | 数据转换放客户端；上传走 zip；首个版本不做断点续传 | 服务端只接受 YOLO 文本标签 `labels/<split>/<stem>.txt`（`stem` = 图片文件名去掉原扩展名，§4.1）；`data.yaml` 由服务端生成，客户端不得上传 |
| C2 | 首个版本仅 Detect + Segment | 任务字段保留可扩展；配置里 `tasks` 不含这两项时拒绝启动（§4.4） |
| C3 | 训练进程 detached，服务重启后自动接管；systemd `KillMode=process` | 见 §4.3；部署单元见 `deploy/` |
| C4 | 取消 / 失败保留部分结果，客户端可见，展示为「已中止」类任务 | 落 `artifacts/<job_id>/partial/`，`files` 接口以 `partial: true` 标注（§3） |
| C5 | 图片按 sha256 内容寻址缓存 + 两阶段增量上传；**标注文件每次必须上传，绝不允许缓存** | 见 §4.1 的两阶段上传与其边界规则 |
| C6 | 严格 FCFS；保留提交时「可行性预检」 | 预检见 §4.2，防止超大任务永久堵队头 |
| C7 | **train/val 划分 = 按类别独立分层 + 可配随机种子**：客户端对每个类别收集图片集 `I_c`、用**派生种子**逐类确定性洗牌、按 `val_ratio` 分配**每类名义 val 配额**，并按「**先扣继承再补新图**」（本类配额先减去已在 val 且属于 `I_c` 的图片数）保证 \|I_c\| ≥ 2 时 train / val 两侧各 ≥ 1；多标签图片只归一侧（按类别稀有度升序消解，另有收尾安全网）。**「\|I_c\| ≥ 2 ⇒ 两侧各 ≥ 1」带例外**：收尾安全网只把 val 降回 train、且候选必须「降级后不会把其它类别 val 归零」，因此仍有无法修复的残留（\|I_c\| ≥ 2 且 `train == 0`）——命中的类别进「两侧代表无法保证」清单并在划分预览里**红色高亮**，**不阻断上传**。**划分在客户端完成，服务端只落 schema / 校验 / 记录**（`split_strategy` / `split_stats`），不重算、不纠正 | manifest 字段与校验见 §4.1；算法规范见客户端篇 §5.2 |
| C8 | **标定数据集改为运行时合成生成**：`auto_calibration.dataset: ""`（默认）在**系统临时目录**合成一次性 YOLO 数据集（`xal-bench-` 前缀，标定结束即清理），**不再随包提供内置数据集**；只有显式给出路径时才用运维指定的真实数据集（该路径不可读才走「重试后拒绝启动」分支，「内置数据集缺失」这一启动失败分支消失） | 语义与生命周期见 §2.5；合成参数见 §4.4；标定算法与指纹联动见 §4.2 |

**校验与恢复决策（N）**

| 编号 | 决策 | 落地要点 |
| --- | --- | --- |
| N1 | 无标注图片校验：无同名 `.json` → 阻断上传并列缺失文件；`.json` 有效但 `shapes` 为空 → 作背景图（空 `txt`）；`shapes` 部分可转换 → 保留可用 + 警告计数；`.json` 损坏 → 阻断 | 完整矩阵与服务端复核见 §4.1 |
| N2 | 数据集保留期可配置，默认 30 天；blob 缓存保留期默认同为 30 天（不能短于数据集，否则增量上传失效） | `dataset_ttl_days` / `blob_unused_ttl_days`；启动自检见 §4.4 |
| N3 | systemd 用 `KillMode=process` | 每任务独立 cgroup 的 `systemd-run` 留待后续版本（§7） |
| N4 | 客户端可对「非正常结束」的任务手动恢复；自动重试耗尽后必须提示用户；**手动恢复后重试预算重置**；可恢复状态 = `failed` / `interrupted` / `cancelled`；`completed` 不可恢复 | 恢复矩阵见 §4.3，接口见 §3 |

**版本策略决策（P）**

| 编号 | 决策 | 落地要点 |
| --- | --- | --- |
| P1 | 不建独立训练 venv：同一环境 + fork 自有 `requirements/custom/training.txt` **按部署方选定的版本记录**（不设硬下限、不做版本治理）；按模型家族下发 optimizer preset + 服务端强制（目的是参数显式化与跨版本可复现）；`python_executable` 为逃生舱（默认空 = 服务端解释器），将来真要双环境只改配置不改代码 | 见 §4.4 |
| P2 | 恢复任务排队尾，严格 FCFS | `resume_to_queue_head: false`（默认）；恢复接口返回 `queue_position`（§3、§4.3） |
| P3 | 允许恢复用户自己取消的任务 | `cancelled` 属可恢复集合（N4） |


---

## §2 架构、目录与启动

### §2.1 零改动上游原则与禁改清单

上游任何文件被改，未来 `git merge upstream/main` 都可能冲突。上游当前没有 `/custom/train/*` 相关代码，因此全部新增能力必须落在**上游不认识的新目录**里（服务端零改动上游）。

**禁改文件表（7 项，一行不动）**

| 文件 / 目录 | 说明 |
| --- | --- |
| `app/main.py` | 上游入口文件，一行不动 |
| `app/api/*`、`app/core/*`、`app/schemas/*`、`app/models/*`、`app/tasks/*`、`app/utils/*` | 上游代码，一行不动 |
| `pyproject.toml`、`requirements.txt` | 版本下限不写进上游文件，写 `requirements/custom/training.txt` |
| `configs/server.yaml`、`configs/models.yaml`、`configs/auto_labeling/*` | 上游配置；训练配置写 `configs/custom/training.yaml` |
| `docs/site.yml`、`docs/openapi.json`、`docs/source/*`、`docs/README.md` | 文档站点与 schema 快照 |
| `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md` | 上游工程文件 |
| `tests/test_yolo_class_filter.py`、`scripts/export_openapi.py`、`.github/workflows/*` | 上游测试与 CI 脚本 |

**上游 CI 影响**：上游 `.github/workflows/check-openapi.yml` 按路径触发（`app/api/**`、`app/schemas/**`、`app/main.py`、`pyproject.toml`、`docs/openapi.json`、`scripts/export_openapi.py`）并执行 `python scripts/export_openapi.py --check`。新增 `app/custom/**` 既不在触发路径内，也不改变 `app.main:app` 的 schema，**不会让上游 CI 失败**；训练接口的契约快照另出 `scripts/custom/export_openapi.py`（固定导入 `app.custom.server:app`，产物 `docs/custom/openapi.custom-train.json`），用于客户端联调。

**fork 安全的验收口径**：`git diff upstream/main --stat` 只应显示**新增目录 / 新文件**（`app/custom/**`、`configs/custom/**`、`docs/custom/**`、`deploy/**`、`tests/custom/**`），**无任何上游文件被修改**。

### §2.2 组合 app 装配

组合 app 把训练路由与训练启动流程挂到上游 app 上，**不改 `app/main.py` 的网页入口**：

```python
# app/custom/server.py
# 远程训练组合 app：uvicorn app.custom.server:app
# 启动顺序（唯一顺序，§2.6；每一步都必须在下一步之前完成）：
#   ① 配置自检（含鉴权 fail-closed，§2.4）
#   ② 扫描并接管存活训练进程（**①–④ 中唯一写「既有 job 状态」的步骤**：读 jobs/*/state.json + 探活 + 终态消费 + intent 对账；
#      会写 state.json / queue.json / archive/ 与 resume_intent.json，但不加载模型、不碰 GPU，见 §4.3）
#   ③ 判定标定需求（configs/custom/vram_table.auto.yaml 存在性与环境指纹，§2.5）
#   ④ 需要标定且存在存活训练任务 → 按 calibration_conflict_policy 处理（默认 defer，§2.5）
#   ⑤ 必要时标定（默认路径：合成一次性数据集 → 一轮完整标定 → 写 auto 产物）
#   ⑥ 进入上游 lifespan（加载推理模型 / 建推理执行器 / 检查更新）
#   ⑦ 启动调度器与 TTL 清理器（TrainingService.start()）
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.custom.api import router as training_router
from app.custom.bootstrap import (
    check_config_fail_closed,      # ① 配置自检（含鉴权 fail-closed）
    take_over_live_jobs,           # ② 扫描 + 探活 + 终态消费（①–④ 中唯一写「既有 job 状态」的步骤：写 job 目录与 queue.json；不加载模型、不碰 GPU）
    calibration_needed,            # ③ auto 产物存在性 + 指纹判定
    resolve_calibration_conflict,  # ④ 存活训练任务与标定的冲突策略（defer / wait / terminate）
    run_startup_calibration,       # ⑤ 必要时跑一轮完整标定并写产物
)
from app.custom.service import TrainingService, set_service
from app.main import app  # 导入上游 app（其 lifespan 里的模型加载原样保留）

# 1) 训练路由：前缀 /custom/train（不带版本段），沿用 Token 鉴权（上游是 app 级中间件）
app.include_router(training_router, prefix="/custom/train", tags=["Training"])

# 2) 组合 lifespan：严格按 ①→⑦ 的顺序执行；退出时先停训练服务
_upstream_lifespan = app.router.lifespan_context


@asynccontextmanager
async def combined_lifespan(app: FastAPI):
    # ①②③④⑤ 全部发生在进入上游 lifespan 之前（标定必须在推理模型加载之前，§2.5）
    settings = check_config_fail_closed()        # ① 失败即非零退出（含缺 key / 非回环地址的 allow_no_auth）
    takeover = take_over_live_jobs(settings)     # ② 先消费终态与产物，再探活；返回存活训练任务集合
    if calibration_needed(settings):             # ③ auto 产物缺失或指纹失配
        action = resolve_calibration_conflict(   # ④ 默认 defer（延期标定）
            settings, live_jobs=takeover.live_jobs
        )
        if action == "calibrate_now":
            run_startup_calibration(settings)    # ⑤ 在干净 GPU 上标定
        # action == "defer"：本次以手工基线 / 内置默认启动；运行期只复查条件并置 deferred_ready=true
        #   + 提示重启，真正的补标定在**下次启动**的干净 GPU 阶段执行（§2.5）
        # action == "wait" / "terminate"：等待 / 受控终止存活训练任务后**继续本轮标定**（步骤 ⑤）

    async with _upstream_lifespan(app):          # ⑥ 上游：模型注册表 / 推理执行器 / 更新检查
        service = TrainingService.load_from_config()
        set_service(service)
        service.start()                          # ⑦ 调度线程 + TTL 清理器 + 队列恢复 + （defer 时）deferred 条件复查
        try:
            yield
        finally:
            service.stop()                       # 先停训练服务：停止派发、落盘队列、重挂 tail
        # 退出上游 lifespan：推理执行器 shutdown / 模型卸载


app.router.lifespan_context = combined_lifespan
```

| 要点 | 约定 |
| --- | --- |
| 路由前缀 | `include_router(training_router, prefix="/custom/train", tags=["Training"])`：路由模块内部只写相对路径（`/capabilities`、`/jobs`、`/health` …），前缀由组合 app 统一给出；**前缀不含版本段**，上游既有接口（`/health`、`/v1/models`、`/v1/predict`、`/v1/video/...`）路径一概不动（§3） |
| 包装时机 | 必须先 `import app.main` 再包装 `app.router.lifespan_context`：该属性此刻已是上游 `lifespan`（加载模型 / 建推理执行器 / 检查更新），包装后语义不变 |
| 退出顺序 | `service.stop()` 先执行（停止派发、落盘队列、重挂 tail），随后才跑上游收尾（推理执行器 shutdown / 模型卸载） |
| ①–⑤ 顺序 | **不可交换、不可省略**：①③ 只读磁盘 / 配置；**①–④ 中只有 ② 写「既有 job 状态」**（`state.json` / `queue.json` / `archive` / `resume_intent.json`）；① 的首启初始化只幂等创建**空骨架**且先于 ②；⑤ 只写标定产物与系统临时目录；①②③ 都不需要 GPU、也不需要上游；⑤ 必须在 `async with _upstream_lifespan(app)` **之前**；② 必须在 ⑤ **之前**（否则重启时存活训练进程仍占显存，标定会与它抢显存并把错误的 `max_batch` 固化成长期生效的 auto 产物） |
| 打包 | `app/custom/__init__.py` 必须存在；上游 `pyproject.toml` 的 `include = ["app*"]` 已覆盖 `app.custom`，无需改打包配置 |
| 启动命令 | `uvicorn app.custom.server:app --host 0.0.0.0 --port 8000`（部署脚本 / systemd 只改 `ExecStart`）；systemd 只负责进程存活与重启，**不负责 readiness**（§2.6） |


### §2.3 新增目录与实现文件清单

服务端全部为**新增**文件，不含任何上游文件：

```text
X-AnyLabeling-Server/
  app/custom/
    __init__.py
    server.py            # 组合 app（§2.2）
    api.py               # 训练路由模块（模块内只写相对路径，前缀由组合 app 统一给出）
    bootstrap.py         # ①–⑤ 启动步骤（§2.6）
    service.py           # TrainingService 与 set_service
    config.py            # 训练配置加载 + 启动自检键校验
    storage/             # blob 内容寻址缓存、数据集物化、TTL 清理器、软删除
    scheduler/           # 队列（FCFS）、显存账本、可行性预检、派发
    executor/            # 训练进程（detached）、事件协议、重启接管、终态 finalizer
  configs/custom/
    training.yaml        # 手工维护的训练配置（键的权威定义见 §4.4）
    vram_table.auto.yaml # 启动期自动基线标定产物（§2.5）
    server.custom.yaml   # fork 自有的完整服务配置（含鉴权 key，运维维护；§2.4）
  requirements/custom/
    training.txt         # 训练依赖记录（按部署方选定的版本记录）
  scripts/custom/
    export_openapi.py    # 训练接口契约快照
  tests/custom/          # 训练相关测试（随上游 testpaths 自动收集）
  deploy/                # systemd unit、安装脚本、反向代理示例
```

| 路径 | 用途 | 关键约束 |
| --- | --- | --- |
| `app/custom/server.py` | 组合 app（§2.2） | 唯一启动入口；不修改 `app/main.py` |
| `app/custom/api.py` | 训练路由模块 | 模块内只写相对路径；`/custom/train` 前缀由组合 app 给出；**不新增任何鉴权代码**（§2.4） |
| `app/custom/bootstrap.py` | ①–⑤ 启动步骤的实现 | ② 的 intent 对账 / 终态收账 / 归档 / 重排落在本文件，是**①–④ 中唯一写「既有 job 状态」的步骤**（① 只做首启空骨架的幂等初始化，⑤ 只写标定产物，§2.6、§4.3） |
| `app/custom/service.py` | `TrainingService` 与 `set_service` | ⑦ 的调度线程与 TTL 清理器由其 `start()` / `stop()` 管理 |
| `app/custom/config.py` | 训练配置加载与启动自检 | 配置键的权威定义见 §4.4；自检结论与启动决策见 §2.4、§2.6 |
| `app/custom/storage/` | blob 内容寻址缓存、数据集物化、TTL 清理器 | 物化优先 `hardlink`、跨卷回退 `copy2`；一切删除先软删除进 `.trash/`（§4.1） |
| `app/custom/scheduler/` | 队列（严格 FCFS）、显存账本、可行性预检、派发 | 进程内全局互斥（单 worker）；见 §4.2 |
| `app/custom/executor/` | 训练进程（detached）、事件协议、服务重启接管、终态 finalizer | **Linux 唯一实现**：`Popen(..., start_new_session=True)` / `os.killpg` / `fcntl.flock`；见 §4.3 |
| `configs/custom/training.yaml` | 手工维护的训练配置 | 服务端新增文件，不属于上游（§4.4） |
| `configs/custom/vram_table.auto.yaml` | 启动期自动标定产物 | **标定产物的唯一落点**；缺失或指纹失配即自动重标（§2.5）；`configs/custom/` 必须对服务进程可写 |
| `configs/custom/server.custom.yaml` | fork 自有的完整服务配置 | 复制上游配置后开启鉴权；含密钥，权限建议 `0600`、不入版本库；由 `XANYLABELING_SERVER_CONFIG` 指向（§2.4） |
| `requirements/custom/training.txt` | 训练依赖记录 | 按部署方选定的版本记录，不表达版本策略（P1 / §4.4） |
| `scripts/custom/export_openapi.py` | 训练接口契约快照 | 固定导入 `app.custom.server:app`；产物 `docs/custom/openapi.custom-train.json` |
| `tests/custom/` | 训练相关测试 | 随上游 `testpaths` 自动被收集 |
| `deploy/` | systemd unit、安装脚本、反向代理示例 | `KillMode=process`（§4.4） |

两条落点约定：

- `app/custom/` **不随包携带任何标定数据集资源**：标定数据集默认在运行期合成生成（§2.5）。
- 客户端侧的新增目录与文件见客户端篇 §5.3.6；两侧都遵守 §1.4 的 fork 约定。
- `scripts/custom/` 下**只有** `export_openapi.py` 一个脚本；标定产物的**唯一落点**是 `configs/custom/vram_table.auto.yaml`；运行期数据目录 `training.work_dir` 的布局与约束见 §4.4。


### §2.4 鉴权：零新增代码 + 启动期 fail-closed

**训练侧不新增任何鉴权代码**：上游 `APIKeyMiddleware` 是 **app 级中间件**，与路径无关地覆盖所有路由，因此 `/custom/train/*` 自动受保护；训练路由只做业务判断（例如「数据集被引用时禁止删除」）。

| 行为 | 依据 |
| --- | --- |
| 请求头 `Token: <api_key>` | 名字取自上游 `settings.security.api_key_header`（默认 `Token`），值为 API key；训练路由与上游接口共用同一套鉴权 |
| 仅上游 `/health` 免鉴权 | 上游中间件对路径做**精确相等**匹配，只放行上游 `/health`；因此 `/custom/train/health` **不在**免鉴权之列（其四态鉴权表见 §3） |
| 失败响应体 | `{"success": false, "error": {"code": "UNAUTHORIZED", "message": "Invalid or missing API Key"}}` |
| `api_key_enabled=false` 时直接放行 | 该状态下**整个服务没有鉴权**（含 `/custom/train/health`：无 `Token` 也返回 200）。这是上游既有语义，本规格不引入新的运行期强制机制；训练侧的兜底是**启动自检**（下节），逃生开关 `allow_no_auth` **只放宽启动自检、不改运行期鉴权** |

**为什么需要 fail-closed（一句话）**：上游默认部署没有鉴权（`security.api_key_enabled` 默认 `false`、`host` 默认 `0.0.0.0`），而训练端点含上传数据、提交训练、取消 / 恢复、删除数据集、下载全部产物——按默认值部署后，任何能访问端口的人都能占满磁盘与 GPU、查看或下载全部任务。**裁决**：不改上游任何文件（尤其不改 `configs/server.yaml` 的默认值），改为在 `app/custom/` 的启动自检里 fail-closed。

| 项 | 规则 |
| --- | --- |
| 检查时机 | 组合 lifespan 的**配置自检步骤**，即启动顺序的**第 ① 步**（先于接管存活进程、标定与上游 lifespan；§2.6） |
| 触发条件 | `training.enabled: true`（默认） |
| 通过条件（二者都要） | ① `settings.security.api_key_enabled == true`；② key 非空：`settings.security.api_key` 为非空字符串（上游已在 `api_key == ""` 时用环境变量 `XANYLABELING_API_KEY` 兜底填充） |
| 不通过 | **拒绝启动**：日志写明**具体缺哪一项**与**如何设置**（提示文本见下），进程以**非零退出码**结束；**HTTP 端口始终未监听** |
| 逃生开关 | `training.allow_no_auth`（新增配置键，默认 `false`）：置 `true` 时**不**要求 key，但**仍**要求 `server.host ∈ {127.0.0.1, localhost}`——不是回环地址则**同样拒绝启动**；只为本机开发存在，**生产不得使用** |
| 作用面（写死） | 该自检**只决定「能否启动」**，不进入运行期鉴权路径：置 `allow_no_auth: true` 不会让任何接口变成免鉴权、也不会让它变成强制鉴权；运行期鉴权**只**由上游 `security.api_key_enabled` 决定（四态见 §3） |

拒绝启动时输出的提示（逐字实现，第一行为日志前缀）：

```text
[FATAL] training.enabled=true 但鉴权未开启（fail-closed）：需要 security.api_key_enabled=true 且 key 非空。
        远程训练端点含上传 / 训练 / 取消 / 恢复 / 删除数据集 / 下载全部产物，无鉴权时不得对外提供服务。
        判定口径（二者都要满足，缺一即拒绝启动）：
          ① security.api_key_enabled 必须为 true；
          ② security.api_key 必须为非空字符串（key 为空时才会用环境变量 XANYLABELING_API_KEY 兜底填充）。
          —— 只设置环境变量 XANYLABELING_API_KEY 而 api_key_enabled 仍为 false，**不足以**开启鉴权，服务仍会拒绝启动。
        请二选一：
          A) 开启鉴权（推荐，生产必须；不改上游 configs/server.yaml）：
             复制上游 configs/server.yaml 为 fork 自有的 configs/custom/server.custom.yaml，至少改：
               security.api_key_enabled: true
               security.api_key: "<高强度随机串>"
             然后在 systemd unit / 启动环境里指向它：
               Environment="XANYLABELING_SERVER_CONFIG=/opt/X-AnyLabeling-Server/configs/custom/server.custom.yaml"
             （可叠加 Environment="XANYLABELING_API_KEY=..."，但它只能填 key、不能开启鉴权；
               环境变量 XANYLABELING_SERVER_CONFIG 由 app/core/config.py:110-117 读取，用于指定配置文件路径）
          B) 仅本机开发：training.allow_no_auth: true 且 server.host 为 127.0.0.1 / localhost
        当前：security.api_key_enabled=false, security.api_key 为空/未设置; server.host=0.0.0.0
              配置文件：<app/core/config.py:110-117 解析出的实际路径>
```

**权威做法：fork 自有的完整配置文件**（而不是只设环境变量）：

| 项 | 约定 |
| --- | --- |
| 新增文件 | `configs/custom/server.custom.yaml`（**fork 新增文件**，不是对上游文件的修改，见 §2.3） |
| 生成方式 | 复制上游 `configs/server.yaml` 后**至少**改两处：`security.api_key_enabled: true` 与 `security.api_key: "<高强度随机串>"`；其余键保持与上游一致即可（`server.host` / `port` / `logging` / `performance` / `concurrency` 由部署方按需调整） |
| 归谁维护 | 运维；该文件含密钥，权限建议 `0600`、**不得**提交进版本库；与 `configs/custom/training.yaml` 互不覆盖 |
| 生效方式 | 环境变量 `XANYLABELING_SERVER_CONFIG=<绝对路径>`：优先于默认的 `configs/server.yaml` |
| 与上游的关系 | 上游 `configs/server.yaml` **一字不改**，因此 `git merge upstream/main` 仍无冲突 |
| 环境变量的角色 | `XANYLABELING_API_KEY` 仍可用（仅在 `api_key == ""` 时兜底填 key），但**单靠自己不足以开启鉴权**：`api_key_enabled` 不会因它变成 `true` |

`/custom/train/health` 的**四态鉴权表**（含 `enabled=false` 与 `api_key_enabled` 的组合）在 §3 定义，此处不重述。


### §2.5 启动期自动基线标定

**没有任何新增启动参数、也没有标定专用命令行入口**：显存基线标定由组合 app 在**每次启动时自动检查**，缺失或过期就自动跑一轮，跑完再继续启动。

#### §2.5.1 唯一路径：先标定，再加载推理模型

标定测量的是「一次训练进程自身需要多少显存」，因此**必须在干净 GPU 上做**。

| 规则 | 内容 |
| --- | --- |
| 顺序固定 | `⑤ 标定 → ⑥ 进入上游 lifespan（加载推理模型）`；标定调用必须在 `async with _upstream_lifespan(app)` **之前**（§2.2） |
| 反例（为什么不能颠倒） | 若标定放在上游 lifespan **之后**（推理模型已加载并常驻），标定点位的可用显存会凭空少掉推理占用的那一块：**大模型（如 yolo11x / yolo26x）的标定点会假性 OOM**，测出的 `max_batch` 偏小、`reserved` 曲线偏高，这些组合还会被误判为「本设备不可调度」（提交 422 `VRAM_ESTIMATE_UNAVAILABLE`）——一个纯粹由**测量顺序**造成的假结论 |
| 标定值与运行期账本分离 | 标定记录里**同时保存 `device_total_mb`**（该卡总显存）；运行期**不**拿标定时的空闲显存当可用显存，而是由显存账本另行扣除推理常驻占用（体现在 `free_mb` 实测值里）与 `gpu_reserve_mb`（§4.2）——两者互不污染 |
| 「干净 GPU」的第二层含义 | 不只要求没有推理模型，还要求**没有存活的训练进程**：「服务尚未派发任务」不等于「没有训练进程在跑」——训练进程是 detached 的（`start_new_session=True`），systemd 用 `KillMode=process`（只杀主进程），因此**服务重启不会杀掉训练子进程**；所以标定之前必须先做启动顺序第 ② 步（扫描并接管存活训练进程，§2.6、§4.3） |
| 跳过第 ② 步的后果 | 在存活训练进程仍占显存时标定，后果与上一条完全同类：**错误的 `max_batch` 与「不可调度」结论被写进 `configs/custom/vram_table.auto.yaml`**，并在指纹未变期间长期生效（下次启动只读产物、不重测） |

#### §2.5.2 触发条件、范围与产物

| 项 | 约定 |
| --- | --- |
| 触发条件 | `configs/custom/vram_table.auto.yaml` **缺失**、**内容不完整**，或**指纹与当前环境不一致**（判据见 §4.2） |
| 指纹未变且文件存在 | 只读文件、直接启动（**秒级**，不重新测量） |
| 指纹字段 | GPU 型号 / 数量 / 总显存 / 驱动 / CUDA / torch / ultralytics（形状见下） / 标定参数 / 标定数据集标识——任一变化即失配 ⇒ 自动重标 |
| `ultralytics` 指纹形状 | 有家族声明后端时是**映射** `{"__host__": <服务端版本>, "<family>": <该家族后端版本>}`（每个 `backends.<family>.python_executable` 非空的家族一项；某家族探测失败时该项为 `null`）；**没有任何家族声明后端**时仍是**单一字符串**（服务端版本），与旧产物兼容 |
| 指纹不符时的旧文件 | **整体忽略、不部分采纳**，随后覆盖写 |
| 标定范围 | 一轮完整标定覆盖「**通过可用性过滤后**」的白名单 `(model, task)` 组合（含 `detect` 与 `segment`），逐组合输出进度日志（`[i/N] model/task batch=B reserved=…MB`）；`N` = **过滤后剩余**的组合数 |
| 档位与测量 | 每点用按模型规模自适应的 batch 档位短跑；OOM 向下取半重试（下限 4）；算法与测量口径见 §4.2 |
| 内容完整性判定（唯一口径） | 「完整」= **可用性过滤后剩下的** `(model, task)` 组合**全部有标定记录**（成功记录与「全点位 OOM」的失败记录都算）；被过滤跳过的组合**不进产物、也不触发重标**；全点位 OOM 的组合**视为已标定**，其「不可调度」结论同样入产物 |
| 产物 | 写 `configs/custom/vram_table.auto.yaml`（启动期生成，可随时删除重做）；**无可用设备时整轮跳过、不写产物** |
| 运行时加载优先级 | `configs/custom/vram_table.auto.yaml`（本机实测）→ `training.yaml` 的 `vram_table`（手工基线）→ 内置默认表；`capabilities` 如实标注每个组合的来源 `auto` / `manual` / `default`（§3、§4.2） |
| 手动重新标定 | **删除 `configs/custom/vram_table.auto.yaml` 即可**（下次启动自动重标）；指纹变化时同样自动重标。不提供 `--force` / 环境变量 / 独立脚本等其它入口 |
| 与上游文件的关系 | 全程**零改动上游**（§2.1）：标定代码、配置项、产物全部落在 `app/custom/**` 与 `configs/custom/**` |

#### §2.5.3 与存活训练进程的冲突策略

需要标定**且**存在存活训练任务时，按 `training.calibration_conflict_policy` 处理：

| 策略 | 取值 | 行为 | 默认 |
| --- | --- | --- | --- |
| **延期标定** | `defer` | **本次运行的启动流程不标定**：以 `configs/custom/training.yaml` 的手工基线 / 内置默认值启动（与 `require_vram_calibration: false` 的加载路径相同，但**保留**补标定义务）；`capabilities.calibration.deferred=true`、`deferred_ready=false`、`vram_table.auto_loaded=false`、`entries[].source` 全为 `manual` / `default`，`capabilities.warnings` 记 `VRAM_CALIBRATION_DEFERRED`（**WARN**、信息性、不进 `needs_attention`）。**运行期只复查条件**：调度线程每 `deferred_calibration_retry_min`（默认 5）分钟复查一次「队列为空 **且** 无 `preparing` / `running` 任务 **且** 无存活训练进程」，满足即把 `deferred_ready=true` 置位并提示「可以重启服务完成补标定」；**运行期既不标定、也不清零 `deferred`** | ✅ |
| 等待存活任务结束 | `wait` | 阻塞在启动流程里，等存活训练任务**全部结束**后**继续本轮标定**（接着走步骤 ⑤，不是「本轮先不标定」），标定完成后再继续 ⑥⑦（期间 HTTP 端口仍未监听，客户端只会看到连接失败）。适用于「宁可晚启动也要用实测基线」的机器 | ❌ |
| 受控终止存活任务 | `terminate` | 按 §4.3 的取消语义受控终止存活训练任务（Linux：`os.killpg(SIGTERM)` → `cancel_grace_seconds` → `SIGKILL`），落 `partial/` 并把状态置 `cancelled`，然后**继续本轮标定**（步骤 ⑤）。会中断用户任务，**必须显式开启** | ❌ |

补充规则：

- **补标定的唯一路径**：`deferred=true` 时推理模型**已经常驻**显存，此时直接跑标定必然回到假性 OOM 结论，因此唯一正确做法是「运行期只复查条件 + 提示重启，真正的补标定在下次启动完成」：
  1. **运行期（服务已就绪）**：调度线程每 `deferred_calibration_retry_min` 分钟复查一次「队列为空 **且** 无 `preparing` / `running` 任务 **且** 无存活训练进程」；满足即置 `capabilities.calibration.deferred_ready=true`（并在日志与 `capabilities.warnings` 提示「可以重启服务完成补标定」）。**运行期不标定、不写 `vram_table.auto.yaml`、不把 `deferred` 置回 `false`**。
  2. **下次启动（干净 GPU）**：①②③④⑤ 正常执行；⑤ 标定成功并**覆盖** `configs/custom/vram_table.auto.yaml` 之后，才把 `deferred` 置回 `false`、`deferred_ready` 置回 `false`、`entries[].source` 变为 `auto`。**清零只发生在下次启动完成标定之后**。
  3. **客户端可见语义**：`deferred=true` ⇒ 「本次未做本机标定，正在用兜底层数值（估算偏保守）」；`deferred_ready=true` ⇒ 「服务端当前已空闲，重启服务即可完成补标定」——两条都是信息条、不阻断提交（客户端篇 §5.1.3）。
- **`defer` 与逃生舱的区别**：`require_vram_calibration: false` 是运维**显式放弃**标定（`VRAM_CALIBRATION_SKIPPED`，长期以手工基线运行）；`defer` 是**自动延期**（`VRAM_CALIBRATION_DEFERRED`，条件满足后**必须**补标定）。两者在 `capabilities.warnings` / `calibration` 里是**两个不同状态**，不得互相冒充。
- **`defer` 不影响服务可用性**：它是「先让服务起来、稍后补实测基线」的默认路径；`vram_table.entries[].source` 会如实标注 `manual` / `default`，客户端据此显示「未在本机标定，估算偏保守」（客户端篇 §5.1.3）。
- **`wait` / `terminate` 的适用性**：两者都默认关闭；打开它们会显著改变启动行为（前者可能长时间不监听端口，后者会中断用户任务），属运维显式决策，不改变默认语义。
- **绝不允许**在存活训练进程占用 GPU 时做标定：那会把错误的 `max_batch` / 不可调度结论固化成长期生效的 auto 产物。

#### §2.5.4 首次启动的耗时与客户端表现

| 场景 | 表现 | 要求 / 建议 |
| --- | --- | --- |
| 首次启动 / 换卡 / 换驱动 / 换 CUDA / 换 torch / 换 ultralytics / 改标定参数 | 启动时间 = 「**10–15 分钟**标定 + 上游模型加载」；标定期间无 HTTP 服务、无调度器、无队列消费，因此训练任务天然不会与标定并发；readiness 步骤（轮询 `GET /custom/train/health` 返回 200）在此之前不可能通过（§4.4） | 用 `journalctl` 观察逐组合进度日志；`configs/custom/` 与**系统临时目录**必须可写 |
| 同上，客户端视角 | **连接失败**（连接被拒绝 / 超时），**不是**某个错误码——训练子系统在此阶段没有任何 HTTP 端点可用 | 客户端必须把连接类失败与「服务端未就绪」区分：显示「服务端正在启动（首次启动可能需要 10–15 分钟做显存基线标定），将自动重试」并按退避策略重试，**不**提示「Token 无效」「地址不正确」这类结论性错误（客户端篇 §5.5） |
| 已跑过标定且指纹未变的机器 | 启动是**秒级**的：只读 `vram_table.auto.yaml`、不重新测量 | 不要把「启动慢」当成常态；若某台机器每次启动都很慢，检查 auto 文件是否被写在了不持久的位置（容器 / `tmpfs`）或被清理，或指纹是否每次都在变（§4.2） |
| 一轮完整标定的开销上界 | 短跑次数的上界 = 10 模型 × 2 任务 × 3 个 batch 点 = **60 次**；**实际 = 可用性过滤后剩余的 N 个组合 × 每组合 3 个 batch 点**（N ≤ 20） | 可用性过滤（§2.5.6）直接缩短首启时间：家族不可用 / 权重缺失的组合不参与标定 |


#### §2.5.5 标定数据集：运行时合成生成

| `auto_calibration.dataset` | 行为 |
| --- | --- |
| `""`（默认） | **运行时合成生成**：在**系统临时目录**下创建一次性数据集目录（`tempfile.mkdtemp(prefix="xal-bench-")`），写入 `images/{train,val}/` + `labels/{train,val}/` + `data.yaml`（合成参数见 §4.4）；标定结束后（成功或失败都）清理该目录；**不写 `work_dir`**、不占数据集配额、不参与 TTL 清理器（它是标定的临时输入，不是「数据集」实体） |
| `"<路径>"` | 使用运维**显式指定**的真实 YOLO 数据集：要求 `images/{train,val}` 与 `labels/{train,val}` 非空，否则按「标定数据集不可读」走**重试后拒绝启动**分支（§2.5.7 分级 ③） |

| 规则 | 说明 |
| --- | --- |
| 启动失败分支的变化 | 「内置数据集缺失 / 损坏」这一分支**消失**：默认路径不再依赖任何随包资源，启动自检中的该条**仅当显式指定真实数据集路径时适用** |
| 清理失败不清退 | 临时目录清理失败（文件被占用 / 权限不足等）**不影响启动与标定结论**：记 **WARN** 日志并**保留路径**供运维手工清理；该告警**不进** `capabilities.warnings`（它是本机的临时文件清理事件，不代表服务能力状态） |
| 生命周期 | 合成 → 标定（约 10–15 分钟）→ 清理都发生在组合 lifespan 内、上游 lifespan 之前；标定期间 HTTP 端口未监听，客户端只会看到连接失败（§2.5.4） |
| 确定性 | 同一份 `auto_calibration.synthetic`（含 `seed`）⇒ 每次标定生成**逐文件一致**的数据集（PNG + 固定种子 + 固定实例数区间），因此测量可复现、指纹可判定 |
| 指纹联动 | 合成参数摘要进环境指纹的 `dataset` 字段：任一生成参数变化 ⇒ 指纹失配 ⇒ 下次启动**自动重标** |

#### §2.5.6 标定矩阵的可用性过滤（跳过，而不是失败）

启动自检声明「缺权重、低版本 ultralytics、无 CUDA 都**可以成功启动**」，而标定矩阵默认覆盖 `model_families` 白名单的**全部** `(model, task)` 组合。若把「该组合根本不该在这个环境里标定」也当成失败，就会落到「有限次重试后拒绝启动」，与上面那条声明自相矛盾。因此**「不可用」与「失败」显式分开**：不可用 ⇒ **跳过**（不重试、不拒绝启动、不下发为可调度）；失败 ⇒ 仍按 §2.5.7 的分级 ②/③ 处理。

| 过滤条件（按此顺序判定） | 影响范围 | 行为 | `reason` |
| --- | --- | --- | --- |
| ① **家族不可用**：环境 `ultralytics` 版本 < 该家族 `model_families[<family>].min_ultralytics`（事实性能力检查，不是版本门禁） | 该家族的**全部** `(model, task)` 组合 | **跳过**这些组合的标定；`model_families[<family>].available=false`、`unavailable_reason=MODEL_FAMILY_UNSUPPORTED`；组合进 `unschedulable[]`；提交时 422 `MODEL_FAMILY_UNSUPPORTED` | `MODEL_FAMILY_UNSUPPORTED` |
| ② **权重缺失**：`allow_weight_download=false` **且**该模型的权重文件不在 `<work_dir>/weights/` 内 | 该模型对应的组合 | **跳过**该组合的标定；组合进 `unschedulable[]`；提交时 422 `WEIGHT_NOT_AVAILABLE` | `WEIGHT_NOT_AVAILABLE` |
| ③ **无可用设备**：`torch.cuda.device_count() == 0` 或 CUDA 探测失败 | **整轮标定** | 整轮**跳过**：不合成数据集、不跑任何点位、**不产生 / 不写** `vram_table.auto.yaml`；`calibration.skipped_reason=no_device`、`calibration.required=false`（生效值）、`skipped[]` 为空；`capabilities.devices=[]`；提交任何任务 503 `NO_DEVICE_AVAILABLE` | `no_device`（记在 `calibration.skipped_reason`，不是逐组合的 `reason`） |

补充规则：

- **被跳过的组合一律写入 `capabilities.vram_table.unschedulable[]`（带 `reason`）**，并同时在 `capabilities.calibration.skipped[]` 里留下同一条记录（`{"model","task","reason"}`）——前者是**可调度性**的权威结论，后者是**本次标定过程**的如实记录，两者不得互相矛盾。
- **`unschedulable[].reason` 的四值口径（写死）**：`MODEL_FAMILY_UNSUPPORTED`（过滤 ①）/ `WEIGHT_NOT_AVAILABLE`（过滤 ②）/ `VRAM_CALIBRATION_FAILED`（真正跑了、但全部点位 OOM，分级 ②）/ `VRAM_TABLE_INCOMPLETE`（既不过滤也不失败，但任何一层表里都没有行——组合仍进 `unschedulable[]`，提交时 422 `VRAM_ESTIMATE_UNAVAILABLE`）。
- **跳过 ≠ 逃生舱**：跳过是**逐组合 / 逐环境**的事实性结论（该组合在本机跑不了）；逃生舱 `require_vram_calibration: false` 是运维**全局**放弃标定（记 `VRAM_CALIBRATION_SKIPPED`，所有组合退回手工基线 / 内置默认且**仍可提交**）。两者不得互相冒充。
- **不新增告警码**：过滤 ① 已有 `MODEL_FAMILY_UNSUPPORTED` 的能力字段、过滤 ② 已有 `WEIGHTS_MISSING`（缓存缺失提示）、过滤 ③ 走 `calibration.skipped_reason`；`capabilities.warnings` 的 code 集合**不因本节扩大**。
- **过滤后剩下的组合为空时**：整轮跳过、不写产物（与过滤 ③ 的处理一致，只是 `skipped_reason` 不是 `no_device`）。

#### §2.5.7 失败分级与逃生舱

| 级别 | 情形 | 行为 |
| --- | --- | --- |
| ① | **单点 OOM**（属预期） | 向下取半重试（下限 `auto_calibration.batch_min`，默认 4），**不计入失败** |
| ② | 某组合的**全部点位都 OOM**（有效测量点 < `auto_calibration.min_points`，默认 2） | 该组合标记为**本设备不可调度**：进 `capabilities.vram_table.unschedulable[]`（`reason=VRAM_CALIBRATION_FAILED`），`capabilities.warnings` 记 `VRAM_CALIBRATION_FAILED`（`details.failed[]` 含该组合与原因），提交该组合 422 `VRAM_ESTIMATE_UNAVAILABLE`；**不导致启动失败** |
| ②′ | **被可用性过滤跳过的组合**（家族不可用 / 权重缺失 / 无设备） | **同样不导致启动失败**、也**不计入重试**：逐条进 `unschedulable[]`（带 `reason`）与 `calibration.skipped[]`；**无可用设备是整轮跳过**（只写 `calibration.skipped_reason=no_device`、`skipped[]` 为空） |
| ③ | **真正的异常**：标定子进程崩溃、`vram_table.auto.yaml` 写入失败、**显式指定**的标定数据集缺失 / 不可读（默认合成路径不存在该分支） | **有限次重试**（默认 `auto_calibration.startup_retries` = 3 次、间隔 `startup_retry_interval_seconds` = 30 秒）后**拒绝启动**：打日志写明失败步骤与 errno，进程以**非零退出码**结束（systemd 视为启动失败） |

| 逃生舱开关 | 取值 | 行为 |
| --- | --- | --- |
| `training.require_vram_calibration` | `true`（默认） | 正常标定与校验 |
| | `false` | **跳过标定与校验**，直接回退到 `training.yaml` 的 `vram_table` 或内置默认值，并在日志与 `capabilities.warnings` 记 `VRAM_CALIBRATION_SKIPPED`（**WARN**，不是静默降级）；此时所有组合的 `source` 都是 `manual` / `default`。**语义限定**：该开关的「要求标定」**以存在可用 CUDA 设备为前提**——无设备时标定本来就会被整体跳过（`calibration.skipped_reason=no_device`），此时**既不记 `VRAM_CALIBRATION_SKIPPED`、也不构成任何失败** |
| `auto_calibration.enabled` | `false` | 等价于 `require_vram_calibration: false`（保留一个显式开关） |

#### §2.5.8 与部署的关系

- **耗时预期**：首次启动（以及换卡 / 换驱动 / 换 CUDA / 换 torch / 换 ultralytics / 换标定参数之后）的启动时间 = 「10–15 分钟 + 上游模型加载」；运维要求见 §4.4。
- **可写要求**：`configs/custom/` 必须对服务进程可写（写 auto 产物）；**系统临时目录**必须可写（合成标定数据集，仅几十 MB 量级，容器部署要给 `/tmp` 留空间）。
- **不要用 `ExecStartPre` 或额外的入场脚本去「先跑标定」**：标定已内建在启动流程里，多一个前置步骤只会带来顺序与超时的双重不确定性。
- **systemd 的职责边界**：只负责进程存活与重启，**不负责 readiness**（`Type=simple` 下主进程 `exec` 后即视为 active，与 HTTP / lifespan 是否就绪无关）；readiness 由健康检查 / 反向代理轮询 `GET /custom/train/health` 负责（§2.6、§4.4）。


### §2.6 七步启动顺序

唯一顺序：每一步**完成之后**才进入下一步（实现见 §2.2 的 `combined_lifespan`）。

| 步 | 动作 | 读 / 写 | 失败后果 |
| --- | --- | --- | --- |
| ① | **配置自检**（含鉴权 fail-closed，§2.4）：`training.enabled` 与鉴权的组合判定；`work_dir` 存在 / 可写；`tasks` 必须含 `detect` 与 `segment`；TTL 关系（`blob_unused_ttl_days` 不得短于 `dataset_ttl_days`）；`max_committed_tokens` 必须为正整数；`resume_fallback` 取值合法。**首启初始化也在本步内**：幂等创建 `work_dir` 及全部子目录、`queue.json`（初始 `{"items": []}`）、`.trash/`，全部 `exist_ok=True`，不触碰任何上游目录；**创建先于第 ② 步**（② 的队列重建要求 `queue.json` 已存在） | 只读配置 + 首次创建目录（写） | 不通过 ⇒ **拒绝启动**（非零退出码），**HTTP 端口未监听**。`enabled: false` 时鉴权检查不适用：服务照常起来、训练路由全部 503，但 `/custom/train/health` 仍返回 200 + `enabled=false`（§3） |
| ② | **扫描并接管存活训练进程**（**①–④ 中唯一写「既有 job 状态」的步骤**）：intent 对账 → **先消费终态**（`done` 与产物）→ 存活判定与队列交叉修复；读 `jobs/*/state.json`；Linux 探活 = `os.kill(pid, 0)` 且 `/proc/<pid>/stat` 第 22 字段 `starttime` 与 `state.json.proc_start_time` 相等；会写 `state.json` / `queue.json` / `archive/` / `resume_intent.json`（**不加载模型、不碰 GPU、不需要上游**）；产出「存活训练任务集合」 | 读写 job 目录与队列 | 单个 job 异常不影响整体接管（逐 job 记 WARN 后跳过）；**不得**因为没有 GPU 而失败；必须能纯离线完成 |
| ③ | **判定标定需求**：读 `configs/custom/vram_table.auto.yaml`，判「文件存在 **且** 内容完整 **且** 指纹与当前环境一致」（§2.5.2） | 只读 | 只决定是否进入 ④ / ⑤，本身不失败 |
| ④ | **冲突策略**：若「需要标定」**且**存在存活训练任务 ⇒ 按 `training.calibration_conflict_policy` 处理（默认 `defer`） | 只读配置 | `defer` ⇒ 本次不标定、以兜底层启动并置 `deferred`；`wait` / `terminate` ⇒ 等待 / 受控终止后**继续本轮标定**（§2.5.3） |
| ⑤ | **必要时标定**：先按可用性过滤裁剪矩阵，再执行**一轮完整标定**，写 `configs/custom/vram_table.auto.yaml`（无可用设备时整轮跳过、不写产物） | 写 auto 产物 + 系统临时目录 | 逐组合**跳过 ≠ 失败**；非 OOM 类异常 ⇒ 有限次重试后**拒绝启动**（§2.5.6、§2.5.7） |
| ⑥ | **进入上游 lifespan**：加载推理模型 / 建推理执行器 / 检查更新 | 上游语义（本规格不改） | 上游加载失败即启动失败（上游原有行为） |
| ⑦ | **启动调度器与 TTL 清理器**（`TrainingService.start()`）：调度线程 + 清理器 + 队列恢复 +（`defer` 时）deferred 条件复查任务 | 读写队列 / 清理磁盘 | 此时服务已在监听端口；本步异常按启动失败处理 |

顺序上的硬约束：

- ①–⑤ **全部**发生在 `async with _upstream_lifespan(app)` **之前**；⑥ 之后才 ⑦。
- ①③ **只读**磁盘 / 配置；**①–④ 中只有 ② 写「既有 job 状态」**（`state.json` / `queue.json` / `archive/` / `resume_intent.json`）——① 的首启初始化只幂等创建**空骨架**（`work_dir` 及子目录、初始 `queue.json`、`.trash/`）且**必须先于 ②**；⑤ 只写标定产物与系统临时目录；⑦ 恢复写队列并清理磁盘；①②③ **都不需要 GPU、也不需要上游**。
- **② 必须在 ⑤ 之前**：否则存活训练进程仍占显存，标定会与它抢显存，并把错误的 `max_batch` 固化成长期生效的 auto 产物（§2.5.1）。
- **⑤ 必须在 ⑥ 之前**：标定必须在干净 GPU 上做（推理模型尚未常驻）。
- **退出顺序**：先 `service.stop()`（停止派发、落盘队列、重挂 tail），随后才跑上游收尾（推理执行器 shutdown / 模型卸载）。
- **`enabled: false` 的启动**：服务正常启动（不因训练被禁用而拒绝启动），训练业务路由统一 503 `TRAINING_DISABLED`，唯一的例外是 health（§3）。

与部署的边界：systemd 只负责进程存活与重启（`KillMode=process`、`Restart=always`），**不负责 readiness**；readiness = 轮询 `GET /custom/train/health`（带 `Token` 头）返回 200，即表示标定与上游模型加载都已结束。部署步骤、systemd unit 与首装流程见 §4.4；配置键的完整定义同样见 §4.4。

**首启初始化（幂等，发生在第 ① 步内、先于第 ② 步）**：创建 `work_dir` 及全部子目录、`queue.json`（初始 `{"items": []}`）、`.trash/`，全部 `exist_ok=True`，不触碰任何上游目录；创建失败或目录不可写 ⇒ 拒绝启动。标定产物 `configs/custom/vram_table.auto.yaml` **由服务在启动期写出**（缺失、内容不完整或指纹过期时自动跑一轮完整标定）；已存在且指纹一致时服务**只读**该文件、不重新测量；`configs/custom/` 必须对服务进程可写（否则属分级 ③ 的写文件失败，重试耗尽后拒绝启动）。

---

## §3 共享契约

**本章地位**：§3 是全文**唯一的契约定义处**——路由、封装、错误码、状态机、job 对象、事件、`capabilities`、`health`、参数面、warnings 通道、产物下载与跨侧常量都只在 §3 定义一次；服务端篇 §4 与客户端篇 §5 一律以「见 §3.x」引用，**不得复制、改写或另行补充**。两侧实现或文档出现歧义时，一律以 §3 为准。

### §3.1 通用约定

| 项 | 约定 |
| --- | --- |
| 前缀 | 全部训练接口在 **`/custom/train`** 下，**不带版本段**；由组合 app 的 `include_router(..., prefix="/custom/train")` 统一加。上游既有接口（`/health`、`/v1/models`、`/v1/predict`、`/v1/video/...`）路径保持原样、不受影响 |
| 鉴权 | 请求头 `Token: <api_key>`；缺失或错误 → 401 `UNAUTHORIZED`（由上游中间件直接返回）。`security.api_key_enabled=false` 时上游中间件**直接放行**，整个服务（含全部 `/custom/train/*`）无鉴权；四种组合见 §3.7 |
| 成功封装 | `{"success": true, "data": {...}}`；`data` 的形状逐路由见 §3.2.2 |
| 失败封装 | `{"success": false, "error": {"code": "...", "message": "...", "details": {...}}}`；`details` 可缺省，各码的 `details` 字段名以 §3.3 表为准 |
| 封装例外（唯一） | 两条下载路由（`GET /jobs/{job_id}/files/{file_id}` 与 `GET /jobs/{job_id}/download`）**成功响应不包 JSON envelope**（二进制流 / `application/zip`），**失败仍返回标准 `ErrorResponse`**；客户端按 `Content-Type` 分流（§3.10、客户端篇 §5.6） |
| 时间格式 | ISO8601 UTC，形如 `2026-01-01T10:11:12Z`；所有时间字段一律 UTC、带 `Z` |
| ID 格式 | 数据集 `ds_<yyyymmdd>_<6hex>`；任务 `job_<yyyymmdd>_<6hex>`；上传 token `ut_<32hex>`；产物文件标识 `f_<hex>`（§3.10）；客户端提交幂等键建议 `sub_<32hex>` |
| 幂等 | `GET` 全部幂等；`cancel` 幂等（对已终态任务再次 cancel 返回 200 与当前状态）；`upload` 同 `upload_token` + 同 body 重放 → 返回**原响应**（同 token 异 body → 409 `VALIDATION_FAILED`）；`POST /jobs` 同 `client_submission_id` + 同请求体 → 返回**原 `job_id`**（异体 → 409 `VALIDATION_FAILED`）。**不引入** `Idempotency-Key` 头 |
| `schema_version` | 请求体 `schema_version` = `1`：这是**契约（schema）版本**，与接口路径无关（路径不含版本段）。服务端在 `capabilities` 里回报 `schema_version` 与 `server_version` |
| `param_schema` 区间语义 | 排他界**统一为布尔标志**：`{min, exclusive_min: bool, max, exclusive_max: bool}`——`min` / `max` 为闭区间边界，`exclusive_min: true` 表示下界开，`exclusive_max: true` 表示上界开。**没有**「`exclusive_min` 直接给边界数值」这种写法；「0 < r < 1」写为 `{"min": 0, "exclusive_min": true, "max": 1, "exclusive_max": true}`。`warn_above` 只触发告警、不拒绝（§3.8.3） |
| 分页 | 只有 `GET /datasets` 有服务端分页（`?limit=` 默认 **50**、上限 **200**，`?offset=` 默认 **0**）；`GET /jobs` **无 `offset`**、不做服务端分页，改用 `?ids=` 分批查询（§3.2.4、§3.2.5） |
| 单 worker | 服务端 v1 **单进程单 worker**（`uvicorn --workers 1`）：进程内全局互斥与内存态即权威，跨 token / 跨请求的准入判定因此可串行化（§3.11） |

**示例省略（与 §0.3 同一口径）**：JSON / YAML 示例里的 `sha256` / `mtime` / `size` / 计数型数值（条数、字节数、序号）属**示例省略**——可缩短、可用占位（如 `"0f2b8c9d..."`）；真实 `sha256` 恒为 64 位小写十六进制。示例里的数组与对象**可截断展示**（如 `optimizer_presets` / `vram_table.entries[]` / `model_families[].weights` 只列部分条目）。**计数型字段是字面值、不参与截断**（`weights.cached` / `weights.missing` / `vram_table.sources` / `queue.*` / `jobs.*`）：示例数字即该次部署的真实计数。示例只说明形状与层级，**权威定义一律以同节字段表 / 规则段为准**。

**三处不得臆造**（§0.3 的三条例外，逐条落到本章）：① `file_id` 的示例值必须能按 §3.10 的生成规则**复算命中**，不得写成无法复算的随机值；② `state.json` / `queue.json` 的字段名逐字为准，不得改名、不得省略；③ 错误码字符串必须与 §3.3 的表逐字一致，不得自造近义词。

### §3.2 路由全表（16 条）

#### §3.2.1 路由总表

| # | 方法 | 路径（前缀 `/custom/train`） | 用途 | 客户端 v1 |
| --- | --- | --- | --- | --- |
| 1 | `GET` | `/capabilities` | 能力协商：家族与可用性（`weights_ready` / `available` / `unavailable_reason`）、权重下载开关、auto-batch 开关、OOM 兜底、参数 schema（23 项）、preset 与默认策略、设备与显存账本、队列深度、`cancel_grace_seconds`、显存估算表（含每组合 `source` / `max_batch` / `calibration_at`）、标定状态、`training_env` 指纹、启动告警、`server_version` | 消费 |
| 2 | `POST` | `/datasets/plan` | 增量上传阶段 1：声明 manifest、判定需上传哪些图片、发一次性 `upload_token` | 消费 |
| 3 | `POST` | `/datasets/upload` | 增量上传阶段 2：multipart 提交 zip，写 blob 与数据集目录 | 消费 |
| 4 | `GET` | `/datasets` | 列出数据集（服务端分页） | **不消费** |
| 5 | `DELETE` | `/datasets/{dataset_id}` | 软删除数据集（移入 `.trash/`） | **不消费** |
| 6 | `GET` | `/cache/stats` | blob 命中率 / 占用 / 回收统计 | **不消费** |
| 7 | `POST` | `/jobs` | 提交任务（预检 + 入队，严格 FCFS） | 消费 |
| 8 | `GET` | `/jobs` | 任务列表 / 批量查询（`?ids=` / `?status=` / `?limit=`） | 消费 |
| 9 | `GET` | `/jobs/{job_id}` | 任务详情（单个 job 对象） | 消费 |
| 10 | `GET` | `/jobs/{job_id}/events` | 增量事件流（`?after=<seq>`） | 消费 |
| 11 | `POST` | `/jobs/{job_id}/cancel` | 取消任务（幂等） | 消费 |
| 12 | `POST` | `/jobs/{job_id}/resume` | 手动恢复（`mode` ∈ `resume` / `restart`） | 消费 |
| 13 | `GET` | `/jobs/{job_id}/files` | 产物清单（**权威清单，同时是下载白名单**） | 消费 |
| 14 | `GET` | `/jobs/{job_id}/files/{file_id}` | 下载单个产物（受控文件 ID；支持 `Range` / `ETag`） | 消费 |
| 15 | `GET` | `/jobs/{job_id}/download` | 流式打包下载全部结果（zip） | 消费 |
| 16 | `GET` | `/health` | **训练子系统**健康与可训练状态（独立新增接口，不触碰上游 `/health`） | 消费 |

#### §3.2.2 逐条请求要点 / 响应要点

全部 16 条都走 §3.1 的封装与鉴权；下表的「响应要点」一律指成功响应的 `data`。

| # | 请求要点 | 响应要点（`data`） |
| --- | --- | --- |
| 1 | 无请求体、无查询参数（`api_key_enabled=true` 时需 `Token` 头） | §3.6 的 18 键对象：`schema_version` / `server_version` / `enabled` / `tasks` / `allow_weight_download` / `allow_auto_batch` / `oom_retry` / `model_families` / `param_schema` / `optimizer_presets` / `preset_policy` / `devices[]` / `queue` / `cancel_grace_seconds` / `vram_table` / `calibration` / `training_env` / `warnings` |
| 2 | JSON body：`schema_version` + manifest 元数据（逐图 `name` / `split` / `sha256` / `size` / `label_sha256` / `label_size`，以及 `classes` / `counts` / `split_stats` / `val_ratio` / `seed` / `split_strategy`） | `upload_token` / `total_images` / `blob_hits` / `missing_images[]`（每项只含 `name` / `split` / `sha256` / `size`）/ `upload_bytes` / `rejected[]`（条目级拒绝，不阻断整次 plan）/ `expires_at` |
| 3 | multipart 两个部件：`upload_token`（来自 #2）+ `archive`（zip，内含 `manifest.json` 与 `images/` / `labels/`） | `dataset_id` / `task` / `classes` / `counts`（**服务端按落盘实况派生**，客户端不上报：`total` / `train` / `val` / `background`）/ `split_stats`（客户端上报值**原样回显**，同时写入数据集 `meta.json` 与 #4 列表）/ `bytes`（`{"images", "labels", "dataset"}`）/ `blob`（`{"written", "hit"}`）/ `warnings[]`（§3.9）/ `created_at` / `expires_at` |
| 4 | `?limit=`（默认 50、上限 200）、`?offset=`（默认 0）；两者都必须是非负整数，取值非法或 `limit` 超上限 → 400 `VALIDATION_FAILED`（`details.field` = `limit` / `offset`，越限时另带 `details.limit` / `details.provided`） | `data` **就是**数据集数组（形如 `[...]`，**不是** `{"datasets": [...]}` 包装），每条含 `dataset_id` / `task` / `classes` / `counts` / `split_stats` / `bytes` / `created_at` / `expires_at` / `referenced_by_jobs[]`；翻页规则见 §3.2.5 |
| 5 | 路径参数 `dataset_id` | 200 `{"deleted": true, "moved_to_trash": "<path>"}`；被 queued / preparing / running 任务引用 → 409 `DATASET_IN_USE`（`details.referenced_by_jobs[]`） |
| 6 | 无 | `blobs` / `blob_bytes` / `blob_hit_rate` / `unreferenced_blobs` / `reclaimed_24h` / `trash_bytes` / `work_dir_free_gb` |
| 7 | JSON body：`schema_version` / `dataset_id` / `task` / `model_family` / `model` / `params`（23 项，§3.8.2），可选 `client_job_name`（只落库、**不回传**）与 `client_submission_id`（幂等键、**不回传**） | `job_id` / `status`（首次提交恒为 `queued`）/ `queue_position` / `device_index`（提交时恒为 `null`）/ `vram_estimate_mb` / `queued_reason` / `resolved_params`（**`device` 必须为 `null`**，§3.8.5）/ `warnings[]`（§3.9）/ `created_at` |
| 8 | `?ids=`（逗号分隔；**单次 id 数上限 = `limit`**，超限 400 `VALIDATION_FAILED`）、`?status=`（逗号分隔，过滤）、`?limit=`（默认 50）；**无 `offset`** | `jobs[]`（job 对象，含 `is_terminal`，§3.4.4）+ `total`（= `jobs[]` 长度，不含 `not_found_ids[]`）+ `not_found_ids[]`；七条规则见 §3.2.4 |
| 9 | 路径参数 `job_id` | 单个 job 对象（§3.4.4）；未知 id → 404 `JOB_NOT_FOUND`（`details.job_id`） |
| 10 | `?after=<seq>`（缺省按 0 处理） | `{"events": [...], "last_seq": N}`：只返回 `seq` **严格大于** `after` 的事件；`last_seq` = 当前文件最大 `seq`（§3.5） |
| 11 | 路径参数 `job_id` | `{"job_id": ..., "status": "cancelled"}`；幂等（重复调用返回 200 与当前状态）；取消语义见 §3.4.3 |
| 12 | 路径参数 `job_id` + 可选 body `{"mode": "resume"}`（取值只允许 `resume` / `restart`，缺省 `resume`，且必须出现在该任务的 `resume_mode_available` 中，否则 400 `VALIDATION_FAILED`） | `{"job_id": ..., "status": "queued", "attempt": 1, "resume_cycles": 2, "mode": "resume", "queue_position": 4}`——恢复后 `attempt` **重置为 1**、`resume_cycles` **+1**；不可恢复 → 409 `JOB_NOT_RESUMABLE`（仅在中间态 / 等锁超时 / 上一进程存活三条路径另带 `details.reason` ∈ `resume_in_progress` / `lock_timeout` / `process_alive`）；数据集或检查点不可用 → 409 `JOB_ARTIFACTS_EXPIRED`（`details.reason` ∈ `checkpoint_missing` / `dataset_expired`） |
| 13 | `?include_partial=`（默认 `true`；`false` 时清单**不含** `partial/` 前缀条目，相应 `file_id` 视为不在清单内 ⇒ 400 `VALIDATION_FAILED`） | `job_id` / `status` / `partial_available` / `files[]`；每条 = `file_id`（**不可变**，§3.10）/ `path`（恒为相对 `artifacts/<job_id>/` 的 POSIX 路径，部分结果带 `partial/` 前缀）/ `size` / `sha256` / `mtime` / `partial` |
| 14 | 路径参数 `file_id`（**必须来自 #13 清单**）+ 可选 `Range` / `If-Range` 请求头；**不接受任何路径字符串** | 二进制流，**成功不包 envelope**；`200` 全量 / `206` + `Content-Range` / `416` 区间不可满足；`ETag` 字面格式 `"<file_id>.<sha256>"`；失败：400 `VALIDATION_FAILED`（不在清单）/ 404 `ARTIFACT_NOT_FOUND`（在清单但非普通文件），见 §3.10 |
| 15 | `?include_partial=`（默认 `true`，语义与 #13 / #14 同口径） | `application/zip` 流式下载，`Content-Disposition: attachment; filename="<job_id>.zip"`；**成功不包 envelope**、失败仍返回 `ErrorResponse`；打包内容**只从 #13 清单枚举** |
| 16 | 无（`api_key_enabled=true` 时需 `Token` 头） | §3.7 的 12 键对象；`enabled=false` 时本接口仍 **200 + 全字段**，其它 `/custom/train/*` 返回 503 `TRAINING_DISABLED` |

#### §3.2.3 客户端 v1 的消费集合

**客户端 v1 消费 16 条中的 13 条**（16 − 3 = 13）；**不消费**的正好是三条只读 / 管理型路由：

| 不消费的路由 | 原因 |
| --- | --- |
| #4 `GET /datasets` | v1 不做数据集管理页（范围决策，客户端篇 §5.1） |
| #5 `DELETE /datasets/{dataset_id}` | 同上；过期清理由服务端 TTL 清理器负责 |
| #6 `GET /cache/stats` | 同上；413 `QUOTA_EXCEEDED`：**优先**提示「服务端已触发自动回收，请稍后重试」；**仅当回收无法解除**才提示联系管理员手工清理（§4.1.7、客户端篇 §5.6） |

数据集的全部客户端可见信息都来自它实际消费的路由：#2 / #3 的响应与 #7 / #8 / #9 的 job 对象。因此**服务端不得要求客户端读取 #4 / #5 / #6 才能完成任务流程**。

#### §3.2.4 批量查询规则（`GET /jobs?ids=...`，逐条冻结）

| 规则 | 约定 |
| --- | --- |
| 返回顺序 | `jobs[]` 严格**按请求 `ids` 的顺序**排列，客户端**不需要**自行排序 |
| 未找到的 id | 放入 `not_found_ids[]`（与 `jobs[]` 同级），**HTTP 仍 200、不报错**；客户端据此判定「服务端已无该任务」（孤儿条目） |
| 重复 id | **去重**后只返回一次（保留首次出现的位置） |
| 超过 `limit` | 请求里的 id 数 > `limit`（默认 50）→ 400 `VALIDATION_FAILED`（`details.field=ids`、`details.limit`、`details.provided`）；**不做静默截断**（截断会让客户端把被截断的 id 误判为孤儿） |
| `total` | 本次响应实际返回的 job 数（= `jobs[]` 长度），**不含** `not_found_ids[]` |
| 与 `status` 组合 | `?status=` 是**过滤**：先按 `ids` 取（含未找到判定），再按 status 过滤；被 status 过滤掉的 id **不**进 `not_found_ids[]` |
| 分页 | **无 `offset`、不做服务端分页**：客户端把待刷新 id 按每批 ≤ `limit` 切块后发出，再按 `job_id` 合并回本地台账（客户端篇 §5.5） |

```json
{
  "success": true,
  "data": {
    "jobs": [{"job_id": "job_20260101_7f2a91", "status": "running", "is_terminal": false}],
    "not_found_ids": ["job_20260101_000000"],
    "total": 2
  }
}
```

（示例里的 job 对象按 §3.1「示例省略」只列 3 个键，权威字段见 §3.4.4。）

#### §3.2.5 分页规则（`GET /datasets`）

| 项 | 约定 |
| --- | --- |
| `limit` | 默认 **50**、上限 **200**；非负整数，非法或超上限 → 400 `VALIDATION_FAILED`（`details.field=limit`；超上限时另带 `details.limit` / `details.provided`） |
| `offset` | 默认 **0**；非负整数，非法 → 400 `VALIDATION_FAILED`（`details.field=offset`） |
| 翻页终止判据 | **不引入 `total` / `has_more` 字段**：返回条目数 < `limit` ⇒ 已到末页；= `limit` ⇒ 可能还有下一页，客户端以 `offset += limit` 继续请求 |
| 越界 | `offset` 超过条目总数时返回**空数组**、HTTP 仍 200、不报错 |
| 响应形状 | `data` **就是数组**；不做 `{"datasets": [...]}` 包装 |

### §3.3 错误码全表（30 对 / 29 个不同 code）

**表即权威**：下表逐行给出 `(HTTP, code, 触发条件, details 字段名)`。**唯一统计口径**见本节末尾；**文字叙述与表不一致时，以表为准并立即修正文字**。

| HTTP | code | 触发条件 | `details` 字段名 |
| --- | --- | --- | --- |
| 400 | `VALIDATION_FAILED` | 请求体结构 / 值域非法：`schema_version` 非法、`split` 非法、**划分两侧为空**、`split_strategy` 取值非法、`classes` 重复、**同 split 内 stem 重复**、resume 的 `mode` 不在 `resume_mode_available` 内、**批量查询 id 数超过 `limit`**、**#4 的 `limit` / `offset` 取值非法或 `limit` 超上限**、**`file_id` 不在产物清单内**等 | `field`、`files[]`、`allowed`、`file_id`、`limit`、`provided`（**后二者只由「越限」场景另带**：批量查询 id 数越限、#4 的 `limit` 越限） |
| 400 | `CHECKSUM_MISMATCH` | **图片**解压内容 sha256 与 manifest 声明的 `images[].sha256` 不符 | `files[]` |
| 400 | `LABEL_CHECKSUM_MISMATCH` | **标签**文件 `labels/<split>/<stem>.txt` 的 sha256 与 manifest 声明的 `label_sha256` 不符 | `files[]`（每项含 `name` / `label` / `reason` / `declared` / `actual`） |
| 400 | `MISSING_LABELS` | 图片缺对应 label 文件（服务端复核） | `files[]`（形如 `train/a.jpg`） |
| 400 | `MANIFEST_MISMATCH` | ① zip 内 `manifest.json` 与服务端保存的 plan 副本**在同一规范化函数下不一致**（两边各算同一 `canonical_json_bytes` 后按**字节**比较；规范化字节函数 `canonical_json_bytes` 见 客户端篇 §5.4.1，跨仓同源：键序 / 缩进 / 空白差异**不算**差异）；② zip 内图片集合与 `missing_images` 不符（多传 / 少传）；③ 出现 manifest 未声明的多余条目（顶层未知项、额外目录等） | ① 为 `field=manifest`；②③ 为 `missing[]` / `unexpected[]` |
| 400 | `INVALID_LABEL_FORMAT` | 标签行字段数 / 类别索引非法 | `files[]` |
| 400 | `UNSUPPORTED_EXTENSION` | 扩展名不在白名单（**upload 阶段**；同一问题在 plan 阶段是条目级 `rejected[]`，按阶段限定） | `files[]` |
| 400 | `UNKNOWN_UPLOAD_TOKEN` | token 在「在用表」与「已提交表」里都不存在——**含保留期届满后已被清理器删除**的记录 | — |
| 400 | `TOKEN_EXPIRED` | token 的**有效期确已届满**：① 「在用表」命中但 `expires_at` 已过；② 「已提交表」命中、保留期（默认 24 小时）已过但记录**尚未**被清理（记录被清理后回落为 `UNKNOWN_UPLOAD_TOKEN`）。**仍在保留期内的已提交 token 不走本码**：同 body 幂等重放原响应、异 body 走 409 `VALIDATION_FAILED` | `expires_at` |
| 401 | `UNAUTHORIZED` | `Token` 缺失或错误（由上游中间件返回） | — |
| 404 | `JOB_NOT_FOUND` | 未知 `job_id` | `job_id` |
| 404 | `DATASET_NOT_FOUND` | 未知 `dataset_id` | `dataset_id` |
| 404 | `ARTIFACT_NOT_FOUND` | `file_id` **在清单内**但目标不是可下载的普通文件（不存在 / 是目录 / 是符号链接 / 是设备文件 / 是 FIFO） | `file_id`（**不回显文件系统路径**） |
| 409 | `DATASET_IN_USE` | 数据集被 queued / preparing / running 任务引用，禁止删除 | `referenced_by_jobs[]` |
| 409 | `JOB_NOT_RESUMABLE` | 对 `completed` 任务调用 resume；**或** `status` 不属于可恢复集合；**或**该 job 的 `resume_intent.json` 处于未收尾的中间态；**或**等 `state.lock` 超时（`job_state_lock_timeout_seconds`）；**或**按 `state.json` 的 `pid` / `proc_start_time` 探活判定**上一进程仍存活** | `status`；`reason` **仅在中间态 / 等锁超时 / 上一进程存活三条路径下附带**：`resume_in_progress` / `lock_timeout` / `process_alive`（三条**复用**本码、**不新增**错误码） |
| 409 | `JOB_ARTIFACTS_EXPIRED` | 状态可恢复但检查点或数据集已不可用 | `job_id`、`reason`（`checkpoint_missing`：无 `last.pt` 且 `resume_fallback: fail`；`dataset_expired`：数据集已被 TTL 清理或删除）、`checked_at` |
| 409 | `UPLOAD_IN_PROGRESS` | **同一 token** 并发上传（单 worker 进程内全局互斥保证） | `upload_token` |
| 409 | `VALIDATION_FAILED` | **同一 `upload_token` + 不同 body 重放**（该 token 已成功 commit，不能再用它提交别的内容）；job 侧同 `client_submission_id` + 不同请求体同理 | `field`（`upload_token` / `client_submission_id`）、`committed_dataset_id` / `existing_job_id` |
| 413 | `QUOTA_EXCEEDED` | 上传 / blob / 总量超配额 | `quota`、`used_bytes`、`requested_bytes` |
| 422 | `PARAM_OUT_OF_RANGE` | 参数越界（`epochs` / `batch`（含 `allow_auto_batch: false` 时提交的 `-1` / 比例值）/ `imgsz` / `workers` / `lr0` …） | `field`、`min`、`max`、`allowed` |
| 422 | `PARAM_NOT_OVERRIDABLE` | 请求体里出现服务端注入参数（`model` / `data` / `device` / `project` / `name` / `resume`） | `fields[]` |
| 422 | `OPTIMIZER_UNSUPPORTED` | 裸优化器名（`SGD` / `AdamW` / `MuSGD` …）、**preset 不属于所选家族**（不在 `model_families[family].presets` 内）、环境 < 8.4 却要求 MuSGD，或 `auto` 与 7 个优化器超参（`lr0` / `lrf` / `momentum` / `weight_decay` / `warmup_epochs` / `warmup_momentum` / `warmup_bias_lr`）**同送**（§3.8.2） | `optimizer`、`ultralytics`、`model_family`、`allowed`（**只有「跨家族 preset」子情形才给 `allowed`**，= 所选家族 `presets` 的合法取值数组）；**`auto` 冲突子情形改给 `details.fields`**（同送的键名数组） |
| 422 | `MODEL_FAMILY_UNSUPPORTED` | 环境版本低于家族**能力下限** `min_ultralytics`（事实性能力检查，不是版本门禁） | `model_family`、`min_ultralytics`、`ultralytics` |
| 422 | `WEIGHT_NOT_AVAILABLE` | `allow_weight_download: false` 且 `<work_dir>/weights/<name>` 不存在（即 `weights_ready=false` 且不允许按需下载），提交预检 | `model`、`weights_dir` |
| 422 | `INSUFFICIENT_VRAM` | 提交预检两种子情形（**同码不同 `details.reason`**）：① `reason=insufficient_capacity` —— `est_mb > min_device_total_mb − gpu_reserve_mb`（语义 = 「本机永远跑不了」，判据是设备**最终容量**而非瞬时可用量）；② `reason=ratio_cap_below_one` / `table_cap_below_one` —— auto-batch 折算中**任一有效 cap < 1** | `vram_estimate_mb`、`min_device_total_mb`、`gpu_reserve_mb`、`reason`；cap 类另带 `field=batch`、`batch_cap_by_ratio`、`batch_cap_by_table`、`ratio`、`total_mb`、`suggestion` |
| 422 | `VRAM_ESTIMATE_UNAVAILABLE` | 该 `(model, task)` 组合**不可调度**：要么在任何一层显存表里都既无本行、也无同模型其他 task 的行；要么在本机启动期标定中**全部点位 OOM**（`reason` 取 `VRAM_TABLE_INCOMPLETE` / `VRAM_CALIBRATION_FAILED`） | `model`、`task`、`unschedulable`、`reason` |
| 429 | `COMMITTED_TOKEN_CAPACITY_EXCEEDED` | 本次 upload 会**新写入**一条已提交 token 记录（仅判定表 ⓪ 会写记录），且容量准入判定为拒：`current`（已提交表中**未过期**记录的条数）≥ `max_committed_tokens`（默认 10000）。**保留期内禁止淘汰**（不得 LRU），因此对新提交施加容量上限 | `limit`、`current`、`retry_after_seconds`（恒 ≥ 1）；响应头带 `Retry-After`（语义见服务端篇 §4.1.4） |
| 503 | `TRAINING_DISABLED` | `enabled: false`——**除 `GET /health` 外**的全部 `/custom/train/*` | — |
| 503 | `NO_DEVICE_AVAILABLE` | 无可用 CUDA 设备 | `devices` |
| 500 | `INTERNAL_ERROR` | 未预期异常（含 traceback 摘要，不泄露路径以外的敏感信息） | `error_id` |

**唯一统计口径**：

| 项 | 值 |
| --- | --- |
| `(HTTP, code)` 对数 | **30** |
| 不同 code 数 | **29**（`VALIDATION_FAILED` 同时以 400 与 409 出现 ⇒ 只算 1 个 code） |
| 按 HTTP 分布 | 400 类 **9**、401 **1**、404 **3**、409 **5**、413 **1**、422 **7**、429 **1**、503 **2**、500 **1**（合计 **30**） |
| 复核方式 | 直接数上表的数据行（表头不计）；任何文字统计与表行数不一致时**以表为准并立即修正文字** |

**与表相关的三条硬约定**：

- **400 与 409 的 `VALIDATION_FAILED` 是同一 code 的两条 `(HTTP, code)` 对**：触发场景与 `details` 都不同，客户端必须按 **HTTP + code 联合分支**处理（客户端篇 §5.6）。**没有删除任何 code**。
- **`file_id` 的两种失败互斥**：不在 #13 清单内（含任意非法值 / 穿越串）→ 400 `VALIDATION_FAILED`；在清单内但解析后的目标不存在或不是普通文件 → 404 `ARTIFACT_NOT_FOUND`（§3.10）。
- **不走错误码的三个边界**：过大 `batch` 走**自动收敛 + 提交响应 `warnings[]`**（`CONVERGED_TO_DEVICE_MAX`）；标定失败导致的不可调度复用 422 `VRAM_ESTIMATE_UNAVAILABLE`；标定期间的「服务端未就绪」表现为**连接失败**（没有 HTTP 响应，故不成其为错误码）；环境类信息**只有告警码、没有错误码**（§3.9）。

### §3.4 状态机与 job 对象

#### §3.4.1 状态集合

状态固定为 7 个：`queued` → `preparing` → `running` → {`completed` / `failed` / `cancelled` / `interrupted`}；其中 `interrupted` 在满足条件时自动重入队。终态集合 = {`completed`、`cancelled`、`failed`（本轮预算用尽）、`interrupted`（不自动重排）}，判定**只看 `finished_at`**（§3.4.3）。

#### §3.4.2 状态转移表（15 行）

| 当前状态 | 事件 | 下一状态 | 备注 |
| --- | --- | --- | --- |
| — | 提交任务，预检通过 | `queued` | 写 `jobs/<job_id>/request.json`、`state.json.status=queued`，`queue.json` 追加队尾 |
| `queued` | 调度器分到卡、并发位可用 | `preparing` | 写 `data.yaml`、建 `run/train`、注入参数；同一次原子写落 `preparing_at` / `deadline_at`（§4.3.4） |
| `queued` | 用户取消 | `cancelled` | 直接出队，无 artifacts |
| `preparing` | **训练进程写入首个心跳**——`state.json.heartbeat_at` **首次出现**（随该次合并写把 `status` 置为 `running`） | `running` | 同一次合并写落 `pid` / `proc_start_time` / `heartbeat_at` / `status=running`。**首条 `progress` 事件不是判据**（它在一个 epoch 结束后才到，必然晚于首个心跳） |
| `preparing` | 准备失败（IO / 环境 / 权重缺失） | `failed` | 本轮 `attempt < max_attempts` 时自动重排队尾（`attempt+1`）；否则终态 + `needs_attention_reason=attempts_exhausted` |
| `running` | 训练正常结束且产物校验通过 | `completed` | 写 `summary.json`，复制到 `artifacts/<job_id>/` |
| `running` | 训练异常退出（非 0 退出码 / 明确异常；**含 OOM 且降级未成功**） | `failed` | 保留 `partial/`；本轮 `attempt < max_attempts` 时自动重排队尾（`attempt+1`）。**例外**：取消路径上 SIGKILL 后仍存活的进程按取消语义收口（`status` 保持 `cancelled` + 写 `finished_at`、**不**自动重排，§4.3.2） |
| `running` | 心跳超时（`heartbeat_timeout_min`） | `interrupted` | 杀进程树；随后按下表两行二选一 |
| `running` / `preparing` | 用户取消 | `cancelled` | SIGTERM → 宽限 `cancel_grace_seconds` → SIGKILL；落 `partial/` |
| `interrupted` | `auto_resume=true`（默认）**且** `attempt < max_attempts` | `queued` | 同一轮内 `attempt+1`、排队尾；**入队前必须先归档上一 attempt 的终态证据** |
| `interrupted` | `auto_resume=false` **或** `attempt >= max_attempts` | `interrupted`（终态） | **终态分支**：`status` 保持 `interrupted`、写 `finished_at`（`is_terminal` 随之为 `true`）、`needs_attention=true` 且 `needs_attention_reason=resume_anomaly`，等用户手动恢复 |
| `failed` | 本轮 `attempt < max_attempts` | `queued` | 自动重试、排队尾（`attempt+1`）。**不受 `auto_resume` 约束**（该开关只管 `interrupted`）——判据只有「本轮 `attempt < max_attempts`」；入队前**必须**先归档上一 attempt 的终态证据，且**不写** `finished_at` |
| `failed` | 本轮 `attempt >= max_attempts` | `failed`（终态） | 本轮重试预算用尽（**不会**自动重排）：写 `finished_at`、`needs_attention=true`（`needs_attention_reason=attempts_exhausted`）。手动恢复会**重置**本轮预算并开启新一轮 |
| `cancelled` | — | 终态 | 可被用户**手动**恢复，**不会**自动续训 |
| `completed` | — | 终态 | **不可恢复** |

**取消语义（三条要点，逐字冻结）**：① 各阶段行为——`queued` 直接出队（无 artifacts）；`preparing` 终止准备流程、清理 `run/train` 半成品、落 `partial/`（若有）；`running`（Linux）`os.killpg(os.getpgid(pid), SIGTERM)` → 等 `cancel_grace_seconds`（默认 15）→ 仍存活则 `SIGKILL`；三者都写 `status=cancelled` + `finished_at`。② `partial/` 落 `weights/last.pt`、已产出的 `results.csv`、`args.yaml`、`train.log`、`events.jsonl`，清单条目 `partial: true` 且 `path` 统一带 `partial/` 前缀。③ **无法杀死（唯一收口）**：SIGKILL 后仍存活 ⇒ **不改变取消语义**——`status` 保持 `cancelled`、写 `finished_at`（`is_terminal=true`）、`needs_attention=true` / `needs_attention_reason=resume_anomaly`，并记一条 WARN 日志；该终态**不参与自动重排**，只能经 `POST /jobs/{id}/resume` 手动恢复（§4.3.2）。

#### §3.4.3 状态机不变量（4 条）

| # | 不变量 |
| --- | --- |
| 1 | 只有**调度器**能写 `queued → preparing`；**只有训练进程**能把状态推进到 `running`，判据**唯一** = `state.json.heartbeat_at` **首次出现**（随该次合并写把 `status` 置为 `running`）。服务端**不得**替训练进程写 `status=running`；「首条 `progress` 事件到达」「心跳推进」等说法**都不是**判据 |
| 2 | 任一时刻同一 `job_id` **只有一个活进程**（`state.json.pid` 唯一） |
| 3 | **终态判据（唯一）**：`is_terminal := (finished_at != null)`——终态 ⟺ 写了 `finished_at`，与 `status` 的字面取值**解耦**。写 `finished_at` 只有四种情形：① `completed`；② `cancelled`；③ `failed` / `interrupted` **且不会自动重排**（本轮 `attempt >= max_attempts`；`interrupted` 另加 `auto_resume=false`）；④ 取消路径上 SIGKILL 后仍存活的 `cancelled`（进程仍存活，重排没有意义）。**`failed` / `interrupted` 且仍会自动重排时一律不写 `finished_at`**（此时 `is_terminal=false`，job 处于「已判重排、尚未派发」的中间态）。之后状态只可能因**手动 resume** 变为 `queued`（`completed` 除外），那时 `finished_at` 被置回 `null`、`is_terminal` 回到 `false` |
| 4 | **`interrupted` 的两个分支互斥且穷尽**：`auto_resume=true` **且** `attempt < max_attempts` 是**唯一**的「自动重入队」组合；**其余一切组合**（含 `auto_resume=true` 且 `attempt >= max_attempts`）都落到**终态分支**（写 `finished_at`、`status=interrupted`、`needs_attention=true` / `resume_anomaly`）。不存在「只要 `auto_resume=true` 就无条件重排队尾」的读法 |

#### §3.4.4 job 对象字段表（23 个字段）

`GET /jobs` 与 `GET /jobs/{job_id}` 返回的 job 对象（两侧同一对象，字段集逐字相同）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `job_id` | str | 任务 ID（`job_<yyyymmdd>_<6hex>`，§3.1）。批量查询与详情响应都必须携带它，否则客户端无法按 `job_id` 合并 |
| `status` | str | 七个状态之一（§3.4.1） |
| `is_terminal` | bool | **权威终态标志**（服务端计算，客户端唯一的终态判据来源）。定义：`is_terminal := (finished_at != null)`，与状态机天然一致。手动 resume 把 `finished_at` 置回 `null` ⇒ 本字段随之回到 `false`。**必须在 `GET /jobs` 与 `GET /jobs/{job_id}` 都出现**（与 `job_id` / `resolved_params` 同级）；客户端优先用它，**缺失时**才回退到 `finished_at != null` |
| `attempt` | int | **本轮（当前恢复周期）内的尝试序号，从 1 开始**：每次自动重试（含 `interrupted` 自动重入队）在本轮内 +1；**客户端手动恢复后重置为 1** |
| `max_attempts` | int | **每轮**的自动重试上限（默认 3，含首次）；只在本轮内累计，**不跨手动恢复累加**。字段名是 `attempt` / `max_attempts`——**不存在** `attempt_max` |
| `resume_cycles` | int | 手动恢复次数：从 `0` 开始，每次成功的 `POST /jobs/{id}/resume`（`resume` 与 `restart` 都算）+1；与 `attempt` 正交 |
| `queue_position` | int 或 null | 队列位次（1 = 队头）；非 queued 时为 `null` |
| `eta_seconds` | int 或 null | 预计剩余秒数 = 已用时长 / 已完成 epoch × 剩余 epoch；样本不足（< 2 epoch）时为 `null` |
| `queued_reason` | str 或 null | 排队原因（**排队是正常路径、不是拒绝**）；**三值**：`WAITING_DEVICE_VRAM`（该卡可调度量不足以放下本任务，等它释放）/ `WAITING_PREVIOUS_JOBS`（同一卡上仍有 `running` 任务占用实测空闲显存）/ `WAITING_CONCURRENCY_SLOT`（并发位占满），可附人类可读细节。文案**不得**写成「本机跑不了」（那对应 422 `INSUFFICIENT_VRAM` 的容量判据） |
| `device_index` | int 或 null | 分配的卡号；未分配为 `null` |
| `vram_estimate_mb` | int | 提交时算出的估算显存，全程不变；`batch` 为 auto（`-1` 或比例值）时按 `batch_assumed` 估算 |
| `progress` | object | `{"epoch": 12, "total_epochs": 100, "percent": 12.0}`；queued 时为 `{"epoch": 0, "total_epochs": N, "percent": 0.0}` |
| `metrics` | object | 末次 `metrics` 事件内容，如 `{"box_loss": 1.02, "mAP50": 0.512, "mAP50-95": 0.331, "precision": 0.61, "recall": 0.55}`（segment 另有 `mask_mAP50` 等） |
| `created_at` | str 或 null | 入队时刻（ISO8601 UTC）；未发生为 `null` |
| `started_at` | str 或 null | 首次进入 `running` 的时刻；未发生为 `null` |
| `finished_at` | str 或 null | 终态时刻；**`is_terminal` 的唯一依据**（§3.4.3 第 3 条） |
| `error_summary` | str 或 null | 失败原因摘要（末 20 行 `train.log` 关键行 + 退出码）；因 OOM 最终失败时必须带**实际使用的 batch** 与**显存峰值**（若可获取） |
| `needs_attention` | bool | 需要用户介入（本轮重试耗尽 / 产物可疑 / resume 异常） |
| `needs_attention_reason` | str 或 null | `needs_attention=true` 时的**单一原因**（**三值**）：`attempts_exhausted`（**本轮**预算用尽）/ `resume_anomaly` / `artifact_suspect`。多条件同时成立时按 **`attempts_exhausted` > `resume_anomaly` > `artifact_suspect`** 取优先级最高者；`needs_attention=false` 时为 `null`。**环境信息不进入该字段**（只走 `capabilities.warnings`，§3.9） |
| `artifact_suspect` | bool | 产物可疑（best.pt 复算指标异常，或 resume 后 loss 爆炸） |
| `partial_available` | bool | `artifacts/<job_id>/partial/` 是否有内容 |
| `resolved_params` | object | **服务端最终生效的参数快照**（§3.8.5）；`GET /jobs` 与 `GET /jobs/{job_id}` 都必须返回；queued 阶段 `resolved_params.device` 为 `null` |
| `resume_mode_available` | str[] | **服务端实时判定**的可恢复模式：`["resume"]`（有 `last.pt`，可续训）/ `["restart"]`（无 `last.pt` 但 `resume_fallback: restart`）/ `[]`（不可恢复）。客户端按「数组非空」决定恢复按钮可用性；仅对可恢复状态 `failed` / `interrupted` / `cancelled` 有意义，为空时**置灰**（按钮存在但不可用） |

**字段出现范围（三条澄清，避免「只出现在示例里的字段」）**：

- `client_job_name`：`POST /jobs` 请求体的可选字段，**只落库**到 `jobs/<job_id>/request.json`，**不在 job 对象里回传**。
- `suspect_reason`：**不属于 job 对象**，只在 `summary.json` 与终态摘要里出现（取值 `artifact_metric_mismatch` / `resume_loss_spike`）；job 对象用布尔 `artifact_suspect` + `needs_attention_reason=artifact_suspect` 表达。
- OOM 降 batch 次数：**不新增 job 字段**，通过 `log` 事件（`data.code=OOM_BATCH_DOWNGRADE`）与 `summary.json.oom_downgrades[]` 暴露（§3.5）。

### §3.5 事件协议

事件落在 `jobs/<job_id>/events.jsonl`（终态后随产物复制到 `artifacts/<job_id>/events.jsonl`），每行一个事件：`{"seq": n, "ts": "...", "type": "...", "data": {...}}`。

| type | 写者 | `data` 字段 | 触发时机 | 客户端用途 |
| --- | --- | --- | --- | --- |
| `progress` | 训练进程（必需） | `{"epoch": 12, "total_epochs": 100, "percent": 12.0}` | 每个 epoch 结束 | 进度条 + ETA |
| `metrics` | 训练进程（必需） | `{"mAP50": ..., "mAP50-95": ..., "box_loss": ..., "cls_loss": ..., "dfl_loss": ..., "precision": ..., "recall": ..., "mask_mAP50": ...}` | 每个 epoch 的验证结束 | 指标曲线 / 最新指标 |
| `log` | 训练进程（必需） | `{"level": "info" / "warning" / "error", "message": "..."}`，可带扩展字段；OOM 降级为 `{"code": "OOM_BATCH_DOWNGRADE", "from_batch": 64, "to_batch": 32}`，降级轮次用尽为 `{"code": "OOM_RETRY_EXHAUSTED"}` | 关键行（含 resume 后 loss 爆炸告警、OOM 降 batch 与降级用尽） | 详情页日志面板（`warning` / `error` 触发黄条；**未知 `code` 按普通日志展示**） |
| `done` | 训练进程（必需） | `{"status": "completed" / "failed" / "cancelled", "exit_code": 0, "artifact_suspect": false, "best_pt": "run/train/weights/best.pt", "attempt": 2, "resume_cycles": 1}`——**`attempt` 与 `resume_cycles` 为必需字段**，取值 = 训练进程从 `state.json` 读到的本次 attempt / 当前周期序号；**缺任一字段的 `done` 不得计入任何终态判据** | 训练进程退出前最后一条（**同一 attempt 内唯一**的终态事件） | 触发终态刷新与产物校验 |
| `manual_resume` | **服务端**（可选第 5 类） | `{"attempt": 1, "mode": "resume" / "restart", "resume_cycles": 1}`（恢复后 `attempt` 重置为 1、`resume_cycles` 递增） | 服务端在**入队之前**写入，充当**恢复周期分界** | 事件流标注人工恢复，并据此说明「本轮重试预算已重置」 |

| 规则 | 约定 |
| --- | --- |
| 写者分工 | 四类必需事件由**训练进程**追加；`manual_resume` 由**服务端**追加。两者**天然不重叠**（服务端写 `manual_resume` 时该 job 没有活着的训练进程），因此无需文件级追加锁 |
| `seq` 交接 | **全局严格递增、跨恢复周期不重置**：事件文件是跨周期共用的同一个文件。① 训练进程在 `Popen` 之后、写首条事件之前读当前最大 `seq`，从 `last_seq + 1` 开始编号；② 服务端写 `manual_resume` 时同样取「当前最大 `seq` + 1」。**两个写者在求 `last_seq` 之前都先截断残缺尾行**（`last_seq` 取最后一条**完整**记录的 `seq`），**不得**把新行直接接在残缺 JSON 之后 |
| 增量语义 | `GET /jobs/{job_id}/events?after=<seq>` 返回 `seq` **严格大于** `after` 的事件，响应含 `last_seq`；**缺省 `after` 按 0 处理**（客户端据此从头重放与去重） |
| 读取容错 | 事件文件保留到 job 进入终态并打包进 `artifacts/`，客户端随时可从头重放；**读取方**对最后一行不完整 JSON **一律丢弃** |
| 落盘 | **两个写者都** `flush` + `os.fsync` 每条事件，保证读取方读到完整行 |
| 未知 `type` | **客户端对未知 `type` 必须降级为日志展示**，不得丢弃、不得报错（服务端演进时可新增类型） |
| 与 job 对象的关系 | `GET /jobs/{id}` 的 `progress` / `metrics` 是 events 的投影，不额外产生新语义 |

```json
{
  "success": true,
  "data": {
    "events": [
      {"seq": 1, "ts": "2026-01-01T10:21:00Z", "type": "log", "data": {"level": "info", "message": "train: 960 images, val: 240 images"}},
      {"seq": 2, "ts": "2026-01-01T10:24:12Z", "type": "progress", "data": {"epoch": 1, "total_epochs": 100, "percent": 1.0}},
      {"seq": 3, "ts": "2026-01-01T10:24:40Z", "type": "metrics", "data": {"mAP50": 0.112, "mAP50-95": 0.048, "box_loss": 2.31}}
    ],
    "last_seq": 3
  }
}
```

### §3.6 `capabilities` 契约

```json
{
  "success": true,
  "data": {
    "schema_version": 1,
    "server_version": "0.0.12",
    "enabled": true,
    "tasks": ["detect", "segment"],
    "allow_weight_download": true,
    "allow_auto_batch": true,
    "oom_retry": {"enabled": true, "max_retries": 2},
    "model_families": {
      "yolo11": {"min_ultralytics": "8.3.0", "available": true, "unavailable_reason": null,
                 "weights": {"detect": ["yolo11n.pt", "yolo11s.pt"], "segment": ["yolo11n-seg.pt"]},
                 "weights_ready": {"yolo11n.pt": true, "yolo11s.pt": false, "yolo11n-seg.pt": true},
                 "presets": ["yolo11-sgd", "yolo11-adamw", "auto"]},
      "yolo26": {"min_ultralytics": "8.4.0", "available": true, "unavailable_reason": null,
                 "weights": {"detect": ["yolo26n.pt"], "segment": ["yolo26n-seg.pt"]},
                 "weights_ready": {"yolo26n.pt": false, "yolo26n-seg.pt": false},
                 "presets": ["yolo26-default", "auto"]}
    },
    "param_schema": {
      "epochs": {"type": "int", "min": 1, "max": 1000},
      "batch": {"type": "int_or_auto", "min": 1, "max": 128, "auto": {"values": [-1], "ratio": {"min": 0, "exclusive_min": true, "max": 1, "exclusive_max": true}}},
      "imgsz": {"type": "enum", "values": [320, 416, 512, 640, 768, 896, 1024, 1280, 1536]},
      "workers": {"type": "int", "min": 0, "max": 16},
      "optimizer": {"type": "preset", "values": ["yolo11-sgd", "yolo11-adamw",
                                             "yolo26-default", "auto"]},
      "lr0": {"type": "float", "min": 0.0, "exclusive_min": true, "max": 0.05, "warn_above": 0.001},
      "lrf": {"type": "float", "min": 0.0, "exclusive_min": true, "max": 1.0},
      "momentum": {"type": "float", "min": 0.0, "max": 1.0},
      "weight_decay": {"type": "float", "min": 0.0, "max": 1.0},
      "warmup_epochs": {"type": "float", "min": 0.0},
      "warmup_momentum": {"type": "float", "min": 0.0},
      "warmup_bias_lr": {"type": "float", "min": 0.0},
      "cos_lr": {"type": "bool"},
      "amp": {"type": "bool"},
      "cache": {"type": "bool"},
      "rect": {"type": "bool"},
      "single_cls": {"type": "bool"},
      "patience": {"type": "int", "min": 0, "max": 1000},
      "close_mosaic": {"type": "int", "min": 0, "max": 1000},
      "save_period": {"type": "int", "min": 0, "max": 1000},
      "fraction": {"type": "float", "min": 0.0, "exclusive_min": true, "max": 1.0},
      "seed": {"type": "int"},
      "dropout": {"type": "float", "min": 0.0, "max": 1.0}
    },
    "optimizer_presets": {
      "yolo11-sgd": {"optimizer": "SGD", "lr0": 0.01, "momentum": 0.937, "weight_decay": 0.0005, "warmup_bias_lr": 0.1}
    },
    "preset_policy": {"type": "auto", "threshold": null, "default_preset": {}},
    "devices": [
      {"device_index": 0, "name": "NVIDIA GeForce RTX 4090", "total_mb": 24564,
       "free_mb": 21000, "reserved_mb": 1024,
       "in_flight_estimate_mb": 0, "running_estimate_mb": 5200, "available_mb": 19976}
    ],
    "queue": {"queued": 2, "running": 1, "max_concurrent_jobs": 2, "max_concurrent_per_device": 1},
    "cancel_grace_seconds": 15,
    "vram_table": {"task_factor": {"detect": 1.0, "segment": 1.25},
                   "auto_batch": {"assumed": 16, "ratio": 0.60},
                   "auto_loaded": true,
                   "fingerprint_matched": true,
                   "calibrated_at": "2026-01-01T09:40:12Z",
                   "fingerprint": {"gpu": "NVIDIA GeForce RTX 4090 x1", "gpu_total_mb": [24564],
                                   "driver": "560.94", "cuda": "12.4", "torch": "2.6.0+cu124",
                                   "ultralytics": {"__host__": "8.4.84", "yolo11": "8.3.253", "yolo26": "8.4.150"},
                                   "bench_params": {"imgsz": 640, "amp": true, "cache": false, "rect": false, "epochs": 3,
                                                    "batches": {"n": [16, 32, 64], "s": [16, 32, 64], "m": [8, 16, 32], "l": [8, 16, 32], "x": [4, 8, 16]}},
                                   "dataset": "synthetic-v1(seed=0,n=auto,inst=1..15,nc=10,size=640,fmt=png)"},
                   "entries": [
                     {"model": "yolo11s", "task": "detect", "baseline_mb": 1580, "per_image_mb": 155,
                      "process_overhead_mb": 210, "max_batch": 96,
                      "points": [{"batch": 16, "reserved_mb": 4060}, {"batch": 32, "reserved_mb": 6540}, {"batch": 64, "reserved_mb": 11500}],
                      "fit": {"r2": 1.0, "residual_pct": 0.0}, "source": "auto", "calibration_at": "2026-01-01T09:40:12Z"},
                     {"model": "yolo26n", "task": "detect", "baseline_mb": 1320, "per_image_mb": 99,
                      "process_overhead_mb": null, "max_batch": null, "points": [], "fit": null,
                      "source": "manual", "calibration_at": null}
                   ],
                   "sources": {"auto": 19, "manual": 1, "default": 0},
                   "unschedulable": [{"model": "yolo26n", "task": "detect", "reason": "VRAM_CALIBRATION_FAILED"}]},
    "calibration": {"required": true, "deferred": false, "deferred_ready": false, "deferred_reason": null,
                    "conflict_policy": "defer", "calibrated_at": "2026-01-01T09:40:12Z", "auto_loaded": true,
                    "fingerprint_matched": true,
                    "skipped": [],
                    "skipped_reason": null,
                    "failed": [{"model": "yolo26n", "task": "detect", "reason": "VRAM_CALIBRATION_FAILED"}]},
    "training_env": {
      "ultralytics": "8.4.84", "torch": "2.6.0+cu124", "cuda": "12.4", "python": "3.12.4"
    },
    "warnings": [
      {"code": "VRAM_CALIBRATION_FAILED", "message": "部分 (model, task) 组合在本次标定中全部点位 OOM，已标记为不可调度（见 details.failed）",
       "details": {"failed": [{"model": "yolo26n", "task": "detect", "reason": "all_points_oom"}]}},
      {"code": "VRAM_TABLE_INCOMPLETE", "message": "白名单中存在不可调度的模型与任务组合（见 details.unschedulable）",
       "details": {"unschedulable": [{"model": "yolo26n", "task": "detect"}]}}
    ]
  }
}
```

（本示例按 §3.1「示例省略」截断展示：`model_families[].weights` / `optimizer_presets` / `vram_table.entries[]` / `warnings[]` 都只列部分条目——默认部署的家族白名单是 yolo11 与 yolo26 各 `detect` 5 + `segment` 5 = 20 个权重文件，`entries` 的组合数为 20，`warnings` 的 code 枚举有 7 个。示例只说明形状与层级。）

**顶层字段说明（18 键，全部必需）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | int | 契约版本，恒为 `1`（§3.1） |
| `server_version` | str | 服务端版本（`app/__init__.py` 的 `__version__`），与 `health.server_version` 同源 |
| `enabled` | bool | 训练子系统开关；`false` 时**除 `GET /health` 外**的全部 `/custom/train/*` 返回 503 `TRAINING_DISABLED`；本接口本身仍 200（§3.7） |
| `tasks` | str[] | 支持的任务类型，当前为 `["detect", "segment"]`；提交的任务类型必须在此集合内 |
| `allow_weight_download` | bool | 权重缺失时是否允许服务端按需下载；**与 `weights_ready` 正交**。客户端可提交判定 = `weights_ready[file] == true` **或** `allow_weight_download == true` |
| `allow_auto_batch` | bool | 是否允许提交 `batch=-1` 或比例值；为 `false` 时 `param_schema.batch` 退化为 `{"type": "int", "min": 1, "max": 128}` |
| `oom_retry` | object | `{"enabled": bool, "max_retries": int}`（默认 `true` / `2`）；表示是否把 OOM 交给训练侧降 batch 重试。**不改变**参数校验与显存账本口径 |
| `model_families` | object | 家族 → `{min_ultralytics, available, unavailable_reason, weights, weights_ready, presets}`，**每个家族的键集形状完全一致**（不得缺键）。`available` = 当前环境 ultralytics 版本是否不低于该家族 `min_ultralytics`；`unavailable_reason` 为 `null` 或不可用时的错误码字符串（当前只会是 `MODEL_FAMILY_UNSUPPORTED`）。`weights_ready[file]` **只有一个含义**：该文件是否已存在于 `<work_dir>/weights/`——不表示是否允许下载，也不表示不可提交 |
| `param_schema` | object | **恰好覆盖 §3.8.2 的全部 23 个客户端可传参数**，是客户端本地校验的唯一依据；区间语义见 §3.1。`optimizer.values` 是**全部家族 preset 的并集**（与家族可用性无关），客户端必须先按所选家族的 `model_families[family].presets` 过滤再展示 |
| `optimizer_presets` | object | preset 名 → `{optimizer, lr0, momentum, weight_decay, warmup_bias_lr}`（对象形状即超参白名单，示例只列 `yolo11-sgd`） |
| `preset_policy` | object | `{"type": "auto", "threshold": null, "default_preset": {}}`；`type ∈ {auto, iterations_threshold}`（缺省 ⇒ 数据类默认 `auto`，与出货默认一致；显式 `null` 属非法取值，§4.4.2）；`type:"auto"` 时 `threshold=null`、`default_preset={}`——键集与形状不变；选择算法与兜底顺序见 §3.8.4 |
| `devices[]` | object[] | 各卡显存账本快照：`device_index` / `name` / `total_mb` / `free_mb` / `reserved_mb` / `in_flight_estimate_mb`（**仅 `preparing` 任务估算之和**）/ `running_estimate_mb`（**只读诊断字段，不参与账本公式**）/ `available_mb`。**账本公式（唯一）**：`available_mb = free_mb − reserved_mb − in_flight_estimate_mb`（`running` 任务的实际占用已包含在 `free_mb` 里，**不得再减一次**）。自洽性：`running_estimate_mb > 0` ⟺ `queue.running > 0`；`in_flight_estimate_mb > 0` ⟺ 存在 `preparing` 任务。跨卡判定一律用 §3.11 的 `min_device_total_mb` |
| `queue` | object | `{"queued", "running", "max_concurrent_jobs", "max_concurrent_per_device"}`；与 `health.queue` 同源同字段 |
| `cancel_grace_seconds` | int | 取消时 SIGTERM 后等待再 SIGKILL 的秒数（默认 **15**）；客户端用它显示「正在停止…（最长 N 秒）」，**不得硬编码**（§3.11） |
| `vram_table` | object | 显存估算表与标定产物（子字段见下） |
| `calibration` | object | 启动期标定状态（子字段见下）；与 `health.calibration` **同源同字段** |
| `training_env` | object | 环境指纹，**只有四个字段**：`ultralytics` / `torch` / `cuda` / `python`。不含 `verified` / `verified_at`、不含嵌套 `warnings`，也不构成任何门禁 |
| `warnings` | object[] | 启动自检与服务状态的非阻断告警，`{"code", "message", "details"}`；**code 枚举见下表**。文案**不写死计数**，条数与 `details` 数组元素数一致 |

**`vram_table` 子字段**：

| 子字段 | 说明 |
| --- | --- |
| `task_factor` | 任务类型系数（`detect` / `segment`），用于估算折算 |
| `auto_batch` | `{"assumed": 16, "ratio": 0.60}`：auto-batch 的假定值与比例上限参数 |
| `auto_loaded` | 是否加载到本机 auto 产物（`configs/custom/vram_table.auto.yaml`） |
| `fingerprint_matched` | auto 产物指纹是否与当前环境一致；不一致 = 需重新标定 |
| `calibrated_at` | 标定时刻（ISO8601 UTC），未标定为 `null` |
| `fingerprint` | 环境指纹原样下发（`gpu` / `gpu_total_mb[]` / `driver` / `cuda` / `torch` / `ultralytics` / `bench_params` / `dataset`），仅供展示与排障。`ultralytics` 与上文同形：有家族后端时是 `{"__host__": …}` 映射，无家族后端时是单一字符串 |
| `entries[]` | 每行 `{"model", "task", "baseline_mb", "per_image_mb", "process_overhead_mb", "max_batch", "points[]", "fit", "source", "calibration_at"}`。**`source` 是必答字段**：`auto` = 本机启动期标定的实测值（`points` / `max_batch` / `calibration_at` 均非空）；`manual` = 手工基线的工程起点值；`default` = 内置默认值；后两者的 `max_batch` 与 `points` 可为 `null`。客户端必须据此区分「本机已实测」与「仍是起点值」 |
| `entries[].max_batch` | 该 `(model, task)` 在本机的**实测 batch 上限**：提交超过它的整数 `batch` 时自动收敛并在提交响应 `warnings[]` 记 `CONVERGED_TO_DEVICE_MAX`；auto 取值的 `batch_assumed` 也按它封顶。非 `auto` 来源时为 `null`（此时不收敛） |
| `sources` | `{"auto", "manual", "default"}` 三个来源的组合计数，三者之和 = 白名单组合数（默认部署为 10 模型 × 2 任务 = 20） |
| `unschedulable[]` | `[{"model", "task", "reason"}]`，**不可调度**的白名单组合；`reason` **四值**：`VRAM_TABLE_INCOMPLETE`（任何一层表都无本行且无同模型其他 task 行）/ `VRAM_CALIBRATION_FAILED`（本机标定全部点位 OOM）/ `MODEL_FAMILY_UNSUPPORTED`（家族不可用，提交时 422 同名码）/ `WEIGHT_NOT_AVAILABLE`（`allow_weight_download: false` 且权重缺失，提交时 422 同名码）。前两类的提交都返回 422 `VRAM_ESTIMATE_UNAVAILABLE` |

**`calibration` 子字段**：

| 子字段 | 说明 |
| --- | --- |
| `required` | 配置 `require_vram_calibration` 的**生效值**；**无可用 CUDA 设备时为 `false`**（存在可用设备时才要求标定） |
| `deferred` / `deferred_ready` / `deferred_reason` | 本次是否因存在存活训练任务而**延期标定**（服务已以手工基线 / 内置默认启动）；`deferred_ready` = defer 期间条件复查结果（队列为空、无 `preparing` / `running` 任务且无存活训练进程 ⇒ 可重启补标定），**置位不改 `deferred` 本身**；`deferred_reason` 取 `"live_training_jobs"` 或 `null` |
| `conflict_policy` | 生效的 `calibration_conflict_policy` 取值（默认 `defer`） |
| `calibrated_at` / `auto_loaded` / `fingerprint_matched` | 与 `vram_table` 同源同值 |
| `skipped[]` | 本轮**被可用性过滤逐组合跳过**的组合 `[{"model","task","reason"}]`，`reason` ∈ `MODEL_FAMILY_UNSUPPORTED` / `WEIGHT_NOT_AVAILABLE`；**无可用设备时的整轮跳过不进本数组** |
| `skipped_reason` | 整轮跳过的原因，当前只有 `"no_device"`；未整轮跳过时为 `null` |
| `failed[]` | 真正跑了但**全部点位 OOM** 的组合。**不存在** `vram_table.calibration.failed[]` 这条路径——`failed[]` 只属于顶层 `calibration`。`skipped[]` 是**过程记录**（本轮为何没测），`unschedulable[]` 是**可调度性结论**；家族不可用 / 权重缺失的组合**两处都会出现**（同源同 `reason`） |

**`warnings[]` 的 code 枚举（7 个）**：

| code | 语义 | `details` |
| --- | --- | --- |
| `WEIGHTS_MISSING` | `allow_weight_download: false` 且 `weights/` 为空 | — |
| `VRAM_TABLE_INCOMPLETE` | 存在不可调度的白名单组合 | `unschedulable[]` |
| `VRAM_CALIBRATION_FAILED` | 本次启动期标定中有组合全部点位 OOM（这些组合不可调度）；**信息性**、不进 `needs_attention` | `failed[]` |
| `VRAM_CALIBRATION_SKIPPED` | `require_vram_calibration: false`（或 auto 标定被关闭）而跳过标定，退回手工基线 / 内置默认值；所有组合 `source` 为 `manual` / `default` | — |
| `VRAM_CALIBRATION_DEFERRED` | 需要标定但存在存活训练任务且 `calibration_conflict_policy: defer` ⇒ 本次延期（`calibration.deferred=true`、`vram_table.auto_loaded=false`）；**信息性**、不进 `needs_attention` | — |
| `BLOB_MATERIALIZE_DEGRADED` | `blob_materialize: hardlink` 但 `blobs` 与 `work_dir` 不同文件系统，自动降级为 `copy` | — |
| `OOM_RETRY_UNAVAILABLE` | 环境 ultralytics < 8.4.13：无原生「CUDA OOM 自动降 batch 重试」能力，**改由 runner 自行捕获 OOM 并降 batch 重跑**（OOM 仍不直接判失败，只是重试耗时更长）；**信息性** | — |

**异构多卡（本规格的唯一口径）**：各卡 `total_mb` 不一致时**不引入专门告警码**，也不再新增 `warnings` 条目；跨卡判定一律按**保守口径** `min_device_total_mb = min(各卡 total_mb)` 计算（§3.11）。客户端的可展示信息来自 `devices[].total_mb` 本身。

### §3.7 `health` 契约与四态鉴权表

`GET /custom/train/health` 是**独立新增的训练路由**，语义冻结为**受鉴权、但不受 training-enabled gate**：`enabled=false` 时本接口仍返回 **200 + `enabled=false`**（不是 503），且**字段仍完整**；只有其它 `/custom/train/*` 返回 503 `TRAINING_DISABLED`。

```json
{
  "success": true,
  "data": {
    "enabled": true,
    "server_version": "0.0.12",
    "time": "2026-01-01T10:30:00Z",
    "queue": {"queued": 2, "running": 1, "max_concurrent_jobs": 2, "max_concurrent_per_device": 1},
    "jobs": {"queued": 2, "running": 1, "needs_attention": 0},
    "devices": [
      {"device_index": 0, "name": "NVIDIA GeForce RTX 4090", "total_mb": 24564, "free_mb": 21000,
       "reserved_mb": 1024, "in_flight_estimate_mb": 0, "running_estimate_mb": 5200,
       "available_mb": 19976, "running_jobs": 1}
    ],
    "work_dir": {"path": "/data/xanylabeling/training", "free_gb": 412.5, "total_gb": 1000.0, "writable": true},
    "training_env": {"ultralytics": "8.4.84", "torch": "2.6.0+cu124", "cuda": "12.4", "python": "3.12.4"},
    "calibration": {"required": true, "deferred": false, "deferred_ready": false, "deferred_reason": null,
                    "conflict_policy": "defer", "calibrated_at": "2026-01-01T09:40:12Z", "auto_loaded": true,
                    "fingerprint_matched": true,
                    "skipped": [],
                    "skipped_reason": null,
                    "failed": [{"model": "yolo26n", "task": "detect", "reason": "VRAM_CALIBRATION_FAILED"}]},
    "weights": {"cached": 14, "missing": 6, "allow_weight_download": true},
    "blobs": {"count": 12840, "bytes": 81234567890, "unreferenced": 120, "materialize": "hardlink"},
    "warnings": [{"code": "VRAM_CALIBRATION_FAILED", "message": "部分 (model, task) 组合在本次标定中全部点位 OOM，已标记为不可调度（见 details.failed）",
                  "details": {"failed": [{"model": "yolo26n", "task": "detect", "reason": "all_points_oom"}]}}]
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `enabled` | bool | 训练子系统开关；`false` 时**除本接口外**的 `/custom/train/*` 返回 503 `TRAINING_DISABLED`，而本接口本身**始终 200**（受鉴权、不受 training-enabled gate），只是该字段为 `false` |
| `server_version` | str | 服务端版本，与 `capabilities.server_version` 同源 |
| `time` | str | 服务端当前时间（ISO8601 UTC）；客户端可据此校正本地时钟偏差 |
| `queue` | object | 队列深度与并发配置，字段同 `capabilities.queue` |
| `jobs` | object | 任务计数：`queued` / `running` / `needs_attention`（当前处于「需人工介入」的终态任务数） |
| `devices[]` | object[] | 各卡显存账本快照（口径同 `capabilities.devices[]`），另含 `running_jobs`（该卡 `running` 任务数） |
| `work_dir` | object | `work_dir` 可用空间：`path` / `free_gb` / `total_gb` / `writable`（不可写时启动即失败；此处只做实时反映） |
| `training_env` | object | 训练环境指纹（与 `capabilities.training_env` 同源），**只有四个字段**：`ultralytics` / `torch` / `cuda` / `python` |
| `calibration` | object | 标定状态（与 `capabilities.calibration` **同源同字段**）。服务端能响应本接口即说明「标定已成功结束」**或**「因存在存活训练任务而延期（`deferred=true`，服务已以兜底层启动）」**或**「无可用设备而整轮跳过（`skipped_reason="no_device"`）」——标定期间端口未监听，客户端只会**连接失败** |
| `weights` | object | 权重就绪摘要：`cached`（已缓存文件数）/ `missing`（白名单中缺失数）/ `allow_weight_download` |
| `blobs` | object | blob 仓库就绪摘要：`count` / `bytes` / `unreferenced` / `materialize`（实际生效的物化方式，与 `BLOB_MATERIALIZE_DEGRADED` 呼应） |
| `warnings` | object[] | 与 `capabilities.warnings` **同源同 code 枚举**（§3.6），便于客户端只用一个接口就显示状态 |

**四态鉴权表（逐字保留）**：

| `security.api_key_enabled` | `training.enabled` | `GET /custom/train/health`（无 `Token`） | `GET /custom/train/health`（带正确 `Token`） | 其它 `/custom/train/*` | 说明 |
| --- | --- | --- | --- | --- | --- |
| `true` | `true` | **401 `UNAUTHORIZED`** | **200** + `enabled=true` | 正常（同样要求 `Token`） | 生产推荐配置（启动自检 fail-closed 通过） |
| `true` | `false` | **401 `UNAUTHORIZED`**（鉴权先于业务 gate） | **200** + `enabled=false`（**不是** 503） | **503 `TRAINING_DISABLED`** | 训练关闭时 health 仍可达、字段仍完整 |
| `false` | `true` | **200**（整个服务无鉴权） | **200** + `enabled=true` | 正常（无鉴权） | 上游默认值；服务默认会因 fail-closed 自检**拒绝启动**，本行只在逃生开关 `allow_no_auth=true` + `server.host` 回环时可达 |
| `false` | `false` | **200** + `enabled=false` | **200** + `enabled=false` | **503 `TRAINING_DISABLED`** | 无鉴权 + 训练关闭的对照行 |

**四条要点**：

- **鉴权由上游中间件决定、不由本接口决定**：「health 受鉴权」的准确含义是「它**不在**上游 `/health` 的免鉴权白名单内」——上游中间件只精确放行上游 `/health`。`api_key_enabled=false` 时整个服务（含全部 `/custom/train/*`）都没有鉴权，因此无 `Token` 也返回 200；**不是**「即使 `api_key_enabled=false` 也强制 401」。
- **逃生开关 `allow_no_auth` 只作用于启动自检**（放宽 fail-closed 的启动条件），**不进入运行期鉴权路径**：上表四行与它无关。
- **训练关闭时字段仍完整**：`enabled=false` 时仍返回全部 12 个字段。
- **上游 `GET /health` 一字不改**：路径、路由对象与响应字段（`status` / `models_loaded` / `timestamp`）保持上游原样，**不含任何训练字段**；**客户端不得用上游 `/health` 做连接测试**，连接测试与「可训练状态」显示一律用本接口（客户端篇 §5.1）。最小暴露：只给计数、容量与指纹，不含数据集名 / 路径 / 任务标题等敏感内容。

### §3.8 参数面

#### §3.8.1 服务端强制注入、客户端不可覆盖（6 个）

请求体里出现下列任一参数 ⇒ 422 `PARAM_NOT_OVERRIDABLE`（`details.fields[]`）：

| 参数 | 服务端取值 |
| --- | --- |
| `model` | 由 `model_family` + `model` 白名单解析成本地权重路径 |
| `data` | `jobs/<job_id>/data.yaml`（服务端生成） |
| `device` | 调度器分配的 `device_index`（字符串形式 `"0"` / `"0,1"`）；**`queued` 阶段为 `null`**，`preparing` 选卡后写入 |
| `project` / `name` | `jobs/<job_id>/run` / `train`；固定 `exist_ok=True` |
| `resume` | 由恢复语义决定（`resume=True` 仅当有 `weights/last.pt`） |

#### §3.8.2 客户端可传参数（23 个，范围权威定义）

越界 → 422 `PARAM_OUT_OF_RANGE`（`details.field` 指明字段）。**区间语义（§3.1，唯一定义）**：`{min, exclusive_min: bool, max, exclusive_max: bool}`——`min` / `max` 为**闭**区间边界，`exclusive_min: true` 表示下界开、`exclusive_max: true` 表示上界开；**没有**「`exclusive_min` 直接给边界数值」的写法；`warn_above` 只触发告警、不拒绝（§3.8.3）。每个字段的类型 / 范围**以本表为权威定义**，`capabilities.param_schema`（§3.6）必须覆盖本表全部 23 项且逐项一致，客户端一律按 `param_schema` 做本地校验。

**「默认」列只描述取值来源**：本规格不为这 23 项定义数值默认值——客户端缺省不传的键由服务端按 **preset**（若所选 preset 定义了同名键）与训练侧内置默认值补齐。

| 参数 | 类型 | 范围 / 取值 | 默认 |
| --- | --- | --- | --- |
| `epochs` | int | 1 ≤ x ≤ 1000 | 不注入（训练侧默认生效） |
| `batch` | int 或 auto | 整数 1 ≤ x ≤ 128；`allow_auto_batch: true` 时**另接受** `-1`（按 60% 显存自动）或 `0 < r < 1`（按比例自动）。**超过 128 一律 422**；1–128 内但超过该组合 `max_batch` 时**自动收敛**并在响应 `warnings[]` 记 `CONVERGED_TO_DEVICE_MAX` | 不注入；`allow_auto_batch: false` 时只接受整数 |
| `imgsz` | enum | ∈ {320, 416, 512, 640, 768, 896, 1024, 1280, 1536} | 不注入 |
| `workers` | int | 0 ≤ x ≤ 16 | 不注入 |
| `optimizer` | preset 名或 `auto` | 取值域 = `capabilities.param_schema.optimizer.values`（= 各家族 `model_families[family].presets` 的并集，含 `auto`；`capabilities.optimizer_presets` 只映射真实 preset 名 → 超参对象，`auto` 不是它的键，§3.6）；显式传入的值必须属于所选家族；`auto` 是允许的显式取值，**裸优化器名不接受**（`SGD` / `AdamW` / `MuSGD` …）⇒ 422 `OPTIMIZER_UNSUPPORTED` | 缺省 → 按 `preset_policy` 选；出货策略 `type:"auto"` ⇒ 等价于显式 `auto`（§3.8.4） |
| `lr0` | float | 0 < lr0 ≤ 0.05（`warn_above: 0.001` 见 §3.8.3） | preset 定义该键时以 preset 为准 |
| `lrf` | float | 0 < lrf ≤ 1 | 不注入 |
| `momentum` | float | 0 ≤ x ≤ 1 | preset 定义该键时以 preset 为准 |
| `weight_decay` | float | 0 ≤ x ≤ 1 | preset 定义该键时以 preset 为准 |
| `warmup_epochs` | float | ≥ 0 | **不注入**：客户端未传时由训练侧默认值生效（preset 对象形状不含本键 ⇒ 本键**永远不由 preset 提供**，§3.6） |
| `warmup_momentum` | float | ≥ 0 | **不注入**：客户端未传时由训练侧默认值生效（preset 对象形状不含本键 ⇒ 本键**永远不由 preset 提供**，§3.6） |
| `warmup_bias_lr` | float | ≥ 0 | preset 定义该键时以 preset 为准 |
| `cos_lr` | bool | true / false | 不注入 |
| `amp` | bool | true / false | 不注入 |
| `cache` | bool | true / false | 不注入 |
| `rect` | bool | true / false | 不注入 |
| `single_cls` | bool | true / false | 不注入 |
| `patience` | int | 0 ≤ x ≤ 1000 | 不注入 |
| `close_mosaic` | int | 0 ≤ x ≤ 1000 | 不注入 |
| `save_period` | int | 0 ≤ x ≤ 1000（**`-1` 不在取值域内**：它表示「不保存周期快照」，不得原样透传，应转为 `0` 或不传） | 不注入 |
| `fraction` | float | 0 < x ≤ 1 | 不注入 |
| `seed` | int | 任意整数 | 不注入 |
| `dropout` | float | 0 ≤ x ≤ 1 | 不注入 |

#### §3.8.3 AdamW 与 `lr0` 的联动校验（冻结）

| 规则 | 行为 |
| --- | --- |
| preset 的优化器为 `AdamW` / `Adam`（即 `yolo11-adamw` 或等价显式值）且 `lr0 > 0.001` | 返回提交响应 `warnings[]` 的 `ADAMW_LR0_HIGH`，**任务仍可提交** |
| 任意 preset 下 `lr0 > 0.05` | 422 拒绝（`PARAM_OUT_OF_RANGE`） |
| `optimizer=auto` **且**同送 `lr0` / `lrf` / `momentum` / `weight_decay` / `warmup_epochs` / `warmup_momentum` / `warmup_bias_lr` 中**任一**键 | 422 拒绝（`OPTIMIZER_UNSUPPORTED`，`details.optimizer=auto`、`details.fields[]` 列出同送的键）。理由：`auto` 表示**完全交给训练侧**——ultralytics 的 auto 分支会覆盖 `optimizer` / `lr0` / `momentum` 并把 `warmup_bias_lr` 置 0，其余四个键仍会生效；只让一半生效会造成歧义，因此**一律拒绝**而不是半生效。这七项就是完整冲突键集，`auto` 之外的取值不受影响 |

`ADAMW_LR0_HIGH` **不适用于** `MuSGD`（`yolo26-default`），**也不适用于** `optimizer=auto`（auto 与 `lr0` 同送即 422，见上）。

#### §3.8.4 preset 家族与默认策略

```text
train_images = 数据集 train split 图片数
batch_for_estimate = resolved_params.batch_assumed   # batch 为 auto 时用假定 batch
total_iterations = epochs * ceil(train_images / batch_for_estimate)
family = request.model_family                          # 家族过滤在第一步就生效

if preset_policy.type == "auto":                         # 出货策略默认值
    preset = "auto" if "auto" in model_families[family].presets else None
else:                                                    # iterations_threshold
    if total_iterations <= preset_policy.threshold:      # 默认 10000
        preset = 该家族 presets 中的 adamw 类 preset（如 yolo11-adamw）
    else:
        preset = 该家族 presets 中的 sgd 类 preset（如 yolo11-sgd）
    preset = preset or preset_policy.default_preset[family] or 该家族 presets 的首项
    assert preset in model_families[family].presets      # 不变量：选出的 preset 必属于所选家族
# auto 不是 preset：由策略（type:"auto"）或客户端给出的 auto 都原样透传，
# 不进 preset 候选，也不进 iterations_threshold 的 fallback 链；家族未在
# presets 里声明 auto ⇒ preset 为 None ⇒ 422 OPTIMIZER_UNSUPPORTED
# （details.allowed=[]）——**不回落**到 preset 注入路径，这是有意为之。
```

| 取值来源 | `resolved_params.optimizer` | `resolved_params.optimizer_preset` | `resolved_params.optimizer_source` |
| --- | --- | --- | --- |
| 客户端显式传入 preset 名 | 该 preset 的裸优化器名（如 `AdamW`） | `null`（原始取值在请求体 `params.optimizer` 中） | `client` |
| 客户端显式传入 `auto` | `auto`（原样透传，服务端不解析成裸名） | `null` | `auto` |
| 服务端按 `preset_policy` 选出 | 选中 preset 的裸优化器名（如 `SGD`） | 选中的 preset 名（如 `yolo11-sgd`） | `preset` |
| 服务端策略 `type:"auto"` 命中 | `auto` | `null` | `server_auto` |

- **默认 preset 按家族映射（仅 `type:"iterations_threshold"` 生效）**：`preset_policy.default_preset` = `{"yolo11": "yolo11-sgd", "yolo26": "yolo26-default"}`；兜底顺序为「家族映射 → 该家族 `presets` 的首项」，**绝不回退全局默认**（全局回退会把 yolo26 请求变成 `yolo11-sgd`，跨家族、服务端会拒绝）。客户端下拉**默认停在第 0 项**（`data=None`，文案「（服务端按默认策略选择）」），**不计为显式选择**、请求体**不含** `optimizer`；用户选 `auto` 时发 `"auto"`、选具体 preset 时发该名（§5.2.2）。**第 0 项文案有两套模板**：capabilities 回显了该家族的 `default_preset` 时渲染「（由服务端默认策略决定；家族默认 X）」，否则（含出货 `type:"auto"`、回显 `default_preset: {}` 时）渲染短文案「（服务端按默认策略选择）」；两套都表示**不发** `optimizer`、由服务端策略决定，只是提示详略不同。
- **`auto` 的取值**：`resolved_params.optimizer` 的取值域是**四值** `SGD` / `AdamW` / `MuSGD` / `auto`——前三个来自 preset 的裸优化器名，`auto` 是取值哨兵，**可由客户端显式选择，也可由出货策略给出**，两者都不解析成裸名；`auto` 不注入任何超参，且与 `lr0` / `lrf` / `momentum` / `weight_decay` / `warmup_epochs` / `warmup_momentum` / `warmup_bias_lr` 同送即 422（§3.8.2 / §3.8.3）。
- **仅当策略为 `iterations_threshold` 时**才注入 preset 裸名与超参：客户端未显式传 `optimizer` 时，`resolved_params.optimizer` 是 preset 的**裸优化器名**（`SGD` / `AdamW` / `MuSGD`），最终写入训练侧参数的也是该裸名与 preset 超参；策略为 `type:"auto"` 或客户端显式传 `auto` 时服务端**不选 preset、不注入超参**，`optimizer` 原样透传 `auto`，来源分别记 `server_auto` / `auto`（§3.8.5）。

#### §3.8.5 `resolved_params`（8 个权威字段）

服务端把最终生效的参数快照写入 `jobs/<job_id>/request.json` 的 `resolved_params`，并通过 `GET /jobs/{job_id}` 与 `GET /jobs` 返回（客户端详情页展示「服务端实际使用参数」）。

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `optimizer` | `SGD` / `AdamW` / `MuSGD` / `auto` | 服务端最终传给训练侧的优化器取值：选用 preset 时是该 preset 的**裸优化器名**；客户端显式传 `auto`、或服务端策略 `type:"auto"` 命中时是哨兵 `auto`（表示完全交给训练侧自选，服务端不注入超参） |
| `optimizer_preset` | preset 名或 `null` | 由 `preset_policy`（`iterations_threshold`）选出时为该 preset 名；**客户端显式指定优化器、或服务端策略给出 `auto` 时为 `null`** |
| `optimizer_source` | `preset` / `client` / `auto` / `server_auto` | `preset` = 服务端按 `preset_policy`（`iterations_threshold`）选出 preset；`client` = 客户端显式传入 preset 名；`auto` = 客户端显式传入 `auto`；`server_auto` = 服务端策略 `type:"auto"` 命中——后两者服务端都不注入超参 |
| `batch` | int 或 number | **服务端生效值（收敛后）**：整数超过 `max_batch` 时记收敛后的值；客户端提交 auto（`-1` / 比例值 `r`）时**原样保留 auto 取值**（实际 batch 由训练侧在运行期决定）。**传给训练侧的值就是这个字段** |
| `requested_batch` | int 或 number | **客户端原值**（未收敛前的请求体取值：整数 / `-1` / 比例值 `r`）；与 `batch` 分离后详情页可同时展示「请求值」与「生效值」 |
| `batch_assumed` | int | 提交预检与显存账本使用的**假定 batch**：`batch` 为整数时等于收敛后的生效值；auto 时按 §4.2.5 ④ 的递减穷举折算出 `batch_cap_by_ratio`（比例预算下最大可行整数）与 `batch_cap_by_table`（= 本机实测 `max_batch`）两个 cap——**仅当两个 cap 都 ≥ 1 时**取 `min(batch_cap_by_ratio, batch_cap_by_table)`（**不得超过任一 cap**；此时 `max(1, ·)` 是空操作）；**任一有效 cap < 1 ⇒ 422 `INSUFFICIENT_VRAM`**（`details.reason` = `ratio_cap_below_one` / `table_cap_below_one`，见 §3.3），**不得钳位到 1**（钳位会直接突破比例预算并绕过提交期门禁）。权威算法见 §4.2.5 |
| `device` | str 或 `null` | 调度器分配的 `device_index` 的字符串形式（`"0"` / `"0,1"`）；**未选卡前必须为 `null`**——`queued` 阶段（含提交响应）为 `null`，只有 `preparing` 选定卡后才写入；与 job 对象的 `device_index` 同源 |
| `oom_retry_max` | int | 配置 `oom_retry.max_retries` 的落库值（默认 `2`），**唯一用途**是把 OOM 降 batch 的重试上限下发给训练侧；达到上限即放弃重试、写 `done(status=failed)` |

- `resolved_params` 一经写入即固定；手动恢复沿用原快照（参数未变则不重算）。
- 同一对象里还包含服务端注入项（**6 个**：`model` / `data` / `device` / `project` / `name` / `resume`；`exist_ok` 是训练侧落地参数，不在可注入清单内）与客户端参数（`epochs` / `imgsz` / `lr0` …）的生效值；**上表 8 项是语义最易漂移、必须逐字实现的字段**。

### §3.9 warnings 三通道归属

**三个 `warnings[]` 通道互不混用**：同一个 code 只属于一个通道，客户端按**通道**分别处理（提交提示 / 上传提示 / 服务状态提示），不得跨通道搬运。

| 通道 | 出现位置 | code 集合 |
| --- | --- | --- |
| 提交响应 `warnings[]` | `POST /jobs` 的 `data.warnings[]` | `ADAMW_LR0_HIGH`、`CONVERGED_TO_DEVICE_MAX` |
| upload 响应 `warnings[]` | `POST /datasets/upload` 的 `data.warnings[]` | `BACKGROUND_IMAGES`、`ORPHAN_LABELS`、`WARN_COUNT_MISMATCH`、`SPLIT_CLASS_MISSING_VAL` |
| 服务状态 `warnings[]` | `capabilities.warnings`（`health.warnings` 与之**同源同 code 枚举**） | `WEIGHTS_MISSING`、`VRAM_TABLE_INCOMPLETE`、`VRAM_CALIBRATION_FAILED`、`VRAM_CALIBRATION_SKIPPED`、`VRAM_CALIBRATION_DEFERRED`、`BLOB_MATERIALIZE_DEGRADED`、`OOM_RETRY_UNAVAILABLE`（共 **7** 个，逐条语义见 §3.6） |

**通道 A（提交响应）**：

| code | 语义 | `details` |
| --- | --- | --- |
| `ADAMW_LR0_HIGH` | preset 的优化器为 AdamW / Adam 且 `lr0 > 0.001`（§3.8.3）；**任务仍可提交** | — |
| `CONVERGED_TO_DEVICE_MAX` | 提交的整数 `batch` 超过该组合的标定上限 `max_batch`，已自动收敛 | `requested` / `applied` / `max_batch`（示例另带 `model` / `task`） |

**通道 B（upload 响应）**：

| code | 语义 | 附加键 |
| --- | --- | --- |
| `BACKGROUND_IMAGES` | 标签为空（0 字节）的图片，已作为背景图训练；计入 `counts.background` | `files[]`（示例只列部分） |
| `ORPHAN_LABELS` | `labels/` 下未被 manifest 引用的标签文件：忽略、不阻断整包 | `files[]` |
| `WARN_COUNT_MISMATCH` | manifest 上报的 `split_stats` 与 `counts` 计数不自洽，**②③④ 之一成立即记**：② `Σ_c split_stats[c].train < counts.train` 或 `Σ_c split_stats[c].val < counts.val`；③ `split_stats` 出现不在 `classes` 里的键；④ `counts.background > Σ_c (split_stats[c].train + split_stats[c].val)`。① `counts.total ≠ counts.train + counts.val` 是服务端自身派生实现的**断言式自检**，**不**记本告警 | — |
| `SPLIT_CLASS_MISSING_VAL` | 某类别满足「`train + val ≥ 1` 且 `val == 0`」（该类在 val 中缺失），**非阻断** | `details.classes[]` |

**通道 C（服务状态）**：7 个 code 的逐条语义与 `details` 见 §3.6；它们都是**信息性**告警，**不阻断**提交，也**不**进 `needs_attention`（唯一例外是与产物可疑 / 恢复异常 / 重试耗尽相关的三值，见 §3.4.4）。

**三条通用形状约定**：

- 条目形状统一为 `{"code", "message", ...}`：通道 A / C 用 `details` 承载结构化信息，通道 B 可用 `files[]` 或 `details`。
- `message` 文案**不写死计数**，条数与 `details` / `files` 数组的实际元素数一致，客户端按数组长度展示。
- `capabilities.training_env` 内**不含** `warnings`：环境相关告警一律以 code 形式走通道 C。

### §3.10 产物下载安全与 `file_id`

#### §3.10.1 `file_id` 生成规则（写死）

```text
file_id = "f_" + sha256(相对 artifacts/<job_id>/ 的 POSIX 路径字符串)[:8]
```

| 规则 | 约定 |
| --- | --- |
| 哈希对象 | **相对路径字符串**（POSIX 分隔符，如 `weights/best.pt`、`partial/results.csv`）——**与文件内容无关** |
| 内容哈希的用途 | 清单里的 `sha256` 字段（文件内容哈希）**只用于 `ETag`**（§3.10.6），**不参与** `file_id` 计算 |
| 不可变 | 同一路径 ⇒ 同一 `file_id`：**内容变化、`mtime` / `size` 变化、重新生成清单都不改 id**；路径变化（例：`partial/weights/last.pt` → `weights/last.pt`）⇒ 新的 `file_id` |
| 路径唯一 ≠ 哈希唯一 | 8 hex 只是短标识，同一 job 内不同路径的 8 hex 前缀**可能相同**，按 §3.10.2 扩展 |
| 客户端 | 把 `file_id` 当**不透明标识**，**不解析长度与结构**、不自行拼接、不假定格式 |

#### §3.10.2 碰撞扩展与确定性

| 项 | 约定 |
| --- | --- |
| 判定范围 | **只在同一 job 的清单内**判定碰撞；跨 job 不比较（不同 job 的条目允许共用同一 8 hex 前缀） |
| 扩展规则 | 把清单内条目的 8 hex 前缀分组：组内只有 1 条 ⇒ **保持 8 hex**；组内 ≥ 2 条 ⇒ 该组**全部**条目统一扩展到 **12 hex**；若 12 hex 在该组内仍有重复，继续以 **4 hex 为步长**扩展（16 / 20 / … / 64 位全长），直到组内互不相同。**只有参与碰撞的条目被扩展**，未碰撞的条目保持 8 hex |
| 下发 | 服务端在清单里**如实下发**扩展后的 `file_id` |
| 确定性 | `file_id` 是「路径 → ID」的**纯函数**：分组与扩展只依赖「该 job 清单的路径集合」，与遍历顺序、生成时刻、文件 `mtime` / 大小 / 内容无关 ⇒ **同一清单重复生成必得同一组 `file_id`** |

#### §3.10.3 清单即白名单（两种失败互斥）

下载只允许 **#13 清单里出现过的 `file_id`**：

| 情形 | 结果 |
| --- | --- |
| ① `file_id` **不在清单内**（含任意非法值、目录穿越串） | **400 `VALIDATION_FAILED`**（`details.field=file_id`） |
| ② `file_id` **在清单内**、但解析后的目标不存在或不是普通文件（目录 / 符号链接 / 设备文件 / FIFO） | **404 `ARTIFACT_NOT_FOUND`**（`details.file_id`，**不回显文件系统路径**） |

两者**互斥**、不共用状态码。

#### §3.10.4 路径解析（不接受任何来自请求的路径字符串）

路由参数**只有 `file_id`**；服务端从清单里取回 `path` 后依次做：

| 步骤 | 规则 |
| --- | --- |
| ① 规范化 | POSIX 分隔符；**拒绝**含 `..`、反斜杠、盘符前缀、NUL 的条目 |
| ② 归属校验 | 解析后的绝对路径必须落在 `artifacts/<job_id>/` 之内（`os.path.commonpath` 校验） |
| ③ 逐段打开 | **逐段打开 + 拒绝符号链接**（`O_NOFOLLOW` / `lstat` 判定）；只允许**普通文件**：目录、符号链接、设备文件、FIFO 一律不下载 |

⇒ **不存在**「用户输入拼进路径」的目录穿越面。

#### §3.10.5 TOCTOU

打开句柄后 **`fstat` 校验仍是普通文件**（`size` / `mtime` 从已打开句柄取），随后只从该句柄读；**不使用**「先 `stat` 再按路径打开」的两段式。下载期间产物被 pin（TTL 清理器跳过正在被下载的 job）。

#### §3.10.6 `Range` / `ETag` / `If-Range`

| 项 | 约定 |
| --- | --- |
| `Range` | 支持：命中 ⇒ **206** + `Content-Range`；不可满足 ⇒ **416**；无 `Range` ⇒ 200 全量 |
| `ETag` 字面格式 | `"<file_id>.<sha256>"`——**强校验**（**不带** `W/` 前缀）、**带 ASCII 双引号**、两段之间用**单个半角句点**连接；`<file_id>` 与 `<sha256>` **逐字取自该条目的清单** |
| `sha256` 取值时机（唯一口径） | `ETag` 与 `If-Range` 比对使用的 `sha256` 必须是**当次下载请求**对**已打开句柄 / 重新扫描产物目录**得到的**最新**值；**进程内缓存的清单不得让 `ETag` 滞后于文件内容**（否则「内容已变 + 携带变化前的旧 `ETag`」会错误命中 206） |
| `If-Range` | 与**当次请求算出的同一字面串**做**逐字**比较：完全相等 ⇒ **206** + `Content-Range`；不相等（含格式不符 / 无法解析）⇒ 按失效处理，返回 **200** 全量（**不是** 206、也不是 412） |
| 客户端 | **不得**自行拼接 `ETag`、**不得**假定格式：只把响应头的 `ETag` 逐字回存，续传时把该值逐字作为 `If-Range` 回传（客户端篇 §5.6） |

#### §3.10.7 打包下载（#15）

只从 #13 清单枚举条目（同样的符号链接 / 普通文件 / 根目录约束），**不遍历文件系统**；zip 内保留与清单一致的 `partial/` 目录结构。

#### §3.10.8 响应包装与 `include_partial`

| 项 | 约定 |
| --- | --- |
| 成功响应 | 两条下载路由**不包 JSON envelope**（二进制流 / `application/zip`）——这是 §3.1「成功封装」的**显式例外**；**失败仍返回标准 `ErrorResponse`**（JSON），客户端必须按 `Content-Type` 分流 |
| `?include_partial=` | 默认 **`true`**；为 `false` 时 `partial/` 前缀条目**视为不在清单内**：#13 的清单不含它们，#14 用相应 `file_id` 下载 ⇒ 400 `VALIDATION_FAILED`，#15 的 zip 也不含它们。语义在 #13 / #14 / #15 三条路由上**统一** |

#### §3.10.9 示例 `file_id`（按 §3.10.1 复算，可直接核对）

| 相对路径 | `file_id` |
| --- | --- |
| `weights/best.pt` | `f_af8212b3` |
| `results.csv` | `f_fb6ea6b3` |
| `partial/weights/last.pt` | `f_96c7e1ba` |
| `partial/results.csv` | `f_8380ff1a` |
| `partial/args.yaml` | `f_baf9c1a6` |
| `partial/train.log` | `f_66f4bcd2` |
| `partial/events.jsonl` | `f_e19d8ec7` |
| `summary.json` | `f_87249a4c` |

上表 8 个 8 hex 前缀**互不相同** ⇒ 同一清单内不触发 12 hex 扩展、清单里保持 8 hex。**示例里的 `file_id` 一律取自上表；不得写随机值**（§0.3）。

### §3.11 跨侧常量与口径

| 项 | 值 / 口径 | 约束 |
| --- | --- | --- |
| 进程模型 | **单进程单 worker**：`uvicorn --workers 1` | 写作约束（全文前提）：进程内全局互斥与内存态即权威；跨 token / 跨请求的准入判定因此可串行化 |
| 跨卡容量判据 | `min_device_total_mb = min(各卡 total_mb)` | 全文**只用这一个名字**；异构多卡不再有专门告警码（§3.6），也不得另立别名的 `max_*` 口径 |
| `cancel_grace_seconds` | 默认 **15**（秒） | SIGTERM 后等待再 SIGKILL 的宽限；**客户端不得硬编码**，一律取自 `capabilities.cancel_grace_seconds`（§3.6） |
| `limit` | 默认 **50**、上限 **200** | `GET /datasets` 的分页（§3.2.5）；`GET /jobs` 的 `limit` 默认同为 50，并作为 `?ids=` 单次批量上限（§3.2.4） |
| `upload_token` 有效期 | 默认 **60 分钟**（配置 `upload_token_ttl_minutes`） | plan 响应下发 `expires_at`；过期后 `upload` 返回 400 `TOKEN_EXPIRED`（§3.3） |
| `client_submission_id` 保留期 | 默认 **24 小时**（= `max(upload_token_ttl_minutes, 1440)` 分钟） | 与已提交 token 的保留期**同口径**；到期只清理索引条目、不删除 job 记录（§3.1 幂等） |
| `max_attempts` | 默认 **3**（含首次） | **每轮**的自动重试上限，不跨手动恢复累加（§3.4.4）；字段名是 `max_attempts`，**不存在** `attempt_max` |
| `auto_resume` | 默认 **true** | 只作用于 `interrupted` 的自动重入队；`failed` 的自动重试**不受**它约束（§3.4.2） |
| 并发与容量 | `max_concurrent_jobs` 默认 **2**、`max_concurrent_per_device` 默认 **1**、`max_committed_tokens` 默认 **10000** | 前两者随 `capabilities.queue` 下发；第三者只在 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED` 的 `details.limit` 与准入判定中出现（§3.3） |
| 心跳周期 | **15 秒** | 训练进程写 `state.json.heartbeat_at` 的周期；超时判据 `heartbeat_timeout_min` 见服务端篇 §4.3（§3.4.2 `interrupted` 行） |

---

## §4 服务端篇

**本章地位**：§4 只写**服务端侧的行为与实现口径**。凡是已在 §3 定义的契约（路由、封装、错误码、状态机、job 字段、事件、`capabilities` / `health`、参数面、warnings 三通道、跨侧常量）在 §4 一律写成「见 §3.x」，**不复制、不改写、不补充**。客户端侧的对称规则（本地扫描、预检阻断矩阵、上传编排与重试策略、UI 文案）属客户端篇 §5。

**全章前提（不逐节重申）**：**内部小规模**——约 10 个用户共享同一台服务器与同一把 `api_key`；服务端**仅 Linux**；**单进程单 worker**（`uvicorn --workers 1`，§3.11）。**单 worker 是有意保留的约束、不是保守选择**：真实流量约 1–3 req/s（10 个客户端按 3 / 10 / 15 秒档轮询），单 worker 既够用，又是队列锁 / 单 token 上传闸门 / 容量准入这三处进程内互斥成立的前提（§4.1.3、§4.2.1、§4.4.5）。

### §4.1 数据集与上传

本节覆盖两条写路由的服务端实现：#2 `POST /datasets/plan` 与 #3 `POST /datasets/upload`，以及与它们共享磁盘状态的数据集生命周期（#4 / #5 / #6 的读取口径见 §3.2.2）。路由的请求 / 响应要点、错误码与 `details` 字段名一律见 §3.2.2 与 §3.3；本节只补**处理顺序、磁盘状态、判定规则与失效语义**。

#### §4.1.1 三阶段协议

上传拆成三个阶段，**转换与划分全部发生在客户端**（阶段 0），服务端**永远看不到原始标注文件**——它只接收已经转好的 YOLO 标签与图片字节。

| 阶段 | 位置 | 内容 | 服务端可见性 |
| --- | --- | --- | --- |
| 阶段 0 | 客户端本地 | 扫描目录配对图片与同名 `.json` → 转换为 YOLO 标签（`detect` / `segment` 两种行格式）→ 按 `val_ratio` 与 `seed` 做**按类别分层**的 train/val 划分 → 流式计算每张图片与每个标签文件的 `sha256` → 按需打包 zip | **不可见**（转换与划分都不上服务端；服务端**不重算划分**、**不读图片内容**） |
| 阶段 1 | `POST /datasets/plan` | 只发**元数据**（manifest）：每张图片的 `name` / `sha256` / `size` / `split` / `label_sha256` / `label_size`，加 `classes` / `split_stats` / `val_ratio` / `seed` / `split_strategy` | 建一次性 `upload_token` 与暂存目录；**无任何数据集副作用** |
| 阶段 2 | `POST /datasets/upload` | multipart zip，内含 `manifest.json` + **仅缺失图片**的 `images/` + **全部**标签的 `labels/` | 校验 → blob 入库 → 物化 → 写数据集目录 → 写已提交 token 记录 → 返回 `dataset_id` |

关键取舍（写死）：

- **标签每次全量上传、绝不缓存**：blob 仓库**只存图片**。因此即使一张图片都没缺失，也**必须**跑一次 upload 才能落盘标签并拿到 `dataset_id`；`missing_images = []` 时 zip 内**可以完全没有 `images/` 目录**。
- **图片按内容去重**：blob 路径由 `sha256` 决定，同一张图在不同数据集 / 不同批次里只落盘一次。
- **不做整包级去重**：标签每次都传，整包内容必然不同；「同一份数据重复提交」会得到**新的 `dataset_id`**（幂等只在同一 `upload_token` 上做，§4.1.4）。

```text
客户端                                              服务端
  |-- 阶段 0：扫描 / 转换 / 按类分层划分 / 逐文件算 sha256 ----|
  |-- 阶段 1：POST /datasets/plan  (manifest，只含元数据) ---->|
  |                                                          | ① 结构 / 值域 / 配额校验（无副作用）
  |                                                          | ② 逐图命中判定 blobs/<sha[0:2]>/<sha[2:4]>/<sha256>
  |                                                          | ③ 建 upload_token + tmp/uploads/<token>/
  |<-- {upload_token, blob_hits, missing_images[], rejected[]} |
  |                                                          |
  |-- 阶段 2：POST /datasets/upload (multipart zip) --------->|
  |      zip = manifest.json                                  | 步骤 0  并发闸门（全局 max_concurrent_uploads 有界等待；单 token 在处理中 ⇒ 409）
  |          + images/<split>/<name>（仅 missing_images）      | 步骤 1  六行判定表（token 状态，§4.1.3）
  |          + labels/<split>/<stem>.txt（全部，可 0 字节）    | 步骤 2  配额预检（413）
  |                                                          | 步骤 3  容量准入（429）
  |                                                          | 步骤 4  流式解压：路径安全 / 扩展名 / 累计字节
  |                                                          | 步骤 5  逐图 sha256 比对
  |                                                          | 步骤 6  标签存在性 + 标签 sha256 比对
  |                                                          | 步骤 7  标签行格式复核
  |                                                          | 步骤 8  blob 入库（内容寻址，已存在则跳过）
  |                                                          | 步骤 9  标签落盘（全部、逐次覆盖）
  |                                                          | 步骤 10 物化 content/images（硬链接）
  |                                                          | 步骤 11 os.replace 原子落位 datasets/<dataset_id>/
  |                                                          | 步骤 12 写已提交 token 记录（含响应快照）
  |<-- {dataset_id, counts, split_stats, bytes, blob, warnings}|
  |                                                          |
  |-- 提交训练：POST /jobs (dataset_id + 参数) -------------->| 预检 + 入队（严格 FCFS，§4.2.1）
```

阶段 0 与阶段 1 / 2 的分工**不重叠**：客户端负责「数据对不对」（类别表、形状转换、划分、本地阻断矩阵见客户端篇 §5.2），服务端负责「字节与声明是否一致、落盘是否安全、配额是否够」（§4.1.6）。

#### §4.1.2 plan：请求体字段与响应字段

请求体就是 manifest，**逐字段约束见下表**（字段名逐字为准，不得改名、不得增删必需字段）：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `schema_version` | int | 必须是 `1`（§3.1）；不匹配 ⇒ 400 `VALIDATION_FAILED`（`details.field=schema_version`） |
| `task` | str | ∈ `capabilities.tasks`（当前 `detect` / `segment`）。**只决定标签行格式**（`detect` ⇒ 每行 5 字段；`segment` ⇒ 每行 `1+2k` 字段）与 `data.yaml` 的任务类型，**与 `images[].split` 无关** |
| `val_ratio` | float | `0 < x < 1`（区间语义见 §3.1）；服务端**只校验、不重算** |
| `seed` | int | 任意整数；服务端**只记录、不重算** |
| `split_strategy` | str | 可选，缺省 `per_class`；**只接受 `per_class`**，其它取值 ⇒ 400 `VALIDATION_FAILED`（`details.field=split_strategy`） |
| `split_stats` | object | 可选：`{"<class>": {"train": N, "val": M}}`，客户端上报的**实际**划分计数。客户端必须为 `classes` 中每一个类别给出条目（零图片类别记 `{"train": 0, "val": 0}`）；服务端**原样回显**并只做结构与自洽性校验，**不重算划分** |
| `classes` | str[] | 非空、元素唯一、不含换行；**顺序即类别索引顺序**（写入 `data.yaml` 的 `names`，也是标签行类别索引的判据） |
| `images[]` | object[] | **每条恰好六个字段**：`name` / `sha256` / `size` / `split` / `label_sha256` / `label_size` |

`images[]` 六字段的逐条口径：

| 字段 | 口径 |
| --- | --- |
| `name` | **相对文件名，不含任何目录分隔符**；含分隔符 ⇒ 400 `VALIDATION_FAILED`（`details.files[]`） |
| `sha256` | 64 位小写十六进制；非法 ⇒ 条目级 `rejected[]`（`INVALID_SHA256`） |
| `size` | 图片字节数；与 blob 实际大小不一致时视为**未命中**（该图进 `missing_images[]`） |
| `split` | `train` / `val`；其它取值 ⇒ 条目级 `rejected[]`（`INVALID_SPLIT`） |
| `label_sha256` | 标签文件 `labels/<split>/<stem>.txt` 的 sha256（**0 字节标签 = 空文件哈希**）；`stem` = 图片名去掉原扩展名。**服务端在 upload 阶段逐文件复核它**，不符 ⇒ 400 `LABEL_CHECKSUM_MISMATCH` |
| `label_size` | 标签字节数（`0` 合法，表示背景图）。**仅信息性**：服务端**不校验**该字段、不参与任何判定——完整性一律以 `label_sha256` 的**逐字节比对**为准（大小判定与哈希比对冗余，且会在哈希已失配时重复报同一错误，故不引入） |

**响应字段**（`data`，形状见 §3.2.2 #2）：

| 字段 | 含义 |
| --- | --- |
| `upload_token` | 一次性上传令牌（`ut_<32hex>`，§3.1）；服务端同时建暂存目录 `tmp/uploads/<upload_token>/` |
| `total_images` | manifest 中 `images[]` 的**声明**条目数（**含**随后进 `rejected[]` 的条目——与 upload 响应的 `counts.total` 在有条目被拒时**可不相等**） |
| `blob_hits` | 命中 blob 缓存的图片数 |
| `missing_images[]` | 需要上传的图片，**每项只含四项** `name` / `split` / `sha256` / `size`（**不含** `label_sha256` / `label_size`：标签每次全量上传，服务端不需要它们判断缺失） |
| `upload_bytes` | `missing_images[]` 的 `size` 合计（**不含标签**；用于进度与提示） |
| `rejected[]` | **条目级**被拒条目及原因（`UNSUPPORTED_EXTENSION` / `INVALID_SHA256` / `INVALID_SPLIT` / `NAME_EMPTY`）；被拒条目不进 `missing_images[]`、也**不得**出现在后续 zip 内 |
| `expires_at` | token 的有效期到期时刻（ISO8601 UTC）；默认 = plan 时刻 + 60 分钟（§3.11 的 `upload_token` 有效期行） |

**plan 阶段的校验表（同步、早失败、无数据集副作用）**：

| 校验 | 失败返回 |
| --- | --- |
| `enabled=false` | 503 `TRAINING_DISABLED`（§3.3） |
| `schema_version` / `task` / `val_ratio` / `seed` 非法或字段缺失 | 400 `VALIDATION_FAILED`（`details.field`） |
| `split_strategy` 存在但不是 `per_class` | 400 `VALIDATION_FAILED`（`details.field=split_strategy`） |
| **划分两侧为空**（`images[]` 中 `split=train` 的集合为空，或 `split=val` 的集合为空） | 400 `VALIDATION_FAILED`（`details.field=split`）；**只做存在性校验**，不按 `val_ratio` 纠正客户端结果 |
| `classes` 为空或含重复项 | 400 `VALIDATION_FAILED`（`details.field=classes`） |
| 条目缺字段、`name` 含路径分隔符、**同 split 内 stem 重复**（如 `a.jpg` 与 `a.png`） | 400 `VALIDATION_FAILED`（`details.files[]` 列出冲突的 stem 与全部文件名） |
| **条目级非法**：扩展名不在白名单、`INVALID_SHA256` / `INVALID_SPLIT` / `NAME_EMPTY` | 该条目进 `rejected[]`，**不阻断整包**；**全部条目都被拒** ⇒ 400 `VALIDATION_FAILED` |
| 图片总字节或预估数据集容量超配额 | 413 `QUOTA_EXCEEDED`（`details`：`quota` / `used_bytes` / `requested_bytes`） |

**同 split 内 stem 唯一的理由（写死）**：`stem` = 图片名去掉原扩展名，`a.jpg` 与 `a.png` 会映射到**同一个** `labels/<split>/a.txt`（后写覆盖前写，且服务端复核仍能找到该文件 ⇒ 静默串标注）。这是**整包级**失败、不是条目级：`rejected[]` 只装条目级原因，**不装**重名 / 同 stem 冲突。

**「二次 plan」是修正条目级拒绝的唯一路径**：客户端剔除被拒条目后，先在本地重做「两侧非空 / 重名 / 同 stem」校验，然后**重新发起 plan**（新 `upload_token`、新 `missing_images[]`），用新 manifest 执行 upload。**不允许**本地重建 manifest 后直接用旧 token upload——zip 内 manifest 与 plan 阶段保存的副本不一致 ⇒ 400 `MANIFEST_MISMATCH`。服务端**没有** token 撤销 / supersede 契约：旧 token 按其自然状态被处理（未提交 ⇒ 异 manifest 走 `MANIFEST_MISMATCH`；已提交 ⇒ 同 body 幂等重放、异 body 409；过期 ⇒ `TOKEN_EXPIRED`）。

#### §4.1.3 upload：处理顺序、判定表与落盘

请求是 `multipart/form-data`，**恰好两个部件**：

| 部件 | 类型 | 说明 |
| --- | --- | --- |
| `upload_token` | text | plan 返回的 token |
| `archive` | file | zip，结构见 §4.1.5 |

服务端严格按下列顺序处理，**任一步失败即整体失败**（失败时数据集目录不落盘、已入库的 blob 保留、临时内容进 `.trash/`）：

```text
步骤 0   并发闸门（两把，见下）：同一 upload_token 已有请求在处理中 ⇒ 409 UPLOAD_IN_PROGRESS；
         全局在途上传数已达 max_concurrent_uploads（§4.4.2）⇒ 新请求在有界闸门上等待（不新增错误码）
步骤 1   六行判定表（表见下；判定顺序自上而下、逐行互斥）⇒ 仅 ⓪ 继续
步骤 2   配额预检（只读请求中立即可得的数据，不解压）⇒ 超出 ⇒ 413 QUOTA_EXCEEDED（任何副作用之前；纯优化、非准入依据）
步骤 3   容量准入（已提交 token 表）⇒ 拒 ⇒ 429 COMMITTED_TOKEN_CAPACITY_EXCEEDED（任何副作用之前）
步骤 4   流式落盘解压到 tmp/uploads/<upload_token>/dataset/：路径安全、扩展名白名单、累计字节上限
步骤 5   逐张图片按实际解压内容算 sha256，与 manifest 的 images[].sha256 比对 ⇒ 不符 400 CHECKSUM_MISMATCH
步骤 6   标签复核（同一次遍历）：按 (split, name) 找 labels/<split>/<stem>.txt
           → 不存在 400 MISSING_LABELS；存在但 sha256 与 label_sha256 不符 400 LABEL_CHECKSUM_MISMATCH
步骤 7   标签行格式复核（补充校验）：detect 5 字段 / segment 1+2k 字段、类别索引 < len(classes)
           ⇒ 不符 400 INVALID_LABEL_FORMAT
步骤 8   图片写 blob 仓库 blobs/<sha[0:2]>/<sha[2:4]>/<sha256>（已存在则跳过；先写同目录临时名再 os.replace）
步骤 9   标签写该数据集 content/labels/<split>/<stem>.txt（全部、逐次覆盖；标签永不进 blobs）
步骤 10  物化 content/images/<split>/<name>（按 blob_materialize 建硬链接；跨文件系统时降级为复制并记 WARNING）
步骤 11  提交期容量复检（与队列锁同一临界区，见下）⇒ 不过 ⇒ 413 QUOTA_EXCEEDED；通过才
         os.replace 把暂存目录原子落位为 datasets/<dataset_id>/（同时生成并落盘 meta.json）
步骤 12  写已提交 token 记录（含本次响应 data 的完整快照）⇒ 落盘成功之后才返回响应
```

**上传路径不得阻塞事件循环（硬要求，写死）**：收流、写 blob、硬链接物化、解压、`sha256` 计算等重活**必须在工作线程中执行**——端点写成同步 `def`（交给 Starlette 线程池）或显式 `run_in_threadpool`，**不得**在 `async def` 里做同步磁盘 / 哈希工作。理由：约 10 个用户共享**同一个事件循环**，阻塞它会让所有人的轮询、`GET /custom/train/health` 乃至反代 / systemd 的 readiness 探活一起抖动（§4.4.5）。本做法**与上游既有模式一致**：上游 `app/api/*` 全部是 `async def`，重活交给 `app/tasks/inference.py` 里有界的 `ThreadPoolExecutor`（`InferenceExecutor`）——本功能照抄该模式（有界池 ⇒ 天然背压）。

**全局上传并发闸门（写死）**：仅靠单请求的 `max_upload_gb` 不够（10 个并发上传 × 单请求上限仍可打满磁盘 / 内存），因此增设**全局在途上传数上限** `max_concurrent_uploads`（数值权威落点见 §4.4.2）。它在**最外层**、先于单 token 闸门与六行判定表生效：超出时新上传请求在有界闸门上**等待**（不忙转），槽位释放即自然进入；**不新增错误码**（错误码表冻结为 30 对 / 29 码，§3.3）：等待期间客户端 `read timeout`（§5.4.4 为 600 s）到期 ⇒ 落入**「（无响应）」行**：**不改 phase**、保留条目与 `archive.zip`、按 §5.4.1 用**同一 token + 同一 zip 重放**（**绝不重新 plan**）。该闸门 **FIFO 放行**；等待在**工作线程**（同步端点 / `run_in_threadpool`），**不占用事件循环**。

**六行判定表（全文唯一口径，逐字保留）**：判定顺序自上而下、**逐行互斥且覆盖全部情形**（范围限定在「token 状态」维度，**不含**步骤 0 的并发闸门）。

| # | 情形 | HTTP | code | `details` |
| --- | --- | --- | --- | --- |
| ⓪ | **在用 token 命中、未过期，且 zip 内 `manifest.json` 与服务端保存的 plan 副本按同一规范化函数比对一致**（两边各算同一份规范化字节后**字节相等即一致**；键序 / 缩进 / 空白差异**不算**差异） | — | — | **通过校验、继续步骤 2**（唯一放行分支） |
| ① | **未知 token：两张表都没有**（含保留期届满后**已被清理器删除**的记录） | 400 | `UNKNOWN_UPLOAD_TOKEN` | — |
| ② | **在用 token 命中但已过期**（`now > expires_at`，且从未 commit） | 400 | `TOKEN_EXPIRED` | `expires_at` |
| ③ | **已提交 token 命中且在保留期内** | 同 body → 与首次相同；异 body → 409 | 同 body → 与首次相同；异 body → `VALIDATION_FAILED` | 同 body → 原响应原样返回；异 body → `field=upload_token`、`committed_dataset_id` |
| ④ | **已提交 token 命中但保留期已过**（`now > committed_at + max(60, 1440)` 分钟，记录**尚未**被清理器删除） | 400 | `TOKEN_EXPIRED` | `expires_at`（= 该记录的 `token_retention_expires_at` = 首次 commit 时刻 + 保留期） |
| ⑤ | **在用 token 命中、未过期，但 manifest 与 plan 副本不一致**（未提交的旧 token + 新 manifest） | 400 | `MANIFEST_MISMATCH` | `field=manifest`（**不是** 409、**不是** `TOKEN_EXPIRED`） |

判定顺序与互斥性（写死）：先按 `upload_token` 定位到**哪一张表**——两张表都没有 ⇒ ①；命中「在用表」再分「已过期 ⇒ ②」与「未过期 ⇒ ⓪ / ⑤」；命中「已提交表」再按 `now` 与 `committed_at + 保留期` 的大小分 ③（未过）/ ④（已过、尚未清理），③ 内再按 body 比对分「幂等重放」与「异 body 409」。**一个请求最多命中一行**；同时看似命中多行时，以**表定位结果**为准（先判 token，再判内容）。

- **③ 的 body 比对**：对「本次 zip 内 `manifest.json` + `upload_token`」按同一规范化函数算出的指纹与该 token 记录里的值**按字节**比对。相同 ⇒ **幂等重放**：直接返回首次成功时的原响应（HTTP 状态、`dataset_id`、`counts` / `bytes` / `warnings` 全部与首次同源），**不重新解压、不重复写 blob、不产生第二个数据集目录**；不同 ⇒ 409。
- **⑤ 与 ③ 共用同一套比对**，区别只在 token **是否已提交**；**两张表都没有时先判 ①**，不要落入 ⑤。
- **④ 与 ① 的分界**：保留期届满但记录**仍在** ⇒ ④ `TOKEN_EXPIRED`；记录**已被清理器删除** ⇒ 回落为 ① `UNKNOWN_UPLOAD_TOKEN`。幂等承诺**只覆盖保留期**；保留期外客户端应**重新 plan**（新 token ⇒ 新副本 ⇒ 走 ⓪）。**不引入**过期 tombstone。

**配额准入（步骤 2 / 3 的先后顺序写死）**：**先配额（413）、后容量（429）**。同一个请求同时超配额与撞容量上限时，应答 **413**。两条拒绝路径都必须在**任何副作用之前、任何文件落盘之前**返回。

| 准入 | 判据 | 拒绝 |
| --- | --- | --- |
| 配额（步骤 2，**仅预检**） | 只用请求中立即可得的数据：zip 内 `manifest.json` 声明的 `images[].size` 合计、plan 副本的 `missing_images[]` / `rejected[]`、当前 blob 已用字节与工作区已用字节；对 `max_upload_gb` / `max_blob_gb` / `max_total_gb` 三项各自比较（键的完整定义见 §4.4） | 413 `QUOTA_EXCEEDED`（`details`：`quota` / `used_bytes` / `requested_bytes`）；**这是尽早失败的优化，不是准入依据** |
| 配额（步骤 11，**提交期复检 = 真正的准入**） | 在临界区内重算「已用字节 + 本次落盘字节」并与上述三项比较：N 个上传者可**各自通过**步骤 2 的预检后**一起**提交，只有「复检 + 落盘」同锁才挡得住（见下条） | 413 `QUOTA_EXCEEDED`（`details` 同上；**不落盘、不改 token 状态**） |
| 容量（步骤 3） | `current` = 已提交 token 表中**未过期**记录的条数；`current ≥ max_committed_tokens`（默认 10000，§3.11）即拒 | 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED`（`details.limit` / `details.current` / `details.retry_after_seconds`，响应头带 `Retry-After`） |

- **准入对 zip 的读取边界**：准入阶段**不做完整解压**——白名单校验、字节累计与任何路径落盘一律不发生。唯一允许的成员访问是对 `manifest.json` 这**一个成员**的**只读单成员读取**（从中央目录定位、读入内存并做长度上限保护，不落盘、不建目录、不做路径规范化），用于六行判定表与配额预检。该读取失败（成员缺失 / 损坏 / 不是合法 JSON 对象 / 解压后字节超过单文件上限）一律 400 `MANIFEST_MISMATCH`（**不**回落到 plan 副本继续放行）。
- **过期记录只排除、不删除**：容量计数**先排除**已过保留期、尚未被清理的记录再计数——**排除只影响计数，不得在此处物理删除**（记录仍在文件里，正是判定表 ④ 能给出正确应答的依据）；物理删除只由清理器按 `committed_at + 保留期` 执行。
- **跨 token 的原子性（写死）**：**提交期的配额复检、容量判定、「写已提交记录」与 `datasets/<id>/` 的落位共用同一把进程内全局互斥**（即 §4.2.1 的队列锁）；检查不过 ⇒ 413 `QUOTA_EXCEEDED`（不落盘、不改 token 状态）。步骤 4–10 的**重活在临界区之外**执行（不持锁跑全程，否则慢上传会堵死其他 token 的准入与提交）。本契约是**进程内**的 ⇒ v1 必须**单 worker**（§3.11），也因此**不需要** token 预留 / 转正 / 释放的生命周期与 `retry_after_seconds` 的逐情形算法（§4.1.4 末段）。
- **准入的保守性**：若本请求随后因步骤 5–7 的校验失败而未提交，则不写记录、不占容量（提交期复检也未执行），下次仍可提交。

**步骤 11 / 12 的持久化顺序与原子性（写死）**：

| 规则 | 约定 |
| --- | --- |
| `dataset_id` 生成时机 | 在**步骤 11 落位时**生成（`ds_<yyyymmdd>_<6hex>`，§3.1）；本规格**没有**「token 预留 id」这一状态——数据集目录的落位是 id 的**唯一诞生点**，客户端的 `dataset_id` 也只来自 upload 响应 |
| 落位的原子性 | 数据集内容先在 `tmp/uploads/<upload_token>/dataset/` 下完整生成，全部校验通过后一次性 `os.replace` 到 `datasets/<dataset_id>/`。`os.replace` 只让**步骤 11 这一步**成为原子落盘 |
| 两个独立事务 | 步骤 11（数据集目录）与步骤 12（已提交 token 记录）**不是**一个文件系统事务，两者之间任何时刻都可能崩溃，第二次写盘本身也可能失败 |
| 记录先于响应 | **落盘成功之后**才返回 `dataset_id`；**不得**先发响应再补写记录（否则「响应已发出、记录丢失」会同时废掉幂等重放与容量账） |
| 步骤 12 写盘失败但进程未崩溃 | 应答用既有 500 `INTERNAL_ERROR`（**不新增错误码**）：**不回滚目录、不释放任何东西**；同一 token + 同一 body 重放时，服务端先按下面的启动对账规则就地补齐已提交记录，再走判定表 ③ 幂等重放**同一 `dataset_id`** |
| 幂等不变量 | 同一 `upload_token` **恒只对应一个数据集目录**、至多一条已提交记录 |
| 崩溃窗口 | 崩溃落在步骤 11 与 12 之间会留下「数据集目录已落盘 + 无已提交记录 + token 仍在用表」；**不猜测性回滚目录**，按表下一条的**启动对账**处理 |

**启动对账（与队列重建同阶段、早于开始受理 upload；在 §4.2.1 的同一把全局互斥内执行）**：扫描 `datasets/` 与两张 token 表，按下表逐条判定。

| # | 启动时的观察 | 对账动作 |
| --- | --- | --- |
| a | 存在数据集目录，但其 `dataset_id` 在已提交表里**没有**记录，且该目录不是**在途训练任务**引用的数据集 | **疑似崩溃残留**：**不自动删除**（避免误删用户数据），记一条 WARN；目录照常出现在 #4 列表，并按 `meta.json.expires_at` 参与数据集 TTL 清理 |
| b | 收到同 token + 同 body 的重放，而 `datasets/<id>/` 已存在、目录内 `manifest.json` 的规范化字节与 plan 副本一致，但已提交表里没有记录（步骤 12 写盘失败，或崩在步骤 11 与 12 之间） | **补齐提交**：按目录内 `meta.json` 的**响应快照**重建已提交记录（`dataset_id` = 目录名、`committed_at` = `meta.json.created_at`、保留期到期时刻 = `meta.json.created_at` + `max(60, 1440)` 分钟），随即走判定表 ③ 返回**原响应** |
| c | 同 b，但目录内 `manifest.json` 的规范化字节与 plan 副本**不一致** | **回滚**：把该目录移入 `.trash/`（软删除），记 WARN，并按判定表 ⑤ 返回 400 `MANIFEST_MISMATCH` |
| d | 已提交表里**已有**该 token 的记录（无论 `dataset_id` 是否与目录同名） | **跳过重建**：不得再写第二条记录、不得覆盖既有记录的任何字段；既有记录的 `dataset_id` 与目录不同名时，该目录按行 a 处理 |
| e | 对账**幂等**：重复执行不得重复补齐、不得写第二条记录 | — |

**响应字段与 `counts` 的派生**（`data`，形状见 §3.2.2 #3）：

| 字段 | 含义 |
| --- | --- |
| `dataset_id` | `ds_<yyyymmdd>_<6hex>` |
| `task` / `classes` | manifest 原样回显 |
| `counts` | **服务端按落盘实况派生**（客户端不上报，见下表） |
| `split_stats` | 客户端上报值**原样回显**，同时写入 `meta.json` 与 #4 列表 |
| `bytes` | `{"images", "labels", "dataset"}`：`images` / `labels` 为**实际解压内容**的字节合计，`dataset` = 两者之和 |
| `blob` | `{"written", "hit"}`：本次**新写入** blob 的图片数 / 命中缓存的图片数；`written + hit = counts.total` |
| `warnings[]` | 通道 B（§3.9）：`BACKGROUND_IMAGES` / `ORPHAN_LABELS` / `WARN_COUNT_MISMATCH` / `SPLIT_CLASS_MISSING_VAL` |
| `created_at` | 数据集创建时刻（ISO8601 UTC） |
| `expires_at` | **数据集 TTL 到期时刻** = `created_at` + `dataset_ttl_days` 天（默认 30 天）。**它不是 token 保留期**（§4.1.4 的三个限定名）；客户端不应把它当作 token 有效期的依据 |

**`counts` 的派生规则（唯一权威定义；plan 请求体里没有这个字段，`counts` 是响应侧派生量）**：服务端在步骤 11 落盘 `meta.json` 时按下面三条**字段级**规则算出 `counts`，并作为响应 `data.counts` 与 `meta.json.counts` 的**同源值**下发（**不**取客户端上报的任何计数字段）：

| 字段 | 派生规则（唯一口径） | 与其它字段的关系 |
| --- | --- | --- |
| `counts.total` | `images[]` 中**被接受**（未被 `rejected[]` 拒绝、且通过步骤 5 / 6 / 7 逐张校验）的条目数 = **实际落盘的图片张数** | = `counts.train + counts.val`（**恒等式**）；与 plan 响应的 `total_images`（**声明**条目数）在有条目被拒时**可不相等** |
| `counts.train` / `counts.val` | `images[]` 中 `split` 分别等于 `train` / `val` 的**已接受**条目数 | 两者之和 = `counts.total`；**与 `split_stats` 各类之和没有等式关系**（多标签下一张图被多个类别各计一次） |
| `counts.background` | **实际解压出的标签为空文件（0 字节）**的图片张数——以步骤 6 校验通过后的**实际内容**为准（清单声明的 `label_size` 不参与任何判定）；背景图不属于任何类别，故它只计入 `total` / `train` / `val`，**不**为任何 `split_stats[c]` 贡献计数 | `counts.background ≤ counts.total` |

**两条恒等式（写死，也是 `WARN_COUNT_MISMATCH` 判据的基础）**：

| # | 恒等式 | 含义 |
| --- | --- | --- |
| (i) | `counts.total == counts.train + counts.val` 必须**恒成立**（每张被接受的图片恰属于一个 split） | 「`counts.total ≠ counts.train + counts.val`」**不是**清单内容错误，而是**服务端自身派生实现的断言式自检**命中 ⇒ 视为服务端 bug（记服务端日志并按内部错误处理），**不**记 `WARN_COUNT_MISMATCH` |
| (ii) | `Σ_c split_stats[c].train ≥ counts.train`、`Σ_c split_stats[c].val ≥ counts.val`（差额方向只能是 `<`） | 背景图不属于任何类别，只会把差值拉大；单标签且无背景图时取等号。因此告警判据**不得**写成 `≠`（那会在多标签清单上误报） |

**upload 的失败返回汇总**（逐条以 §3.3 的 `(HTTP, code)` 表为准）：400 `VALIDATION_FAILED`（zip 内出现条目级非法内容、同 split 内 stem 冲突等）· 400 `CHECKSUM_MISMATCH` · 400 `MISSING_LABELS` · 400 `LABEL_CHECKSUM_MISMATCH` · 400 `INVALID_LABEL_FORMAT` · 400 `UNSUPPORTED_EXTENSION` · 400 `MANIFEST_MISMATCH` · 400 `UNKNOWN_UPLOAD_TOKEN` · 400 `TOKEN_EXPIRED` · 409 `UPLOAD_IN_PROGRESS` · 409 `VALIDATION_FAILED` · 413 `QUOTA_EXCEEDED` · 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED`。失败时**数据集目录不落盘**（临时内容进 `.trash/`），**已入库的 blob 保留**（客户端重传时继续命中）。

#### §4.1.4 令牌的幂等重放与保留期

| 项 | 规则 |
| --- | --- |
| 幂等粒度 | **只在同一个 `upload_token` 上做**。新的 plan ⇒ 新 token ⇒ 新 `dataset_id`（「重复提交相同内容仍新建 `dataset_id`」的规则不变） |
| 两张表 | 「**在用表**」`tmp/uploads/_inuse.json`：`{upload_token → {expires_at, manifest_hash}}`；「**已提交表**」`tmp/uploads/_committed.json`：`{upload_token → {dataset_id, manifest_hash, response_snapshot, committed_at, token_expires_at}}`。两张表都走「写 `.tmp` → `fsync` → `os.replace`」的原子写，读改写都在同一把全局互斥内（§4.2.1） |
| 判定顺序 | **先查已提交表，再查在用表**（顺序不可交换）。**两表同时残留时以已提交表优先**：命中已提交表即走判定表 ③ / ④，**不得**再看在用表 |
| 清单指纹 | `manifest_hash = sha256(upload_token 的 UTF-8 字节 + 单个 0x00 字节 + 清单的规范化字节)`。规范化字节 = **§5.4.1 的 `canonical_json_bytes`**（键序 / 缩进 / 空白差异不算差异）。**不对整个 zip 取哈希**（zip 头含时间戳，重打包会变化） |
| 同 token + 同 body | 返回**原响应**（HTTP 状态与首次一致、`data.dataset_id` 相同、`counts` / `bytes` / `warnings` 同源）；不重新解压、不重新校验、不写新目录 |
| 同 token（已提交）+ 异 body | **409 `VALIDATION_FAILED`**（`details.field=upload_token`、`details.committed_dataset_id`）；**不是** `TOKEN_EXPIRED` |
| 未提交的 token + 新 manifest | **400 `MANIFEST_MISMATCH`**（`details.field=manifest`） |
| 保留期 | 已提交记录保留 `max(60, 1440)` 分钟（即 `max(upload_token_ttl_minutes, 1440)`；默认 **24 小时**，§3.11）。**保留期内重放一律幂等**；**保留期内不得淘汰任何已提交记录**（不存在 LRU、不存在按容量提前淘汰）——因此判定表 ③ 的幂等承诺在保留期内**无条件成立** |
| 保留期届满 | 记录仍在 ⇒ 400 `TOKEN_EXPIRED`（`details.expires_at` = 该记录的 `token_expires_at` = 首次 commit 时刻 + 保留期）；记录已被清理器删除 ⇒ 回落为 400 `UNKNOWN_UPLOAD_TOKEN`。「可原样重试」是**条件句**：仅当等待后该 token **仍在用表有效期内**时才成立，否则客户端必须重新 plan（图片按 `sha256` 命中缓存、**不重传**） |
| 到期清理 | 清理器**只**按 `committed_at + 保留期` 到期删除已提交记录、按 `expires_at` 删除在用记录；`tmp/uploads/<upload_token>/` 在成功提交后清空并转入 `.trash/` |
| 容量上限与 429 | 本次请求会**新写入**一条已提交记录（判定表 ⓪）、且 `current ≥ max_committed_tokens`（`current` = 已提交表中**未过期**记录的条数）⇒ 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED`。判定表 ①②④⑤ 本就不写记录、③ 是幂等重放，**都不**走本分支 |
| `retry_after_seconds` | 取值 = `max(1, ceil(表中最早到期时刻 − now))`，**恒 ≥ 1**，与响应头 `Retry-After` 是**同一个值**；等待到点即被排除出计数（**不必**等清理器真正删除记录）。恢复路径只有两条：① 按 `Retry-After` 等待后重试；② 由运维提高 `max_committed_tokens`（改配置、重启生效）。**已删除的无效做法**：缩短 token 有效期（已提交记录保存的是固定的 `token_expires_at`，改配置不会加速任何记录到期），以及淘汰未到期记录腾位置（禁止） |

**「到期时刻」的三个限定名（互不混用，写死）**：

| 限定名 | 算法 | 落点 |
| --- | --- | --- |
| `token_expires_at`（**在用 token** 的有效期） | plan 时刻 + `upload_token_ttl_minutes`（默认 60 分钟，§3.11） | plan 响应 `data.expires_at`；在用表记录的 `expires_at`；判定表 ② |
| `token_retention_expires_at`（**已提交记录**的保留期） | 首次 commit 时刻 + `max(upload_token_ttl_minutes, 1440)` 分钟（默认 24 小时） | 已提交表记录的 `token_expires_at`；判定表 ④ 与步骤 3 的容量计数 |
| `dataset_expires_at`（**数据集 TTL**） | `created_at` + `dataset_ttl_days` 天（默认 30 天） | upload 响应 `data.expires_at`；`meta.json.expires_at`；#4 列表的 `expires_at` |

**两条不变量**：① 已提交记录的重建**只**按 `token_retention_expires_at` 的算法，**绝不**取 `dataset_expires_at`（否则幂等重放窗口会被悄悄放大到 30 天，判定表 ④ 的 `details.expires_at` 也会给错时刻）；② `dataset_expires_at` **不参与任何** token 判定（判定表 ② / ④、保留期、容量计数一律不用它）。

**本版本明确不做**：token 预留与转正、预留槽位的释放窗口、写盘失败后「同一预留复用同一 `dataset_id`」的快路径，以及 `retry_after_seconds` 的逐情形取值算法。**取代它们的规则**：① 容量判定只看**未过期的已提交记录条数**（`current ≥ max_committed_tokens` 即拒）；② `dataset_id` 只在**步骤 11 落位时**生成；③ 步骤 12 写盘失败后靠**启动对账**（§4.1.3 行 b）在重放时就地补齐记录并返回同一 `dataset_id`。**为什么不需要预留机制**：单 worker + 同一把队列锁（§4.1.3 的提交期复检）已给出「检查 + 落盘」的原子性，预留 / 转正 / 释放三态只会多出失败窗口。

#### §4.1.5 zip 规格与安全

```text
<archive>.zip
  manifest.json                  # 与 plan 请求体同构；客户端把 plan 阶段序列化出的原始字节原样写入
  images/<split>/<name>          # 仅缺失图片（split ∈ {train, val}）；missing_images = [] 时本目录可整体缺席
  labels/<split>/<stem>.txt      # 全部标签，每次必传（允许 0 字节）
```

> `stem` = 图片文件名去掉**原扩展名**（`road_0001.jpg` → `road_0001`），标签文件名 = `stem` + `.txt`。**不得**把含扩展名的图片名直接拼 `.txt`（那会得到 `road_0001.jpg.txt` 这种错误路径）。

**manifest 自校验口径（唯一口径）**：服务端以 plan 阶段保存的副本为权威，对「zip 内 `manifest.json`」与「plan 副本」各算**同一规范化函数**下的字节，**字节相等即一致**——键序 / 缩进 / 空白的差异**不算**差异，任何**语义**差异（`images[]` 集合、`split_stats`、`classes`、`val_ratio` / `seed` / `split_strategy`）必然改变规范化字节 ⇒ 400 `MANIFEST_MISMATCH`。**客户端义务**：把 plan 阶段序列化出的 `manifest.json` 原始字节原样写进 zip，upload 时**不**重新 dump；**服务端义务**：步骤 11 把该字节**逐字节原样落盘**（不重新序列化、不排序键、不增删字段）。

| 安全项 | 处理 |
| --- | --- |
| zip-slip | 解析后规范化路径必须落在 `tmp/uploads/<upload_token>/` 之内；出现 `..`、绝对路径、符号链接、设备文件一律 400 |
| 文件名编码 | 优先按 zip 的 UTF-8 标志位（`flag_bits & 0x800`）解码；未置位时按 CP437 解码；**两种方式得到的名字都必须通过白名单与路径检查** |
| 扩展名白名单 | 图片：`.jpg` / `.jpeg` / `.png` / `.bmp` / `.webp` / `.tif` / `.tiff`；标签：`.txt`；清单：`.json`。**级别按阶段限定**：同一问题在 plan 阶段按**条目级**进 `rejected[]`，在 upload 的解压阶段**一律整包 400**（`UNSUPPORTED_EXTENSION`）并列文件 |
| 未知顶层条目 | 除 `manifest.json` / `images/` / `labels/` 之外的顶层项 ⇒ 400 `MANIFEST_MISMATCH` 并列出条目名 |
| 多余条目与孤儿标签 | `images/` 下未在 `missing_images` 里声明的图片、额外目录 ⇒ 400 `MANIFEST_MISMATCH`；`labels/` 下未被 manifest 引用的**孤儿标签忽略**并计入 `warnings[ORPHAN_LABELS]`（不阻断整包） |
| zip bomb | 累计解压字节上限 `max_upload_gb`；**单文件解压上限 = `max_upload_gb` 的 1/2**；压缩比 > 200:1 且解压总量 > 1 GB ⇒ 400。**超限按整包级处理**（不做「丢弃单文件继续」的降级） |
| 累计字节复核 | 边解压边累计，并与 `max_upload_gb` 实时比较；声明值与实际落盘字节的差异由此兜底（两类检查都返回 413） |
| 单 token 并发闸门 | 同一 `upload_token` 只允许**一个**上传请求在处理中，第二个请求**立即** 409 `UPLOAD_IN_PROGRESS`；该闸门在**步骤 0**、**先于**六行判定表，且**不是**判定表的一行；它之外还有一层**全局上传并发闸门** `max_concurrent_uploads`（§4.1.3、§4.4.2） |
| 跨 token 并发 | 容量准入与提交由**同一把进程内全局互斥**串行化（§4.2.1）；该互斥是进程内的 ⇒ v1 **单 worker** |
| 原子落盘 | 数据集内容先在暂存目录完整生成，再 `os.replace` 到 `datasets/<dataset_id>/`；`os.replace` 只覆盖**步骤 11 这一步**，跨步骤的窗口由启动对账兜底（§4.1.3） |
| 物化方式 | 默认硬链接；`blobs` 与工作目录**不同文件系统**时自动降级为复制，并记**服务状态**告警 `BLOB_MATERIALIZE_DEGRADED`（通道 C，§3.9） |
| 客户端不上传 `data.yaml` | `data.yaml` 由服务端按 `classes` 与实际落盘路径生成（避免路径穿越） |

#### §4.1.6 N1 校验矩阵（服务端侧）与最小复核

**N1 矩阵**（服务端列；客户端预检与阻断矩阵见客户端篇 §5.2）：

| # | 场景 | 服务端复核（upload 时） | HTTP | 返回字段 |
| --- | --- | --- | --- | --- |
| 1 | 图片无同名 `.json`（客户端应阻断） | 若被绕过：标签缺失 | 400 | `MISSING_LABELS`（`details.files[]`，含 split 前缀，如 `train/a.jpg`） |
| 2 | `.json` 有效但 `shapes` 为空 | 允许 0 字节标签，计 `counts.background` | 200 | `warnings[BACKGROUND_IMAGES]` |
| 3 | `shapes` 部分可转换（不在类别表 / 点数不足 / 类型不匹配） | 只核对文件级完整性（内容由客户端产出）；计数不自洽时报 | 200 | `warnings[WARN_COUNT_MISMATCH]`（判据见 §3.9 通道 B） |
| 4 | `.json` 损坏 | **服务端看不到 `.json`**，只能通过标签缺失间接发现 | 400 | `MISSING_LABELS` |
| 5a | 有标签无图片（孤儿标签） | 忽略，不参与校验、不阻断整包 | — | `warnings[ORPHAN_LABELS]`（`files[]`） |
| 5b | zip 内出现 manifest 未声明的多余条目（顶层未知项、未声明图片、额外目录） | 整包级：列出多余条目并拒绝 | 400 | `MANIFEST_MISMATCH` + `details.unexpected[]` |
| 6 | 图片实际 `sha256` 与 manifest 声明不符 | 逐张按**实际解压内容**算 `sha256` 比对 | 400 | `CHECKSUM_MISMATCH` + `details.files[]` |
| 7 | zip 内图片集合与 `missing_images` 不符（多传 / 少传） | 整包级 | 400 | `MANIFEST_MISMATCH` + `details.missing[]` / `details.unexpected[]` |
| 8 | 标签行格式非法（补充校验） | 逐行解析：`detect` ⇒ 5 字段、`segment` ⇒ `1+2k` 字段；类别索引 < `len(classes)` | 400 | `INVALID_LABEL_FORMAT` + `details.files[]` |
| 9 | 图片扩展名不在白名单 | **plan：条目级**（`rejected[]`）；**upload：整包 400** | 200 / 400 | plan：`rejected[]`；upload：`UNSUPPORTED_EXTENSION` + `details.files[]` |
| 10 | `split` 取值非 `train` / `val` | **plan：条目级**（`INVALID_SPLIT`）；**upload：整包 400** | 200 / 400 | plan：`rejected[]`；upload：`VALIDATION_FAILED`（`details.field=split`） |
| 11 | 配额超限（上传 / blob / 总量） | plan 阶段在同一张校验表内；upload 阶段在**步骤 2**、**先于**步骤 3 的容量准入 | 413 | `QUOTA_EXCEEDED` + `details`：`quota` / `used_bytes` / `requested_bytes`（同请求同时超配额与撞容量上限 ⇒ **413**） |
| 12 | token 未知 / 在用超期 / 已提交（保留期内 / 已过）/ 旧 token 配新 manifest | 按 §4.1.3 的**六行判定表**（唯一口径、**无 404 分支**） | 400 / 409 | ⓪ 继续；① `UNKNOWN_UPLOAD_TOKEN`；② `TOKEN_EXPIRED`；③ 原响应 / 409 `VALIDATION_FAILED`；④ `TOKEN_EXPIRED`；⑤ `MANIFEST_MISMATCH` |
| 13 | `classes` 与标签索引不匹配（类别表给错） | 通过 #8 的索引越界发现 | 400 | `INVALID_LABEL_FORMAT` |
| 14 | 标签内容与 manifest 的 `label_sha256` 不符 | 解压后**逐文件**算 `labels/<split>/<stem>.txt` 的 `sha256` 并与 manifest 比对 | 400 | `LABEL_CHECKSUM_MISMATCH` + `details.files[]`（含 `name` / `label` / `reason` / `declared` / `actual`） |

**服务端最小复核（冻结要求）**：**每张图片必须有对应的标签文件**（允许 0 字节）。实现方式：解压完成后按 manifest 的 `(split, name)` 逐个检查 `labels/<split>/<stem>.txt` 是否存在（`os.path.exists` + 常规文件判定），缺失集合非空 ⇒ 400 `MISSING_LABELS`，并把缺失的图片名（含 split 前缀）放进 `details.files[]`。

**复核顺序（写死）**：先跑「**存在性**」（缺失集合非空 ⇒ `MISSING_LABELS`），**再**跑「**内容比对**」（存在但 `sha256` 不符 ⇒ `LABEL_CHECKSUM_MISMATCH`）。两类失败**不同时**返回，按此先后顺序取先命中的那一类。因为标签**每次全量上传、绝不缓存**，这两步在**每一次** upload 上都真正执行（不存在「缓存命中就跳过校验」的旁路）。

**校验时机总表**：

| 时机 | 内容 | 失败后果 |
| --- | --- | --- |
| 客户端预检（阶段 0 末尾） | N1 全部场景 + 配额预估（客户端侧口径见客户端篇 §5.2） | 不上传；UI 列出问题文件 |
| plan（阶段 1） | 结构 / 值域 / 配额校验 + 逐条 `rejected[]` | 400 / 413；**无任何数据集副作用** |
| upload 准入（步骤 0–3） | 全局上传并发闸门 + 单 token 并发闸门 + 六行判定表 + 配额预检 + 容量准入 | 400 / 409 / 413 / 429；**先于任何副作用**：不解压、不写 blob、不建数据集目录、不改 token 状态 |
| upload 解压中（步骤 4） | 路径安全 / 扩展名白名单 / 累计字节 | 400 / 413；临时内容进 `.trash/` |
| upload 校验（步骤 5–7） | 逐张图片 `sha256` + 标签存在性 + 标签 `sha256` + 标签行格式 | 400 并列出文件名；**已入库的 blob 保留** |
| 提交任务（`POST /jobs`） | 参数白名单与范围、模型家族能力检查、显存可行性预检 | 422 / 503（§4.2.2） |

#### §4.1.7 数据保留、回收与软删除

| 项 | 规则 |
| --- | --- |
| 数据集 TTL | `dataset_ttl_days`（默认 30 天）；到期时刻 `dataset_expires_at` = `created_at` + 该天数，写进 `meta.json.expires_at` 并随 upload 响应与 #4 列表下发 |
| 未被引用的 blob | `blob_unused_ttl_days`（默认 **30 天**，数值权威落点见 §4.4.2）：blob 的引用计数为 0 且超过该天数 ⇒ 由清理器回收（先移入 `.trash/`） |
| 引用计数只对在途任务生效 | `queued` / `preparing` / `running` 的引用**既**阻止 `DELETE /datasets/{id}`（409 `DATASET_IN_USE`，§3.3）**也**阻止 TTL 清理；`completed` / `failed` / `interrupted` / `cancelled` 的引用**不阻止**到期清理（否则 TTL 会被终态任务永久钉住）。**代价是明确的**：终态任务可能在其数据集过期后无法恢复（恢复时 409 `JOB_ARTIFACTS_EXPIRED`，`details.reason=dataset_expired`） |
| 被任何数据集引用的 blob | **永不回收** |
| `0` = 不清理 | `dataset_ttl_days = 0` ⇒ 数据集**不做 TTL 清理**；`blob_unused_ttl_days = 0` ⇒ 未被引用的 blob **不回收**。两者语义一致：**`0` 一律表示「不清理」**，不是「立刻清理」。启动自检仍校验「非 0 时 `blob_unused_ttl_days` 不得短于 `dataset_ttl_days`」（§2.6 第 ① 步）；**`0` 不触发任何即时清理** |
| 删除一律软删除 | 数据集 / blob / 过期 token 暂存目录都**先移入 `.trash/`**，超过 `trash_ttl_hours` 后才物理回收（**唯一例外**：**超配额自动回收路径**，见下行） |
| 删除的响应 | `DELETE /datasets/{dataset_id}` ⇒ 200 `{"deleted": true, "moved_to_trash": "<path>"}`；被在途任务引用 ⇒ 409 `DATASET_IN_USE`（`details.referenced_by_jobs[]`） |
| 崩溃残留目录 | 启动对账判定的疑似崩溃残留目录**不自动删除**、照常出现在 #4 列表、按 `meta.json.expires_at` 参与 TTL 清理（§4.1.3 行 a） |
| 上传中断 | token 到期后暂存目录被清理；**已入库的 blob 仍命中**，客户端重新 plan 即可跳过这些图片 |
| **超配额自动回收**（回收的第三个触发源） | **仅当**已用字节超过 `max_total_gb` / `max_blob_gb`（§4.4.2）时触发；**只回收未被引用的条目**——被在用 / 未过期数据集、未过期 token 记录、或任何任务（含终态任务恢复所依赖的 `jobs/<id>/run/`）引用的对象**一律不碰**。顺序**确定性**：**最旧未引用优先**，排序键 = `(创建时间, 路径)` 的全序比较 ⇒ 同一状态必得同一顺序（可复现）；每次回收**逐条记日志**（路径 / 字节 / 触发配额 / 回收后占用） |
| 回收与 `.trash/` 的关系 | 回收**仍先移入 `.trash/`**（软删除铁律不变），但**回收路径下的 trash 在下一轮清理时即时物理清除**（**不等** `trash_ttl_hours`）——否则磁盘不会真正释放、超配额无法解除 |
| 413 的可操作文案 | 413 `QUOTA_EXCEEDED` 的 `details` 仍严格按 §3.3 给出 `quota` / `used_bytes` / `requested_bytes`；`message` 要写明**已触发自动回收（最旧未引用优先）、请稍后重试**，把「手工删文件」降为兜底选项（客户端文案见客户端篇 §5.6） |
| TTL 与 `0` 语义 | 四个 TTL 键见 §4.4.2、§4.4.1、§4.4.3；本节只定义**超配额回收**，不重复定义 TTL |
| 本版本明确不做 | **LRU（按最近使用时间淘汰）**与「按配额压力触发两段式回收」的细节：回收顺序是确定性的「最旧未引用优先」，**不引入**访问时间记账；也**不**以自动淘汰**被引用**数据的方式腾空间 |
**`meta.json` 与数据集目录（落位后的样子；`work_dir` 全树见 §4.4）**：

```text
datasets/<dataset_id>/
  manifest.json        # zip 内那份的**字节原样落盘**（不重新序列化、不排序键、不增删字段）
  meta.json            # counts / split_stats / bytes / created_at / expires_at / referenced_by_jobs
                       #   + response_snapshot（本次 upload 响应 data 的完整快照；内部字段、不下发）
  content/images/<split>/<name>    # 硬链接到 blobs/<sha[0:2]>/<sha[2:4]>/<sha256>
  content/labels/<split>/<stem>.txt
data.yaml              # 服务端按 classes + 实际落盘路径生成（客户端不得上传）
```

**upload 成功响应示例（形状；数值口径见上面的字段表与 `counts` 派生表）**：

```json
{
  "success": true,
  "data": {
    "dataset_id": "ds_20260101_7f2a91",
    "task": "detect",
    "classes": ["person", "car", "traffic_light"],
    "counts": {"total": 1200, "train": 960, "val": 240, "background": 12},
    "split_stats": {"person": {"train": 960, "val": 228},
                    "car": {"train": 960, "val": 228},
                    "traffic_light": {"train": 960, "val": 228}},
    "bytes": {"images": 2415919104, "labels": 115200, "dataset": 2416034304},
    "blob": {"written": 250, "hit": 950},
    "warnings": [
      {"code": "BACKGROUND_IMAGES", "message": "12 张图片的标注为空，已作为背景图训练", "files": ["road_0002.png"]}
    ],
    "created_at": "2026-01-01T10:11:12Z",
    "expires_at": "2026-01-31T10:11:12Z"
  }
}
```

（示例里的 `bytes` / `counts` 是**示例值**：`counts` 是服务端派生量，示例与 `split_stats` 的复算关系见上表的两条恒等式；`blob.written + blob.hit = counts.total` 在本例中为 `250 + 950 = 1200`。）


---

### §4.2 显存标定与调度

本节写**队列与并发、提交期预检、显存账本、心跳与僵尸判定、显存估算表、OOM 兜底与 auto-batch、标定矩阵**七件事。标定的**启动期流程、触发条件、指纹与冲突策略**已在 §2.5 写完（本节不重述，只写矩阵、点位与失败分级）；`work_dir` 全树、配置键的完整定义与启动自检清单属 §4.4；进程模型、文件协议、重启接管、恢复与取消属 §4.3。

#### §4.2.1 队列与并发

**队列文件 `queue.json` 的结构在此定义一次**（§4.4 的目录树只写路径，不再重复结构）：

```json
{"items": [{"job_id": "job_20260101_7f2a91", "attempt": 1, "created_at": "2026-01-01T10:11:12Z"}]}
```

| 字段 | 类型 | 口径 |
| --- | --- | --- |
| `items[]` | object[] | **队列表项的有序数组**，严格 FCFS 升序；每项**至少**含下列三个字段，允许携带其它诊断字段（如 `device_index`，仅排障用） |
| `job_id` | str | 任务 ID |
| `attempt` | int | **该次入队对应的 attempt 序号**，从 1 开始。由同一次入队事务的**两次原子写**（**先** `state.json`、**后** `queue.json`）落盘，并与 `state.json.attempt` 保持一致；它是 `state.json` 缺失 / 损坏时重建最小 `state.json` 的**权威取值** |
| `created_at` | str | 入队时刻（ISO8601 UTC）；**排序键**（FCFS 顺序与启动时「按 `created_at` 升序补回队尾」的依据） |

| 规则 | 取值 / 行为 |
| --- | --- |
| 队列顺序 | **严格 FCFS**：`items[]` 按 `created_at` 升序，**不插队**。提交、手动恢复、自动重试、`interrupted` 自动重入队**一律入队尾**（恢复任务不回到原位） |
| 队列位次 | `queue_position` = `items[]` 中的 1-based 下标（1 = 队头）；非 `queued` 时为 `null`（§3.4.4） |
| 全局并发 | `max_concurrent_jobs`（默认 **2**，§3.11）：`running` + `preparing` 的任务总数上界 |
| 每卡并发 | `max_concurrent_per_device`（默认 **1**，§3.11）：单张卡上 `running` + `preparing` 的任务数上界 |
| 调度周期 | 调度线程每 **2 秒**扫描一次（设计值）：先处理终态与出队，再尝试派发；显存与并发位不足时不做任何事，等下一轮 |
| 队列持久化 | 入队 / 出队 / 位次变化 / 启动重建**每一次读-改-写**都立即落盘，且一律走「写 `queue.json.tmp` → `fsync` → `os.replace`」的**原子写** |
| 队列锁 | **进程内（in-process）的全局互斥对象**（单 worker）：保护 `queue.json` 的**每一次读-改-写**——入队（提交 / 手动恢复 / 自动重试）、出队（取消 / 派发）、队内位次重排、启动时的队列重建**全部**必须在持锁状态下完成。该互斥对象与「已提交 token 表」的准入—提交临界区是**同一把锁**（§4.1.3）；**不是**两把独立锁，也**不**依赖获取顺序去避免死锁 |
| 与 `state.lock` 的关系 | 需要同时持有时**获取顺序固定**为「先 `state.lock`、后队列锁」，任何路径都**不得**反向获取（文件锁的粒度与层级见 §4.3） |
| 队列恢复 | 服务启动时读 `queue.json` 与 `jobs/*/state.json` 重建 `queued` 列表（用 `items[].attempt` 修 `state.json` 缺失 / 损坏的 attempt，用 `items[].created_at` 恢复 FCFS 位次）；该重建与 §4.1.3 的启动对账同阶段，属 §2.6 第 ② 步（扫描并接管存活训练进程）的一部分，细节见 §4.3 |
| 无卡可派 | 保持 `queued` 并写明 `queued_reason`（见 §4.2.3）；**不是拒绝**——提交期已按最终容量放过（§4.2.2） |

**派发循环（唯一顺序，写死在调度线程里）**：

```text
每 2 秒：
  1. 消费终态与出队（先于派发）：把已完成 / 已取消 / 已判失败的任务移出 items[]（原子写 queue.json）
  2. 若 running + preparing 的总数 >= max_concurrent_jobs -> 本轮结束
  3. 自队头向后扫描 items[]（**严格 FCFS**，最多尝试前 N 项，N = 全局并发上限）：
       跳过已在 running / preparing 的项
       a. 设备可行性：available_mb(dev) >= vram_estimate_mb 的卡才算「可立即派发」
          若无任何卡满足，再算「最终容量可容纳」的卡集合：
              满足 est_mb <= total_mb - gpu_reserve_mb（与提交期同一判据）
          - 卡集合为空       -> 本项**永久等待**：queued_reason = WAITING_DEVICE_VRAM（写日志 WARN）
          - 卡集合非空、但每张卡上都已有 running / preparing 任务
                             -> queued_reason = WAITING_PREVIOUS_JOBS
          - 卡集合非空、且有卡空着，只是当前 available_mb 不够
                             -> queued_reason = WAITING_DEVICE_VRAM
          - 全局并发位已满   -> queued_reason = WAITING_CONCURRENCY_SLOT（第 2 步已挡；这里是兜底写法）
          记下 queued_reason，继续看下一项（**队头不阻塞其它卡的候选**）
       b. 每卡并发位：该卡已派发数 >= max_concurrent_per_device 的卡从候选里剔除
          （剔除后候选为空 -> queued_reason = WAITING_CONCURRENCY_SLOT）
       c. 设备亲和：若该 job 的 state.json 有 last_device_index 且该卡仍在候选里 -> 直接选它
                    否则 -> best-fit：候选里 available_mb 最小者
       d. 取锁（先 state.lock、后队列锁）后二次校验：任务仍是 queued、并发位与 available_mb 仍满足
       e. 原子写（同一事务，顺序不可交换）：queue.json（移出该项）→ 同一临界区内写 state.json
                              （status=preparing、device_index、preparing_at、deadline_at）→ 落盘 queue.json.tmp 并 os.replace
       f. 交给准备流程建 data.yaml / 目录 / 注入参数（§4.3）；立即继续扫描下一项（准备是异步的）
```

**三条补充规则**：

- **队头不阻塞其它卡**：FCFS 是「入队顺序」的约束，不是「必须等队头跑完」的约束。队头若因显存不足无法派发，**后面凡是打算用同一张卡的任务也不得越过它**（否则同一卡上会出现乱序执行）；而目标卡完全不同的任务**允许被派发**。
- **设备亲和（`last_device_index`）**：同一任务的重试与恢复**优先回到上次那张卡**（避免每次重试都换卡导致显存碎片化）。字段属于 `state.json`，由 §4.3 维护；本节的调度器只读它。
- **派发判据是「当前可调度量」**：`available_mb`（§4.2.3 的账本公式），**不得**用 `min_device_total_mb` 或瞬时空闲显存替代；提交期与派发期的两级判据见 §4.2.2。

#### §4.2.2 提交时可行性预检

`POST /jobs` 同步估算显存并做可行性判定，**只判设备最终容量**（不看此刻有多少空闲显存）。伪码（公式细节见 §4.2.5）：

```text
# 1) 参数校验（越界字段 -> 422，details.field）
  batch / imgsz / epochs / workers / lr0 / ... 逐项按 param_schema 校验
  请求体出现服务端注入参数 -> 422 PARAM_NOT_OVERRIDABLE（§3.8.1）
  optimizer preset 不属于所选家族 -> 422 OPTIMIZER_UNSUPPORTED
  allow_auto_batch = false 而提交了 -1 / 比例值 -> 422 PARAM_OUT_OF_RANGE

# 2) 组合可调度性（model_family / model / task）
  家族 ultralytics 版本 < min_ultralytics        -> 422 MODEL_FAMILY_UNSUPPORTED
  allow_weight_download = false 且权重缺失       -> 422 WEIGHT_NOT_AVAILABLE
  组合在 unschedulable[] 里（含全点 OOM / 无任何表行）-> 422 VRAM_ESTIMATE_UNAVAILABLE

# 3) 取行与折算
  按 (model, task) 取行：命中本行 -> factor = 1.0
                        未命中但有同模型其它 task 行 -> factor = task_factor[目标] / task_factor[该行]
                        两者都没有 -> 422 VRAM_ESTIMATE_UNAVAILABLE
  batch 为整数且超过该组合 max_batch -> 收敛为 max_batch + 响应 warnings[] 记 CONVERGED_TO_DEVICE_MAX
  batch 为 auto（-1 / 0<r<1）        -> 按 §4.2.5 折算 batch_assumed（任一 cap < 1 -> 422 INSUFFICIENT_VRAM）

# 4) 估算与容量判定（唯一判据：设备最终容量）
  est_mb = (process_overhead_mb + interp_val_s) * factor * vram_safety_factor
  min_device_total_mb = min(各张卡的 total_mb)          # §3.11
  est_mb > min_device_total_mb - gpu_reserve_mb -> 422 INSUFFICIENT_VRAM (reason=insufficient_capacity)

# 5) 通过 -> 写 jobs/<job_id>/request.json + state.json(status=queued)，
#    并在同一把队列锁内把 {"job_id","attempt":1,"created_at"} 入队尾（原子写 queue.json）
```

**5 个拒绝分支（写死；提交期的全部拒绝路径）**：

| # | 分支 | 判据 | 应答 |
| --- | --- | --- | --- |
| 1 | **不可调度** | 家族不可用 / 权重缺失 / 组合在 `unschedulable[]` 里 / 三层表都没有该组合的行 | 422 `MODEL_FAMILY_UNSUPPORTED` / `WEIGHT_NOT_AVAILABLE` / `VRAM_ESTIMATE_UNAVAILABLE`（`details.reason` 取 `VRAM_TABLE_INCOMPLETE` / `VRAM_CALIBRATION_FAILED`） |
| 2 | **容量不足** | `est_mb > min_device_total_mb − gpu_reserve_mb`（语义 = 「本机**永远**跑不了」：即使 GPU 全空也跑不动） | 422 `INSUFFICIENT_VRAM`（`details.reason=insufficient_capacity`，带 `vram_estimate_mb` / `min_device_total_mb` / `gpu_reserve_mb`） |
| 3 | **auto 折算不可行** | auto-batch 折算中**任一有效 cap < 1**（比例 cap 或实测 `max_batch` 折算出的 cap） | 422 `INSUFFICIENT_VRAM`（`details.reason=ratio_cap_below_one` / `table_cap_below_one`，带 `field=batch` / `batch_cap_by_ratio` / `batch_cap_by_table` / `ratio` / `total_mb` / `suggestion`） |
| 4 | **参数非法** | `param_schema` 越界、注入参数、`optimizer` preset 跨家族、`allow_auto_batch: false` 时的 auto 取值 | 422 `PARAM_OUT_OF_RANGE` / `PARAM_NOT_OVERRIDABLE` / `OPTIMIZER_UNSUPPORTED` |
| 5 | **无可用设备** | 整机 `torch.cuda.device_count() == 0` 或 CUDA 探测失败 | 503 `NO_DEVICE_AVAILABLE`（`details.devices`） |

**两级判据（不得混用）**：

| 阶段 | 判据 | 超限结果 |
| --- | --- | --- |
| **提交期（预检，本节）** | **设备最终容量**：`est_mb > min_device_total_mb − gpu_reserve_mb`（`min_device_total_mb` = 各卡 `total_mb` 的**最小值**，§3.11）；**不看**当前空闲显存 | 422 `INSUFFICIENT_VRAM`（`reason=insufficient_capacity`） |
| **派发期（调度器，§4.2.3）** | **当前可调度量**：`available_mb(device) >= est_mb` | **不拒绝**：保持 `queued` 并写明 `queued_reason`（等显存 / 等前方任务 / 等并发位） |

- **「当前没有空闲显存」不构成拒绝**：卡在跑别的任务时，一个**本来可以在它结束后跑**的任务会被错误拒掉；因此「无卡可用」只指**整机没有可用 CUDA 设备**（分支 5），两者不得混用。
- **收敛与拒绝的边界**：整数 `batch` 超过该组合实测 `max_batch` 时**自动收敛**并在提交响应 `warnings[]` 记 `CONVERGED_TO_DEVICE_MAX`（通道 A，§3.9）；超过 `param_schema` 硬上限 128 才是 422 `PARAM_OUT_OF_RANGE`。
- **预检失败不留痕**：5 个分支都在**入队之前**发生，不产生 `jobs/<job_id>/` 目录、不占队列位次、不改变任何 token 状态。

#### §4.2.3 显存账本与选卡

**账本公式（唯一）**：

```text
available_mb(device) = free_mb(实测空闲)
                     - Σ(该卡上**仅** preparing 任务的 vram_estimate_mb)     # 即 capabilities.devices[].in_flight_estimate_mb
                     - gpu_reserve_mb
```

| 项 | 说明 |
| --- | --- |
| `free_mb` | 每卡**实测空闲**显存（`torch.cuda.mem_get_info` / `nvidia-smi` 轮询，缓存 5 秒）。**它已包含 `running` 训练任务与推理服务的真实占用** |
| `Σ(仅 preparing)` | 该卡上 `preparing` 任务（含刚被选中、进程尚未吃满显存的）的 `vram_estimate_mb` 之和。**不含 `running`**：`running` 的实际占用已经体现在 `free_mb` 里，再减一次会造成**双重扣减**，账本会越来越偏离实际、把本可派发的任务长期压在队列里。`capabilities.devices[].running_estimate_mb` 是**只读诊断字段**，不参与本公式（§3.6） |
| `gpu_reserve_mb` | 每卡为推理预留的显存（默认 **1024** MB），**任何情况下不给训练用**。训练与推理**不区分卡**：推理的实际占用只体现在 `free_mb` 里，本项只是额外留出的安全余量 |
| 选卡 | **best-fit**：在满足 `available_mb >= est_mb` 的卡中选 `available_mb` **最小**者（减少碎片）；若该任务有 `last_device_index` 且那张卡仍在候选中，**优先回到原卡**（§4.2.1） |
| 账本的一致性 | `in_flight_estimate_mb > 0` ⟺ 存在 `preparing` 任务；`running_estimate_mb > 0` ⟺ `queue.running > 0`（§3.6 的自洽性要求） |
| 标定期不适用 | 标定在启动期完成（§2.5），**不占运行期并发位**：调度器与账本在标定之后才建立，本表所有规则只在服务就绪后适用 |

**`queued_reason` 三值（写死；排队是正常路径、不是拒绝）**：

| 值 | 触发条件 | 文案要求 |
| --- | --- | --- |
| `WAITING_DEVICE_VRAM` | 设备集合里没有任何卡的 `available_mb >= est_mb`，但**最终容量**放得下（等它释放 / 等前方任务释放） | 可附人类可读细节（如「设备 0 可用 12345 MB < 需要 15800 MB」） |
| `WAITING_PREVIOUS_JOBS` | 目标卡上都已有 `running` / `preparing` 任务占用**实测空闲显存** | 同上 |
| `WAITING_CONCURRENCY_SLOT` | `max_concurrent_jobs` 或某卡的 `max_concurrent_per_device` 已占满 | 同上 |

**文案纪律（写死）**：三者**都不得**写成「本机跑不了」之类的绝对化说法——「本机永远跑不了」对应提交期的 422 `INSUFFICIENT_VRAM`（`reason=insufficient_capacity`），与排队是不同语义，客户端据此给不同的 UI（客户端篇 §5.5）。`min_device_total_mb` 的口径见 §3.11。

#### §4.2.4 心跳与僵尸判定

| 项 | 取值 / 行为 |
| --- | --- |
| 心跳周期 | **15 秒**（§3.11）：训练进程周期性把 `{"pid", "proc_start_time", "heartbeat_at"}` 合并写进 `state.json`（心跳与 `status=running` 由**同一次合并写**落盘，§3.4.3 不变量 1） |
| 僵尸判据 | `now − heartbeat_at > heartbeat_timeout_min`（默认 **10 分钟**）⇒ 视为僵尸 |
| 探活（Linux，唯一实现） | `os.kill(pid, 0)` 做存在性检查 **且** 读 `/proc/<pid>/stat` 第 22 字段 `starttime`，与 `state.json.proc_start_time` **相等**——**两者都通过**才认为「该进程仍在」，从而防 **PID 复用**被误判为存活 |
| 探活失败（如 `/proc` 不可读、权限不足） | 以**心跳时间作为唯一判据**：`now − heartbeat_at > heartbeat_timeout_min` 即视为僵尸 |
| 两级判定（写死） | 「**探活通过 且 心跳新鲜**」才认为存活；只要心跳超时，无论探活结果如何都判僵尸（心跳是**兜底**判据，先行判据是心跳超时本身） |
| 处理 | 杀进程树（`os.killpg(os.getpgid(pid), SIGTERM)` → 宽限 → `SIGKILL`）→ 状态置 `interrupted`；随后的归档、自动重排队尾或落终态由 §4.3 的恢复流程与 §3.4.2 的状态转移表决定 |
| 服务重启场景 | **同样只按上表判定**（没有平台分支）：先消费终态与产物，再判存活；否则「服务离线期间已正常完成」的任务会被误判为僵尸而重训（顺序见 §4.3） |
**`queue.json` 的读-改-写：每一步都必须在队列锁内完成（清单）**：

| 操作 | 触发 | 对 `items[]` 的动作 | 同一事务里的另一次写 |
| --- | --- | --- | --- |
| 入队 | 提交 `POST /jobs`（预检通过） | 追加到**队尾** | 写 `jobs/<job_id>/request.json` |
| 入队 | 手动恢复 `POST /jobs/{id}/resume` | 追加到**队尾** | 写 `manual_resume` 事件、重置 `attempt=1` / `finished_at=null` |
| 入队 | 自动重试 / `interrupted` 自动重入队 | 追加到**队尾**（`attempt+1`；`created_at` 取本次入队时刻） | **先**写 `state.json`（`attempt`）、**后**写 `queue.json` |
| 出队 | 派发成功 | 移出该项 | 写 `state.json`（`status=preparing`、`device_index`、`preparing_at`、`deadline_at`） |
| 出队 | 用户取消（`queued` 直接出队） | 移出该项 | 写 `state.json`（`status=cancelled` + `finished_at`） |
| 位次重排 | 任何一次入队 / 出队 | 重新按 `created_at` 升序落盘 | — |
| 重建 | 服务启动（§2.6 第 ② 步） | 用 `queue.json` + `jobs/*/state.json` 重建 `queued` 列表 | 修 `state.json` 的 `attempt`（以 `items[].attempt` 为准） |

**状态流转的一小段示例（说明位次与 `created_at` 的关系）**：

```text
t1  提交 A -> items = [A(attempt 1)]
t2  提交 B -> items = [A, B]                    # 严格 FCFS，B 不插队
t3  派发 A（有卡、有并发位）-> items = [B]        # 出队 + state.json: preparing
t4  取消 B -> items = []                        # queued 直接出队、无 artifacts
t5  提交 C、D -> items = [C, D]
t6  C 运行中崩溃、被判 failed 且 attempt < max_attempts
        -> items = [D, C(attempt 2)]            # **重排队尾**，不是回到队头
t7  手动恢复 D（D 已 cancelled）-> items = [C(attempt 2), D(attempt 1, 新周期)]
```


---

#### §4.2.5 显存估算表

**先看字段与取值来源**（同一份表既服务提交期预检、也服务 auto-batch 折算）：

| 字段 / 量 | 来源 | 取值与口径 |
| --- | --- | --- |
| `source` | 三层加载优先级 | `auto`（本机启动期标定的实测行）/ `manual`（training.yaml 的手工基线）/ `default`（内置默认）。**只有 `auto` 是实测值**，后两者是工程起点值 |
| `points[]` | 标定产物（仅 `auto`） | 每点 `{"batch", "reserved_mb"}`，按 `batch` 升序；`reserved_mb` 取自 `torch.cuda.max_memory_reserved()`。`manual` / `default` 行为**空数组** |
| `baseline_mb` / `per_image_mb` | 标定产物或手工基线 | `auto` 行 = 对 `points[]` 做**最小二乘直线拟合**的参数（用于区间外外推与展示）；`manual` / `default` 行 = 表中直接给出的起点值 |
| `process_overhead_mb` | 标定产物（仅 `auto`） | 空进程开销：在 `torch.cuda.init()` 之后、加载模型之前测得（CUDA context + 非 torch 分配）。`manual` / `default` 行**没有这一项**（`null`）⇒ **按 0 处理**，主公式退化为两项直线式 |
| `max_batch` | 标定产物（仅 `auto`） | 该 `(model, task)` 在本机的**实测 batch 上限**，由「最高有效点 + OOM 上限证据」一起给出；`manual` / `default` 行为 `null` ⇒ **不参与封顶、也不派生数值** |
| `fit` | 标定产物（仅 `auto`） | `{"r2", "residual_pct"}`，随产物与 `capabilities` 下发，说明「这次标定可信到什么程度」 |
| `task_factor` | 常量 | `detect` 1.00 / `segment` 1.25（§3.6）。**只在跨任务折算时用**：`factor = task_factor[目标 task] / task_factor[该行 task]`；命中本行时 `factor = 1.0`，**不乘** task_factor（否则 segment 会被双重放大） |
| `vram_safety_factor` | 配置 | 默认 **1.25**（全局安全系数），可由配置覆盖 |
| `imgsz` | 请求参数 | 标定固定在 `imgsz=640`；其它取值只把**面积比**施加在 `per_image` 项上 |
| `s = (imgsz / 640)²` | 派生 | **面积比**：`baseline_mb` 与 `process_overhead_mb` 是**固定占用**，**不随 `imgsz` 缩放**，只有 `per_image` 项随 `s` 缩放 |
| `min_device_total_mb` | §3.11 | 各卡 `total_mb` 的**最小值**（跨卡判定的唯一口径） |
| `gpu_reserve_mb` | 配置 | 每卡为推理预留，默认 **1024** MB（§4.2.3） |
| `total_mb` | 设备快照 | auto-batch 比例预算里的目标卡总显存；多卡时按 `min_device_total_mb` **保守取最小值** |
| `auto_batch` | §3.6 | `{"assumed": 16, "ratio": 0.60}`：`batch=-1` 的官方语义是「按 60% 显存自动」，`ratio` 即该 0.60 |

**公式与折算（一段伪码，逐字保留要点）**：

```text
# ① 取行与 factor
命中 (model, task) 行                      -> 用该行，factor = 1.0
未命中本行但有同模型其它 task 行           -> 用该行，factor = task_factor[目标 task] / task_factor[该行 task]
两者都没有                                 -> 422 VRAM_ESTIMATE_UNAVAILABLE（不可估算）

# ② 主公式（三项式 + 分段线性插值）
interp_val   = interp(points[], batch)      # 区间内：相邻两点线性插值（穿过每个实测点）；区间外：直线外推
                                            # 无 points[] 时退化为直线式 baseline_mb + per_image_mb * batch
interp_val_s = interp_val + ((imgsz / 640) ** 2 - 1) * per_image_mb * batch
est_mb       = (process_overhead_mb + interp_val_s) * factor * vram_safety_factor
# manual / default 行代入后即 (baseline_mb + per_image_mb * batch * s) * factor * vram_safety_factor
# 外推（区间外）时在响应 / 日志标注 details.extrapolated = true（精度下降）

# ③ 整数 batch 的收敛（先于容量判定）
batch 为整数且 > max_batch（仅 auto 行 max_batch 非 null） -> batch = max_batch
    + 提交响应 warnings[] 记 CONVERGED_TO_DEVICE_MAX（details.requested / applied / max_batch）
batch 为整数且 > 128（param_schema 硬上限）               -> 422 PARAM_OUT_OF_RANGE（参数校验阶段，§4.2.2）
batch 为整数且 < 标定下限（低于最低实测点）               -> 不收敛：按区间外规则用拟合直线外推 + 标注 extrapolated

# ④ auto-batch（batch = -1 或 0<r<1）折算 batch_assumed
ratio    = 0.60（batch = -1）或客户端给定的 r
budget   = ratio * total_mb                                # 原始预算；total_mb 多卡取 min_device_total_mb
batch_cap_by_table = max_batch                             # 仅 auto 行非 null；manual / default 行 = null（不封顶）
batch_upper        = min(max_batch or 128, 128)            # 有效整数 batch 范围 = [1, batch_upper]
search_cap(budget):                                        # 唯一算法：自 batch_upper 向 1 **递减穷举**
    for batch = batch_upper down to 1:
        if est(batch) <= budget: return batch              # 第一个可行值 = 最大可行整数（不依赖 est 单调）
    return 0                                               # 可行集为空 -> 0（不是 -1、不是 1）
batch_cap_by_ratio = search_cap(ratio * total_mb)
# 后验（每次都要做）：③-1 est(batch_cap_by_ratio) <= ratio * total_mb；
# ③-2 最大性：断言所有 batch ∈ (cap, batch_upper] 都不可行（cap = batch_upper 时区间为空、天然成立）；
# ③′ cap = 0（空集）时不计算 est(0)，改断言 [1, batch_upper] 内不存在可行候选
# 任一有效 cap < 1 -> 422 INSUFFICIENT_VRAM（reason = ratio_cap_below_one / table_cap_below_one）
#   **不得**写 max(1, ...)：cap = 0 时按 1 记账会直接突破比例预算
caps           = [能算出的各个 cap]                        # 不含 null / 缺参项
batch_assumed  = min(caps)                                 # 仅当两个 cap 都 >= 1 时才走到这里（此时 max(1, min(caps)) 等价）
# caps 为空（缺 per_image_mb / total_mb / process_overhead_mb）-> 退化为 auto_batch.assumed（默认 16），
#   并在响应 warnings[] 标注估算精度下降（「算不出来」≠「算出来 < 1」）
```

**兜底 1–5（写死；「兜底」= 取行与折算的兜底路径）**：

| # | 情形 | 取值 | 结果 |
| --- | --- | --- | --- |
| 1 | 命中 `(model, task)` 行 | 用该行；`factor = 1.0` | 正常估算 |
| 2 | 未命中本行，但同 `model` 存在其它 `task` 的行 | 用该行的 `baseline_mb` / `per_image_mb`；`factor = task_factor[目标 task] / task_factor[该行 task]` | 正常估算（用 `yolo11n/detect` 行推 `yolo11n/segment`：`1.25 / 1.00 = 1.25`；反向推 `detect`：`1.00 / 1.25 = 0.80`） |
| 3 | 三层表都没有可用的行 | — | **不可估算**：提交 422 `VRAM_ESTIMATE_UNAVAILABLE`；该组合进 `unschedulable[]`（`reason=VRAM_TABLE_INCOMPLETE`） |
| 4 | `batch` 为 auto（`-1` 或 `0<r<1`，需 `allow_auto_batch: true`） | 先按 ④ 折算 `batch_assumed`（**优先用实测 `max_batch` 封顶**），再按兜底 1 / 2 取行套主公式 | 正常估算，但**精度低于整数 batch**（实际 batch 由训练侧依当刻空闲显存决定）；**任一有效 cap < 1 时直接 422 `INSUFFICIENT_VRAM`**（不返回 1） |
| 5 | `batch` 为整数且**超过**该组合的标定上限 `max_batch` | 自动收敛为 `max_batch`（`resolved_params.batch` 记生效值、原值另存 `requested_batch`），**不拒绝** | 响应 `warnings[]` 记 `CONVERGED_TO_DEVICE_MAX`。理由：提交这类请求的用户意图明确（用满该卡），直接放行会让任务运行期 OOM，而一律 422 会把「只是填大了」的合法请求拒掉 |

**一条自洽的算例（本规格只保留这一个完整算例）**：`yolo11s` / `detect` / `batch=-1` / `imgsz=640`，单卡总显存 `total_mb = 24564`、`ratio = 0.60`、`factor = 1.0`（命中本行）、`vram_safety_factor = 1.25`。

```text
行数据（source = auto，与 capabilities 示例里 yolo11s/detect 那一条是**同一组点值**）：
  process_overhead_mb = 210
  points[]            = [{16, 4060}, {32, 6540}, {64, 11500}]
  fit（对三点做最小二乘；三点精确共线：1580 + 155b = 4060 / 6540 / 11500）：baseline_mb = 1580、per_image_mb = 155、r2 = 1.0、residual_pct = 0.0%
  max_batch = 96       ⇒ batch_upper = min(96, 128) = 96
  imgsz = 640 ⇒ s = 1 ⇒ interp_val_s(batch) = interp_val(batch)

预算与区间：budget = 0.60 × 24564 = 14738.4 MB；有效整数范围 [1, 96]
递减穷举（自 96 向下，逐点套最终估算器）：
  batch = 96 -> 区间外外推 1580 + 155 × 96 = 16460
             -> est = (210 + 16460) × 1.25 = 16670 × 1.25 = 20837.5 MB > 14738.4 MB ✗
  batch = 90 -> 1580 + 155 × 90 = 15530 -> est = (210 + 15530) × 1.25 = 19675 MB > 14738.4 MB ✗
  batch = 89 -> 1580 + 155 × 89 = 15375 -> est = (210 + 15375) × 1.25 = 19481.25 MB > 14738.4 MB ✗
  batch = 88 -> 1580 + 155 × 88 = 15220 -> est = (210 + 15220) × 1.25 = 19287.5 MB > 14738.4 MB ✗
  batch = 83 -> 1580 + 155 × 83 = 14445 -> est = (210 + 14445) × 1.25 = 18318.75 MB > 14738.4 MB ✗
  ...（自 96 一路下行；进入已测区间后由插值承接，仍不命中）
  batch = 65 -> 区间外外推 1580 + 155 × 65 = 11655
             -> est = (210 + 11655) × 1.25 = 14831.25 MB > 14738.4 MB ✗
  batch = 64 -> **实测节点值** 11500 -> est = (210 + 11500) × 1.25 = 14637.5 MB ≤ 14738.4 MB ✓
             -> **命中：batch_cap_by_ratio = 64**
# 区间内插值（穿过每个实测节点，因此区间内没有建模误差）：
#   batch ∈ [16, 32]：4060 + 2480 × ((batch − 16) / 16)      # 2480 = 6540 − 4060
#   batch ∈ [32, 64]：6540 + 4960 × ((batch − 32) / 32)      # 4960 = 11500 − 6540
# 区间外（batch > 64 与 batch < 16）：外推直线 1580 + 155 × batch
后验双向验证：
  ③-1 可行性：est(64) = 14637.5 MB ≤ 14738.4 MB ✓（余 100.9 MB）
  ③-2 最大性：(64, 96] 内每一档在递减扫描时都已被逐点判为不可行（扫描留痕，增量代价 0）⇒ 64 确为最大整数
caps = [batch_cap_by_ratio = 64, batch_cap_by_table = 96]
batch_assumed = min(64, 96) = 64                           # 两个 cap 都 ≥ 1，无需 max(1, ·)
  ⇒ vram_estimate_mb = est(64) = 14637.5 MB（入队后按它记账：vram_estimate_mb 全程不变）
  ⇒ 实测上限 96 不是瓶颈，比例预算才是较小者；若先把 batch 拉到 96 再算会得到 20837.5 MB，直接突破 60% 预算
```

**算例的三点说明（避免读者误解）**：

- **算例里的点值是唯一一组**：`capabilities` 示例中 `yolo11s/detect` 的 `baseline_mb` / `per_image_mb` / `process_overhead_mb` / `max_batch` / `points[]` 与本算例**逐字相同**（示例里的 `reserved_mb` 即本算例的节点值），**不再存在两套实测点值并存**的读法。
- **跨任务与其它 `imgsz` 只需换 `factor` 与 `s`**：例如 `yolo11m` / `segment` / `batch=8` / `imgsz=1024`（未命中本行、用同模型 `detect` 行）：`factor = 1.25 / 1.00 = 1.25`、`s = (1024/640)² = 2.56`，代入 `est = (2400 + 280 × 8 × 2.56) × 1.25 × 1.25 = (2400 + 5734.4) × 1.5625 ≈ 12710 MB`——公式不变，只换 `factor` / `s` / 行数据。
- **本规格不再保留的东西**：早期把「整条曲线用一条直线反解」的做法（在节点处与实测值冲突、在小尺寸下会出现零 / 负分母）、同一公式的多份重述、以及历轮的对照反例。现行口径只有一段伪码（本节）+ 一张取值来源表 + 一个算例。

#### §4.2.6 OOM 兜底与 auto-batch

| 机制 | 规则 |
| --- | --- |
| OOM **不判失败** | 训练侧遇 CUDA OOM 时**降 batch 重试**，服务端**不**因此改状态、不重排队、不判失败 |
| 降级留痕 | 每次降 batch 由训练进程写一条 `log` 事件：`{"level": "warning", "message": "...", "code": "OOM_BATCH_DOWNGRADE", "from_batch": 64, "to_batch": 32}`（`code` / `from_batch` / `to_batch` 是 `log` 事件的扩展字段，§3.5）；终态时同一信息汇总进 `summary.json.oom_downgrades[]` |
| 降级上限 `oom_retry_max` | = 配置 `oom_retry.max_retries`（默认 **2**），随 `resolved_params.oom_retry_max` **下发**给训练侧（§3.8.5）。计数口径 = **单次训练进程内** `OOM_BATCH_DOWNGRADE` 的条数。达到上限后写 `log`（`level=error`、`code=OOM_RETRY_EXHAUSTED`，带实际 batch 与显存峰值）与 `done(status=failed)`，服务端按常规 `failed` 分支处理（§3.4.2） |
| 能力下发 | `capabilities.oom_retry` = `{"enabled": true, "max_retries": 2}`（§3.6）；`vram_table.entries[].max_batch` 与 `calibration` 分别给出实测上限与标定状态 |
| `error_summary` 的取值 | 「实际 batch」取事件流中**最后一条** `OOM_BATCH_DOWNGRADE` 的 `to_batch`（无降级则取 `resolved_params.batch`）；「显存峰值」取 `summary.json.oom_downgrades[].peak_mb`——两者都是训练侧落盘内容，服务端**不自行推断**（§3.4.4） |
| 环境无**原生**降 batch 能力 | `capabilities.warnings` 记 `OOM_RETRY_UNAVAILABLE`（**信息性**，§3.6）：语义是「该环境无原生能力，改由 runner 自行捕获 CUDA OOM 并降 batch 重跑，**重试耗时更长**」，**不是**「OOM 直接失败」；可观测口径（事件、`summary.json`、终态）与新版**完全一致**，只是耗时更长 |
| 两条计数轴互不相干 | `oom_retry_max` 限的是**单次训练进程内**降 batch 的次数；`max_attempts`（§3.11）限的是**本轮**自动重跑任务的次数。每次自动重跑都拉起新进程、降级计数重新开始，而 `summary.json.oom_downgrades[]` 按整轮汇总 |

**auto-batch 的四重上界（写死）**：该模式的显存估算精度低于整数 batch，但 `batch_assumed` 受四重约束，任何一条都不可能被突破：

| # | 上界 | 来源 |
| --- | --- | --- |
| ① | `batch_cap_by_ratio`（`ratio × total_mb` 预算下按最终估算器**递减穷举**出的最大可行整数） | §4.2.5 ④ |
| ② | `batch_cap_by_table = max_batch`（本机实测上限；`manual` / `default` 行为 `null` ⇒ 不参与封顶） | 标定产物 |
| ③ | `gpu_reserve_mb` **永不给训练用** | §4.2.3 |
| ④ | 每卡并发默认 1（`max_concurrent_per_device`） | §4.2.1 |

- **实际 batch 由训练侧决定，但「不会超预算」由服务端保证**：auto 值原样保留在 `resolved_params.batch`（客户端详情页展示「服务端按假定 batch 估算，实际 batch 由训练侧决定」），账本按 `batch_assumed` 折算出的 `vram_estimate_mb` 记账。
- **多卡按最小卡算**：`ratio × total_mb` 里的 `total_mb` 取 `min_device_total_mb`（§3.11），宁可低估也不高估。

**过大 batch 的收敛（唯一路径）**：

| 情形 | 行为 |
| --- | --- |
| 整数 `batch` ≤ `max_batch`（或该行 `max_batch` 为 `null`） | 不收敛，按常规预检处理 |
| 整数 `batch` > `max_batch`（且 ≤ 128） | 自动收敛为 `max_batch`：`resolved_params.batch` = 收敛后的生效值、`requested_batch` = 客户端原值；提交响应 `warnings[]` 记 `CONVERGED_TO_DEVICE_MAX`（`details.requested` / `applied` / `max_batch`） |
| 整数 `batch` > 128 | 422 `PARAM_OUT_OF_RANGE`（`param_schema` 硬上限，参数校验阶段即拒绝） |
| auto 取值（`-1` / `0<r<1`） | **不触发**本告警：折算已由 `min(caps)` 封顶（四重上界 ①②），不存在「收敛」这一动作 |

#### §4.2.7 标定矩阵与失败分级

**标定矩阵（10 个模型 × 2 个任务）**：

| 维度 | 取值 |
| --- | --- |
| 模型 | 两个家族各五档：`yolo11{n,s,m,l,x}`、`yolo26{n,s,m,l,x}` ⇒ **10** |
| 任务 | `detect`、`segment` ⇒ **2** |
| 组合数 | 10 × 2 = **20**（= `vram_table.sources` 的 `auto` / `manual` / `default` 三者之和，§3.6） |
| 档位（batch 点位） | 按模型规模自适应，**一次标定整体使用**：`n` / `s` 类 ⇒ `[16, 32, 64]`；`m` / `l` 类 ⇒ `[8, 16, 32]`；`x` 类 ⇒ `[4, 8, 16]`（默认档位，配置键见 §4.4） |
| 每点轮数 | **3 个 epoch**：训练 + 验证各跑一轮即达显存峰值，无需跑满 |
| 规模上界 | 20 组合 × 3 点 = **60 次短跑**；实际 = **可用性过滤后剩余的 N 个组合** × 3 点（`N ≤ 20`），约 **10–15 分钟**；只在 auto 产物缺失 / 指纹不一致时执行 |
| `min_points` | **2**：某组合的**有效**测量点少于 2 ⇒ 判「标定失败」（失败分级 ②） |
| 拟合依据 | `torch.cuda.max_memory_reserved()`（含缓存分配器持有的块，比 `max_memory_allocated()` 更接近真实占用） |
| 同点交叉校验 | 同时记录 `torch.cuda.max_memory_allocated()` 与 `nvidia-smi` 的**设备级增量**（采样前后差值）；三者差异过大时在日志与产物中标注 `mismatch` |
| 测量点结构 | 每点记录 `{batch, reserved_mb, allocated_mb, device_delta_mb, process_overhead_mb, epochs, ok}`；OOM 点记录 `ok=false` 与失败时的显存峰值（作为上限证据） |
| 空进程开销 | `process_overhead_mb`：在 `torch.cuda.init()` 之后、加载模型之前测得（三项式的第一项） |
| `max_batch` 的给出方式 | 由「最高有效点 + OOM 上限证据」一起给出（因此可高于最高实测点，但不会高于硬上限 128） |
| 合成标定数据集的参数 | **权威定义在 §4.4 的配置键**；本节只写行为：默认路径下运行时合成、系统临时目录、标定结束即清理（§2.5） |

- **`vram_table.sources.auto` 与 `optimizer=auto` 无关**：前者指本机标定产物的来源计数（`auto` = 本机实测行，与 `manual` / `default` 并列，§3.6），后者是优化器取值哨兵（§3.8.4 / §3.8.5）；两者**不得混用表述**。

**单点 OOM 的自适应（属预期，不算失败）**：某点 OOM = 一条「该 batch 不可行」的**上限证据**——记录该点（`ok=false` + 峰值）后**向下取半重试**，下限由 `batch_min`（默认 4）给出；降半重试本身复用 OOM 兜底语义（每点至多 `oom_retry_max` 次，§4.2.6）。**单点 OOM 绝不算标定失败。**

**失败与跳过的分级（写死）**：

| 级别 | 情形 | 行为 |
| --- | --- | --- |
| ① | **单点 OOM** | 取半重试（下限 `batch_min`），**不计入失败** |
| ② | 某组合**全部点位都 OOM**（有效测量点 < `min_points`） | 该组合在本设备**不可调度**：进 `unschedulable[]`（`reason=VRAM_CALIBRATION_FAILED`）与 `calibration.failed[]`，`warnings` 记 `VRAM_CALIBRATION_FAILED`；提交该组合 422 `VRAM_ESTIMATE_UNAVAILABLE`。**不导致启动失败、也不触发下次重标**（该组合算「已标定」） |
| ②′ | **被可用性过滤跳过的组合**（家族不可用 / 权重缺失 / 无可用设备） | **跳过 ≠ 失败**：不重试、不拒绝启动、不进产物；逐条进 `unschedulable[]`（带 `reason`）与 `calibration.skipped[]`（`model` / `task` / `reason`）。**无可用设备是整轮跳过**：只写 `calibration.skipped_reason=no_device`、`skipped[]` 为空、不产生也不写 auto 产物 |
| ③ | **真正的异常**：标定子进程崩溃、auto 产物写入失败、显式指定的标定数据集缺失 / 不可读 | **有限次重试后拒绝启动**：重试次数与间隔见 §4.4；耗尽仍失败 ⇒ 打日志写明失败步骤与 errno，进程以**非零退出码**结束（systemd 视为启动失败） |

**落到 `capabilities` 的四处（形状见 §3.6）**：

| 落点 | 内容 |
| --- | --- |
| `vram_table.unschedulable[]` | `[{"model", "task", "reason"}]`——**可调度性的权威结论**，`reason` 取 `VRAM_TABLE_INCOMPLETE` / `VRAM_CALIBRATION_FAILED` / `MODEL_FAMILY_UNSUPPORTED` / `WEIGHT_NOT_AVAILABLE`（四值口径见 §3.6） |
| `calibration.skipped[]` | 本轮**被可用性过滤逐组合跳过**的记录（**本次标定过程**的如实记录）；与 `unschedulable[]` **不得互相矛盾**（家族不可用 / 权重缺失的组合两处都出现，同源同 `reason`） |
| `calibration.failed[]` | 真正跑了但**全部点位 OOM** 的组合（`reason=VRAM_CALIBRATION_FAILED`） |
| `calibration.skipped_reason` | 整轮跳过时只有 `"no_device"`；未整轮跳过时为 `null` |

- **不新增告警码**：过滤 ① 复用既有 `MODEL_FAMILY_UNSUPPORTED` 能力字段、过滤 ② 复用既有 `WEIGHTS_MISSING`、过滤 ③ 走 `calibration.skipped_reason`；`capabilities.warnings` 的 code 枚举**不因本节扩大**（§3.6 的 7 个）。
- **不可调度优先于下层表**：全点 OOM 的组合**即使下层表有可用行**也一律**不可调度**（下层行只用于 `entries[]` 展示 `source` 与容量参考，**不参与放行**）。
- **标定矩阵与调度无关**：矩阵里的点位是**启动期**的短跑，不占运行期并发位、不进 `queue.json`、不受 `max_concurrent_jobs` 约束（§2.5）。

---

### §4.3 任务执行与恢复

**本节范围**：训练进程模型、取消与恢复语义、`needs_attention` 置位、文件协议、服务重启接管、产物与终态 finalizer、训练环境落地。状态机与 job 字段见 §3.4，事件协议见 §3.5，队列结构与派发循环见 §4.2.1，心跳与僵尸判据见 §4.2.4，参数面与 preset 算法见 §3.8，跨侧常量见 §3.11，配置键见 §4.4。

#### §4.3.1 恢复语义

**可恢复集合（状态级资格，决定「恢复」按钮是否可能出现）= `failed` / `interrupted` / `cancelled`；`completed` 不可恢复**（唯一没有恢复出口的终态）；`queued` / `preparing` / `running` 不可恢复但**可取消**（§4.3.2）——恢复按钮是否真的可用，还要叠加下面的实时判定。

**实时判定（`resume_mode_available`；不缓存，接口层入队前再判一次）**：

| 判定条件（实时） | `resume_mode_available` | `POST /jobs/{id}/resume` 结果 |
| --- | --- | --- |
| `status` ∉ {`failed`, `interrupted`, `cancelled`} | `[]` | 409 `JOB_NOT_RESUMABLE`（`details.status`） |
| 按 `state.json` 的 `pid` / `proc_start_time` 探活判定**上一进程仍存活** | `[]` | 409 `JOB_NOT_RESUMABLE`（`details.reason="process_alive"`；**无副作用**） |
| `resume_intent.json` 处于未收尾中间态 | `[]` | 409 `JOB_NOT_RESUMABLE`（`details.reason="resume_in_progress"`） |
| 数据集缺失 / 已被 TTL 清理 | `[]` | 409 `JOB_ARTIFACTS_EXPIRED`（`details.reason="dataset_expired"`） |
| 数据集在、`run/train/weights/last.pt` 存在 | `["resume"]` | 200，`mode=resume`（以 `resume=True` 续训） |
| 数据集在、无 `last.pt`、`resume_fallback: restart` | `["restart"]` | 200，`mode=restart`（从零重训） |
| 数据集在、无 `last.pt`、`resume_fallback: fail` | `[]` | 409 `JOB_ARTIFACTS_EXPIRED`（`details.reason="checkpoint_missing"`） |

错误码、`details` 与响应封装见 §3.3；`resume_mode_available` 为空时客户端把按钮置灰，服务端在接口层仍兜底返回上表的 409。请求体可选 `{"mode": "resume"}`、缺省等价 `resume`；只接受出现在 `resume_mode_available` 中的取值，否则 400 `VALIDATION_FAILED`（`details.field=mode`）。

**手动恢复处理链（同一把 `state.lock` 内的多步事务；顺序写死）**：

| 步 | 动作 | 落盘的副作用 |
| --- | --- | --- |
| 0 | 取锁并复核：**无活进程**（探活）、`status` 属于可恢复集合、intent 已收尾 | `resume_intent.json`（`phase` 单调前进） |
| 1 | `attempt` **重置为 1**，开启新一轮自动重试预算 | — |
| 2 | `resume_cycles` +1，追加 `manual_resume` 事件（`flush` + `fsync`）——**恢复周期分界** | `events.jsonl` |
| 3 | 同一把锁内原子写 `state.json`：`status=queued`、`attempt=1`、`resume_cycles`、`cycle_started_at`、`finished_at=null`、`needs_attention=false` / `needs_attention_reason=null`、`pid` / `proc_start_time` / `heartbeat_at` 清零 | `state.json` |
| 3b | 把上一周期的终态证据**移出原位** → `jobs/<job_id>/archive/attempt-<cycle>-<attempt>/`（布局见 §4.3.7） | `archive/` |
| 4 | 在同一把锁内追加到 `queue.json` 队尾（队列里已有该 job ⇒ 替换该条目） | `queue.json` |
| 5 | intent 置 `enqueued` 并释放锁，响应新的 `queue_position` | `resume_intent.json` |

- **第 2 步必须早于第 3 步（不可交换）**：崩溃在两步之间属于**安全侧**（周期分界已生效、`state.json` 还停在旧周期）⇒ 旧周期的 `done` 立刻不再属于当前周期（§4.3.6 ①）；反过来会留下「周期序号已递增、事件文件里却没有分界」的危险中间态——旧周期的 `done` 会被当成本周期终态收账。
- `cycle_started_at` 与 `resume_cycles` 在**同一次原子写**里落盘；它是「产物是否属于当前周期」的周期标记（§4.3.6 ②）。首个周期的取值口径与 `preparing_at` 同源。
- **加锁顺序固定为「先 `state.lock`、后队列锁」**：任何路径都不得反向获取（§4.2.1）。
- **等待上限 `job_state_lock_timeout_seconds`**（取值见 §4.4.2）：`resume` 接口拿不到锁 ⇒ 409 `JOB_NOT_RESUMABLE` + `details.reason="lock_timeout"`；服务端内部路径（调度器 / 心跳回调 / 接管扫描）超时 ⇒ 记 WARN 并本轮跳过该 job，**绝不**在未持锁时继续写。
- **中途异常**：判据只看**事件文件**，不看 `phase` 标签——没有 `data.resume_cycles == resume_cycles_target` 的 `manual_resume` ⇒ 周期分界未生效：把 intent 置 `aborted` 后返回 500 `INTERNAL_ERROR`，**业务状态零副作用**（`status` / `finished_at` / `resume_cycles` / `cycle_started_at` / `queue.json` / `events.jsonl` 均不变），客户端可安全重发；已有该 `manual_resume` ⇒ **不回滚、不标 `aborted`**，把 intent 留在当前 `phase` 并返回 500，由启动对账幂等前滚（§4.3.6 第 0 步）。
- **两个并发 `resume`**：第二个请求在 `state.lock` 上等待，等第一个把 `status` 置 `queued` 并释放锁后进入判定表第 1 行 ⇒ 409 `JOB_NOT_RESUMABLE`（`details.status="queued"`）。净效果：`resume_cycles` **恰好 +1**、`manual_resume` **恰好一条**、`queue.json` 里该 job **恰好一个条目**。

**恢复语义（生效值）**：

| 项 | 口径 |
| --- | --- |
| `attempt` | **本轮（当前恢复周期）内**从 1 开始：每次自动重试（含 `interrupted` 自动重入队）在本轮内 +1；**手动恢复后重置为 1** |
| `max_attempts` | 默认 **3**（含首次，§3.11）；**只约束本轮自动重试**，不跨手动恢复累加；手动恢复开启新一轮预算 |
| `resume_cycles` | 每次成功的 `resume`（`resume` / `restart` 都算）+1；与 `attempt` 正交 |
| 恢复的排队位置 | **队尾**（`resume_to_queue_head: false`）；响应返回新的 `queue_position` |
| `resume_fallback` | `restart`（默认：无 `last.pt` 时从零重训）/ `fail`（无 `last.pt` 即不可恢复） |
| 显存 | 恢复时重新申请；参数未变则沿用原 `vram_estimate_mb`（不重算） |
| `auto_resume` | 默认 `true`；**只**决定 `interrupted` 是否自动重入队，**不**约束 `failed` 的自动重排 |
| 数据集到期 | **不因终态任务阻止**：`failed` / `interrupted` / `cancelled` 引用的数据集照常过期，之后该任务 `resume_mode_available=[]` |
| 对 `completed` 调 `resume` | 409 `JOB_NOT_RESUMABLE` |

**自动重试入队协议（`failed` / `interrupted` 的自动重排共用；同一把 `state.lock` 内三步，顺序写死）**：

```text
① 归档上一 attempt 的终态证据 → jobs/<job_id>/archive/attempt-<cycle>-<attempt>/
   （cycle = 当前周期序号、attempt = 刚结束的 attempt 序号；只归档实际存在的文件；last.pt 永不移动）
② 单次原子写 state.json：status="queued"、attempt=a+1、finished_at=null、
   needs_attention=false / needs_attention_reason=null、
   pid / proc_start_time / heartbeat_at 一并清零为 null（清掉上一 attempt 的探活痕迹）
③ 紧随其后写 queue.json：追加队尾，attempt 取 a+1、created_at = now；
   队列里已有该 job ⇒ 在同一次队列原子写里「替换」该条目，不得追加出第二个
```

- **② 是 `attempt` 计数器的唯一落盘点** ⇒ `state.json.attempt` 与队列项的 `attempt` 恒一致；队列项的 `attempt` 是 `state.json` 缺失 / 损坏时重建最小 `state.json` 的**权威取值**（§4.2.1）。
- **两个顺序都安全**：② 之前崩溃 ⇒ 重启时该 job 不在 `queue.json`、`status` 仍可重排且 `finished_at=null` ⇒ 由 §4.3.6 的在途分支重走本协议（幂等重排，不丢任务）；③ 之前崩溃 ⇒ 重启时 `status="queued"` 而 `queue.json` 无它 ⇒ 由 §4.3.6 的队列交叉修复按 `created_at` 补回队尾。
- **不截断也不改写 `events.jsonl`**：旧 attempt 的 `done` 留在文件里，靠恢复周期分界与 `done.data.attempt` 排除（§4.3.6 ①）。
- **`failed` 的自动重排不读 `auto_resume`**：判据只有「本轮 `attempt < max_attempts`」。`interrupted` 的自动重入队**同时**要求 `auto_resume=true` **且** `attempt < max_attempts`，其余组合一律落 `interrupted` 终态分支（§3.4.2、§3.4.3）。

#### §4.3.2 取消语义

| 阶段 | 行为 |
| --- | --- |
| `queued` | 直接从 `queue.json` 出队；`status=cancelled` + 写 `finished_at`；**不产生 artifacts** |
| `preparing` | 终止准备流程、清理 `run/train` 半成品；落 `partial/`（若有）；`status=cancelled` + 写 `finished_at` |
| `running` | `os.killpg(os.getpgid(pid), SIGTERM)` → 等 `cancel_grace_seconds`（默认 15，§3.11）→ 仍存活则 `os.killpg(..., SIGKILL)`；终止成功即 `status=cancelled` + 写 `finished_at` |

- **幂等**：对已处于终态的任务再次 `cancel` 返回 200 与当前状态，不报错、不改任何字段。
- **取消是终态且不会被自动续训**：只有 `interrupted` 会按 §4.3.1 自动重入队；用户取消的任务只能**手动**恢复。
- **`partial/` 内容**（只落实际存在的文件）：`weights/last.pt`、已产出的 `results.csv`、`args.yaml`、`train.log`、`events.jsonl`。产物清单里这些条目 `partial: true` 且 `path` 统一带 `partial/` 前缀（`files[].path` 恒为相对 `artifacts/<job_id>/` 的路径）；下载只接受受控 `file_id`（§3.10），打包下载在 zip 内保留同一 `partial/` 目录结构。
- **SIGKILL 后仍存活（唯一收口，写死）**：进程杀不掉**不改变取消语义**——`status` 保持 `cancelled`、写 `finished_at`（`is_terminal=true`）、`needs_attention=true` / `needs_attention_reason=resume_anomaly`，并记一条 **WARN** 日志（含 `pid` / `pgid`）。**同一取消路径只有一个终态**：不得改写成 `failed`，也不得为它另造专属的 `error_summary` 取值（如「进程不可杀」这类文案）。该终态**不参与自动重排**（进程仍存活，重排没有意义），只能经 `POST /jobs/{id}/resume` 手动恢复——而 `resume` 的「无活进程」前置判据会先挡住并返回 409 `JOB_NOT_RESUMABLE` + `details.reason="process_alive"`。该 job 若仍在 `queue.json` 中，由 §4.3.6 的队列子分支出队。
**取消路径的落盘细节（同一把 `state.lock` 内，顺序写死）**：

| 步 | 动作 | 说明 |
| --- | --- | --- |
| 1 | `queued`：在同一次队列原子写里把该 job 移出 `items[]` | 无 `partial/`、无 `artifacts/`；`partial_available=false` |
| 2 | `preparing` / `running`：先终止进程树（未启动时跳过） | 按 `SIGTERM` → `cancel_grace_seconds` → `SIGKILL`；每步都判「是否已退出」 |
| 3 | 落 `artifacts/<job_id>/partial/`（只落实际存在的文件） | 进程退出后复制，不复制半截文件 |
| 4 | 原子写 `state.json`：`status="cancelled"` + `finished_at` + 两个 `needs_attention` 字段（按 §4.3.3 现算） | 取消路径**不**写 `summary.json`、**不**触发终态 finalizer |
| 5 | 若该 job 仍在 `queue.json` 中（取消与派发并发）⇒ 在同一次队列原子写里出队 | 与 §4.3.4 的「出队先于 `Popen`」配合 ⇒ 不残留队列项 |

- **取消与派发并发**：取消请求在 `state.lock` 上等待；若此刻调度器已把该 job 出队并置 `preparing`，取消按 `preparing` 处理（终止进程树）——**不会**出现「已取消的 job 又被派发一次」。
- **取消不触发归档**：`cancelled` 是用户可见的终态，产物留在 `jobs/<job_id>/run/` 与 `artifacts/<job_id>/partial/`；只有**重新入队**（手动恢复或自动重排）才会把上一 attempt 的证据移入 `archive/`（§4.3.1、§4.3.7）。
- **与心跳写的并发**：训练进程的心跳写与取消 / 终态写在同一把 `state.lock` 内，且字段所有权不相交 ⇒ 取消写不会抹掉 `pid` / `heartbeat_at`，心跳写也不会把 `status` 改回 `running`（§4.3.5）。

#### §4.3.3 `needs_attention` 置位

| 条件 | `needs_attention_reason` | 置位点 |
| --- | --- | --- |
| 自动重试耗尽 | `attempts_exhausted` | `status=failed` 且本轮 `attempt >= max_attempts` 的终态收账；`failed` 且预算未用尽时**不得**置位（此时不写 `finished_at`，走自动重排） |
| 恢复 / 重试流程异常 | `resume_anomaly` | 心跳判僵尸且放弃自动恢复的 `interrupted` 终态（`auto_resume=false` 或本轮预算用尽）；服务重启接管失败（`state.json` 缺失 / 损坏）；**终态标记丢失**（`status ∈ {completed, cancelled}` 而 `finished_at == null`）；取消路径上 SIGKILL 后仍存活（§4.3.2）；resume 后首 epoch loss 爆炸（同时置 `artifact_suspect`） |
| 产物可疑 | `artifact_suspect` | `artifact_suspect=true`：`best.pt` 复算指标与 `results.csv` 末轮差异超阈值或为 0 / NaN；或终态 finalizer 只能回退 `last.pt`（§4.3.7） |

- **单值优先级**：`attempts_exhausted` > `resume_anomaly` > `artifact_suspect`；`needs_attention=false` 时 `needs_attention_reason=null`（字段与取值域见 §3.4.4）。
- **终态收账一律按本表现算，不得在 `completed` / `cancelled` 分支写死 `false` / `null`**：任何把 job 收成终态（`completed` / `cancelled` / `failed` / `interrupted`）的原子写，都必须在**同一次写**里按优先级算出这两个字段。`completed` 不是无条件 `false`——`artifact_suspect=true` 而前两条都不适用时取 `true` / `artifact_suspect`；终态 finalizer 在刷新 `artifact_suspect` 后必须按同一优先级**重算并覆盖**这两个字段（§4.3.7）。
- **`interrupted` 终态分支不取 `attempts_exhausted`**（它严格限定为 `status=failed` 且本轮预算用尽），取 `resume_anomaly`。
- **环境与标定信息不进入本字段**：它们只走 `capabilities.warnings`（§3.9）的信息性通道，客户端据此显示信息条，不弹「需人工介入」。客户端按 `reason` 分别给文案，`attempts_exhausted` 的文案需给出「可恢复并重置重试预算」的语义。

#### §4.3.4 进程模型

训练是**独立进程**，脱离服务端进程组；服务重启不杀训练（systemd `KillMode=process`，§4.4.5）。**Linux 唯一实现**。

```python
proc = subprocess.Popen(
    cmd, cwd=str(job_dir), env=env,
    start_new_session=True,                     # 新会话 / 新进程组：脱离 uvicorn，可用 killpg 整组终止
    stdout=log_fp, stderr=subprocess.STDOUT, close_fds=True,
)
```

| 项 | 规则 |
| --- | --- |
| 可执行文件 | `training.python_executable` 非空则用它；为空用服务端解释器 `sys.executable`（逃生舱，键见 §4.4.2） |
| 入口 | `-m app.custom.executor.run_train --job-dir <jobs/<job_id>>`；runner 只依赖 `app/custom/` 与 ultralytics |
| 环境变量 | `PYTHONUNBUFFERED=1`；`XANYLABELING_TRAIN_EVENT_FILE` / `XANYLABELING_TRAIN_STATE_FILE`（`events.jsonl` / `state.json` 的绝对路径）；`XANYLABELING_TRAIN_ATTEMPT` / `XANYLABELING_TRAIN_RESUME_CYCLES`（本次 `attempt` / 当前周期序号，写 `done` 用）；其余继承服务端环境 |
| 输出 | `stdout` / `stderr` 全部重定向到 `jobs/<job_id>/train.log`（**不用管道**：避免服务端阻塞与僵尸） |
| 终止 | `os.killpg(os.getpgid(pid), SIGTERM)` → 等 `cancel_grace_seconds` → `SIGKILL`（§4.3.2）；**不得**只对主进程发信号——训练进程会派生 dataloader 子进程 |
| 探活 | `os.kill(pid, 0)` **且** `/proc/<pid>/stat` 第 22 字段 `starttime` 与 `state.json.proc_start_time` **相等**——两者都通过才算「该进程仍在」（防 PID 复用）；判据与心跳兜底见 §4.2.4 |
| OOM 轮次上限 | runner 从 `request.json` 的 `resolved_params.oom_retry_max` 读取（§3.8.5）；达到上限即放弃降 batch 重试、写 `done(status=failed)` |
| 权重 | 服务端把模型名解析为 `<work_dir>/weights/<name>.pt`（§4.4.1）：文件在则直接用；缺失且 `allow_weight_download: true` 时从官方下载并缓存；缺失且为 `false` ⇒ 提交预检 422 `WEIGHT_NOT_AVAILABLE` |
| 数据描述 | 服务端生成 `jobs/<job_id>/data.yaml`（`path` / `train` / `val` / `names` 指向数据集 content 目录） |

**`queued → preparing` 的那一次原子写（在 `Popen` 之前，顺序不可交换）**：

```text
持 state.lock 读-改-写 state.json（tmp + fsync + os.replace）：
  status="preparing"、preparing_at=now、attempt=n、
  deadline_at=preparing_at + heartbeat_timeout_min、
  pid=null、proc_start_time=null、heartbeat_at=null
随后才 Popen
```

- `preparing_at` / `deadline_at` 是「本次 preparing 从什么时候开始、宽限到什么时候」的**权威依据**，跨服务重启仍可用（§4.3.6 的 Popen 窗口宽限期）；`Popen` 返回后不再改写这两个字段。
- **先落盘、后 `Popen`**：反过来的话，父进程在 `Popen` 后崩溃会留下「无 `preparing_at` 的孤儿岗位」，宽限期无法判定。
- **派发顺序**（唯一顺序见 §4.2.1 的派发循环）：持 `state.lock` → 取队列锁 → 出队 → 同一临界区内写 `state.json`（上面的原子写）→ **最后**才 `Popen`；**出队先于 `Popen` 不可交换**——反向顺序会留下残留队列项（§4.3.6 的队列子分支正是为这一输入而写）。
- 训练进程启动后把 `pid` / `proc_start_time` / `heartbeat_at` / `status=running` 用**同一次合并写**落盘（心跳周期 15 秒，§3.11）。「`heartbeat_at` **首次出现**」是 `preparing → running` 的**唯一**判据（§3.4.3），服务端不得代写 `status=running`。

**preparing 流程（`Popen` 之前完成）**：

| 步 | 动作 | 落点 |
| --- | --- | --- |
| 1 | 把请求体与服务端解析后的最终参数一起落盘 | `jobs/<job_id>/request.json`（含 `resolved_params`，§3.8.5） |
| 2 | 按数据集 `content/` 生成 Ultralytics 描述文件 | `jobs/<job_id>/data.yaml`（`path` / `train` / `val` / `names`） |
| 3 | 建训练落点目录（`exist_ok=True`） | `jobs/<job_id>/run/train/` |
| 4 | 落盘调度器分配的卡号 | `resolved_params.device`（`queued` 阶段为 `null`） |
| 5 | 出队 + 写 `state.json`（上面的原子写）后 `Popen` | `queue.json` / `state.json` |

**runner（`-m app.custom.executor.run_train --job-dir <job_dir>`）的职责（写死）**：

| 职责 | 说明 |
| --- | --- |
| 读参数 | 从 `request.json` 读 `resolved_params`（含 `oom_retry_max`）；除环境变量外不读任何服务端配置 |
| 落盘探活三件套 | 首次合并写落 `pid` / `proc_start_time` / `heartbeat_at`，并把 `status` 由 `preparing` 置 `running`（§4.3.5） |
| 周期心跳 | 每 15 秒合并写一次 `heartbeat_at`（§3.11） |
| 追加事件 | 四类必需事件（`progress` / `metrics` / `log` / `done`），每条 `flush` + `os.fsync`；追加前先截断残缺尾行（§4.3.5） |
| 成功标记 | 训练成功返回后按「`train_success.json` → `summary.json` → `done` → 退出」的顺序落盘（§4.3.5） |
| 退出码 | `0` = 成功；非 0 或异常退出 ⇒ 服务端按 `failed` 收账（`error_summary` 取末 20 行 + 退出码） |
| 不做的事 | 不触碰服务端所有的字段（`status` 只在首次合并写时由 `preparing` 置 `running`）、不写 `queue.json`、不写 `resume_intent.json`、不删任何文件 |

#### §4.3.5 文件协议

事件类型、`data` 字段、写者分工与增量语义已在 §3.5 定义；本节只写**文件侧**规则：谁写哪个字段、`seq` 交接、`fsync` 与原子替换。

```text
jobs/<job_id>/
  state.json           # 服务端（终态侧）与训练 runner 共写；字段所有权不相交、读-改-写全程持 state.lock
  state.lock           # 跨进程互斥锁载体（fcntl.flock）；只创建、不删除，不参与任何 TTL 清理
  events.jsonl         # 串行追加：服务端写 manual_resume、runner 写其余四类；seq 全局严格递增
  train.log            # 训练进程 stdout/stderr 合并；人类可读，只追加
  train_success.json   # runner 在「训练成功返回之后、写 done 之前」原子落盘的成功完成标记
  resume_intent.json   # 服务端手动恢复链的持久化 intent（最小版，见下）
```

| 文件 | 谁写哪个字段 | 交接与落盘规则 |
| --- | --- | --- |
| `state.json` | **训练进程**：`pid` / `proc_start_time` / `heartbeat_at`（**首次**合并写时一并把 `status` 由 `preparing` 置 `running`；此后**只**更新 `heartbeat_at`，不得再改 `status`，更不得把终态改回 `running`）。**服务端**：`status` / `finished_at` / `attempt` / `resume_cycles` / `cycle_started_at` / `preparing_at` / `deadline_at` / `needs_attention` / `needs_attention_reason` / `error_summary` / `device_index` / `last_device_index`。**服务端不写心跳三件套的取值**，只有两处例外（把三个字段一并清零为 `null`）：① 入队原子写（§4.3.1 的手动恢复链第 3 步与自动重试入队协议 ②）；② `queued → preparing` 的那一次原子写（§4.3.4） | 原子替换：读 → 合并本人字段 → 写 `state.json.tmp` → `flush` + `os.fsync` → `os.replace`，**全程持 `state.lock`**。`os.replace` 只保证读者看不到半个文件，**不提供** read-modify-write 互斥 ⇒ 这把锁是**必需**的，不是优化 |
| `events.jsonl` | **训练进程**：`progress` / `metrics` / `log` / `done`（`done` 是同一 attempt 内**唯一**一条终态事件）。**服务端**：`manual_resume`（**入队之前**写，充当恢复周期分界）。两个写者**天然不重叠**（服务端写时该 job 没有活着的训练进程）⇒ **不需要**文件级追加锁 | 每行一个事件（行格式与 `data` 见 §3.5）；两个写者都 `flush` + `os.fsync` **每条**事件。`seq` 交接：追加前先**截断残缺尾行**、再反向求 `last_seq`，新记录编号 = `last_seq + 1`（runner 在 `Popen` 之后、写首条事件之前完成同一动作） |
| `train.log` | 训练进程（`stdout` + `stderr` 合并） | 只追加。`error_summary` 取末 20 行关键行 + 退出码；因 OOM 最终失败时必须带**实际使用的 batch** 与**显存峰值** |
| `train_success.json` | 训练进程；**训练成功返回之后、写 `done` 之前**原子落盘 | `{"job_id", "status": "completed", "attempt", "resume_cycles", "finished_at", "final_metrics"}`。它是接管期判定「本 attempt 确实成功」的**唯一**依据（`best.pt` / `results.csv` 在训练**中途**就会生成，不能证明成功）；进程被杀 ⇒ 该文件不存在或不完整 ⇒ 一律按 `interrupted` / `failed` 处理，**服务端不得补写**。落盘顺序写死：① 本文件 → ② `summary.json` → ③ `done` 事件 → ④ 退出 |
| `resume_intent.json` | 服务端；手动恢复链的每一步（**写入时机 = 该步副作用之后**） | `{"phase", "mode", "resume_cycles_target", "requested_at", "aborted_at"}`；原子写 + `fsync`。`phase` **恒描述「已完成的副作用」**、单调前进：`declared` →（`manual_resume` 落盘）→ `boundary` →（`state.json` 写完）→ `state` →（归档完成）→ `archived` →（入队完成）→ `enqueued`；回滚终态 `aborted`（`aborted_at` 记回滚时刻，其余 phase 该字段为 `null`） |

**三条不可删的最小语义（写死）**：

① **跨进程 `state.lock`**：`state.json` 的写者是**两个不同进程**（服务端的心跳回调 / 终态写 与 训练 runner），双方的读-改-写**都必须在同一把 `state.lock` 内**完成（`fcntl.flock(fd, LOCK_EX | LOCK_NB)`，失败则每 50 ms 重试，直到 `job_state_lock_timeout_seconds` 到期），且**字段所有权不相交**（见上表）。持锁进程异常退出时由 OS 自动释放（`flock` 随 fd 关闭）⇒ **不存在**「持锁进程被杀后永久死锁」。`state.lock` **只创建、不删除**：删除即破坏互斥，它是锁载体而非任务数据，不参与任何 TTL 清理，也不适用「一切删除先软删除」。

② **`events.jsonl` 尾行截断**：追加前先截断残缺尾行（文件非空且末尾不是换行 ⇒ `truncate` 到最后一个换行的偏移并 `fsync`），**禁止**把新行直接接在残缺 JSON 之后（否则新行会与残缺行粘成一条不可解析记录，`manual_resume` 本身也会变成坏行）；读取方对最后一行不完整 JSON **一律丢弃**；`seq` **跨恢复周期不重置**，在两个写者之间靠「先截断、再反向求最大 `seq`、从其 +1 续号」交接。

③ **`state.json` 收敛断言**：任意时刻读到的 `state.json` 永远是**完整 JSON**，且**并发写不丢字段**——心跳写不会抹掉 `status` / `finished_at`，终态写也不会抹掉 `pid` / `heartbeat_at`。

**`resume_intent.json` 的最小版语义（写死）**：

```text
手动恢复 = 入队前写 intent（phase 单调，唯一含义是「已完成到哪一步」）
          → 归档 → 写 manual_resume 事件 → 改 state.json → 入队
崩溃后：启动时按 intent 前滚一次（§4.3.6 第 0 步）
resume 接口：10 秒内拿不到 state.lock ⇒ 409 JOB_NOT_RESUMABLE（details.reason="lock_timeout"）
```

- **入口判据先于 `phase` 标签**：前滚 / 回滚只看「事件文件里是否存在 `data.resume_cycles == resume_cycles_target` 的 `manual_resume`」——**不存在** ⇒ 一律回滚为 `aborted`（业务状态零副作用，仅 intent 落盘）；**存在** ⇒ 从断点逐项补齐到 `enqueued`：① `state.json.resume_cycles == resume_cycles_target`（为真则**不得**重复执行该步，避免二次递增）→ ② `jobs/<job_id>/archive/attempt-<target-1>-<a>/` 齐备且原位无残留 → ③ `queue.json.items` 已含该 `job_id`（为真则**不**入队，并核对 `attempt` / `status` 一致）。每步先判「是否已完成」再动手；**禁止**产生第二个队列条目、**禁止**再次递增、**禁止**改写已有 `manual_resume` / `cycle_started_at`。
- **前滚失败** ⇒ 记 ERROR 并把该 job 留在当前 phase：它**不参与**接管扫描的任何分支（§4.3.6），等运维人工介入——这是**唯一**需要人工裁决的恢复中间态。
- 两个交接文件都在 job 目录内、随 `job_record_ttl_days` 管理（§4.4.1）；**都不下发**给客户端（产物接口只暴露 `artifacts/<job_id>/` 下的内容，§3.10）。

#### §4.3.6 服务重启接管

接管发生在启动顺序的**第 ② 步**（§2.6）：配置自检之后、标定判定之前，是「是否需要标定」与「标定会不会与训练抢显存」的判据来源（§2.5）；它是启动过程中**唯一写「既有 job 状态」的一步**（§2.6），且**不得**依赖 GPU 或上游模块（纯离线可完成）。**恢复顺序固定为「终态 → 产物 → 探活 → 才算中断」**：服务离线期间**已经正常完成**的任务绝不能被误判为 `interrupted` 重训。

```text
0. intent 对账：扫 jobs/*/resume_intent.json，按入口判据幂等前滚 / 回滚（§4.3.5）；未收尾中间态或已记 ERROR 的 job 由本步独占，不参与以下任何分流
1. 读 queue.json 重建 queued 列表（保序、重算 queue_position）
2. 扫 jobs/*，按持久化状态分流（命中即止：对账独占 → 2a → 2b → 2c；2c 内 (iv) 优先于 (i)(ii)(iii)）
   2a 在 queue.json 中 或 status == "queued"：只按 queue.json 恢复，跳过 ①②③ 与宽限期
      子分支1 finished_at != null ⇒ 出队 → 按 2b 跳过
      子分支2 status ∈ {completed, cancelled} 且 finished_at == null ⇒ 出队 → 按 2c(iv) 收口
      子分支3 status ∈ {queued, preparing, running} 且有可消费的当前 attempt 终态 done ⇒ 按该 done 收账
              （completed ⇒ 出队 + finalizer；cancelled ⇒ 出队；failed 且预算未用尽 ⇒ 转 ④）
      子分支3′ status ∈ {preparing, running} 且无可消费的终态 done ⇒ 先出队，再按 ③ 探活：判活 ⇒ 只 tail；判死 ⇒ 交 2c
      子分支4 其余 ⇒ 维持 queued；交叉修复：不在 queue.json 且 status=="queued" 且 finished_at==null ⇒ 按 created_at 补回队尾 + WARN；反之 queue.json 有它而 state.json 缺失 / 损坏 ⇒ 以 queue.json 为准重建最小 state.json（attempt 取队列项）
   2b finished_at != null（= is_terminal）⇒ 跳过：不接管、不探活、不重排、不改 needs_attention
   2c 在途项 / 信息丢失项：(i) status ∈ {preparing, running} 且不在队列；(ii) finished_at == null 且不在队列；(iii) state.json 缺失 / 损坏且不在队列；(iv) status ∈ {completed, cancelled} 且 finished_at == null 且不在队列
      (iv) ⇒ 只按 ③ 探活：判死 ⇒ 按 status 补写 finished_at + needs_attention=true / resume_anomaly + WARN；判活 ⇒ 维持现状继续 tail；一律不走 ④、不补写 summary.json、不重训
      (i)(ii)(iii) ⇒ 依次 ① → ② →（必要时 ②′）→ ④，命中即停
   ① 消费终态 done（反向扫描 events.jsonl）：cycle_start_seq = 最后一条 manual_resume 的 seq（无则 0）；当前周期序号 = 该事件 resume_cycles → state.json.resume_cycles → 0；当前 attempt = state.json.attempt
      命中条件（三条全满足）：data.status ∈ {completed, failed, cancelled}、seq > cycle_start_seq、data.attempt == 当前 attempt（缺字段的 done 不得计入）
      分流：completed ⇒ 收账 + finalizer；cancelled ⇒ 收账（不写成功摘要、不走 finalizer）；failed ⇒ 预算未用尽只落 status="failed"（不写 finished_at）并转 ④，预算用尽 ⇒ 收账 + attempts_exhausted
      第一条 done 不满足任一条（旧周期 / 旧 attempt / 缺字段）⇒ 记 INFO（写明 seq / attempt / resume_cycles）后继续 ②
   ② 校验产物（三项全满足才算完整终态证据；任一不满足 ⇒ 不计入，继续 ③）：run/train/weights/best.pt 存在且非空；summary.json 存在可解析且 status == "completed" 且 job_id 匹配；run/train/results.csv 存在且末轮指标可读。三项全满足 ⇒ 按 completed 收账 + finalizer
   ③ 探活：alive = 探活通过（§4.2.4）且 now − heartbeat_at <= heartbeat_timeout_min；state.json 缺失 / 损坏 ⇒ 视为 preparing 并进入 Popen 窗口宽限期（见下）
      alive ⇒ 重新 tail 并原样保留 attempt / resume_cycles / cycle_started_at；不得收账、不得补写 summary.json、不得把该进程当残留杀掉
   ②′ 探活判死后的补收（只能在 ③ 之后；三条全满足才允许）：alive == false 且当前 attempt 无终态 done；train_success.json 存在且校验通过（status / job_id / attempt / resume_cycles / finished_at 齐全）；results.csv 可解析（best.pt **不是**必要条件）
      ⇒ 按 completed 收账 + finalizer + WARN；任一不满足 ⇒ 一律按 ④
   ④ 按 interrupted / 重试处理：本轮 attempt < max_attempts ⇒ 先归档（§4.3.1 自动重试入队协议）再 queued（attempt+1，队尾）；否则 ⇒ 终态（写 finished_at）：status = interrupted（心跳判僵尸路径）/ failed（非 0 退出码或明确异常），needs_attention=true，reason 按 §4.3.3 取（interrupted ⇒ resume_anomaly；failed ⇒ attempts_exhausted）
   finished_at 取值优先级：train_success.json.finished_at → summary.json.finalized_at → results.csv 的 mtime → null
   Popen 窗口宽限期（只对 2c 在途项生效；不得直接重排）：有可解析的 preparing_at ⇒ deadline = preparing_at + heartbeat_timeout_min（默认见 §4.4.2），超期 ⇒ 判准备失败；缺失 / 不可解析 ⇒ deadline = 本次服务启动时刻 + heartbeat_timeout_min / 2；期间出现心跳或 pid ⇒ 转 ③ 并记 WARN
      失败分支一律按 failed 语义且**不读 auto_resume**：预算未用尽 ⇒ 先归档再 queued（attempt+1）；用尽 ⇒ 终态 failed + finished_at + attempts_exhausted + error_summary="state file missing"
3. 重建在途显存账本（alive 任务按 vram_estimate_mb 计账，§4.2.3）；启动调度线程与 TTL 清理器（第 ⑦ 步）
```

**为什么是这个顺序（一句话）**：`done` 是训练进程退出前写下的最后一条事件，若先按 `alive=false` 判死再重排，「服务离线期间训练已正常完成」的任务会被重复烧 GPU 并覆盖已产出的产物；因此终态与产物必须先于探活消费。

**分流判据互斥且穷尽**：`2a` 内部子分支 1–4 两两不相交（子分支 1 与 3 由 `finished_at` 天然互斥；子分支 3 与 3′ 由「是否存在可消费的当前 attempt 终态 `done`」二分；子分支 3′ 的 `status` 取值域不含 `queued`，与子分支 4 的交叉修复支不相交）；`2b` 只剩 `finished_at != null` 这**唯一**判据；`2c` 的 (iv) 优先于 (i)(ii)(iii)，且 (iv) 覆盖「`status` 是终态值而 `finished_at == null`」的崩溃中间态——**不存在**「三步都不命中、永久卡在非终态」的岗位，也不存在被两步同时命中的岗位。`2a` 下「当前 attempt」的**权威取值 = 队列项的 `attempt`**（那时 `state.json.attempt` 可能仍是上一 attempt 的旧值），队列项缺该字段时退回 `state.json.attempt`，两者都不可得 ⇒ 记为 attempt 未知（**不消费**任何 `done`，维持 `queued`）。

| 场景 | 行为与判定 |
| --- | --- |
| 训练进程活着，服务重启 | **不重启训练**：仅重新 tail `events.jsonl` 与 `state.json`，恢复内存态与显存账本占用；`status` 维持 `preparing` / `running`、`is_terminal=false` |
| 服务离线期间训练已正常完成 | ① 命中当前周期 / 当前 attempt 的终态 `done` ⇒ 按终态收账（`status=completed` + `finished_at`）并走终态 finalizer；**不探活、不重排、不重训** |
| 旧周期的 `done` + 新周期刚恢复 | 最后一条 `manual_resume` 在该 `done` 之后（`seq <= cycle_start_seq`）⇒ 该 `done` 是旧周期证据，**不得**收账；按当前周期证据处理（① 或 ② 或 ③），新周期照常 `queued` / `running` / 继续 tail |
| 同一周期内上一 attempt 的 `done` + attempt 2 刚被重排 | 自动重排入队前已把上一 attempt 的产物移入 `archive/attempt-<cycle>-<attempt>/`，且 `done.data.attempt` ≠ 当前 attempt ⇒ **不得**消费该 `done`、**不得**用其产物收成 `completed`；活着的 attempt 2 继续 tail（判活不杀） |
| 旧周期三件套仍在原位 + 新周期刚恢复 | 归档（三件套已被移入 `archive/`）与产物判据**双双不成立** ⇒ 不得按 `completed` 收账、不得补写 `summary.json`、不得杀进程；按当前周期证据处理。新周期仍停在 `queued` 时按 2a 只按 `queue.json` 恢复、维持 `queued` |
| `done` 缺失但产物已齐备 | ② 三项全满足（`summary.json` 已在原位）⇒ 按 `completed` 收账；**只有** `best.pt`（甚至再加 `results.csv`）而缺 `summary.json` ⇒ **不得**判完成：判活 ⇒ 保持 `running` / `preparing`；判死 ⇒ 才按 ②′ 收账（前置：`train_success.json` 校验通过）并在缺失时补写 `summary.json` |
| `Popen` 之后、写 `state.json` 之前父进程崩溃 | `state.json` 缺失 / 损坏 ⇒ 视为 `preparing`，**不得直接重排**：有 `preparing_at` ⇒ 按「心跳 + `deadline_at`」判；字段缺失 ⇒ 从本次服务启动时刻起给 `heartbeat_timeout_min / 2` 的宽限期。**「`pid` 为 `null`」本身不是判据**（服务端在 `queued → preparing` 的原子写里必然把它清零）：只要 `now <= deadline` 就维持 `preparing` 并持续 tail |
| 训练进程死了（机器重启） | ①②③ 都不命中且判死 ⇒ 按 ④ 重排队尾（重排前先归档）或落终态：`interrupted` ⇒ `resume_anomaly`；`failed` 且预算用尽 ⇒ `attempts_exhausted` |
| `state.json` 缺失 / 损坏 | 先分流：被第 0 步独占的 job 不进入本行；仍在 `queue.json` 中 ⇒ 按 2a 维持 `queued`；否则按 2c——**不再一律视为「死了」**：先走 ① 与 ②，都不命中才进 Popen 宽限期，期满仍未复活才判准备失败并写 `error_summary="state file missing"` |
| PID 被复用 | `/proc/<pid>/stat` 第 22 字段 `starttime` 与 `state.json.proc_start_time` 不一致 ⇒ 判「进程已退出、PID 被复用」；**即使心跳时间看起来新鲜**也不认为存活（仅 ③ 生效，①② 优先） |
| 新周期维持 `queued` 时重启 | 2a 队列项分流：只按 `queue.json` 恢复（保序、重算 `queue_position`），跳过 ①②③ 与宽限期——不得因「没有 `pid`」套用宽限期、不得等心跳超时判准备失败、不得重排、不得写 `finished_at`；任务维持 `queued` 且位次不变 |
| 恢复链在写入边界被强杀 | 判定**不依赖 `phase` 标签**，只看 ⓪ 的入口判据：事件文件里**没有**该 `resume_cycles_target` 的 `manual_resume` ⇒ **回滚**（`phase="aborted"`，job 维持原终态、`resume_cycles` **不增**、`queue.json` 无新条目）；**已有** ⇒ **幂等前滚**到 `enqueued`（`resume_cycles` **恰好 +1**、`manual_resume` **恰好一条**、`cycle_started_at` 与该事件 `ts` 同源、上一周期产物在 `archive/` 且原位无残留、`queue.json` 恰好一个条目） |
| `events.jsonl` 尾部被截断后恢复 | 新写者接管前先截断残缺尾行（截到最后一个换行），再从最后一条**完整**记录的 `seq + 1` 续号 ⇒ 文件里每行都是完整 JSON（无重复 `seq`、无缺口），新追加的 `manual_resume` / 首条事件不得与残缺行粘连 |
| `manual_resume` 距文件尾超过读取窗口 | 反向扫描**从文件尾一直读到命中 `manual_resume` 或文件起点**（不得用固定长度的尾窗）⇒ `cycle_start_seq` 等于最后一条 `manual_resume` 的 `seq`；该 job 更早的 `done`（`seq < cycle_start_seq`）不得被收账；结果与「事件只有几十行」时完全一致 |
| resume 续训成功但没有刷新历史最佳 | 终态 finalizer 必须物化最终 `best.pt`：活动路径不存在时按 `jobs/<job_id>/archive/attempt-*/best.pt` 扫描历史最佳并**复制回**活动路径；`summary.json.status == "completed"` 且 `best_source == "archived_history"`；`run/train/weights/last.pt` **始终未被移动**（§4.3.7） |
| 心跳写与取消 / 终态写并发 | **无丢更新**：终态写落下 `status=cancelled` + 非空 `finished_at`，心跳三件套仍在且 `heartbeat_at` 单调不减，`attempt` / `resume_cycles` / `cycle_started_at` 未被改回；并发读取的每一次解析都成功（永远是完整 JSON）；`status` 不得被心跳写改回 `running` |
| 心跳判僵尸且本轮重入队预算用尽 | 最后一次**不**重排：`status=interrupted`（终态）、`finished_at` 非空、`needs_attention=true` / `needs_attention_reason=resume_anomaly`（**不是** `attempts_exhausted`——后者只属于 `status=failed`）；`queue.json` 里没有该 job。对照：`attempt < max_attempts` 时同一步必须重排队尾且 `attempt+1` |
| 取消时 SIGKILL 后进程仍存活 | `status` 保持 `cancelled`、写 `finished_at`、`needs_attention_reason=resume_anomaly` + WARN；**不**改写 `failed`、**不**自动重排、**不**产生第二个终态（§4.3.2） |

**接管期写什么 / 不写什么（第 ② 步的写入面，写死）**：

| 对象 | 接管期可写 | 说明 |
| --- | --- | --- |
| `state.json` | ✅ | 终态收账、`finished_at` 补写、队列交叉修复后的最小 `state.json` 重建；判活时**不改**任何字段 |
| `queue.json` | ✅ | 出队（终态标记已在 / 丢失 / 残留活进程）、按 `created_at` 补回队尾、位次重算 |
| `jobs/<job_id>/archive/` | ❌（扫描本身不归档） | 归档只由入队协议与手动恢复链写入（§4.3.1、§4.3.7） |
| `resume_intent.json` | ✅ | 只写第 0 步的前滚 / 回滚结果 |
| `events.jsonl` | ❌ | 接管**只读**：不追加、不改写、不截断（截断只发生在**追加之前**，§4.3.5） |
| `summary.json` | ✅（仅已确认成功的路径） | 只有 ① / ② / ②′ 三条成功证据路径允许补写（§4.3.7） |
| 训练产物（`run/`、`partial/`） | ✅（仅归档时的移动） | 不复制、不重写、不删除；`last.pt` 永不移动 |

- **逐 job 容错**：单个 job 的接管异常**不影响整体**——记 WARN 后跳过该 job、继续扫描下一个；只有 intent 对账失败（§4.3.5）才升级为 ERROR 并要求人工介入。
- **内存态重建**：接管完成后，在途表 = 「判活的 job 集合 + 各自的 `attempt` / `resume_cycles` / `device_index` / `vram_estimate_mb`」；显存账本按 §4.2.3 重新计账，调度线程从第 ⑦ 步开始按队列派发。
- **离线可完成**：第 ② 步不加载模型、不碰 GPU、不需要上游模块 ⇒ GPU 驱动异常也不影响接管（只影响其后的标定判定，§2.5）。
- **时钟口径**：所有时间戳（含 `preparing_at` / `deadline_at` / `heartbeat_at`）统一 **wall clock（UTC）+ ISO8601**，比较一律用「当前 UTC 时刻」，**不做**单调时钟假设。观测到 `now < preparing_at`（系统时钟被回拨）⇒ 视为「时钟不可信」：把 `deadline` 重算为 `now + heartbeat_timeout_min / 2` 并记一条 WARN（同一 job 只记一次），**不得**因此把岗位直接判死、也不得无限期拖着。

#### §4.3.7 产物与 `summary.json`

| 产物 | 落点 | 说明 |
| --- | --- | --- |
| 完整结果 | `artifacts/<job_id>/`（镜像 `run/train/`：`weights/best.pt`、`weights/last.pt`、`results.csv`、`args.yaml`、`confusion_matrix.png` …） | 不参与 TTL（`artifact_ttl_days: 0`） |
| 部分结果 | `artifacts/<job_id>/partial/` | 取消 / 失败时保留（§4.3.2） |
| 摘要 | `jobs/<job_id>/summary.json` → **复制**到 `artifacts/<job_id>/summary.json` | 内容一致；成功路径由训练进程写，缺失时只允许终态 finalizer 在**已确认成功**的路径上补写 |
| 事件与日志 | `artifacts/<job_id>/events.jsonl`、`train.log` | 终态后从 `jobs/` 复制；原件按 `job_record_ttl_days` 保留（§4.4.1） |
| 终态证据归档 | `jobs/<job_id>/archive/attempt-<cycle>-<attempt>/` | 由服务端把上一 attempt 的四个文件**移出原位**后放入（§4.3.1）；布局见下 |

```json
{
  "job_id": "job_20260101_7f2a91",
  "status": "completed",
  "attempt": 2,
  "resume_cycles": 1,
  "task": "detect",
  "model": "yolo11s.pt",
  "dataset_id": "ds_20260101_ab12cd",
  "resolved_params": {"epochs": 100, "batch": 16, "requested_batch": 16, "batch_assumed": 16, "imgsz": 640, "optimizer": "SGD", "optimizer_preset": null, "optimizer_source": "client", "lr0": 0.01, "oom_retry_max": 2},
  "duration_seconds": 1843,
  "final_metrics": {"mAP50": 0.612, "mAP50-95": 0.402},
  "verified_metrics": {"mAP50": 0.607, "mAP50-95": 0.399},
  "artifact_suspect": false,
  "suspect_reason": null,
  "best_source": "current_attempt",
  "finalized_at": "2026-01-01T11:02:10Z",
  "oom_downgrades": [{"at": "2026-01-01T10:41:02Z", "from_batch": 16, "to_batch": 8, "peak_mb": 14780}],
  "files": [
    {"file_id": "f_af8212b3", "path": "weights/best.pt", "size": 18621442, "sha256": "..."},
    {"file_id": "f_fb6ea6b3", "path": "results.csv", "size": 8123, "sha256": "..."}
  ],
  "training_env": {"ultralytics": "8.4.84", "torch": "2.6.0+cu124", "cuda": "12.4", "python": "3.12.4"}
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | str | 成功摘要恒为 `"completed"`；**只**能出现在「本次 attempt 成功」的路径上，缺失该取值时不得作为成功证据 |
| `attempt` / `resume_cycles` | int | 本 attempt 序号 / 当前周期序号（语义见 §3.4.4）；写入方必须按 `state.json` 的实际取值落值，**不得**写 0 或沿用上一 attempt 的值 |
| `finalized_at` | str | 终态 finalizer 的落盘时刻；由服务端补写的摘要里必然存在（它就是这次补写的产物） |
| `best_source` | str 或 null | 最终 `best.pt` 的来源：`current_attempt` / `archived_history` / `last_pt` / `null`（四值口径见下面的 finalizer 步骤 1） |
| `suspect_reason` | str 或 null | `artifact_suspect=true` 时的**单一原因**：`artifact_metric_mismatch` / `resume_loss_spike`；**不属于** job 对象（§3.4.4） |
| `oom_downgrades[]` | object[] | OOM 降 batch 记录 `{"at", "from_batch", "to_batch", "peak_mb"}`（`peak_mb` 不可获取时为 `null`）；与 `log` 事件的 `OOM_BATCH_DOWNGRADE` 一一对应（§4.2.6） |
| `training_env` | object | 环境指纹四字段（§4.3.8）；**不含** `verified` / `warnings` 语义 |
| `verified_metrics` | object 或 null | `best.pt` 复算指标；`quality_checks.verify_artifacts: false` 时为 `null` |
| `resolved_params` | object | 权威参数快照（§3.8.5），与 `GET /jobs/{id}` 返回的同一对象同源 |
| `files[]` | object[] | `{"file_id", "path", "size", "sha256"}`；`path` 相对 `artifacts/<job_id>/`，部分结果条目带 `partial/` 前缀；`file_id` 的生成规则与示例见 §3.10 |

**终态 finalizer（触发条件与前置）**：

- **触发条件（只覆盖三条成功证据路径）**：① 当前 attempt 的 `done(status=completed)`；② §4.3.6 ② 的三件套收账；②′ 的 `train_success.json` 补收。**其余不构成 `completed` 判定的补写不触发 finalizer**：例如 §4.3.6 的「终态标记丢失」分支只在探活判死后按 `status` 补写 `finished_at`，其 `completed` 来自崩溃前已持久化的 `status`（不是重新判定出来的）⇒ **不**物化 best、**不**补写 `summary.json`、**不**重排。
- **前置（缺一不可）**：「能证明**当前 attempt 成功**」的成功证据 = ① 当前 attempt 的 `done(status=completed)`，**或** ② `jobs/<job_id>/train_success.json` 校验通过（`status` / `job_id` / `attempt` / `resume_cycles` / `finished_at` 齐全），**或** ③ 活动路径上「`summary.json`（`status="completed"` + `job_id` 匹配）+ `best.pt` + `results.csv`」三项齐备。**没有成功证据 ⇒ 不得物化 best、不得写 `summary.json`、不得收成 `completed`。**

| 步骤 | 规则（写死） |
| --- | --- |
| 1. 选最终 `best.pt` | 候选 ① 活动路径 `run/train/weights/best.pt`（存在、非空 ⇒ `best_source="current_attempt"`）；候选 ② **归档历史最佳**——按**唯一模板** `jobs/<job_id>/archive/attempt-*/best.pt` 扫描，取同级 `summary.json.final_metrics["mAP50-95"]` 最大者（同级摘要缺失 / 无该指标 ⇒ 取该文件 `mtime` 最新），**复制**回活动路径 ⇒ `best_source="archived_history"`（归档原件保留）；候选 ③ 回退 `run/train/weights/last.pt` ⇒ `best_source="last_pt"` + WARN + `artifact_suspect=true`；三者都不可得 ⇒ `best_source=null`、不产出 `artifacts/<job_id>/weights/best.pt`、记 WARN |
| 2. 物化到活动路径 | 候选 ② / ③ 都必须把选中的权重**复制**到 `run/train/weights/best.pt`（归档原件保留），随后随镜像进入 `artifacts/<job_id>/weights/best.pt`。**`last.pt` 始终留在原位**（`resume=True` 与 `resume_mode_available` 都依赖它，§4.3.1） |
| 3. 写 `summary.json` | 先在 `jobs/<job_id>/summary.json` 原子写：`status="completed"`、`job_id`、`attempt`、`resume_cycles`、`best_source`、`finalized_at` = now、`final_metrics` 取 `results.csv` 末轮（缺失项记 `null`）；再复制到 `artifacts/<job_id>/summary.json`。**补写只在成功证据存在时发生** |
| 4. 完成标记与 `needs_attention` 刷新 | `state.json`：`status="completed"` + `finished_at`；`summary.json`：`status` + `attempt` + `resume_cycles` + `finalized_at`——两者同时成立才算该 attempt 收账完毕。**同一次收账原子写必须按 §4.3.3 的单值优先级重算并落盘 `needs_attention` / `needs_attention_reason`**（步骤 1 / 3 可能置 `artifact_suspect`：候选 ③ 回退或 resume 后 loss 爆炸）——**不得**因为 `status` 是 `completed` 就写死 `false` / `null` |
| 5. 可验收性 | `best_source="archived_history"` 时 `artifacts/<job_id>/weights/best.pt` 的 `sha256` **等于**归档中最优者的 `sha256`，且该归档原件**仍在原位**、`run/train/weights/last.pt` **始终未被移动**；归档目录**不参与**接管判据（§4.3.6） |

**归档目录布局（唯一定义；§4.4.1 的目录树只写路径、不重复结构）**：

```text
jobs/<job_id>/archive/attempt-<cycle>-<attempt>/
  summary.json          # 上一 attempt / 上一周期的终态证据（四个文件平铺、没有 weights/ 子层）
  train_success.json    # 只归档实际存在的文件；failed / 被杀路径没有 summary.json 与 train_success.json
  results.csv
  best.pt               # 由 run/train/weights/best.pt 移动并改名而来
```

- **唯一写者 = 服务端**：手动恢复链第 3b 步与自动重试入队协议 ① 共用**同一套**路径与命名（`cycle` = 归档时的当前周期序号、`attempt` = 刚结束的 attempt 序号；同一周期内 attempt 唯一 ⇒ 目录不互相覆盖）。**`last.pt` 永不进入归档**，`args.yaml` 也不是归档项。
- **扫描模板只有一条**：`jobs/<job_id>/archive/attempt-*/best.pt`（同级摘要取 `best.pt` 父目录下的 `summary.json`）——**不得**使用 `archive/**/best.pt` 这类含糊写法，也不得再定义第二种归档布局。
- **归档即分隔**：上一 attempt / 上一周期的终态证据在**新 attempt 入队之前**就被移出活动路径，因此**活动路径上的产物恒属于当前 attempt**——`state.json` 不需要（也**不存在**）attempt 级的时间戳或 `seq` 边界字段，接管期也**不做**「周期 + attempt」双重校验（判据见 §4.3.6 ①②）。
- 归档内容**不参与**接管判据，但**参与** finalizer 候选 ②；归档目录随 `job_record_ttl_days` 管理（§4.4.1）。

**`error_summary` 的构造（写死）**：取 `train.log` **末 20 行**中与失败直接相关的关键行 + 退出码，单行截断到 500 字符；因 OOM 最终失败时必须带**实际使用的 batch** 与**显存峰值**（不可获取记 `null`）。取值来源只有 `train.log` 与退出码——**不得**用产物推断失败原因；若末 20 行里同时出现 OOM 降级记录，摘要必须带上**降级后的 batch**。

**job 对象与产物的投影关系**：`GET /jobs/{id}` 的 `progress` / `metrics` 是 `events.jsonl` 的投影（§3.5），`partial_available` 是 `artifacts/<job_id>/partial/` 是否有内容的投影，`eta_seconds` 由已用时长与已完成 epoch 推算——**都不产生新的持久化字段**。`suspect_reason` 只出现在 `summary.json`，job 对象只用布尔 `artifact_suspect` + `needs_attention_reason=artifact_suspect` 表达（§3.4.4）。

**为什么必须由 finalizer 物化**：Ultralytics 的 `resume=True` 只在「新 epoch 超过检查点里的历史最佳」时才重写 `best.pt`，续训完全可能正常结束却没有新的 `best.pt`；而归档又必须把上一 attempt 的 `best.pt` 移出原位（否则旧证据会污染接管判据）。两者叠加后，只有 finalizer 能在**确认成功之后**把「本次 attempt 候选」或「归档历史最佳」重新物化到活动路径——既不丢结果、又让旧证据不污染判据。

#### §4.3.8 训练环境与 preset

- **不建独立 venv**：训练 runner 与服务端共用同一个解释器环境；依赖只作为**记录**存在于 `requirements/custom/training.txt`（按部署方选定的版本记录，**不表达版本下限、不表达任何门禁**）。
- **家族后端优先于顶层 `python_executable`**：`backends.<family>.python_executable` 非空 ⇒ 该家族用**它**启动训练 runner / 标定子进程；没被家族路由的（家族未声明后端、或调用方无家族信息）**逐字回落**到顶层 `training.python_executable`（再空则服务端解释器）——即旧行为只是「没有任何家族声明后端」时的特例，不是第二套语义。`backends.<family>.packages_dir` 同时被注入该家族子进程的 `PYTHONPATH` 最前（未声明则只注入仓库根），家族 `ultralytics` 盖住服务端自带的那个；两个家族各自的 `YOLO_CONFIG_DIR=<work_dir>/.backends/<family>` 由服务端算，`training.yaml` 不写该键。
- **两个后端共用同一份 torch（部署约束）**：家族后端用 `--target` + `--no-deps` 只装 ultralytics 自身，`torch` / `numpy` / `opencv` 等依赖一律由服务端 venv 提供。本机（RTX 5060 Ti / `sm_120`）的 torch 必须带 sm_120 内核（≥ 2.7+cu128），因此 **8.3 后端必须复用服务端 torch**、不得 pin 旧 torch——pin 旧版会把该家族的训练打回 CPU（或直接不可用）。
- **环境指纹（信息性、非门禁）**：`capabilities.training_env` 与 `summary.json.training_env` 都只写四个字段——`ultralytics` / `torch` / `cuda` / `python`（形状见 §3.6）。指纹**不参与** `needs_attention`、**不参与**任何提交校验，也不含 `verified` / `warnings` 语义；不存在「环境未验证」这类阻塞或告警码。

**`min_ultralytics` 家族下限（能力声明，不是版本门禁）**：

| 家族 / preset | `min_ultralytics` | 环境低于该值时 |
| --- | --- | --- |
| `yolo11` | `8.3.0` | 提交该家族的 `(model, task)` 组合 422 `MODEL_FAMILY_UNSUPPORTED`（附所需最低版本） |
| `yolo26` | `8.4.0` | 同上（YOLO26 随 8.4.0 引入） |
| `MuSGD`（`yolo26-default`） | `8.4.0` | 显式指定也拒绝：422 `OPTIMIZER_UNSUPPORTED`（而 yolo26 的家族下限本就把该组合挡在更前面） |

- 该声明有**两种同源形式**：配置的 `model_families[].min_ultralytics` 与下发的 `capabilities.model_families[].available` / `unavailable_reason`（§3.6）。
- **启动期标定矩阵同用该判据**：家族不可用 ⇒ 该家族的**全部组合**在矩阵中被**跳过**（跳过 ≠ 失败：不重试、不拒绝启动、不触发重标），逐条进 `vram_table.unschedulable[]` 与 `calibration.skipped[]`（§2.5.6）。
- OOM 兜底的能力差异是**信息性**的：`oom_retry.enabled: true` 且环境 `ultralytics < 8.4.13` 时记 `OOM_RETRY_UNAVAILABLE`，由 runner 自行兜底（OOM 仍**不**直接判失败），见 §4.2.6。

**preset 选择算法的训练侧落地**（算法、家族映射兜底与不变量**已在 §3.8.4 定义**，本节不重述）：

- 服务端把选中的 preset 名与超参写进 `resolved_params`（字段口径见 §3.8.5）：服务端按 `preset_policy` 选出时，最终传给 ultralytics 的是 preset 里的**裸优化器名**（`SGD` / `AdamW` / `MuSGD`）与超参；客户端显式传 `auto`、或策略 `type:"auto"` 命中时都**原样透传 `auto`**、不选 preset（`optimizer="auto"` / `optimizer_preset=null`，§3.8.5）。
- 客户端可传的 `optimizer` 必须是所选家族的 preset 名，**或是取值 `auto`**（`auto` 不是 preset 名：服务端不注入超参；它**可由客户端显式选择，也可由出货策略给出**，§3.8.4 / §3.8.5）；跨家族 preset 或裸优化器名一律 422 `OPTIMIZER_UNSUPPORTED`（`details.model_family` / `details.allowed`）；`AdamW` / `Adam` 且 `lr0 > 0.001` ⇒ 提交响应 `warnings[]` 记 `ADAMW_LR0_HIGH`（任务仍可提交），`lr0 > 0.05` ⇒ 422 `PARAM_OUT_OF_RANGE`（§3.8.2）。
- 续训（`resume=True`）沿用请求里同一 preset——恢复不重新选 preset、不改 `resolved_params`（§4.3.1）。
- **质量检查（可关闭，两项都是提示性检查）**：**产物校验** 用 `best.pt` 在 val 复算指标并与 `results.csv` 末轮比对（差异超 `quality_checks.artifact_metric_tolerance` 或为 0 / NaN ⇒ `artifact_suspect=true` + `suspect_reason="artifact_metric_mismatch"`）；**resume 异常检测** 在恢复后首 epoch loss 超恢复前 `quality_checks.resume_loss_spike_n` 倍时写 `log` 告警并置 `artifact_suspect=true` + `suspect_reason="resume_loss_spike"`。两项都只影响 `artifact_suspect` / `needs_attention`（§4.3.3），不改变终态本身。

---

### §4.4 配置、目录布局与部署

**本节范围**：`<work_dir>` 的完整目录树与约束、`configs/custom/training.yaml` 的全量键与默认值（**数值的权威落点**）、启动自检与失败策略、首次启动初始化、systemd 部署与首装流程。启动顺序与各步骤语义见 §2.6，鉴权 fail-closed 自检见 §2.4，启动期标定流程与失败分级见 §2.5，`queue.json` 结构见 §4.2.1，`state.json` 字段与写者见 §4.3.5，归档布局见 §4.3.7。

#### §4.4.1 `<work_dir>` 完整目录树

```text
<work_dir>/
  datasets/<dataset_id>/
    content/
      images/{train,val}/<name>.<ext>       # 由 blob 物化（hardlink 优先，跨卷回退 copy2）
      labels/{train,val}/<stem>.txt         # 客户端上传；标签永不进 blob 仓库（stem = 图片名去扩展名）
    manifest.json                           # zip 内 manifest 的**原样字节**（服务端派生字段在 meta.json，见 §4.1.3）
    meta.json                               # dataset_id / task / classes / counts / split_stats / 字节 / 创建与到期时间 / 引用计数
  blobs/<sha[0:2]>/<sha[2:4]>/<sha256>      # 图片内容寻址，全局唯一一份
  jobs/<job_id>/
    request.json                            # 客户端请求 + 服务端解析后的最终参数（resolved_params）
    state.json                              # 服务端与训练 runner 共写；字段、写者与锁见 §4.3.5
    events.jsonl                            # seq 全局严格递增、跨恢复周期不重置（行格式见 §3.5，文件侧规则见 §4.3.5）
    state.lock                              # state.json 的跨进程互斥锁载体（fcntl.flock）；只创建、不删除
    train.log                               # 训练进程 stdout/stderr 合并（人类可读）
    data.yaml                               # 服务端生成的 Ultralytics 数据集描述
    run/train/                              # Ultralytics project/name 落点（weights/、results.csv、args.yaml …）
    summary.json                            # 终态摘要；成功路径由训练进程写，缺失时由终态 finalizer 补写（§4.3.7）
    train_success.json                      # 训练进程原子落盘的成功完成标记（§4.3.5）
    resume_intent.json                      # 手动恢复链的持久化 intent（最小版语义见 §4.3.5）
    archive/                                # 终态证据归档目录；结构与唯一扫描模板见 §4.3.7
  jobs/_submissions.json                    # client_submission_id → job_id 映射的持久化索引；原子写；启动时从 jobs/*/request.json 重建
  artifacts/<job_id>/
    ...完整结果...                          # 镜像 run/train/（weights/、results.csv、args.yaml、confusion_matrix.png …）
    partial/                                # 取消 / 失败保留的部分结果（§4.3.2）
    summary.json                            # 与 jobs/<job_id>/summary.json 内容一致
    events.jsonl / train.log                # 终态后从 jobs/ 复制而来
  weights/<name>.pt                         # 官方预训练权重缓存；不参与 TTL
  tmp/uploads/<upload_token>/               # 上传的临时落点：失败即弃，token 到期清理
  tmp/uploads/_inuse.json                   # 上传 token 的「在用表」：{upload_token → {expires_at, manifest_hash}}（结构见 §4.1.4）
  tmp/uploads/_committed.json               # 上传 token 的「已提交表」：{upload_token → {dataset_id, manifest_hash, response_snapshot, ...}}（结构见 §4.1.4）；启动对账扫 datasets/ 与这两张表（§4.1.3）
  queue.json                                # 队列持久化 {"items": [...]}（严格 FCFS）；结构、字段与锁见 §4.2.1
  .trash/                                   # 软删除落点（数据集 / blob / job 目录）
```

| 约束 | 说明 |
| --- | --- |
| `work_dir` 必须与 blob 同文件系统 | 物化优先 `hardlink`、跨卷回退 `copy2`；启动时比较 `blobs` 目录与该目录所在设备，配置为 `hardlink` 而设备不同 ⇒ WARN + 自动降级为 `copy`，并在 `capabilities.warnings` 记 `BLOB_MATERIALIZE_DEGRADED` |
| 图片与标签严格分离 | 图片只从 `blobs` 物化；标签只存 `datasets/<id>/content/labels/`，**永不**进 blob 仓库 |
| `artifacts/` 不参与 TTL | `artifact_ttl_days: 0` 表示永久；非 0 时只清理超过该天数的**终态**产物 |
| `jobs/<job_id>/` 的保留规则 | 任务记录与日志默认**永久保留**（`request.json` / `state.json` / `events.jsonl` / `train.log` / `summary.json` / `train_success.json` / `resume_intent.json` / `data.yaml` / `archive/`）：`job_record_ttl_days: 0` 即永久；非 0 时只清理**终态**且超过该天数的这些文件。**例外**：`state.lock` 是锁载体，**只创建、不删除**、不参与任何 TTL 清理（删除会破坏跨进程互斥，§4.3.5）。**训练产物**（`run/` 与 `artifacts/<job_id>/partial/`）随 `artifact_ttl_days` 管理。`jobs/` **不随数据集 TTL 清理**——恢复依赖 `run/train/weights/last.pt` 与 `data.yaml` |
| `weights/` 不参与 TTL | 官方预训练权重缓存：运维预置或首次使用时按需下载（`allow_weight_download`）；清理器**永不**回收该目录 |
| TTL 配置里 `0` 的统一语义 | 四个键**一律**表示「**不清理**」（该清理器关闭）：`dataset_ttl_days: 0` / `blob_unused_ttl_days: 0` = 不做 TTL 回收；`artifact_ttl_days: 0` / `job_record_ttl_days: 0` = 训练产物 / 任务记录永久保留。**不存在**「`0` = 立即过期」的读法；`0` 时按 §4.4.3 记 WARN |
| `tmp/uploads/` 失败即弃 | 上传失败或 token 到期 ⇒ 整个 `<upload_token>` 目录进 `.trash/` 后由清理器回收 |
| 一切删除先软删除 | 统一「先移入 `.trash/`，超过 `trash_ttl_hours` 再物理回收」（**唯一例外**：超配额路径的 trash 下一轮即清除、不等该 TTL，§4.1.7）；**禁止**直接删除 |

所有路径的读写方都在服务端新增代码内（`app/custom/` 的存储 / 调度 / 执行三个子模块），**不触碰**上游目录（§2.3）。

| 目录 / 文件 | 主要写入方 | 清理方 |
| --- | --- | --- |
| `datasets/` | 上传流程（plan / upload / 物化） | 数据集 TTL（`dataset_ttl_days`）⇒ 先入 `.trash/` |
| `blobs/` | blob 缓存（内容寻址） | 未被引用 blob 的 TTL（`blob_unused_ttl_days`） |
| `jobs/` | 调度器 + 训练 runner + 启动接管步骤 | 任务记录 TTL（`job_record_ttl_days`，默认永久）；`state.lock` **永不**清理 |
| `jobs/<id>/run/`、`artifacts/<id>/partial/` | 训练进程（Ultralytics 落点） | 训练产物 TTL（`artifact_ttl_days`，默认永久） |
| `artifacts/<id>/` 的完整结果 | 终态收账时的镜像复制 / 终态 finalizer | 训练产物 TTL（只清**终态**） |
| `weights/` | 运维预置或首次使用时按需下载 | **永不**回收 |
| `tmp/uploads/` | 上传流程 | token 到期或上传失败 ⇒ 整个 token 目录进 `.trash/` |
| `queue.json` | 调度器（每次读-改-写都原子落盘） | 不清理 |
| `.trash/` | 一切删除的落点 | 清理器按 `trash_ttl_hours` 物理回收（**唯一例外**：超配额回收路径的 trash 下一轮即清除、不等该 TTL，§4.1.7） |

#### §4.4.2 `configs/custom/training.yaml` 全量键与默认值

**本节的 YAML 是全部配置键与数值的权威落点**：其余小节只写行为，**不重复**数值；全部键可缺省，缺省值即下面示例值；`XANYLABELING_TRAINING_CONFIG` 可指向其他路径；任何变更一律「**重启生效**」，运行期不热加载。

```yaml
# X-AnyLabeling 远程训练配置（fork 新增文件，不属于上游）。
enabled: true                             # false 时 /custom/train/* 全部返回 503 TRAINING_DISABLED
allow_no_auth: false                      # 仅本机开发的逃生开关：true 时不要求 key，但仍要求 server.host 为回环地址
work_dir: /data/xanylabeling/training     # 必须与 blob 同文件系统
tasks: [detect, segment]                  # 首个版本仅这两项；任务字段保留可扩展

# ---- 保留期（四个键的 0 一律 = 不清理，不是「立即过期」）----
dataset_ttl_days: 30                      # 数据集保留期；0 = 不清理（启动记 WARN）
blob_unused_ttl_days: 30                  # 未被引用 blob 的保留期（默认 30 天，见 §4.1.7）；dataset_ttl_days 非 0 时不得短于它；0 = 不清理（启动记 WARN）
trash_ttl_hours: 24                       # .trash/ 物理回收保留小时数（超配额例外见 §4.1.7）
artifact_ttl_days: 0                      # artifacts/ 与 jobs/<id>/run/、partial/ 的保留期；0 = 不清理（永久保留）
job_record_ttl_days: 0                    # jobs/<id>/ 的任务记录、日志与 archive/；0 = 不清理（永久保留）

# ---- 配额 ----
max_upload_gb: 50                         # 单次上传（zip 解压后累计）上限
max_blob_gb: 500                          # blob 仓库上限
max_total_gb: 800                         # work_dir 总量上限
upload_token_ttl_minutes: 60              # 上传 token 有效期（分钟）
max_committed_tokens: 10000               # 已提交 token 记录的准入上限（条数）；必须为正整数，否则拒绝启动
max_concurrent_uploads: 2                 # 全局在途上传数上限；超出即有界等待、不新增错误码（§4.1）

# ---- 存储 ----
blob_materialize: hardlink                # hardlink | copy；跨文件系统时自动降级为 copy 并记告警
allow_weight_download: true               # 首次使用时下载官方权重到 <work_dir>/weights/；false = 必须由运维预置

# ---- 调度 ----
max_concurrent_jobs: 2                    # 全局并发任务数
max_concurrent_per_device: 1              # 每卡并发任务数
gpu_reserve_mb: 1024                      # 每卡为推理预留的显存
vram_safety_factor: 1.25                  # 显存估算的安全系数
heartbeat_timeout_min: 10                 # 心跳超时判僵尸（分钟）
cancel_grace_seconds: 15                  # SIGTERM 后等待再 SIGKILL 的宽限（秒）；随 capabilities 下发
auto_resume: true                         # interrupted 是否自动重入队（不约束 failed 的自动重试）
max_attempts: 3                           # 每轮自动重试上限（含首次）
job_state_lock_timeout_seconds: 10        # state.lock 等待上限（秒）：resume 接口超时 ⇒ 409；内部路径超时 ⇒ 记 WARN 并本轮跳过
resume_to_queue_head: false               # 恢复任务排队尾
resume_fallback: restart                  # 无 last.pt 时：restart（从零重训）| fail（不可恢复）

# ---- 训练 ----
python_executable: ""                     # 空 = 用服务端解释器；非空 = 用它启动训练 runner（逃生舱）

# ---- 训练后端（按模型家族；空 = 服务端解释器）----
backends:
  yolo11:
    python_executable: ""                 # 该家族训练 / 标定用的解释器
    packages_dir: ""                      # 可选：该后端的 ultralytics 目录，注入 PYTHONPATH
    ultralytics: "8.3.*"                  # 记录 / 校验用的声明（仅声明，不是版本门禁）
  yolo26:
    python_executable: ""
    packages_dir: ""
    ultralytics: "8.4.*"

# ---- OOM 兜底（>=8.4.13 由 ultralytics 原生重试，低于该版本由 runner 自行兜底）----
oom_retry:
  enabled: true                           # true = OOM 交由训练侧降 batch 重试，服务端记录降级事件
  max_retries: 2                          # 降级轮次上限，随参数下发（resolved_params.oom_retry_max）

# ---- 质量检查（可关闭的提示性检查）----
quality_checks:
  verify_artifacts: true                  # 完成后用 best.pt 在 val 复算指标并与 results.csv 末轮比对
  artifact_metric_tolerance: 0.05         # 差异阈值；> 5% 或指标为 0 / NaN ⇒ artifact_suspect=true
  resume_loss_spike_n: 10                 # resume 后首 epoch loss 超恢复前 N 倍 ⇒ 告警 + artifact_suspect

# ---- auto-batch ----
allow_auto_batch: true                    # 允许 batch=-1 与 0<r<1 的比例值；false 时只接受 1..128 的整数
auto_batch_assumed: 16                    # 提交预检与显存账本使用的假定 batch
auto_batch_vram_ratio: 0.60               # batch=-1 的目标显存比例

# ---- 显存估算表（手工基线 / 工程起点值：保守偏大；基准 = detect、imgsz=640、AMP 开、batch=16）----
# 加载优先级：configs/custom/vram_table.auto.yaml（本机实测）→ 本段手工基线 → 内置默认表。
vram_table:
  task_factor:                            # 跨任务换算系数（命中本表的 (model, task) 行时不乘）
    detect: 1.00
    segment: 1.25
  entries:                                # 按 (model, task) 双维；本示例只列 detect 行，segment 由 task_factor 兜底推出
    - {model: yolo11n, task: detect, baseline_mb: 1200, per_image_mb: 90}
    - {model: yolo11s, task: detect, baseline_mb: 1600, per_image_mb: 160}
    - {model: yolo11m, task: detect, baseline_mb: 2400, per_image_mb: 280}
    - {model: yolo11l, task: detect, baseline_mb: 3200, per_image_mb: 380}
    - {model: yolo11x, task: detect, baseline_mb: 4800, per_image_mb: 560}
    - {model: yolo26n, task: detect, baseline_mb: 1320, per_image_mb: 99}
    - {model: yolo26s, task: detect, baseline_mb: 1760, per_image_mb: 176}
    - {model: yolo26m, task: detect, baseline_mb: 2640, per_image_mb: 308}
    - {model: yolo26l, task: detect, baseline_mb: 3520, per_image_mb: 418}
    - {model: yolo26x, task: detect, baseline_mb: 5280, per_image_mb: 616}

# ---- 启动期显存基线标定 ----
require_vram_calibration: true            # 逃生舱：false = 跳过标定与校验（记 VRAM_CALIBRATION_SKIPPED）；无可用设备时标定整体跳过
auto_calibration:
  enabled: true                           # false 等价于 require_vram_calibration: false
  batches: {n: [16, 32, 64], s: [16, 32, 64], m: [8, 16, 32], l: [8, 16, 32], x: [4, 8, 16]}   # 默认档位按模型规模自适应
  epochs: 3                               # 每点短跑轮数（训练 + 验证各一轮即达显存峰值）
  models: []                              # 空 = 取模型家族白名单；标定前先按可用性过滤裁剪（家族不可用 / 权重缺失逐组合跳过）
  tasks: [detect, segment]                # 两类任务都测
  batch_min: 4                            # 单点 OOM 后向下取半的下限
  min_points: 2                           # 每个 (model, task) 至少需要的有效测量点；不足即该组合不可调度
  dataset: ""                             # 空 = 运行时合成一次性数据集（系统临时目录，标定后清理）；非空 = 指定真实数据集路径
  synthetic:                              # 仅 dataset 为空时生效；全部参数进环境指纹，任一改动即触发重标
    images: 0                             # 0/auto = clamp(4 × 本次最大 batch, 32, 128)；正整数 = 直接覆盖
    val_ratio: 0.2                        # val 必须非空（每个点位 3 个 epoch，含验证）
    instances_min: 1                      # 每图最少标注实例数
    instances_max: 15                     # 每图最多标注实例数
    classes: 10                           # 生成类别数（写进 data.yaml 的 nc / names）
    image_size: 640                       # 与标定基准 imgsz 一致；两处必须同步
    format: png                           # PNG 保证字节确定、无压缩伪影
    seed: 0                               # 固定种子：保证每次标定的数据一致
  output: configs/custom/vram_table.auto.yaml   # 标定产物落点（相对仓库根；可随时删除重做）
  oom_retry_max: 2                        # 标定点位自身的降 batch 重试上限
  startup_retries: 3                      # 非 OOM 类异常的重试次数；耗尽仍失败 ⇒ 拒绝启动
  startup_retry_interval_seconds: 30      # 上述重试的间隔（秒）

# ---- 标定与存活训练进程的冲突策略 ----
calibration_conflict_policy: defer        # defer（默认，延期到下次启动补标）| wait（等训练结束再标）| terminate（受控终止后继续标定）
deferred_calibration_retry_min: 5         # defer 下「队列空且无存活任务」的复查周期（分钟）；满足即置 deferred_ready 并提示重启

# ---- optimizer preset（默认 type: auto，服务端不注入超参；旧阈值策略改回 iterations_threshold。目的 = 参数显式化与跨版本可复现）----
presets:
  yolo11-sgd:     {optimizer: SGD,   lr0: 0.01,  momentum: 0.937, weight_decay: 0.0005, warmup_bias_lr: 0.1}
  yolo11-adamw:   {optimizer: AdamW, lr0: 0.001, momentum: 0.9,   weight_decay: 0.0005, warmup_bias_lr: 0.1}
  yolo26-default: {optimizer: MuSGD, lr0: 0.01,  momentum: 0.937, weight_decay: 0.0005, warmup_bias_lr: 0.1}
preset_policy:
  type: auto                             # auto | iterations_threshold（缺省 ⇒ auto，与出货默认一致；旧阈值行为 = 显式写 iterations_threshold）；改键重启生效；改回 iterations_threshold 前先读 §5.2.2 的跨仓耦合告警
  threshold: 10000                       # 仅 type: iterations_threshold 生效；预计总迭代数 <= threshold 选 adamw 类 preset，否则选 sgd 类
  default_preset:                        # 仅 type: iterations_threshold 生效；缺省回退按家族映射（键 = model_families 的家族名），绝不回退全局默认
    yolo11: yolo11-sgd
    yolo26: yolo26-default

# ---- 模型家族（能力声明与权重白名单）----
model_families:
  yolo11:
    min_ultralytics: "8.3.0"             # 能力声明（从哪个 ultralytics 版本起可训练该家族），不是版本门禁
    presets: [yolo11-sgd, yolo11-adamw, auto]
    weights:
      detect:  [yolo11n.pt, yolo11s.pt, yolo11m.pt, yolo11l.pt, yolo11x.pt]
      segment: [yolo11n-seg.pt, yolo11s-seg.pt, yolo11m-seg.pt, yolo11l-seg.pt, yolo11x-seg.pt]
  yolo26:
    min_ultralytics: "8.4.0"             # YOLO26 随 8.4.0 引入
    presets: [yolo26-default, auto]
    weights:
      detect:  [yolo26n.pt, yolo26s.pt, yolo26m.pt, yolo26l.pt, yolo26x.pt]
      segment: [yolo26n-seg.pt, yolo26s-seg.pt, yolo26m-seg.pt, yolo26l-seg.pt, yolo26x-seg.pt]
```

**数值的权威落点（别处不重复）**：

| 数值 | 落在哪个键 | 用于 |
| --- | --- | --- |
| 标定重试次数 / 间隔 | `auto_calibration.startup_retries`（3）/ `startup_retry_interval_seconds`（30） | 标定非 OOM 类异常的有限重试；耗尽 ⇒ 拒绝启动（§4.4.3） |
| 标定取半下限 / 最少测量点 | `auto_calibration.batch_min`（4）/ `min_points`（2） | 单点 OOM 的取半下限、组合「已标定 / 不可调度」的判定（§2.5.7） |
| 合成标定数据集参数 | `auto_calibration.synthetic.*` | 运行时合成的一次性数据集；全部进环境指纹（§2.5.5） |
| 估算安全系数 | `vram_safety_factor`（1.25） | 显存估算（§4.2.5） |
| 锁等待上限 | `job_state_lock_timeout_seconds`（10） | `resume` 接口 409 与内部路径跳过（§4.3.1、§4.3.5） |

**键组的消费方与消费时机**（全部键都在**进程启动时**读入内存，**运行期不热加载**：任何变更都需重启）：

| 键组 | 消费方 | 消费时机 |
| --- | --- | --- |
| `enabled` / `allow_no_auth` | 启动自检（第 ① 步）与路由层 | 启动期；`enabled: false` 时训练路由整体 503（§3.3） |
| TTL 与配额组 | 清理器、上传流程 | 按启动时读入的内存值执行 |
| `work_dir` / `blob_materialize` / `allow_weight_download` | 存储子模块 | 启动期；非法组合 ⇒ 拒绝启动或降级告警（§4.4.3） |
| 调度组（并发 / 预留 / 安全系数 / 心跳 / 宽限 / 重试 / 恢复） | 调度器、心跳回调、取消流程 | 运行期按内存值判定（§4.2、§4.3） |
| 标定组（`require_vram_calibration` / `auto_calibration.*` / `vram_table` / 冲突策略） | 启动第 ③④⑤ 步与 defer 复查任务 | 启动期 + 运行期只复查 defer 条件（不标定，§2.5.3） |
| `presets` / `preset_policy` / `model_families` | 提交期校验与 preset 选择 | 每次提交（§3.8.4） |
| `oom_retry.*` / `quality_checks.*` | runner（经 `resolved_params` 与 `request.json`） | 每个 attempt 开始时 |
| `python_executable` | 派发流程 | 每次 `Popen` |
| `backends.<family>`（`python_executable` / `packages_dir` / `ultralytics`） | `executor/process.py` 的解释器与 `PYTHONPATH` 路由、`scheduler/vram.py` 的家族版本探测、`calibration.py` 的标定子进程 | 启动期读入（家族版本探测一次），运行期不热加载 |
| 上传并发组（`max_concurrent_uploads`） | 上传端点（步骤 0 全局闸门） | 每次 upload；超出即有界等待（§4.1.3） |

#### §4.4.3 启动自检表与失败策略

**拒绝启动的条目（非零退出码 + 日志写明原因；HTTP 端口始终未监听，systemd 记为启动失败）**：

| 自检项 | 行为 |
| --- | --- |
| 鉴权 fail-closed：`training.enabled: true` 时要求 `security.api_key_enabled: true` **且** key 非空 | **拒绝启动**并打印判定口径与设置方法（含 `allow_no_auth` 逃生开关的边界）；判定与提示文本**见 §2.4**，本节不重述 |
| `work_dir` 创建失败、或已存在但不可写 | 拒绝启动，输出具体路径与 errno（不存在时由首次启动初始化幂等创建，§4.4.4） |
| `blob_unused_ttl_days < dataset_ttl_days`（**仅在该键非 0 时适用**） | 拒绝启动（否则增量上传失效）；`dataset_ttl_days: 0` 时该检查**不适用**（`0` = 不清理，不是「立即过期」，因此不存在「blob 比数据集先到期」的矛盾） |
| `max_committed_tokens` 非正整数（非数字 / 非整数 / < 1） | 拒绝启动（非零退出码 + 明确提示） |
| `max_concurrent_uploads` 非正整数（非数字 / 非整数 / < 1） | 拒绝启动（与 `max_committed_tokens` 同口径） |
| `tasks` 不同时包含 `detect` 与 `segment` | 拒绝启动 |
| `resume_fallback` 非 `restart` / `fail` | 拒绝启动 |
| 标定非 OOM 类异常：标定子进程崩溃、auto 产物写入失败、显式指定的标定数据集缺失 / 不可读 | 按 `auto_calibration.startup_retries`（3）次、间隔 `startup_retry_interval_seconds`（30 秒）重试；耗尽仍失败 ⇒ **拒绝启动**（日志写明失败步骤与 errno） |
| `preset_policy.type` 非 `auto` / `iterations_threshold`（键缺失 ⇒ 数据类默认 `auto`，不算非法；显式 `null` 不是字符串，仍 FATAL） | 自检项 `preset_policy_type`：**拒绝启动**（非零退出码 + 日志写明取值域与当前取值）；改键**重启生效**（§4.4.2） |

**启动成功但 WARN 的条目（信息性；落 `capabilities.warnings`，§3.6；**不进** `needs_attention`）**：

| 情形 | 告警 / 行为 |
| --- | --- |
| 家族不可用：环境 `ultralytics` < 该家族 `min_ultralytics` | 该家族**全部组合**在标定矩阵中**跳过**（跳过 ≠ 失败，不重试、不拒绝启动）；`model_families[].available=false` + `unavailable_reason=MODEL_FAMILY_UNSUPPORTED`；提交这些组合 422 `MODEL_FAMILY_UNSUPPORTED` |
| 权重缺失：`allow_weight_download: false` 且该模型权重不在 `weights/` 内 | 该组合单独跳过标定；提交该组合 422 `WEIGHT_NOT_AVAILABLE`；与下一条的 `WEIGHTS_MISSING` 可同时出现 |
| `allow_weight_download: false` 且 `weights/` 为空 | `WEIGHTS_MISSING`；`model_families[].weights_ready` 全为 `false`（只表示缓存未命中，**不**代表该权重不可提交） |
| 无可用 CUDA 设备（`torch.cuda.device_count() == 0` 或探测失败） | **整轮标定跳过**：不产生也不写 auto 产物、`calibration.skipped_reason=no_device`、`skipped[]` 为空、`capabilities.devices=[]`；提交任何任务 503 `NO_DEVICE_AVAILABLE`；`require_vram_calibration` 的语义因此限定为「**存在可用设备时才要求标定**」 |
| 某组合在任何一层显存表里既无本行、也无同模型其他 task 的行 | `VRAM_TABLE_INCOMPLETE` + 该组合进 `vram_table.unschedulable[]`；提交该组合 422 `VRAM_ESTIMATE_UNAVAILABLE` |
| 本次标定中某组合**全部点位都 OOM**（有效测量点 < `min_points`） | **不导致启动失败**：该组合标记为**本设备不可调度**，进 `unschedulable[]`（`reason=VRAM_CALIBRATION_FAILED`）与 `calibration.failed[]`，记 `VRAM_CALIBRATION_FAILED`；提交该组合 422 `VRAM_ESTIMATE_UNAVAILABLE` |
| `require_vram_calibration: false`（或 `auto_calibration.enabled: false`） | 跳过标定与校验：直接用 `vram_table` 或内置默认值，记 `VRAM_CALIBRATION_SKIPPED`（**WARN**，不是静默降级）；`entries[].source` 为 `manual` / `default` |
| 存在存活训练任务且 `calibration_conflict_policy: defer` | 本次不标定：`calibration.deferred=true` + `VRAM_CALIBRATION_DEFERRED`；运行期只复查条件并置 `deferred_ready`，补标定在**下次启动**完成（§2.5.3） |
| `oom_retry.enabled: true` 且环境 `ultralytics < 8.4.13` | `OOM_RETRY_UNAVAILABLE`（信息性）：该环境无原生降 batch 能力，改由 runner 自行兜底——OOM 仍**不**直接判失败，只是重试耗时更长；不是版本门禁 |
| 各卡 `total_mb` 不一致（异构多卡） | **不引入专门告警码**（§3.6）：异构多卡只按 `min_device_total_mb` 保守折算，客户端从 `devices[].total_mb` 自行判断；v1 只支持同构多卡（标定键不含 device 维度） |
| `blob_materialize: hardlink` 但 blob 与 `work_dir` 不同设备 | `BLOB_MATERIALIZE_DEGRADED`：自动降级为 `copy` |
| 标定结束清理临时目录失败 | 只记 WARN 并保留路径；**不**拒绝启动、**不**进 `capabilities.warnings` |
| 任一 TTL 键为 `0`（四个键名见 §4.4.2） | **只记 WARN、不影响启动**：提示「该保留期已关闭（`<键名>: 0`），磁盘不会被自动回收」；`0` 的语义定义见 §4.4.1 |
| `preset_policy.type: "auto"` 但某家族 `model_families[family].presets` 未声明 `auto` | WARN `preset_policy_auto_family`：**点名该家族**并说明「该家族无法提交」——`select_preset` 返回 `None` ⇒ 提交该家族一律 422 `OPTIMIZER_UNSUPPORTED`（**不静默回落**，§3.8.4）；**只落启动日志**，不新增 `capabilities.warnings` 的 code（§3.6 的 7 个枚举不变） |

- **一条交叉约束（写死）**：`dataset_ttl_days` 非 0 时 `blob_unused_ttl_days` **不得短于**它——违反即**拒绝启动**（见上表），**不会**降级为 WARN。
- 上表的 WARN 都只是**信息性**通道：客户端据此显示信息条，**不**弹「需人工介入」，也**不**进入 job 的 `needs_attention`（§3.9、§4.3.3）。

**拒绝启动时的表现与定位（写死）**：

| 项 | 说明 |
| --- | --- |
| 进程行为 | 打印 FAIL 原因后以**非零退出码**结束；HTTP 端口**始终未监听**（客户端探测只会连接失败） |
| systemd 行为 | unit 记为启动失败；配了 `Restart=always` 时按 `RestartSec` 反复重启——**修复配置后再重启**，不要靠反复重启撞运气 |
| 定位顺序 | ① `journalctl -u xanylabeling-server -n 200` 看第一段 FAIL 原因；② 核对 unit 的 `Environment=` 路径与两个配置文件的实际路径一致；③ 核对 `work_dir` 权限与磁盘空间 |
| 不触发自检的情形 | `enabled: false`（训练被禁用）时鉴权 fail-closed 自检**不适用**：服务照常启动、训练路由统一 503，`/custom/train/health` 仍返回 200 + `enabled=false`（§2.4、§3.3） |

#### §4.4.4 首次启动初始化

`app/custom/` 在启动顺序的**第 ① 步（配置自检）内、且先于第 ② 步**幂等创建（写死）：`work_dir` 及全部子目录、`queue.json`（初始 `{"items": []}`）、`.trash/`；全部使用 `exist_ok=True`，**不触碰**任何上游目录。

| 创建的条目 | 说明 |
| --- | --- |
| `datasets/`、`blobs/`、`jobs/`、`artifacts/`、`weights/`、`tmp/uploads/`、`.trash/` | 目录树见 §4.4.1；已存在时保持原样 |
| `queue.json` | 仅在缺失时写入初始 `{"items": []}`；已存在时**不覆盖**（由第 ② 步的队列重建读取，§4.3.6） |

- **不可省、也不可后置**：第 ② 步的队列重建要求 `queue.json` 已存在，因此本步必须先于第 ② 步；创建失败或目录不可写即命中 §4.4.3 的自检行 ⇒ **拒绝启动**。
- `configs/custom/vram_table.auto.yaml` **不在**本步创建（由第 ⑤ 步标定写出，§2.5）；`configs/custom/` 必须对服务进程**可写**，否则标定产物写不出 ⇒ 命中 §4.4.3 的标定失败行。

#### §4.4.5 部署与运维（systemd）

`deploy/xanylabeling-server.service`（示例，Linux）：

```ini
[Unit]
Description=X-AnyLabeling Server (inference + remote training)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xal
WorkingDirectory=/opt/X-AnyLabeling-Server
Environment="XANYLABELING_TRAINING_CONFIG=/opt/X-AnyLabeling-Server/configs/custom/training.yaml"
Environment="XANYLABELING_SERVER_CONFIG=/opt/X-AnyLabeling-Server/configs/custom/server.custom.yaml"
EnvironmentFile=-/etc/xanylabeling/server.env   # 可选：把 XANYLABELING_API_KEY 放进 0600 的环境文件
ExecStart=/opt/xal-venv/bin/uvicorn app.custom.server:app --host 0.0.0.0 --port 8000 --workers 1

# Type=simple：主进程 exec 后即被视为 active，systemd 不等待 HTTP / lifespan 就绪。
# 因此 TimeoutStartSec 与首次启动那 10–15 分钟的标定无关（毫秒级即可）；readiness 由健康检查 / 反向代理负责。
TimeoutStartSec=1800

# 训练子进程脱离服务进程组（start_new_session=True）：只杀主进程，训练继续跑并由启动第 ② 步接管
KillMode=process
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

- **`--workers 1` 必须写死（唯一 worker）**：全部并发保护都是**进程内**的——队列锁是进程内全局互斥对象、显存账本与上传 token 准入表都是内存态、job 目录的互斥依赖单 worker 独占。多 worker 会**静默**破坏原子性（同一 job 被派发两次、`resume_cycles` 双重递增、`queue.json` 出现两个条目），因此 unit 里显式写出 `--workers 1`；不要依赖默认值，也不要用 `--reload` 之类会再起进程的开关。
- **`KillMode=process` 只杀主进程**：训练子进程用 `start_new_session=True` 脱离服务进程组，停止 / 重启服务**不会**杀训练；重启后由启动第 ② 步接管（§4.3.6）。unit 文件放在 `deploy/`（§2.3）。
- **`XANYLABELING_SERVER_CONFIG`** 指向 fork 自有的 `configs/custom/server.custom.yaml`（含鉴权 key，权限建议 `0600`、不入版本库）；只设 `XANYLABELING_API_KEY` **不足以**开启鉴权（§2.4）。
- **readiness 语义（写死）**：systemd **只负责进程存活与重启，不负责 readiness**。就绪判据 = **轮询 `GET /custom/train/health`（带 `Token` 头）返回 200**；此前客户端探测只会连接失败。`Type=simple` 下 unit 在 `exec` 之后**立即** active，首次启动那 **10–15 分钟**的显存基线标定发生在 unit 已 active 之后，因此 `TimeoutStartSec` 对该 unit 只需毫秒级（示例保留宽松值，但它**不是**就绪保障）。容器 / Kubernetes 场景应配 `startupProbe`（给足 30 分钟窗口）并在其通过后由 `readinessProbe` 指向同一接口，**不要**用 `initialDelaySeconds` 硬编码标定耗时；若确实要让 systemd 自身具备 readiness 语义，应改用 `Type=notify` + `sd_notify`（首个版本不采用：额外依赖，收益已被健康检查覆盖）。

**首装 7 步**：

| 步 | 命令 / 动作 | 说明 |
| --- | --- | --- |
| 1 | `python -m venv /opt/xal-venv` + `pip install -r requirements/custom/training.txt` | 训练依赖记录（ultralytics 与 segment 所需的多边形依赖）；**不改**上游依赖声明 |
| 2 | **开启鉴权**：复制上游 `configs/server.yaml` 为 fork 自有的 `configs/custom/server.custom.yaml`，至少改 `security.api_key_enabled: true` 与 `security.api_key: "<高强度随机串>"`（权限 `0600`、不入版本库），并用 `XANYLABELING_SERVER_CONFIG` 指向它 | `training.enabled: true` 时鉴权未开启即**拒绝启动**（§2.4）；`allow_no_auth: true` 仅限本机开发且要求回环地址 |
| 3 | 准备 `configs/custom/training.yaml`（§4.4.2）与 `work_dir`；确认 `configs/custom/` 对服务用户**可写**，且**系统临时目录**可写 | `require_vram_calibration` 保持默认 `true`；标定产物要能写回；默认标定数据集在系统临时目录合成（几十 MB 量级），容器要给 `/tmp` 留空间 |
| 4 | `systemctl daemon-reload && systemctl start xanylabeling-server` | 首次启动会**自动标定 10–15 分钟**；用 `journalctl -u xanylabeling-server -f` 观察逐组合进度日志 |
| 5 | 等待 `GET /custom/train/health`（带 `Token` 头）返回 200 | 该接口可达即表示标定与上游模型加载都已结束（readiness 判据） |
| 6 | 检查 `GET /capabilities`：`vram_table.auto_loaded=true`、`entries[].source=auto` | 若为 `manual` / `default`，说明标定被跳过或某组合标定失败（见 §4.4.3 的告警行） |
| 7 | 更换 GPU / 驱动 / CUDA / torch / ultralytics ⇒ **重启服务即自动重标定** | 无需人工干预；也可删除 `configs/custom/vram_table.auto.yaml` 手动触发 |

首装的完整命令序列（Linux，路径按部署实际情况替换）：

```bash
git clone <fork 仓库> /opt/X-AnyLabeling-Server && cd /opt/X-AnyLabeling-Server
python3 -m venv /opt/xal-venv && /opt/xal-venv/bin/pip install -e .
/opt/xal-venv/bin/pip install -r requirements/custom/training.txt
install -m 600 configs/custom/server.custom.yaml /opt/X-AnyLabeling-Server/configs/custom/server.custom.yaml
$EDITOR configs/custom/training.yaml                 # work_dir / tasks / 并发与显存键（§4.4.2）
install -m 644 deploy/xanylabeling-server.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now xanylabeling-server
journalctl -u xanylabeling-server -f                 # 首次启动：观察逐组合标定进度（10–15 分钟）
curl -sf -H "Token: $XANYLABELING_API_KEY" http://127.0.0.1:8000/custom/train/health   # readiness
```

> **不要**用 `ExecStartPre` 或额外的入场脚本去「先跑标定」：标定已内建在启动流程里（§2.6 第 ⑤ 步），多一个前置步骤只会带来顺序与超时的双重不确定性。

**日志与排障要点**：

| 现象 | 先看哪里 |
| --- | --- |
| 服务起不来 / 端口未监听 | `journalctl -u xanylabeling-server -n 200`：拒绝启动的原因（配置自检、鉴权 fail-closed、标定重试耗尽）会写明缺哪一项与如何设置 |
| 启动很慢（首次 10–15 分钟） | 正常：标定在跑，`journalctl -f` 有逐组合进度日志；无可用 CUDA 设备时整轮跳过、启动很快（§4.4.3） |
| 任务排队不动 | `GET /jobs/{id}` 的 `queued_reason` 与 `GET /capabilities` 的显存表；调度线程每 2 秒一轮（§4.2.1） |
| 任务失败 | `jobs/<job_id>/train.log` 末 20 行（`error_summary` 的来源）、`artifacts/<job_id>/partial/`、`events.jsonl` 里的 `log` / `done` 事件 |
| 需要人工介入 | job 的 `needs_attention` / `needs_attention_reason`（§4.3.3）；`resume_intent.json` 停在中间态并已记 ERROR ⇒ 等人工裁决（§4.3.5） |
| 磁盘回收 | `.trash/`（软删除落点，超过 `trash_ttl_hours` 物理回收；**唯一例外**：超配额回收路径的 trash 下一轮即清除，§4.1.7）；`artifact_ttl_days` / `job_record_ttl_days` 为 0 时永久保留（§4.4.1） |
| 服务重启后训练是否还在 | `systemctl status xanylabeling-server`：`KillMode=process` 保证训练子进程存活；重启后由启动第 ② 步接管并恢复 tail（§4.3.6） |

**升级 / 回滚与备份要点**：

| 事项 | 做法与说明 |
| --- | --- |
| 升级 ultralytics / torch | 直接重启服务：环境指纹变化 ⇒ 自动重标定（§2.5）；已在跑的 `running` 任务**不中断**（`KillMode=process`），它们用原解释器环境跑完 |
| 升级本 fork 代码 | 拉取上游后重装依赖并重启；新增目录与上游文件零冲突（§2.3），重启时由第 ② 步接管存活训练（§4.3.6） |
| 回滚 | 回滚代码 + 重启即可；接管步骤对缺失 / 损坏的 `state.json` 有兜底（§4.3.6），**不需要**手工改文件 |
| 备份 | 至少备份 `work_dir/queue.json`、`jobs/*/state.json`、`jobs/*/resume_intent.json`、`jobs/*/request.json` 与 `configs/custom/`；数据集与产物可增量 `rsync` |
| 恢复 | 把上述路径放回原位后启动：第 ② 步先消费终态、再探产物、最后才判中断——不需要人工改状态（§4.3.6） |
| 反向代理 | 上传与打包下载是大体积长连接：需放大请求体上限与读超时；`GET /custom/train/health`（带 `Token` 头）用作 upstream 健康检查 |
| 时间同步 | 所有时间戳是 wall clock（§3.1）；建议开启 NTP——时钟回拨会让宽限期重算（§4.3.6），但不影响状态机正确性 |

**日常巡检清单（单台服务器，建议每周一次）**：

| 检查项 | 判据 / 命令 |
| --- | --- |
| 服务健康 | `curl -sf -H "Token: <key>" http://127.0.0.1:8000/custom/train/health` 返回 200；`GET /capabilities` 的 `devices` 与 `vram_table.auto_loaded` 符合预期 |
| 队列与在途 | `GET /jobs?status=queued` 与 `running` 的数量不超过并发上限；长期 `queued` 的 job 看 `queued_reason` |
| 需人工介入 | 筛 `needs_attention=true` 的 job；检查是否有 `resume_intent.json` 停在中间态（已记 ERROR）的 job |
| 磁盘与回收 | `du -sh <work_dir>/*`；`.trash/` 不应长期堆积（超过 `trash_ttl_hours` 应被回收）；**任一** TTL 键为 `0` 时该清理器关闭、对应对象只增不减（四个键见 §4.4.3） |
| 标定状态 | 换过 GPU / 驱动 / torch / ultralytics 后重启，确认 `entries[].source=auto` 而非 `manual` / `default` |
| 日志 | `journalctl -u xanylabeling-server --since "7 days ago"` 中筛 `warn` / `error`；训练侧看各 job 的 `train.log` 尾部 |

## §5 客户端篇

**本章地位**：§5 只写**客户端侧的行为与实现口径**。凡是已在 §3 定义的契约（路由、封装、错误码、状态机、job 字段、事件、`capabilities` / `health`、参数面、warnings 三通道、跨侧常量）与在 §4.1 定义的 `plan` / `upload` 协议，本章一律写成「见 §x.y」，**不复制、不改写、不补充**。本章结构：UI 与生命周期（§5.1）、数据管线（§5.2）、台账格式与复用清单（§5.3）、两阶段上传与提交（§5.4）、任务监控（§5.5）、结果与错误（§5.6）。

**全章前提（不逐节重申）**：**内部小规模**——约 10 个用户各跑一个客户端进程、共用一把 `api_key` 与同一个服务端，每个客户端进程仍是单窗口；客户端只在 **Linux** 桌面运行；上游文件**只允许挂载点**（§1.4）。

### §5.1 UI 与生命周期

**本节定位**：从菜单接线到窗口销毁的完整生命周期——挂载点（§5.1.1）、launcher（§5.1.2）、窗口与页面（§5.1.3）、关闭状态机（§5.1.4）、staging 与遗留回收（§5.1.5）。

#### §5.1.1 菜单接线（唯一上游改动）

`anylabeling/views/labeling/label_widget.py` 是**唯一**被改动的上游文件，共**四处挂载点、合计约 8 行**。四处一律照抄同文件里既有的「模型验证」接线形态：

| # | 位置（**符号锚点**，不写行号） | 现有「模型验证」代码 | 远程训练对应写法 | 行数 |
| --- | --- | --- | --- | --- |
| 1 | import 区：符号 = 语句 `from anylabeling.custom.model_validation import launch_model_validation` | 同左 | 在该语句**之后**新增一行 `from anylabeling.custom.remote_training import launch_remote_training` | +1 |
| 2 | action 定义：符号 = 赋值语句 `model_validation = action(...)` | `model_validation = action(self.tr("模型验证"), self.open_model_validation, icon="convert", tip=...)` | 紧跟其后新增 `remote_training = action(self.tr("远程训练"), self.open_remote_training, icon="convert", tip=self.tr("打开远程训练窗口"))` | +3~4 |
| 3 | Tools 菜单 action 元组：符号 = `utils.add_actions(self.menus.tool, (...))` 元组里的元素 `model_validation,` | 元组里的 `model_validation,` | 在 `model_validation,` **之后**追加 `remote_training,` | +1 |
| 4 | handler：符号 = `def open_model_validation(self):` | `def open_model_validation(self): launch_model_validation(self)`（带 `try/except` + `error_message`） | 在该函数**之后**新增同构的 `def open_remote_training(self): launch_remote_training(self)`（`try/except` + `error_message`） | +3~4 |

**定位一律用符号锚点（写死，不得按行号硬拷）**：该文件的行号已被同一仓库里**并行实现的另一个功能**（涂抹工具）改动过——它新增了 2 行挂载点（1 行 import、1 行调用），因此该文件的行号已因这个并行功能**整体漂移**；**这不是本功能的改动，也不是缺陷**。上游同步（任何一次 `rebase` / `merge`）都会让行号再次整体位移，因此本表**只给符号锚点**：import 语句原文、`def` 名、元组内元素名。

**行数的两种写法（都只碰这四处）**：上表「约 8 行」是**四处接线点**的口径；对照物「模型验证」接线实测为 19 行新增（import 1 行、action 定义 6 行、action 列表 2 行、handler 含注释 10 行）。若要严格压到 8 行左右，可把 action 定义写成单行 `remote_training = action(self.tr("远程训练"), self.open_remote_training)`（不带 `icon` / `tip`），并让 handler 直接转发 `launch_remote_training(self)`（不额外包 `try/except`，异常由 launcher 内部处理）。

**除此以外不修改任何上游文件**（禁改清单）：`anylabeling/services/**`、`anylabeling/views/training/**`、`anylabeling/views/labeling/**` 的其余文件、`anylabeling/config.py`、`anylabeling/app_info.py`、`pyproject.toml`、`docs/site.yml`、`README*`、`CHANGELOG.md` 一律不动；新增代码全部落在 §5.3.6 的新目录 `anylabeling/custom/remote_training/`。

**阻塞项登记（写死）**：本轮**不创建也不引用**任何改动登记文件（旧素材里那份登记文件已失效、并不存在）。若实现期发现确需改上游文件（例如被迫新增第 5 个挂载点），一律按仓库根 `AGENTS.md` 的约定**登记为阻塞项**，并在交付报告里逐条列明 `文件:符号` + 改动内容，不得另立登记文件。

#### §5.1.2 launcher（惰性 + 单实例）

新建 `anylabeling/custom/remote_training/launcher.py`，与 `anylabeling/custom/model_validation/launcher.py` 完全同构：

```python
"""Lazy launcher for the remote training sub window."""

from __future__ import annotations

from typing import Any

__all__ = ["launch_remote_training"]

def launch_remote_training(parent: Any = None):
    """Open the remote training window, reusing the existing instance."""

    from .ui.dialog import RemoteTrainingDialog

    dialog = getattr(parent, "_remote_training_dialog", None)
    if dialog is None:
        dialog = RemoteTrainingDialog(parent)
        if parent is not None:
            parent._remote_training_dialog = dialog

        def _forget(_dialog=None, parent=parent):
            if parent is not None:
                parent._remote_training_dialog = None

        dialog.destroyed.connect(_forget)
    dialog.showNormal()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
```

两个前提（缺一不可，逐条一行）：

- `RemoteTrainingDialog.__init__` **必须**显式设置 `QtCore.Qt.WidgetAttribute.WA_DeleteOnClose`（与 `anylabeling/custom/model_validation/ui/dialog.py` 同款）——否则 `close()` 只是**隐藏**窗口、`destroyed` 永不触发，`parent._remote_training_dialog` 会一直指向那个隐藏的旧实例，`launch_remote_training` 再也不会创建新实例。
- 关闭请求**必须**真的走到 `close()`：`Esc`（`keyPressEvent` 的 `Key_Escape` 分支）与 `reject()` **都改道 `self.close()`**（覆写写法见 §5.1.4），**不得**沿用 `QDialog` 的默认路径——默认路径不派发 `QCloseEvent`，会在跳过整个关闭状态机的同时把窗口与子 `QThread` 直接销毁。

`anylabeling/custom/remote_training/__init__.py` 只导出 launcher（镜像 `anylabeling/custom/model_validation/__init__.py` 的 `from .launcher import launch_model_validation`），保证应用启动时不引入 Qt 对话框与网络栈的额外开销。

#### §5.1.3 窗口骨架与页面

窗口骨架照抄「模型验证」的窗口实现：

| 参照物 | 位置（符号锚点） | 照抄要点 |
| --- | --- | --- |
| 窗口骨架 | `anylabeling/custom/model_validation/ui/dialog.py` 的 `class ModelValidationDialog(QtWidgets.QDialog)` | `QDialog` + `QStackedWidget` 多页；初始尺寸贴合内容（`apply_initial_size()`，由 `showEvent` 调用）；`self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)` **必须**照抄——它是 §5.1.2 的 `destroyed` 回收与 §5.1.4 步骤 5「下次打开是新实例」的唯一前提 |
| 页面栈 | 同上 `ui/dialog.py` 的三页构建（`self.config_page` / `self.progress_page` / `self.results_page`） | 扩展为下表四页 |
| 页面切换 | 同上 `ui/dialog.py` 的 `show_config()` / `show_results()` | `setCurrentWidget` 式切换 |
| 只读路径框 | `anylabeling/custom/model_validation/ui/config_page.py` 的 `_with_button` 组合 | `QLineEdit() + setReadOnly(True) + placeholder + "浏览…"按钮` 三件套 |
| 目录选择 | 同上 `ui/config_page.py` 的浏览按钮回调 | `QFileDialog.getExistingDirectory(self, self.tr("选择数据来源目录（只读）"))` |
| 类别表选择 | 同上 `ui/config_page.py` | `QFileDialog.getOpenFileName(..., "类别表 (*.txt);;所有文件 (*)")` |
| 只读日志面板 | `anylabeling/custom/model_validation/ui/progress_page.py`（`setReadOnly(True)`） | 详情页事件日志面板；结果页的 `debugInfoEdit` 同款（§5.1.5） |

**四页结构**：

| 页面 | 职责 | 关键控件 | 数据来源接口（§3.2.1 的编号） |
| --- | --- | --- | --- |
| 配置页 `ConfigPage` | 收集提交所需的一切；提交前做本地预检与服务器能力校验 | 服务器地址 + Token（**读写**框 + 「测试连接」）、数据集目录（**只读** + 浏览）、`classes.txt`（只读 + 浏览）、任务类型下拉（Detect / Segment）、模型家族与权重下拉、参数表单（**按组呈现**：常用参数 / 数据增强参数 / 学习率与优化器 / 训练控制；**日志与产物记录类不暴露**；含 batch 的「自动」选项）、划分参数（`val_ratio` + `seed`，留空则生成并回填，§5.2.8）、「划分预览」表（§5.2.8）、「导入配置 / 导出配置」（§5.2.8）、预检摘要、「提交任务」 | #1 `GET /capabilities`、#16 `GET /health`；本地扫描结果（§5.2） |
| 任务列表页 `JobsPage` | 一览所有任务；批量操作 | `QTableWidget`（任务名 / 状态 / 进度 / 设备 / 耗时 / 需关注 / 恢复次数）、复选框、「刷新」「取消」「恢复」「查看详情」「下载结果」 | #8 `GET /jobs?ids=`（批量规则见 §3.2.4） |
| 任务详情页 `JobDetailPage` | 单任务全貌与操作 | 状态徽标、进度条、指标卡片、`queued_reason` 提示、事件日志面板（只读 `QPlainTextEdit`）、「取消」「恢复」「下载结果」「打开产物目录」 | #9 `GET /jobs/{job_id}` + #10 `GET /jobs/{job_id}/events?after=<seq>` |
| 结果页 `ResultsPage` | 产物浏览与下载 | 文件树（路径 / 大小 / 时间 / 「已中止」徽标；路径含 `partial/` 前缀）、下载按钮、摘要区（`summary.json` 关键字段）、黄条（`artifact_suspect`）、**底部只读调试信息区**（§5.1.5） | #13 `GET /jobs/{job_id}/files`、#15 `GET /jobs/{job_id}/download` |

**提交前的预检结果区（口径写死）**：v1 **不新增**任何路由（§3.2 的 16 条之外一条不加）。配置页的「参数预检」按钮只做两件本地可做的事：① 用 `capabilities.vram_table`（§3.6）显示该 `(model, task)` 的估算上限 `max_batch` 与来源 `source`（`auto` / `manual` / `default`）；② 显示本地校验结论（§5.2.5 的 N1 矩阵、§5.2.7 的划分断言）。**权威估算值**（`vram_estimate_mb` / `batch_assumed` / `resolved_params` / `warnings[]`）只在 `POST /jobs`（#7）的响应里拿到，展示归客户端篇 §5.5。

**配置页顶部状态行（来自 #16 `GET /health`；全部不阻断提交）**：

| health 字段 | 显示 | 文案 / 说明 |
| --- | --- | --- |
| `enabled=false` | 红条 | 「服务端未启用远程训练」（与服务端 503 `TRAINING_DISABLED` 同义，§3.7） |
| `devices` 为空 | 黄条 | 「服务端当前没有可用 GPU」（对应 503 `NO_DEVICE_AVAILABLE`） |
| `work_dir.free_gb` 低于本次预检所需 | 黄条 | 空间可能不足；不阻断提交，服务端仍以 413 `QUOTA_EXCEEDED` 兜底 |
| `weights` / `blobs` 摘要与 `warnings[]` | 信息条 | 只作信息展示；通道 C 的 7 个 code 语义见 §3.6 / §3.9，客户端**不据此阻断** |
| `calibration.required=false` 或 `calibration.auto_loaded=false` | 信息条 | 「服务端未使用本机实测显存基线（退回手工基线 / 内置默认），显存估算可能偏保守」；**不阻断提交** |
| `calibration.deferred=true` | 信息条 | 「服务端本次未做本机显存标定（有训练任务在跑），正在用兜底层数值，估算偏保守」；`deferred_ready=true` 时追加一句「**服务端当前已空闲，重启服务即可完成补标定**」。客户端**不得**承诺「等一会儿就会自动标定」：`deferred` 的清零发生在**下次启动完成标定之后**（§2.5） |
| `queue` 深度 | 信息条 | 只作展示，客户端**不**据此承诺排队时间 |

**连接测试（配置页「测试连接」按钮，请求 #16，通常带 `Token` 头）**：

| 结果 | 结论 | 用户可见文案 |
| --- | --- | --- |
| 401 | 服务端启用了鉴权，而 Token 缺失 / 无效 | 「Token 无效或已过期」 |
| 200 | 连通，且服务端**没有启用鉴权**（本机免鉴权配置） | **不报错**，照常使用返回的 health 字段（四态鉴权见 §3.7） |
| 404 | 地址或前缀写错、服务端未挂训练路由 | 「地址不正确：未找到训练接口」/「服务端未启用远程训练」 |
| 连接失败 | 网络 / 端口问题（顶部红条） | 「服务端正在启动（首次启动可能需要 10–15 分钟做显存基线标定），将自动重试」；退避重试见 §5.5 |

- **不要用上游 `GET /health` 做连接测试**：它免鉴权、且**不含任何训练字段**；客户端**不依赖**上游 `/health`（§3.7）。
- **连接失败也可能是服务端因缺 key 而拒绝启动**（§2.4 的 fail-closed 自检）：此时客户端与「正在标定」**无法区分**，因此同一条文案里补一句：「若长时间（超过 20 分钟）仍然连接失败，请让管理员检查服务端日志：可能因未配置鉴权 key 而拒绝启动（`security.api_key_enabled` / `XANYLABELING_API_KEY`）」。
- 判定「未就绪」与「地址写错」的区别：**连接失败 ⇒ 未就绪**（继续重试）；**能连上但返回 404 ⇒ 地址 / 前缀写错**（停止重试并提示）。已跑过标定、指纹未变的机器启动是**秒级**的，正常不会触发这条提示（§2.5）。

**页面间导航**：列表页双击 → 详情页；详情页「查看结果」→ 结果页；结果页「返回」→ 详情页；任意页 `Esc` → 关闭窗口（与点关闭按钮**逐字同路径**，§5.1.4）。

**页面范围（v1 不做数据集管理页）**：v1 只提供「训练任务列表」——即任务列表页 + 任务详情页 + 结果页；**不做数据集管理页**：**不消费** #4 `GET /datasets`、#5 `DELETE /datasets/{dataset_id}`、#6 `GET /cache/stats`（§3.2.3）。数据集只在 413 `QUOTA_EXCEEDED` 时提示用户：服务端已按「最旧未引用优先」自动回收（§4.1.7），故优先提示**稍后重试**、必要时再找管理员（客户端文案见 §5.6）。

**硬约束（数据集目录严格只读）**：数据集目录**不写入、不修改、不删除**；staging、zip 与导出配置的中转文件一律落在系统临时目录（`tempfile.mkdtemp(prefix="xal_remote_training_")`，§5.1.5），**不污染用户数据目录**；工作目录下只写 §5.3.1 的台账落点树。

#### §5.1.4 关闭状态机（`closeEvent`）

上传与轮询都在 `QThread` 里跑，而窗口可被用户随时关闭（含 `Esc`）。本节的状态机是**唯一**的关闭路径：`Esc`、关闭按钮、`reject()`、收尾重入、应用退出全部走它。

**关闭状态机（步骤 0–6；步骤 0 只在首次进入时执行，步骤 1–6 是收尾六步）**：

| 步骤 | 行为 |
| --- | --- |
| 0. 二次确认（**只在首次进入时执行**） | 有进行中的操作（上传中、轮询中、扫描 / 转换 / 打包中）→ 弹二次确认：「仍有 N 个操作在进行（上传 / 轮询 / 扫描 / 转换 / 打包），关闭窗口会取消它们。已上传的图片会被服务端缓存复用，下次可继续。确定关闭？」仅当用户确认才继续；否则 `event.ignore()` 并**立即返回**（不置 `_closing`、不改任何状态）。确认后置 `self._closing = True`；**重入本状态机时按 `_closing` 跳过本步**（不再二次确认）。括号里的类型串**必须**与实际在进行的类型一致（按实际类型拼接，或整串固定），否则在扫描 / 转换 / 打包中关窗时提示与实际不符 |
| 1. 使旧响应失效 | `self._generation += 1`（**代次计数器**）。所有 worker 的回调 / 信号都带上发起时的 `generation`；回主线程时先比对，不等则**直接丢弃**（不更新 UI、不写台账）。已进入事件队列的信号投递无法撤回，因此关窗后迟到的信号只能靠本比对拦住；轮询侧的迟到响应同样由本步拦住（**不另立机制**） |
| 2. 断开**业务 UI** 连接 | 逐个 `try/except`（未连接时忽略）断开所有**用于更新业务界面与台账**的连接：`progress` / `metrics` / `log` / `state` / `finished` / `failed`。此后 worker 即使完成也**不会**触碰已销毁的控件、**不会**再写台账。本步**不**承担「感知 worker 何时结束」的职责（那是步骤 4），因此不存在「信号已断开却还在等信号」的矛盾 |
| 3. 可唤醒取消 | 置取消标志并**唤醒**：上传用 monitor 回调抛取消异常 + 关闭连接；扫描 / 转换 / 打包用 `should_stop` 回调；轮询用**同一个 `threading.Event`** 的 `Event.set()`。轮询线程的**所有等待**（各页轮询间隔、空转档、退避等待）**必须**写成 `threading.Event.wait(timeout)`（单一 Event，用返回值区分「被唤醒」与「超时」），**不得**写成 `time.sleep(timeout)`——`Event.set()` 只能让 `Event.wait()` 立刻返回，打断不了 `time.sleep`。唤醒后**先查取消标志、再决定是否发下一次请求**；取消必须在**秒级**生效，不依赖 `Session.close()` |
| 4. 收尾等待（**非阻塞**） | **唯一判据**：`all(w.isFinished() for w in self.workers)`（`self.workers: list[QThread]`）。① 全部已结束 → 进步骤 5；② 否则 `event.ignore()`——**保留窗口与线程所有权**，窗口显示「正在停止（等待 <worker 名> 结束）…」并**禁用**关闭按钮，启动（或复用）窗口持有的收尾 `QTimer`（固定 200 ms，槽函数每次重新求值）；③ **提示性上限：所有 worker 一律 5 s**（上传 worker **不设**更长值），由该 `QTimer` 自行累计，超限**只记一条 warning 日志**（每个 worker 至多一条），**绝不**作为强杀线程或强关窗口的理由；④ 求值为真 → `QTimer.singleShot(0, self.close)` **异步**触发最终关闭（重入时：步骤 0 跳过、步骤 3 / 4 立即通过）；⑤ 等待期间**不**卸载 worker、**不** `deleteLater`、**不**放弃等待。**禁止**在 GUI 线程调用**正超时**的 `worker.wait(ms)`（会卡住事件循环、界面冻结），只允许 `isFinished()` 或 `worker.wait(0)` |
| 5. `deleteLater`（**只在所有 worker 已结束之后**） | **先确认**判据为真，**再**把已结束的 worker **移出** `self.workers`，随后对它们与临时控件 `deleteLater()`（**先移除、再销毁，顺序不可交换**；**不得**把已 `deleteLater()` 的对象留在集合里——后续求值会对已删除的包装对象抛 `RuntimeError`）。窗口设置了 `WA_DeleteOnClose`（§5.1.3）后 `close()` 才真正销毁对象并触发 `destroyed`，launcher 的 `destroyed` 槽把 `parent._remote_training_dialog` 置 `None`（§5.1.2）⇒ **下次打开是新实例** |
| 6. 清理 | 走 §5.1.5 的 `finally` 路径（staging / zip 清理）。**只有**「条目已落账（`submitted`）或处于 `void`（任何来源）」才能删 `pending_dir`；仍被 pending 条目引用的可重放数据（`archive.zip` / `manifest.json` / 提交快照）**不随本次关闭删除**（§5.3.1；清理时机归 §5.4） |

**必初始化项（`RemoteTrainingDialog.__init__`；缺一项就会在首次关闭 / 首次退出请求时抛 `AttributeError`）**：

| 属性 | 初值 | 消费点 |
| --- | --- | --- |
| `self._closing` | `False` | 步骤 0 / 4；跳过二次确认的**唯一**判据（业务语义：已确认关闭） |
| `self._closing_in_progress` | `False` | `closeEvent` 执行期重入守卫（进入时置真、`finally` 置假）；**不参与**「是否重入」的判定 |
| `self._generation` | `0` | 步骤 1（读改写） |
| `self._pending_quit` | `False` | 应用退出路径（下表）：登记「本次退出请求正在收尾」 |
| `self.workers` | `[]` | 步骤 4 / 5 的唯一取值来源。**「集合为空 ⇒ 判据为真」写死**：`all(...)` 对空列表为 `True`，实现**不得**另加「集合非空才算有未结束 worker」的特判 |

**`workers` 的入队与出队（写死）**：

```text
入队（五处，各自在发起 / 启动时 append，缺一不可）：
  ① 扫描 worker   ② 转换 worker   ③ 打包 worker   ④ 上传 worker   ⑤ 轮询 worker
        —— 同一时刻至多一条；该引用是窗口对线程的所有权（步骤 4 ② 的「保留所有权」依赖它）
出队（两处，先出队、后销毁）：
  ① worker 发出 finished 时，已连接的收尾槽里 self.workers.remove(w)
  ② 步骤 4 的求值处若 w.isFinished() 为真也立即移出（兜底，覆盖未走到 finished 槽的路径）
```

**`Esc` 与 `reject()` 必须改道 `self.close()`（写死）**：`QDialog` 的默认键盘处理把 `Esc` 交给 `reject()`，而 `reject()` → `done()` 走的是 `close_helper(CloseNoEvent)`——它**不派发 `QCloseEvent`**，本状态机一步都不会执行；同时 `done()` 在设置了 `WA_DeleteOnClose` 时会**删除**该对话框，于是窗口对象与它持有的子 `QThread` 被立刻销毁（正是 `QThread: Destroyed while thread is still running` 的来源）。因此必须：

```python
def keyPressEvent(self, event):
    if event.key() == QtCore.Qt.Key.Key_Escape:
        self.close()
        event.accept()
        return
    super().keyPressEvent(event)

def reject(self):
    self.close()          # 不得调用 super().reject()
```

**应用退出路径（与本节同一套状态机、同一唯一判据）**：

| 项 | 口径 |
| --- | --- |
| 可否决入口（**只有两个**） | ① 顶层窗口的 `QCloseEvent`；② `qApp`（`QApplication` 实例）上的 `QEvent::Quit` |
| `aboutToQuit` | **不可拦**：它没有 `QEvent` 参数、无法 `ignore()`；只允许在上面做「记一条日志」这类最后一刻的同步清理 |
| 拦截实现 | 事件过滤器，落在 fork 自有代码 `anylabeling/custom/remote_training/ui/close_guard.py` 的 `ApplicationCloseGuard(QtCore.QObject)`，由 `RemoteTrainingDialog.__init__` 末尾**唯一一次**调用 `install_close_guard(dialog)` 装配——**不新增任何上游挂载点**（§5.1.1 的四处清单不变） |
| 守卫否决 / 放行 | **否决 = `event.ignore(); return True`**（只 `ignore()` 而返回 `False` 对 `QEvent::Quit` 无效）；**放行 = `return False`** |
| 两条放行分支 | ① **取不到对话框**（`owner` 上没有 `_remote_training_dialog`）：对象不存在就没有 `QThread` 可等、父对象析构的 `qFatal` 风险为零，此时否决会在**没有任何人重发**的情况下让应用永久挂死 ⇒ **放行**（`owner` 非空时记一条 `WARNING` 日志，每次进程至多一条）；② **无未结束 worker**：静默**放行**，退出请求不改变任何行为 |
| 确认与登记 | 有未结束 worker 时：先弹**与步骤 0 逐字相同**的确认框（用户取消 ⇒ 否决本次退出请求，不置任何标志）；用户确认 ⇒ 置 `dialog._pending_quit = True` 并**立即** `dialog.close()`（走同一套状态机：步骤 0 跳过、步骤 1 使旧响应失效、步骤 3 取消、**步骤 4 求值唯一判据**），随后**再求值一次**同一判据 |
| 放行与重发 | 判据已真 ⇒ 先清零 `_pending_quit` 再**放行**（上游 `MainWindow.closeEvent` → `label_widget.closeEvent` 的配置保存 / 落盘行为**不被跳过**，随后窗口关闭、`QApplication.quit()`、`exec()` 正常返回）；仍有未结束 worker ⇒ **否决**并启动守卫自有的 **200 ms `QTimer`**（**唯一启动点**，`if not timer.isActive(): timer.start()` 幂等），槽函数先读 `_pending_quit`（为假 ⇒ 停表、**不重发**）再求值同一判据，为真时 `QTimer.singleShot(0, top_level_window.close)`——**由主窗口自己重发一次 `close()`**（没有顶层窗口可关的退化形态改发 `QApplication.quit()`）。**重发的是 `close()`、不是 `quit()`**；重发的关闭再次进入本路径时判据已真 ⇒ 放行 |
| 等待期间不析构 | worker 未全部结束时，父对象的关闭 / 退出**必须**被 `event.ignore()` 挡住（与步骤 4 ② 同源）：主窗口不关闭、父对象绝不析构，等待的是 worker 真正结束，**不设**「等够 N 秒就强杀」的兜底（步骤 4 ③ 的 5 s 上限**只**记一条 warning） |

**为什么可以就此收口（取舍一句）**：v1 场景下（结论**与用户数无关**——靠的是 §4.1 的 `upload_token` 幂等与 §5.4 的 `client_submission_id` 兜重），「上传中意外退出」的代价 = 下次**重放一次**。因此本节按上表形态写死状态机与守卫，**不**再逐状态论证「确认框恰好命中一次」，也**不**要求守卫与对话框的两个计时器互相代替。

#### §5.1.5 staging 与遗留回收

**staging 落点**：`tempfile.mkdtemp(prefix="xal_remote_training_")`（系统临时目录）。标签工作目录、打包工作区与 zip 都在这里；任务结束时按 `keep_staging` 决定去留。

**owner marker（`owner.json`）**：`mkdtemp` 之后**立刻**在 staging 根写 `{"pid": <pid>, "started_at": "<ISO8601>"}`——它是「这个目录属于哪个进程、什么时候建的」**唯一凭据**，也是**防半写**的必需项。落盘沿用与 `tasks.json` **同一套原子写习惯**（§5.3.4）：先写 `owner.json.tmp` → `flush` + `os.fsync` → `os.replace` 覆盖 `owner.json`，**不得**直接以 `open(..., "w")` 原地写（否则强杀会留下**半截 JSON**）。写入失败（如磁盘满）**不得**让整个上传失败：记一条 `WARNING` 日志后继续（该目录退化为「无 `owner.json` 的目录」，由 TTL 路径兜底）。**只写 `pid` 与 `started_at` 两个被消费字段**，不写任何无规则消费的字段（例如应用版本号）。

**任务结束时（worker 的 `finally`）的两条互斥分支**：

| 分支 | 条件 | 行为 |
| --- | --- | --- |
| ① 默认 | `keep_staging` 未设置 / `false` | 删除**本次** staging 目录与 zip（`shutil.rmtree(..., ignore_errors=True)` + 删 zip） |
| ② 保留 | `keep_staging: true` | **保留**本次 staging 目录（含 zip）作为调试产物，并把保留路径写入一条 `INFO` 级日志（含完整绝对路径）+ 下方的调试信息区 |
| 唯一例外（**两条分支都适用**，硬性） | — | `pending/<id>/` 持久目录（`archive.zip` / `manifest.json` / `submit_request.json` / `meta.json`，§5.3.1）**永不**由本 `finally` 删除：只要对应 pending 条目仍是「未完成」状态（`planned` / `uploading` / `committed` / `submitting`）就必须原样保留，否则重放没有 zip 可用 |

**崩溃 / 强杀遗留回收（下次启动时扫描系统临时目录下所有 `xal_remote_training_*`）**：

| 项 | 规则 |
| --- | --- |
| 扫描对象 | 系统临时目录下**本应用前缀** `xal_remote_training_*` 的目录——**不得**按「同目录下的全部子目录」求集合 |
| 免于回收集合（**硬性、优先**） | **仍被任何 pending 条目（`planned` / `uploading` / `committed` / `submitting`）引用的 staging 目录与持久化目录一律免于回收**——既不受 TTL 影响，也不受任何条数 / 容量上限影响，直到该条目**对账完成**（`submitted`）或**显式作废**（`void`，含用户手动作废）为止。判定依据是 `tasks.json` 的 pending 条目里的 `pending_dir` 与 `staging_dir` 两个字段（§5.3.2），**不是** `owner.json`。该集合**逐目录实时求值**（每判定一个目录之前重新读一次台账），**不得**用扫描开始时的快照 |
| TTL（固定 **7 天**，写死） | 非豁免目录中超期的**一律回收**：按 `owner.json.started_at` 判定，缺失 / 不可解析时按目录 `mtime`。本 TTL 是**固定常量**，**不受 `settings.json` 任何键控制**，与 `pending_ttl_days` 的**缺省值**同值但**不共用键**，也**不是**第四个键（§5.3.3） |
| 日志 | 每次扫描后记「回收 M 个遗留目录，释放 X MB」，便于用户确认没有堆积 |
| 不写工作区 / 用户数据目录 | staging、zip、导出配置的中转文件全部落系统临时目录；台账落 `get_work_directory()`（§5.3.1）——两者都不写工作区 |

**调试信息区（结果页底部，逐字保留）**：结果页底部固定存在一个只读 `QPlainTextEdit`（`setReadOnly(True)`，与 `anylabeling/custom/model_validation/ui/progress_page.py` 同款；窗口内恒存在、**不随结果是否存在而消失**），其**标题文字为「调试信息」**、`objectName` 为 `debugInfoEdit`（供用例按名定位），内容为**只增不改**的多行文本（保留路径一行、其余为生命周期摘要）。「该路径可选中 / 可复制」由 `setReadOnly(True)` + 默认文本交互标志（`Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard`）保证：**不得**设 `NoTextInteraction`、**不得**把它做成不可选标签。**不新增**「帮助 → 打开调试产物目录」这类上游菜单入口——那会是第 5 处上游改动，与 §5.1.1 的四处清单冲突；路径的可见性完全由「日志 + 对话框自有的调试信息区」承担。

**为什么不再写「由用户或系统回收」（一句）**：操作系统**不保证**清理系统临时目录（多数发行版只在重启时或按 `systemd-tmpfiles` 策略清理），而一次失败 / 取消会留下接近 1 GB 的完整 staging + zip，反复操作会长期堆积；因此改为「`finally` 清理 + owner marker + TTL 启动回收」的确定性规则，**唯一显式例外**是 `keep_staging: true` 的调试保留（它**不**使 `finally` 的清理变成「无条件」，两条分支互斥）。

### §5.2 数据管线：扫描 / 校验 / 转换 / 划分 / 打包

本节覆盖目标链路第 2 步（§1.1）：从用户选目录到 zip 交给上传的全部本地处理。**服务端协议（`plan` / `upload` 的请求响应字段、zip 安全、N1 服务端列）一律见 §4.1**，本节不重述；本节只写客户端行为与判据。

#### §5.2.1 12 步流程

```text
选择数据集目录（只读） + classes.txt + 任务类型(Detect/Segment) + val_ratio + seed（可空 = 生成并回填）
        │
        ├─ 1. 扫描配对：仅遍历数据集根目录（不递归；子目录项一律阻断，§5.2.3）
        ├─ 2. 校验：N1 校验矩阵（§5.2.5）+ 完整 schema 校验（§5.2.4）；阻断项存在则停止，不发起任何网络请求
        ├─ 3. 转换：LabelConverter(classes.txt).custom_to_yolo(in_json, out_txt, mode)
        ├─ 4. 标签写入与 split 无关的标签工作目录 <staging>/labels/<stem>.txt（每个 txt 只写一次）
        ├─ 5. 划分 train/val：按类别独立分层 + 派生种子（§5.2.7）→ 在此确定每张图片的 split 与 split_stats
        ├─ 6. 冻结标签字节：逐文件流式计算 label_sha256 / label_size（§5.2.6）；此后任何步骤都不得再改写任一 txt
        ├─ 6′. 划分预览（§5.2.8）：每类 (train/val) 计数、val 总张数与警告；用户确认后继续
        ├─ 7. 流式算每张图片 sha256 / size → 组装 manifest（= plan 请求体，字段见 §4.1.2）
        ├─ 8. plan：POST /datasets/plan（§4.1.2）；成功响应落盘之后才进入 planned（行为见 §5.4）
        ├─ 9. 处理 rejected[]（§4.1.2）：非空 ⇒ 用户确认 → 剔除 → 本地复检 → 二次 plan（取最终 token 与最终 missing_images[]）
        ├─ 10. 按最终 missing_images[] 打包 zip（§5.2.9）：labels/ 用的仍是步骤 6 冻结的同一份字节，split 只决定路径前缀
        ├─ 11. 持久化：archive.zip 与 manifest.json 写入 pending/<id>/ 并 fsync → 台账推进到 uploading（先落盘、后发请求；落点见 §5.3.1，行为见 §5.4）
        └─ 12. upload：POST /datasets/upload（§4.1.3）
```

**顺序本身是规范（写死）**：zip **不能**在 plan 之前打包——zip 里 `images/<split>/<name>` 放哪些图片**完全由 plan 返回的 `missing_images[]` 决定**（§4.1.2 / §4.1.5）。因此步骤 7 只产出 **manifest（元数据）**；`labels/` 的字节在步骤 6 冻结（§5.2.6），步骤 10 的打包只是把**同一份字节**按步骤 5 确定的 `<split>` 路径前缀写进 zip。

**标签工作目录与 `pending_dir/` 的生命周期（写死）**：

- 步骤 4 写出的 txt 落在**与 `split` 无关**的 `<staging>/labels/<stem>.txt`——`split` 要到步骤 5 才产生，步骤 4 不可能知道 `<split>`；
- `<split>` 的**唯一产出点**是步骤 5；`label_sha256` / `label_size` 与 `split` **无关**（只取决于 txt 字节），步骤 6 算一次即可；
- `pending/<id>/` 在**进入 `planned` 的同一次动作**中创建（步骤 8 的响应落盘动作），步骤 4–7 期间不需要它；
- **staging 与 `pending/` 是两个不同的落点**：标签工作目录与打包工作区都在 staging（`finally` 里清理、可按 TTL 回收），`pending/<id>/` 在**工作目录**下、免于 TTL 回收（§5.1.5）；步骤 10 只把结论（zip 字节）搬到 `pending/<id>/`，**不移动**标签工作目录。

#### §5.2.2 复用与映射

| 环节 | 复用对象（**符号锚点**） | 说明 |
| --- | --- | --- |
| 扫描配对 | 只借鉴 `collect_pairs` 的**返回结构**（`pairs` / `image_without_label` / `label_without_image` / `unreadable_label_pairs`） | `anylabeling/custom/model_validation/dataset.py`；**不复用**它的递归遍历（见下「明确不复用」） |
| sha256 | `sha256_file(path) -> str`（1 MiB 分块流式） | `anylabeling/custom/model_validation/dataset.py` |
| 图片扩展名白名单 | `IMAGE_EXTENSIONS`（`.jpg/.jpeg/.png/.bmp/.webp/.tif/.tiff`） | `anylabeling/custom/model_validation/labelme_io.py`；本地扫描与 zip 打包共用 |
| 转换器 | `class LabelConverter` / `__init__(self, classes_file=None, pose_cfg_file=None)`（`self.classes` 来自 `classes.txt` 逐行） | `anylabeling/views/labeling/label_converter.py` |
| 转换调用 | `custom_to_yolo(self, input_file, output_file, mode, skip_empty_files=False, obb_boundary_policy="skip")` | 同上 |
| Detect 模式 | `mode="hbb"`（矩形框分支） | 同上 |
| Segment 模式 | `mode="seg"`（多边形分支） | 同上 |
| 任务→模式映射 | `TASK_LABEL_MAPPINGS["Detect"] == "hbb"`、`TASK_LABEL_MAPPINGS["Segment"] == "seg"` | `anylabeling/services/auto_training/ultralytics/config.py` |
| 参数默认值 | `DEFAULT_TRAINING_CONFIG`（epochs 100 / batch 16 / imgsz 640 / workers 8 …） | `anylabeling/services/auto_training/ultralytics/config.py`；两条硬规则见下 |
| 工作目录 | `get_work_directory()` | `anylabeling/config.py`；台账落点见 §5.3.1 |
| UI 复用件 | `_with_button` 组合、`QFileDialog.getExistingDirectory` / `getOpenFileName`、`setReadOnly(True)` | 见 §5.1.3 的窗口骨架参照表 |
| HTTP 客户端 | 上游核心依赖 `requests`（另有 fork 自有的 `requests-toolbelt`，§5.3.6） | 所有训练接口调用与流式上传 |

**三条「明确不复用」**：

| 不复用对象 | 原因 |
| --- | --- |
| `collect_pairs`（`anylabeling/custom/model_validation/dataset.py`）的 `os.walk` 递归遍历 | 它产出含子目录的相对路径，而协议要求 `manifest.images[].name` 是**不含目录分隔符**的扁平文件名（§4.1.2）；照抄的结果是「整包被拒」或「flatten 后同名互相覆盖」。只借鉴返回结构，扫描另写（§5.2.3） |
| `is_label_usable`（同上文件） | 它在 `shapes` **缺失**时 `return True`（判为可用），而 `label_converter.py` 随后**直接索引** `data["imageWidth"]` / `data["shapes"]` / `shape["shape_type"]` / `shape["label"]` / `shape["points"]`——畸形条目会让 worker 崩溃。远程训练改用**独立的完整 schema 校验**（§5.2.4），把「`shapes` 缺失 / 非 list / 条目非法」全部列为**阻断项** |
| 本地训练的「随机划分」写法（`random.sample` 后按 `dataset_ratio` 切片，**无种子、不可复现**） | `anylabeling/services/auto_training/ultralytics/general.py` 只是「按类别分层」的先例，不是可复制的实现；远程训练改为**派生种子 + 逐类确定性洗牌**（§5.2.7） |

**两条硬规则（写死）**：

- `optimizer` 的**表单初值是第 0 项**（`data=None`，文案「（服务端按默认策略选择）」）：请求体**不含** `optimizer`，生效值由服务端 `preset_policy` 决定（出货策略 `type:"auto"` ⇒ 生效 `auto`、来源 `server_auto`，§3.8.4）；`auto` 与具体 preset 是用户**显式选择**，选了就发（`params.optimizer = "auto"` / `"<preset 名>"`）；`auto` 与 7 个优化器超参同送仍 422（§3.8.3）。
- `save_period: -1` **不得原样透传**：它表示「不保存周期快照」，映射时应转为 `0` 或不传；服务端范围以 `param_schema.save_period`（0–1000）为准。

**跨仓耦合告警（写死）**：客户端表单默认**不发送** `optimizer`，其生效值完全由服务端 `preset_policy.type` 决定。出货配置为 `type: "auto"`；若某部署把服务端改回 `type: "iterations_threshold"`，客户端必须同步改回「默认显式发送 `auto`」（或要求用户显式选择 `auto`），否则未显式选择的提交会重新走 preset 注入路径（历史实测：`yolo11-adamw`（AdamW `lr0=0.001` + `warmup_bias_lr=0.1`）+ 80 类小数据集，100 轮训练 mAP50 从 E1 的 0.4323 崩到 E5 谷底 0.0058，末 20 轮均值 0.1890；`best.pt` 停在 epoch 1，`results.csv` 末值 0.4315 是 `best.pt` 复测，不是末轮指标）。两处改动必须**同批上线**。

**另一条形态约定**：表单**只提交用户显式设置过的键**——未设置的键不出现在请求体 `params` 里，由服务端 / ultralytics 走默认值；参数面范围与分组见 §3.8。

#### §5.2.3 根目录扫描

**约束来源**：协议要求 `manifest.images[].name` 不含目录分隔符（§4.1.2），因此**嵌套数据集无法原样表达**——照搬递归扫描的结果只能是「整包被拒」或「flatten 后同名互相覆盖」，两者都不可接受。

| 规则 | 做法 |
| --- | --- |
| 扫描范围 | **只遍历 `dataset_dir` 根目录**（不递归、`os.scandir` 单层） |
| 发现子目录项 | **阻断并列出**（红色状态行，不发起任何网络请求）：`"数据集包含子目录，v1 只支持根目录下的图片：\n- sub/a.jpg\n- sub/b.png"`。**不 flatten、不上传、不静默忽略** |
| 根目录下的非白名单文件 | 忽略（只按 `IMAGE_EXTENSIONS` 与 `.json` 收集） |
| 同名不同扩展名 | 由 §5.2.5 #10 的**数据集级**同 stem 规则阻断——**不论两张图最终落在同一个 `<split>` 还是分属 train / val** |
| 将来扩展 | 若要让 v1 支持嵌套目录，必须**先改协议**（把 `images[].name` 定义为规范化相对 POSIX 路径，服务端同步改 zip 路径解析与校验）——**本轮不改协议** |

#### §5.2.4 完整 schema 校验与 `points` 基数表

进入转换之前，对每个 `.json` 做**完整**校验（**不复用** `is_label_usable` 的宽松判定）：

| 校验项 | 判定 | 不通过时 |
| --- | --- | --- |
| JSON 可解析 | `json.loads` 成功且是 `dict` | **阻断**（§5.2.5 #4） |
| `imageWidth` / `imageHeight` | 存在、`int`（或可无损转 `int`）、**> 0** | **阻断** |
| `shapes` | **存在**、是 `list`（**缺失或非 list 都是阻断**）；可以为空列表（背景图，§5.2.5 #2） | **阻断** |
| 每个 `shapes[]` 条目 | 是 `dict`；`shape_type` ∈ 该模式支持的集合（Detect：`rectangle`；Segment：`polygon`；**其它类型按「跳过对象」处理**）；`label` 是 `str` 且**在 `classes` 内**（不在内的同样按「跳过对象」处理）；`points` 是 `list`，每项是长度恰为 2 的数值序列（`[x, y]`），且**元素个数必须落在下表按模式 / 形状给出的合法集合内** | 条目**结构非法**（缺 `shape_type` / `points` 不是 `list` / 某个点不是 `[数值, 数值]` / **Detect 的 `rectangle` 点数不在 {2, 4}**）⇒ **阻断**；**Segment 的 `polygon` 少于 3 点**（含空列表）⇒ **跳过该对象 + 计数、不阻断** |
| 文件可读 | 打开成功（权限 / 编码问题显式报错） | **阻断** |

**`points` 列表长度的基数约束（逐条给出依据）**：

| 模式 | `shape_type` | `points` 元素个数 | 依据（转换器实读行为） | 越界处理 |
| --- | --- | --- | --- | --- |
| Detect（`mode="hbb"`） | `rectangle` | **恰好 2 或 4** | 进入矩形分支后，对 `len(points) == 2` 走**已弃用但对角线模式**（`rectangle_from_diagonal` 取 `points[0]` / `points[1]` 并补齐为四点，同时打印弃用 warning）；对 4 点取 `points[0]` 与 `points[2]` 计算中心与宽高 | `len ∈ {0, 1, 3, > 4}` ⇒ **阻断**（索引 `points[2]` 会 `IndexError`；3 点虽不抛错但语义不明，一律按结构非法）。**2 点合法但会打弃用 warning**：预检摘要里提示「该标注使用旧版对角线矩形，已按四点矩形等价转换」 |
| Segment（`mode="seg"`） | `polygon` | **≥ 3** | 进入多边形分支后 `if len(points) < 3: continue`（**转换器自己跳过**，不是抛错）；随后写 `1+2k` 字段 | `len < 3` ⇒ **跳过该对象并计数**（与转换器行为一致）；`len == 0` 同样按跳过处理（不阻断，但要计入跳过计数） |
| Detect / Segment | `point` / `line` / `circle` / `rotation` 等 | 不适用 | 两个模式只匹配 `rectangle` / `polygon`，其它 `shape_type` 直接落空、**不产生任何输出行** | **跳过 + 计数**，**不算阻断**；v1 不做 Pose / OBB，故**不定义** `point` 与 `keypoints` 的基数要求 |

- **为什么必须写清基数**：Detect 分支**无条件**访问 `points[0]` 与 `points[2]`，只有「2 点」会被补齐成 4 点，因此**空列表与单点列表**会让 worker 抛 `IndexError`。本表把约束**前置到 schema 校验**，并仍保留下面的 `try/except` 兜底（双保险）。
- **阻断规则的单一口径（写死，只此一条）**：**只有 Detect 的 `rectangle` 点数不在 {2, 4} 才阻断**（`len` 为 0 / 1 / 3 / > 4）；**Segment 的 `polygon` 少于 3 点一律「跳过该对象 + 计数」、不阻断**（`len == 0` 同样只是跳过，但要计入跳过计数）。本条与上表、§5.2.6 的「多边形点数 < 3」行必须完全一致，实现不得自行选择阻断或降级。
- **与「跳过对象」的边界**：`polygon` 点数不足、`shape_type` 与模式不匹配、`label` 不在 `classes` 内 ⇒ **跳过 + 计数**（与转换器行为一致）；**其余结构非法**（缺 `shape_type`、`points` 不是列表、点不是两个数、`rectangle` 点数不在 {2, 4}）⇒ **阻断**（§5.2.5 #4）。

**逐文件 `try/except` 包裹是硬要求**：`LabelConverter.custom_to_yolo` 内部**直接索引** `data["imageWidth"]`、`data["shapes"]`、`shape["shape_type"]`、`shape["label"]` / `shape["points"]`——即使上面的校验漏掉了某个畸形分支，实现也**必须**把每张图片的转换调用包在 `try/except Exception` 里，把异常**计入该文件的校验结果**（列为阻断项并给出文件名 + 异常摘要），**绝不允许**异常冒泡到 worker 层让线程崩溃（`QThread` 里未捕获异常会直接终止线程，UI 会永久卡在「转换中」）。

#### §5.2.5 N1 校验矩阵（客户端侧）与用户可见文案

| # | 场景 | 客户端动作 | 用户可见提示（可直接使用的文案） |
| --- | --- | --- | --- |
| 1 | 图片无同名 `.json` | **阻断** | "发现 3 张图片缺少同名标注文件（.json），已停止上传：\n- road_0007.jpg\n- road_0011.png\n- road_0012.jpg" |
| 2 | `.json` 有效但 `shapes` 为空 | 继续（背景图） | "12 张图片的标注为空，将作为背景图参与训练（生成空标签文件）" |
| 3 | `shapes` 部分可转换 | 继续 + 警告计数 | "7 个标注对象被跳过（不在类别表 classes.txt 中或形状不合法），已保留可用标注；跳过最多的文件：road_0003.png（3 个）" |
| 4 | `.json` 损坏（**完整 schema 校验**：解析失败 / 非 dict / **`shapes` 缺失或非 list** / 条目缺 `shape_type` 或 `points` 非法 / 缺 `imageWidth` `imageHeight` 或非正整数） | **阻断** | "发现 2 个标注文件损坏，无法解析为 X-AnyLabeling 标注：\n- road_0020.json（shapes 缺失）\n- road_0021.json（imageWidth 非正整数）\n请修复后重试"（逐文件给出原因，异常摘要同样列出，§5.2.4） |
| 5 | 有标签无图片（孤儿标签） | 忽略 + 统计（**条目级**，不阻断） | "忽略 4 个没有对应图片的标注文件（服务端同样忽略并计入 warnings）" |
| 6 | 图片文件在扫描后被移动 / 删除 | **阻断** | "打包过程中有 1 个文件不可读，已停止：road_0009.jpg" |
| 7 | `classes.txt` 为空或不可读 | **阻断** | "类别表 classes.txt 为空或不可读，请重新选择" |
| 8 | 类别表与标注完全不匹配（全部对象被跳过） | 警告 + 允许继续（**有意为之**的非阻断判定，见下） | "有 5 张图片的标注全部无法转换，它们将作为背景图参与训练；请确认类别表是否选对" |
| 9 | 数据集为空 | **阻断** | "所选目录中没有找到图片（支持：.jpg/.jpeg/.png/.bmp/.webp/.tif/.tiff）" |
| 10 | 出现同 stem 的不同扩展名（如 `road_0001.jpg` 与 `road_0001.png`，`stem` 相同 ⇒ 标签路径同名） | **阻断**（客户端提前拦截；**判定粒度 = 数据集级**，见下） | "发现 1 组同名不同扩展名的图片（标签文件同名会互相覆盖）：\n- road_0001.jpg / road_0001.png\n请重命名或移出其中一张后重试" |
| 11 | **数据集含子目录** | **阻断**（不递归、不 flatten） | "数据集包含子目录，v1 只支持根目录下的图片，请把它们移到根目录或改选子目录：\n- sub/a.jpg\n- sub/b.png" |

**阻断类问题（上表 #1 / #4 / #6 / #7 / #9 / #10 / #11，共七行）必须在发起任何网络请求之前给出**，并在配置页顶部以红色状态行列出问题文件（可全选复制）。

- **#10 的判定粒度写死**：客户端按**数据集级**判定——**只要根目录下存在同 stem 的不同扩展名就阻断，不论它们是否落在同一个 `<split>`**（`<split>` 要到 §5.2.1 步骤 5 才产生，而本阻断必须在任何网络请求之前给出，因此不能在步骤 2 按 split 判定）。根因是 `<staging>/labels/<stem>.txt` **与 split 无关**（§5.2.6），跨 split 同 stem 会在本地标签工作目录里**互相覆盖**。**这是客户端严于服务端的本地策略**：服务端只拒绝**同一 split 内**的同 stem（§4.1.5 / §4.1.6）。
- **#8 是有意为之的降级判定**：单张图片的标注全部不可转换，只说明该图片没有可用目标，退化为背景图（0 字节标签）后仍可参与训练；预检摘要同时给出「跳过对象数」与涉及文件。若认为该结果非预期，第一顺位应检查 `classes.txt` 是否选错（其次才是标注本身缺少类别覆盖）。与 #8 的差别在于**可按对象计数判断**：`.json` 本身损坏（#4）无法给出计数，因此仍是阻断项。
- **非白名单扩展名 / 非法 `split`**：扫描阶段只收集 `IMAGE_EXTENSIONS` 内的图片，`split` 由客户端固定取 `train` / `val`，因此客户端**不会**产出这类条目。若服务端 plan 仍返回 `rejected[]` 里的 `UNSUPPORTED_EXTENSION` / `INVALID_SHA256` / `INVALID_SPLIT` / `NAME_EMPTY`，说明客户端 manifest 生成与本地校验不一致：按**条目级**处理（弹窗列出「文件名 + 原因」，用户确认后继续，被拒条目**不进 zip**），**绝不在 zip 里出现**这些条目——它们在 upload 阶段是**整包 400**（§4.1.6）。
- **与服务端 N1 列的口径一致**：客户端这一层把「`shapes` 缺失 / 非 list / 条目非法」都算阻断（服务端看不到 `.json`，只能通过标签缺失间接发现 `MISSING_LABELS`，§4.1.6）。

#### §5.2.6 转换与计数、标签字节冻结

**转换细节与计数规则**：

| 情况 | 转换器行为 | 本功能的处理 |
| --- | --- | --- |
| `shapes == []` | 输出文件被创建但不写任何行 → 0 字节 | 记为 background（空 txt = 背景图），计数并在 UI 提示 |
| 形状类型与模式不匹配（如 Segment 任务里出现 `rectangle`） | 该形状被跳过（`continue`） | 计入「跳过对象数」警告 |
| 标签不在 `classes` 内 | 该形状被跳过 | 计入「跳过对象数」警告，并提示该标签名（去重后取前 N 个） |
| 多边形点数 < 3（Segment） | 跳过 | 计入警告 |
| `.json` 缺 `imageWidth` / `imageHeight` | 取值时抛异常 | 视同「损坏」，阻断并列出文件名（§5.2.5 #4） |
| `.json` 文件不存在 | `skip_empty_files=False` 时创建空文件并返回 | 不应发生（扫描阶段已保证成对）；若发生按阻断处理 |

**计数方式**：转换前统计 `len(data["shapes"])`，转换后统计输出文件行数，差值即「跳过对象数」；每张图片保留 `{name, shapes_total, shapes_written, skipped_labels[]}` 用于 UI 明细。

**标签字节的冻结与 `label_sha256` / `label_size`**

**为什么必须有这一步**：服务端对 manifest 里的**每一条** `images[]` 都会在 upload 阶段按 `stem` 定位解压出的 `labels/<split>/<stem>.txt`、**逐文件计算 sha256** 并与声明的 `label_sha256` 逐字节比对，不符即整包 400 `LABEL_CHECKSUM_MISMATCH`（§4.1.3、§4.1.6）。

| 项 | 规则（写死） |
| --- | --- |
| 冻结时机 | **转换输出写完之后、plan 之前**（步骤 4 + 步骤 6）：每张图片的 `custom_to_yolo` 输出写完即视为**冻结**——每个 txt **只写一次、关闭后再读**（步骤 6 只读），此后**任何步骤都不得再改写它** |
| 标签写在哪里 | 写在**与 `split` 无关的标签工作目录** `<staging>/labels/<stem>.txt`——**不带** `labels/<split>/` 这一层（步骤 4 时 `split` 尚未产生）；打包（步骤 10）时只按步骤 5 确定的 `<split>` 补路径前缀 ⇒ zip 内写作 `labels/<split>/<stem>.txt` |
| 计算方式 | 对每个**已冻结**的 txt **逐文件流式计算 sha256**（复用 `sha256_file`，1 MiB 分块），同时取文件字节数作为 `label_size`；**步骤 6 一次性完成，之后不再复算** |
| 空标签 | `shapes == []` 的背景图输出 **0 字节** txt：`label_size = 0`，`label_sha256` = **空文件哈希** `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。**必须照算**，不得跳过、也不得用其它替代值 |
| 与 `split` 的关系 | `label_sha256` 是**文件内容**的哈希，与 `split` **无关**（`split` 只决定 `labels/<split>/` 这一层路径前缀）；因此不得在划分之后按新路径再算出一份不同的值 |
| **一处内容、三处使用（硬性）** | **plan 请求体（步骤 8 与二次 plan 步骤 9）、zip 里的 `labels/`（步骤 10）、崩溃后的重放（持久化的 `archive.zip` 原样字节）必须使用同一份标签字节**。重放**只允许**复用已持久化的 `archive.zip`，**不得**重新转换 / 重新打包——服务端按判定表用 `upload_token` + **zip 内 `manifest.json` 规范化字节**判断是否同 body（§4.1.3 / §4.1.4） |
| 与图片哈希的分工 | `images[].sha256` / `size` 是**图片**文件的值（步骤 7 流式计算），`label_sha256` / `label_size` 是**标签**文件的值（本步）。两者不得互相顶替：400 `CHECKSUM_MISMATCH` 只管图片、400 `LABEL_CHECKSUM_MISMATCH` 只管标签（§3.3） |

- **失败路径**：`LABEL_CHECKSUM_MISMATCH` 是**整包级**失败、**不产生 `dataset_id`**；客户端逐条展示 `details.files[]`（`name` / `label` / `reason` / `declared` / `actual`，文案见 §5.6），pending 条目的转移与 `void_reason` 取值归 §5.4——**不要**把它当成图片的 `CHECKSUM_MISMATCH`。
- **本地自检（廉价保险）**：打包（步骤 10）时对写进 zip 的每个 `labels/<split>/<stem>.txt` 复算一次 sha256 并与 manifest 比对，可在本地提前抓到「冻结后被改写」的实现 bug（服务端仍会再验一次）。

#### §5.2.7 划分算法（本机唯一实现，服务端只校验）

输入：扫描 + 转换后的图片集合（每张图片携带其**可转换标签行**对应的类别集合 `C_img`，来源见 §5.2.6）、`classes` 顺序（取自 `classes.txt`）、`val_ratio`、`seed`。

记号：`I_c` = 「至少含 1 个类别 c 实例」的图片集合（背景图不属于任何 `I_c`）；`classes` 是类别名列表。

**结构（写死）**：先固定输入顺序 → 循环外初始化 → **单循环内交错计算并立即分配**（4a 继承 → 4b 夹取 → 4c 需求 → 4d 立即分配）→ 循环后收尾安全网；除 `seed_c` 驱动的洗牌外**无任何随机源**。

**步骤 1（固定输入顺序，循环之前）**

- 对每个类别 c（按 `classes` 顺序）收集 `I_c = {img | c ∈ C_img}`（背景图不属于任何 `I_c`），并先把 `I_c` 按**图片名升序**排序；
- 构造 `order = sorted(classes, key=lambda c: (len(I_c[c]), classes.index(c)))`（**稀有度升序**，同稀有度按 `classes` 的稳定顺序）；
- 顺序一旦算出就不再变化——它是可复现性的前提（步骤 7）。

**步骤 2（每类独立洗牌：派生种子，循环之前）**

```python
seed_c = {}
shuffled_c = {}
for c in classes:
    seed_c[c] = int.from_bytes(
        hashlib.sha256(f"{seed}:{c}".encode("utf-8")).digest()[:8], "big"
    )                                           # 派生种子：按类别名（不是索引）派生
    base = sorted(I_c[c])                       # 名字升序：固定输入顺序（可复现的前提）
    random.Random(seed_c[c]).shuffle(base)      # 逐类确定性洗牌
    shuffled_c[c] = base
```

`c` 取**类别名**（不是索引），避免类别表排序变化导致划分漂移；`seed` 就是 manifest 里上报的那个整数。派生式写死为上式（跨机复现要求两端一致）。

**步骤 3（循环外初始化 `assignment`）**

```python
assignment = {}        # img -> "val"；未出现在其中的图片最终归 train
```

**必须在 `for c in order` 循环之前**初始化为空映射。把这一行写进循环体内，会让第 4a 步读到尚未定义的名字；若实现者「补一个空映射」到第 4a 步，则每类的 `inherited_c` 恒为 0，多标签图片会整批进 val、类别在 train 侧零代表。

**为什么必须扣继承**：继承的图片若不占配额，已归 val 的图片就会**绕过** `n_val_c ≤ |I_c| − 1` 这道夹取（单一类别可把配额全额花在**新图**上，多标签图片可能整批进 val，使某些类别在 train 侧零代表）；扣继承后，`inherited_c ≤ n_val_c` 时恒有「继承 + 新增 = `n_val_c` ≤ `|I_c| − 1`」。两点注意：① **继承可以超配**（`inherited_c > n_val_c` 时 `need_c = 0`，极端时本类全部图片已在 val）；② **后续类别仍可能把本类剩余的 train 图选入 val**。因此夹取只约束本类当轮新增额度，「`|I_c| ≥ 2` 两侧各至少 1 张」最终由**步骤 5（收尾安全网）兜底且带例外**（兜不住的进「两侧代表无法保证」清单）。该折算只影响**配额**，不改变循环顺序（`order` 已在循环之前固定）。

**步骤 4（单循环内交错计算并立即分配——唯一结构）**

```python
for c in order:                                  # order 已在步骤 1 固定（稀有度升序，稳定）
    # (4a) 继承：本类图片中**此刻**已被更早处理的类别置为 val 的张数
    #      —— 必须用当前 assignment 求值，因此只能在循环体内计算
    inherited_c = sum(1 for img in I_c[c] if assignment.get(img) == "val")

    # (4b) 名义配额（固定用 Python 内建 round（银行家舍入），实现不得混用其它舍入）
    n_val_c = round(len(I_c[c]) * val_ratio)
    if len(I_c[c]) == 1:
        n_val_c = 0                              # |I_c| == 1：本类不新增 val
    elif len(I_c[c]) >= 2:
        n_val_c = min(max(n_val_c, 1), len(I_c[c]) - 1)   # 夹取：本类 val 不超过 |I_c| - 1

    # (4c) 本类还需要**新增**多少张 val（继承的已算过，不得重复消耗）
    need_c = max(0, n_val_c - inherited_c)

    # (4d) **立即**按洗牌序把尚未分配的图片置为 val，直到 need_c 用尽
    for img in shuffled_c[c]:                    # 已洗牌顺序 ⇒ 确定
        if need_c == 0:
            break
        if img not in assignment:                # 已被其它类别决定过的图片：沿用既有决定，不改判
            assignment[img] = "val"
            need_c -= 1
        # 已被决定过的图片已计入 (4a) 的 inherited_c，因此不重复消耗本类配额
```

循环结束后：`assignment` 中的图片划入 **val**，**其余全部图片划入 train**（含背景图与所有未被任何 val 配额选中的图片）。

判据公式（逐字）：

```text
seed_c      = int.from_bytes(sha256(f"{seed}:{c}".encode("utf-8")).digest()[:8], "big")   # c 取类别名
n_val_c     = round(|I_c| × val_ratio)；|I_c| == 1 ⇒ 0；|I_c| ≥ 2 ⇒ min(max(n_val_c, 1), |I_c| − 1)
inherited_c = Σ [assignment.get(img) == "val"]  对 img ∈ I_c
need_c      = max(0, n_val_c − inherited_c)
```

**步骤 5（收尾安全网，必需环节，不是可有可无的校验）**

主逻辑（4a–4c 扣继承 + 4b 夹取 + 4d 分配）只保证「`|I_c| >= 2` ⇒ 本类**当轮** `val >= 1`」；**后续类别新增 val 时仍可能吃掉本类最后一张 train 图**，因此 `train >= 1` 由本步兜底。全部类别处理完后做一次校验——若某类满足 `|I_c| >= 2` 却 `train == 0`，在 `shuffled_c` 中**逆序**寻找第一张满足「其降级不会使任何其它 `|I_d| >= 2` 的类别 val 归零」的 val 图降回 train 并复检；若不存在这样的候选，保持现状并把该类别记入「两侧代表无法保证」清单、在划分预览里**红色高亮**（**不阻断上传**；步骤 6 仍只对 val / train 为空做阻断）。最多迭代 `K = 3` 次（**每轮重新检查全部类别**）；仍不满足的类别同样进清单 + 预览高亮：

```python
def val_count(cls):                              # 本类当前在 val 的图片数
    return sum(1 for x in I_c[cls] if assignment.get(x) == "val")

def is_safe(img, c):
    """img 降回 train 后，不会把任何其它 |I_d| >= 2 的类别的 val 归零。"""
    for d in classes:
        if d == c or len(I_c[d]) < 2:
            continue
        if img in I_c[d] and val_count(d) <= 1:
            return False
    return True

unresolved = []                                  # 「两侧代表无法保证」清单（过程命中）
for _ in range(3):                               # K = 3；每轮重新检查全部类别
    # 复查：其它类别的降级会把共享 val 图降回 train（train 只增不减），
    # 已满足 train >= 1 的类别必须移出清单
    unresolved = [c for c in unresolved if len(I_c[c]) - val_count(c) == 0]
    fixed = False
    for c in classes:
        n_c = len(I_c[c])
        va = val_count(c)
        tr = n_c - va
        if n_c >= 2 and tr == 0 and va > 0:
            # tr == 0 蕴含 va == n_c >= 2 ⇒ 降级后本类 val 仍 >= 1；
            # 但该图可能正是其它 |I_d| >= 2 的类别最后一张 val，故候选必须筛
            cand = next((img for img in reversed([x for x in shuffled_c[c] if assignment.get(x) == "val"])
                         if is_safe(img, c)), None)
            if cand is None:
                if c not in unresolved:
                    unresolved.append(c)         # 无安全候选：保持现状
                continue
            del assignment[cand]                 # 降回 train（候选已筛过：不会把其它类别的 val 归零）
            fixed = True
    if not fixed:
        break
unresolved = [c for c in unresolved if len(I_c[c]) - val_count(c) == 0]   # 收尾复查：只保留最终 train == 0 的类别
```

安全网**只把 val 降回 train、不反向提升**，不会改变确定性（`shuffled_c` 与 `classes` 顺序都已固定）；但它**并不保证 100% 消除 `train == 0`**——残留个案靠「清单 + 划分预览红色高亮」暴露给用户（**不阻断上传**，是否整体阻断仍由步骤 6 判定）。

**步骤 6（全局保证，循环之后）**

`val` 与 `train` **均非空**，否则配置页给红色状态行并**阻止上传**（不发起任何网络请求）。服务端在 plan 阶段对「划分两侧非空」做同样的存在性校验（400 `VALIDATION_FAILED`，`details.field=split`，§3.3），但**不重算**、不按 `val_ratio` 纠正客户端结果。

**步骤 7（确定性要求，7 条）**

1. `seed_c` 按**类别名**派生（`sha256(f"{seed}:{c}")` 取前 8 字节大端），不按类别索引；
2. 每类的 `I_c` 必须在洗牌前按**图片名升序**排序（固定输入顺序是前提）；
3. `order` 用「稀有度升序 + `classes` 稳定序」，算完不再变化；
4. 名义配额固定用 Python 内建 `round`（银行家舍入），不得混用其它舍入；
5. 实现**禁止**引入其它随机源（时间戳、`set` 迭代顺序、线程调度、字典插入顺序）；
6. 同一 `(数据集内容, classes 顺序, val_ratio, seed)` ⇒ 划分结果**逐图一致**（`images[].split` 完全相同）；
7. 换 `seed` ⇒ 划分变化。

**边界与口径**：

| 情形 | 行为 |
| --- | --- |
| `\|I_c\| == 1` | 本类 `n_val_c = 0`（**本类不新增 val**）；该图**只有当它不属于任何其它有 val 配额的类别时才必然归 train**——若它是多标签图片并被其它有配额的类别选中，则会落在 val（此时 `split_stats[c] = {train: 0, val: 1}`，本类**不**触发下条告警）。「类别 c 未出现在 val」的本地警告**以最终 `split_stats[c].val == 0` 为准**（客户端本地标记，**不进协议**、不阻断上传；与 §5.2.8 预览口径一致）：此时上报的 `split_stats[c] = {train: 1, val: 0}` 满足「`train + val ≥ 1` 且 `val == 0`」，服务端因此在 upload 响应里回一条 `SPLIT_CLASS_MISSING_VAL`（同一语义的复述，§3.9 通道 B） |
| `\|I_c\| >= 2` | **`val >= 1` 恒成立**（4b 夹取 + 4a/4c 扣继承）；**`train >= 1` 带例外**——由步骤 5 安全网**兜底**，安全网找不到安全候选的残留个案进「两侧代表无法保证」清单、在预览里**红色高亮**、**不阻断上传**。因此准确表述是「每个 `\|I_c\| ≥ 2` 的类别**在 val 至少有 1 张**；在 train 至少 1 张**除残留个案外成立**，残留个案必须被清单与高亮如实暴露」——**不得**再写成无例外的「两侧都有代表」 |
| 背景图（`shapes` 为空） | 不属于任何 `I_c`，**永远归 train**；若因此 val 为空则按步骤 6 阻止上传 |
| 多标签图片 | 只出现在一侧：步骤 4 用 `assignment` 记录**首次**决定，后续类别不再改判（输出里不会出现同一图片同时进 train 与 val）；本类在**后续轮到自己时**的第 4a 步会把该图片算进 `inherited_c`，本类不再为它重复花配额 |
| 类别稀有度相同 | 按 `classes` 顺序做稳定排序，保证确定性 |
| val 占比（实际） | 各类 val ≈ `round(\|I_c\| × val_ratio)`（名义配额，步骤 4b）；重叠越重实际 val 越低，全重叠时收敛到 `max_c n_val_c ≈ N × val_ratio`。客户端**必须**在划分预览里展示**实际** val 占比与各类名义配额之和 `Σ_c n_val_c` 作对照（§5.2.8），不再宣称「偏差不超过一个类别配额」 |

> 步骤 4 的「先稀后多」只决定**谁先挑图片**，不改变每类配额：稀有类别的 val 配额不会被多数类抢走，因此「类别覆盖」优先于「比例精确」；比例偏差改由「划分预览展示实际占比 + 用户可调 `val_ratio` / 换 `seed`」暴露与消化，不改用阻断。

**三条硬断言（实现与验收共用，写死）**：

1. `|I_c| ≥ 2` ⇒ **`val ≥ 1` 恒成立**（无例外）；
2. `|I_c| ≥ 2` ⇒ **`train ≥ 1` 带例外**：兜不住的类别必须出现在「两侧代表无法保证」清单里并在预览里红色高亮——**不得**写成无例外的「两侧都有代表」；
3. **全重叠**（`I_a = I_b = … = I`）时 **`|V| = max_c n_val_c ≈ N × val_ratio`**：除**第一个**处理的类别外其余类别的 `need_c` 都因继承而归零（`inherited_c ≥ n_val_c`）。

#### §5.2.8 划分预览、seed 生成与配置导入 / 导出

**划分预览**：扫描 + 转换 + 划分完成后（步骤 5–6′），配置页在「预检摘要」上方展示；用户确认后才进入 manifest 组装、plan、打包与上传（**阻断项存在时预览表本身不可确认**）：

| 展示项 | 内容 | 警告 / 标注（黄条 = 提示、红色高亮 = 标注、红色状态行 = 阻断） |
| --- | --- | --- |
| 每个类别 | 该类别在 train / val 的张数（即上报的 `split_stats` 行，按 `classes` **全量**排列，含零图类别 `{train: 0, val: 0}`） | 某类 `train + val ≥ 1` 却 `val == 0`（含 `\|I_c\| == 1` 的类别）→ 与 `SPLIT_CLASS_MISSING_VAL` 同一口径的本地提示行（§5.2.7 边界表，**不阻断上传**） |
| 两侧代表 | 承接上一行的每类 train / val 张数（本行不另列计数），只标出「两侧代表无法保证」的类别 | **红色高亮**（与黄条语义不同）：`\|I_c\| ≥ 2` 却 `train == 0`、且步骤 5 在 `shuffled_c` 里找不到安全候选降级的类别。高亮**以该类别最终的 `train == 0` 为准**——若同轮或后续轮其它类别的降级把它的一张共享 val 图降回 train、使 `train ≥ 1`，该类别即从清单移出、不再高亮（清单与高亮同源，不得互相矛盾）。**不阻断上传** |
| val 合计 | **实际** val 总张数与**实际占比**（`val_total / total`，与目标 `val_ratio` 并列展示；小数保留 1 位），另给各类名义配额之和 `Σ_c n_val_c` 作对照上界 | 实际占比与 `val_ratio` 相差超过 **0.10**（10 个百分点）时提示「实际 val 占比 X% 与目标 Y% 相差较大，可调整 `val_ratio` 或换 `seed` 重算」（仅提示，不阻断） |
| 阻断项 | — | val 为空 / train 为空 → **阻止上传**（红色状态行，§5.2.7 步骤 6） |
| 服务端回执 | 预检摘要里显示 upload 响应 `warnings[]`（如 `SPLIT_CLASS_MISSING_VAL`）与 `BACKGROUND_IMAGES` 等 | 信息性提示行，不阻断（通道 B，§3.9） |

**seed 生成**：seed 输入框允许留空（占位符「留空则自动生成」）；留空时在**扫描开始时**用 `secrets.randbelow(2**31 - 1)` 生成一个种子并**回填**输入框，之后不再变化——保证「同一次配置 ⇒ 同一次划分」，且导出 JSON 里带着它（可复现）。用户可手工改成任意整数后重新预览。

**训练配置导入 / 导出 JSON**：

```json
{
  "schema_version": 1,
  "server_url": "http://10.0.0.5:8000",
  "dataset_dir": "/data/annotations/person",
  "classes_file": "/data/annotations/person/classes.txt",
  "task": "detect",
  "model_family": "yolo11",
  "model": "yolo11s.pt",
  "val_ratio": 0.2,
  "seed": 20260101,
  "split_strategy": "per_class",
  "params": {"epochs": 100, "batch": 16, "imgsz": 640, "workers": 8, "optimizer": "yolo11-sgd"}
}
```

| 规则 | 说明 |
| --- | --- |
| 必含键 | `val_ratio` / `seed` / `split_strategy` **必须**进导出 JSON：§5.2.7 的确定性只对同一 `(数据集内容, classes 顺序, val_ratio, seed)` 成立，缺了它们就无法复现同一次划分 |
| 不含 Token | `server.json` 里的 Token **不导出**（§5.3.3）；导入后由用户在配置页重填 |
| 导入行为 | 逐键回填表单 → 重新扫描 + 转换 + 划分 + 预览（**不直接复用旧划分结果**），保证「导入的配置在该数据集上重算出的划分」与导出时一致 |
| 未列出的键 | 表单其余字段（如 batch 的 `auto` 取值）按导入值回填；文件里没有的键保持当前界面值，不做隐式重置 |
| `params` 的键集 | **只含用户显式设置过的键**：未设置的键**不出现在导出 JSON 里**（上例中的键仅为示例），导入后保持默认态、由服务端 / ultralytics 走默认值，不做隐式补齐（§3.8） |
| 版本 | `schema_version` 变更时按版本号补齐字段，不删旧键 |

#### §5.2.9 打包

```text
<archive>.zip
  manifest.json                  # = plan 请求体原文（客户端把 plan 阶段序列化出的原始字节原样写入，upload 时不重新 dump）
  images/<split>/<name>          # 只放**最终** missing_images[] 列出的图片；missing_images = [] 时本目录可整体缺席
  labels/<split>/<stem>.txt      # **全量**标签，每次必传（允许 0 字节）；用步骤 6 冻结的同一份字节
```

zip 的**协议侧**规格与安全项（zip-slip、文件名编码、扩展名白名单、未知顶层条目、多余条目与孤儿标签、zip bomb、原子落盘）一律见 §4.1.5；本节只写客户端的打包行为：

| 项 | 规则 |
| --- | --- |
| `stem` 定义 | 图片文件名去掉**原扩展名**（`road_0001.jpg` → `road_0001`），标签文件名 = `stem` + `.txt`；**不得**把含扩展名的图片名直接拼 `.txt`（那会得到 `road_0001.jpg.txt` 这种错误路径） |
| 打包时机 | 只能在**最终 plan 的响应落盘之后**构造（步骤 10）：`images/` 只放该次 plan 返回的 `missing_images[]`；`labels/` 用步骤 6 冻结的字节（全量），`<split>` 由步骤 5 确定、在此**只作路径前缀**——**不重新转换、不重新计算哈希** |
| 打包库 | `zipfile.ZipFile(..., "w", zipfile.ZIP_DEFLATED, allowZip64=True)`；以流式 `write` 逐个文件写入，避免整包进内存 |
| 文件名编码 | Python `zipfile` 对非 ASCII 名自动置 UTF-8 标志位（服务端按 UTF-8 / CP437 双兼容解析，§4.1.5） |
| 临时目录 | **打包工作区**落在系统临时目录（`tempfile.mkdtemp(prefix="xal_remote_training_")`）；生命周期见 §5.1.5。**可重放数据不复用 staging**——`archive.zip` / `manifest.json` / 提交请求快照一律另存到 `pending/<id>/`（§5.3.1）；staging 只是打包工作区，且**只要仍被 pending 条目引用就免于 TTL 回收** |
| 取消 | worker 的 `should_stop` 回调在每张图片 / 每个文件之间检查（与 `anylabeling/custom/model_validation/dataset.py` 的取消语义一致）；取消后按 §5.1.4 步骤 6 与 §5.1.5 的 `finally` 路径清理 |
| 内容自校验 | 打包时对 zip 内 `manifest.json` 的字节与 plan 阶段序列化的字节做一次相等断言，并对每个 `labels/<split>/<stem>.txt` 复算 sha256 与 manifest 比对（§5.2.6 的廉价保险） |

### §5.3 台账格式与复用清单

**本节只写格式**：`pending` 台账的 phase 转移、崩溃对账与清理时机归 §5.4；监控与结果展示归 §5.5 / §5.6；台账字段的协议来源（plan / upload / jobs 响应）见 §3 与 §4.1。

#### §5.3.1 台账落点树

```text
<get_work_directory()>/xanylabeling_data/remote_training/
  server.json      # {"server_url": "...", "api_key": "...", "updated_at": "..."}（§5.3.3）
  tasks.json       # 本地任务台账 + pending 台账（§5.3.2）
  tasks.json.bak   # 上一次成功写入的备份（损坏回退用，§5.3.4）
  settings.json    # 三个本地配置键（§5.3.3）：与 server.json 同级、不进协议、不导出
  pending/<id>/    # 可重放数据（四文件，见下）
```

`pending/<id>/` 的四个文件：

| 文件 | 内容 | 来源 |
| --- | --- | --- |
| `archive.zip` | 最终 zip 的**原样字节**（重放只允许复用它，§5.2.6） | §5.2.1 步骤 11 写入 |
| `manifest.json` | plan 阶段序列化出的 manifest 原文字节 | §5.2.1 步骤 8 的响应落盘动作写入 |
| `submit_request.json` | 提交请求的快照（`POST /jobs` 的请求体原文） | 提交前写入（§5.4） |
| `meta.json` | 该条目的本地元信息（`upload_token` / `client_submission_id` / 创建时间 / 摘要） | 与条目同批写入 |

- `get_work_directory()` 见 `anylabeling/config.py`；`xanylabeling_data` 子目录约定与本地 Ultralytics 训练一致（`anylabeling/services/auto_training/ultralytics/config.py`）。
- `server.json` 单独存放 Token，避免 Token 混进可被导出的任务台账。
- **v1 单服务器**：`server.json` 只保存一套 `server_url` + `api_key`，**不做多服务器切换**；`tasks.json` 每条记录已带 `server_url`，将来若要支持多台训练机，只需再引入 `server_id`，**不改现有字段语义**。
- `pending/` **不在**系统临时目录、**不**匹配 `xal_remote_training_*` 前缀，因此 §5.1.5 的 owner marker 扫描、TTL 回收与 `keep_staging` **都不作用于它**；它的生命周期由 §5.4 的清理时机单独规定。反向约束同样成立：只要仍被 pending 条目引用，它**免于任何回收路径**。

#### §5.3.2 `tasks.json` 字段表

**字段名与枚举逐字为准，不得改名**（示例**每个数组只留 1 条**，省略号与计数仅为示例省略，§0.3）：

```json
{
  "schema_version": 1,
  "pending_uploads": [
    {
      "upload_token": "ut_9f1c2b7d4e5a6b7c8d9e0f1a2b3c4d5e",
      "phase": "uploading",
      "plan_request_hash": "sha256:3d7a1b20...",
      "upload_request_hash": "sha256:6b1f0c9a...",
      "server_url": "http://10.0.0.5:8000",
      "pending_dir": "<workdir>/xanylabeling_data/remote_training/pending/ut_9f1c2b7d",
      "archive_path": "<workdir>/xanylabeling_data/remote_training/pending/ut_9f1c2b7d/archive.zip",
      "manifest_path": "<workdir>/xanylabeling_data/remote_training/pending/ut_9f1c2b7d/manifest.json",
      "staging_dir": "<temp>/xal_remote_training_XXXX",
      "dataset_dir": "/data/annotations/person",
      "classes_file": "/data/annotations/person/classes.txt",
      "task": "detect",
      "val_ratio": 0.2,
      "seed": 20260101,
      "params": {"epochs": 100, "batch": -1, "imgsz": 640, "workers": 8, "optimizer": "yolo11-sgd"},
      "missing_images": [{"name": "0001.jpg", "split": "train", "sha256": "0f2b8c9d...", "size": 184320}],
      "dataset_id": null,
      "created_at": "2026-01-01T10:15:00Z",
      "expires_at": "2026-01-01T11:15:00Z",
      "void_reason": null
    }
  ],
  "pending_submissions": [
    {
      "client_submission_id": "sub_3f7c1a2b4d5e6f708192a3b4c5d6e7f8",
      "phase": "submitting",
      "submit_request_hash": "sha256:11aa22bb...",
      "server_url": "http://10.0.0.5:8000",
      "pending_dir": "<workdir>/xanylabeling_data/remote_training/pending/sub_3f7c1a2b",
      "request_path": "<workdir>/xanylabeling_data/remote_training/pending/sub_3f7c1a2b/submit_request.json",
      "job_id": null,
      "dataset_id": "ds_20260101_ab12cd",
      "created_at": "2026-01-01T10:20:00Z",
      "void_reason": null
    }
  ],
  "records": [
    {
      "job_id": "job_20260101_7f2a91",
      "client_job_name": "person-detect-20260101",
      "server_url": "http://10.0.0.5:8000",
      "dataset_id": "ds_20260101_ab12cd",
      "dataset_dir": "/data/annotations/person",
      "classes_file": "/data/annotations/person/classes.txt",
      "task": "detect",
      "model_family": "yolo11",
      "model": "yolo11s.pt",
      "params": {"epochs": 100, "batch": 16, "imgsz": 640, "workers": 8, "optimizer": "yolo11-sgd"},
      "status": "running",
      "is_terminal": false,
      "attempt": 1,
      "resume_cycles": 0,
      "needs_attention": false,
      "needs_attention_reason": null,
      "calibration_source": null,
      "artifact_suspect": false,
      "partial_available": false,
      "last_seq": 42,
      "last_seen_at": "2026-01-01T10:31:00Z",
      "created_at": "2026-01-01T10:20:30Z",
      "finished_at": null,
      "download_path": null,
      "notes": ""
    }
  ]
}
```

**顶层字段**：

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 契约（schema）版本，恒为 `1`；变更时按版本号补齐字段，**不删旧键**（§5.3.4） |

**`records[]`（25 个字段；`job_id` 是主键，永不从台账删除）**：

| 字段 | 说明 |
| --- | --- |
| `job_id` | 服务端任务 ID，主键；**永不从台账删除** |
| `client_job_name` | 客户端**本地自有**的展示名（用户可改），与服务端 `POST /jobs` 请求体里的同名字段对应；服务端**只落库不回传**（§3.2.2 #7），因此台账里以本地值为准 |
| `server_url` | 提交该任务时使用的服务器根地址（§5.3.3 的单服务器口径下为冗余保存） |
| `dataset_id` | 该任务使用的数据集 ID（upload 响应回填） |
| `dataset_dir` / `classes_file` | 提交时使用的本地路径（只读展示与复现用；**不参与任何写操作**） |
| `task` | `detect` / `segment` |
| `model_family` / `model` | 模型家族与权重文件名 |
| `params` | 提交时实际发出的参数对象：**只含用户显式设置过的键**（§3.8.2） |
| `status` | 最近一次轮询得到的服务端状态快照（§3.4.1）。本地另有 `orphaned`（§5.3.4），**不是**服务端状态 |
| `is_terminal` | **必须持久化**：服务端 job 对象的权威终态判据（§3.4.4），最近一次轮询时原样落盘；**缺失时回退 `finished_at != null`**（服务端只在终态写 `finished_at`）。读到缺该字段的旧条目按回退公式**就地补齐**后写回，不新增字段名、不改 `schema_version` |
| `attempt` | **本轮（当前恢复周期）**内的尝试序号（§3.4.4） |
| `resume_cycles` | **手动恢复次数**；手动恢复后 `attempt` 重置为 1、`resume_cycles` +1（§3.4.2 的恢复行），客户端据此显示「第 N 次人工恢复 · 本轮重试预算已重置（attempt/max_attempts）」 |
| `needs_attention` / `needs_attention_reason` | 需人工介入标志与原因；原因取值 **三值**：`attempts_exhausted`（**本轮**预算用尽）/ `artifact_suspect` / `resume_anomaly`（§3.4.4） |
| `calibration_source` | **客户端本地记录**的标定来源快照：提交时从 `capabilities.vram_table.entries[]` 取到的该 `(model, task)` 的 `source`（`auto` / `manual` / `default`）以及可选的 `max_batch`。**不是服务端 job 字段**（§3.4.4），只用于详情页解释「这条任务的显存估算来自本机实测还是起点值」，并在 `CONVERGED_TO_DEVICE_MAX` 提示里回显上限值 |
| `artifact_suspect` / `partial_available` | 产物可疑 / 部分结果可用的本地快照（结果页黄条与「已中止」徽标，§5.6） |
| `last_seq` | 已消费的最大事件 `seq`（`?after=` 增量拉取的游标，§3.5） |
| `last_seen_at` | 最近一次成功轮询的时间（wall clock，§3.1） |
| `created_at` / `finished_at` | 创建时间与结束时间（`finished_at` 只在终态非空） |
| `download_path` | 最近一次结果下载的落点，便于「打开产物目录」 |
| `notes` | 用户备注（可选） |

**`pending_uploads[]`（20 个字段；键 = `upload_token`）**：

| 字段 | 说明 |
| --- | --- |
| `upload_token` | plan 下发的一次性令牌（`ut_<32hex>`，§3.1），本条目的**键** |
| `phase` | 六个取值之一（见下） |
| `plan_request_hash` | **plan 阶段**请求体的锁存哈希：与 `pending_dir/manifest.json` 同源，进入 `planned` 时**一次性**写入（§5.4.1）；崩溃重放据此确认 manifest 未被改写 |
| `upload_request_hash` | **upload 阶段**请求的锁存哈希：进入 `uploading` 时写入（**必须在发起请求之前就持久化**，是"请求已发出但结果未知"时的对账凭据，§5.4）；`planned` 阶段为 `null` |
| `server_url` | 发起该请求的服务器根地址 |
| `pending_dir` | 该条目的持久目录 `pending/<id>/` |
| `archive_path` / `manifest_path` | `pending_dir` 下**可重放数据**的路径（**不随 staging 的 `finally` 清理**，§5.1.5）；`archive_path` 在进入 `uploading` 前写入 |
| `staging_dir` | 打包工作区（系统临时目录）；被本条目的 `pending_dir` 与 `staging_dir` 引用时免于 TTL 回收（§5.1.5） |
| `dataset_dir` / `classes_file` | 本次 plan 使用的本地数据集目录与类别文件路径（只读展示与复现用；**不参与任何写操作**）；**必须有**——崩溃重放要用它们按 §5.2 重新扫描 / 重新打包 |
| `task` | `detect` / `segment`（本次数据集的任务类型，重放打包时写进 zip 内 manifest 的依据） |
| `val_ratio` / `seed` | 本次划分使用的目标 val 占比与随机种子（**不含 `split_stats`**：逐图划分结果在 manifest 里，由 `pending_dir/manifest.json` 原文承载） |
| `params` | **提交参数摘要**（只含用户显式设置过的键，§3.8.2）：崩溃对账后在 UI 里还原"待提交任务"的参数面 |
| `missing_images[]` | **plan 响应里的完整缺失图片清单**（每项四项 `name` / `split` / `sha256` / `size`，§4.1.2）：**崩溃重放时重新打包的依据**，**不能只存摘要**（§5.4.1 崩溃对账第 1 行依赖它） |
| `dataset_id` | upload 成功后回填；拿到之前写 `null` |
| `created_at` / `expires_at` | 创建时间与令牌到期时间（`expires_at` 取自 plan 响应，§3.11） |
| `void_reason` | 作废原因（取值见下）；非作废条目写 `null` |

**`pending_submissions[]`（10 个字段；键 = `client_submission_id`）**：

| 字段 | 说明 |
| --- | --- |
| `client_submission_id` | 提交幂等键（建议 `sub_<32hex>`，§3.1），本条目的**键** |
| `phase` | 六个取值之一（见下） |
| `submit_request_hash` | 提交请求体的锁存哈希：**必须在发起请求之前就持久化**（§5.4） |
| `server_url` | 发起提交的服务器根地址 |
| `pending_dir` | 该条目的持久目录 `pending/<id>/` |
| `request_path` | `submit_request.json` 的路径（提交请求体快照） |
| `job_id` | 提交成功后回填；拿到之前写 `null`。拿到后**原子**关联到 `records`（§5.4） |
| `dataset_id` | 本次提交引用的数据集 ID |
| `created_at` | 创建时间 |
| `void_reason` | 作废原因（取值见下）；非作废条目写 `null` |

**枚举（逐字，唯一）**：

```text
phase（两个 pending 数组共用，六个取值）：
  planned / uploading / committed / submitting / submitted / void
  —— 不存在 committed_pending_response / submitted_pending_response 这类名字

void_reason（取值集合；每条取值对应的转移与清理时机归 §5.4）：
  token_expired / token_expired_unused / manifest_mismatch / validation_failed /
  checksum_mismatch / label_checksum_mismatch / missing_labels / invalid_label_format /
  unsupported_extension / upload_token_reused / submission_conflict / user_void
```

- **字段名逐字为准**：`attempt` / `max_attempts` 是服务端字段名（§3.11），**不存在** `attempt_max` 这种写法，客户端不得自行命名。
- `dataset_id` / `job_id` 在拿到之前写 `null`；**不得**把它们写成"已知"（那会与 `phase` 语义矛盾）。
- `server_url` / `pending_dir` / `archive_path` / `manifest_path` / `request_path` / `staging_dir` 是**可重放数据的位置**；可重放数据必须落在持久目录、**不得**放在会被清理的位置。

#### §5.3.3 `server.json` 与 `settings.json`

**`server.json`**（Token 专用）：

```json
{"server_url": "http://10.0.0.5:8000", "api_key": "<Token>", "updated_at": "2026-01-01T10:00:00Z"}
```

- 权限：`os.chmod(<path>, 0o600)`（一行，Linux 唯一实现）。
- Token 属敏感信息：**不进协议**、**不导出**（§5.2.8）；v1 单服务器口径见 §5.3.1。

**`settings.json`**（三个键，**同处一个文件、都在顶层、互不嵌套**）：

| 键 | 默认 | 语义 |
| --- | --- | --- |
| `keep_staging` | `false` | **保留**本次 staging 目录（含 zip）作调试产物，供排障 / 复现（§5.1.5） |
| `pending_max_gb` | `5` | `pending/` 目录总量上限（行为见 §5.4） |
| `pending_ttl_days` | `7` | `pending/` 保留期（行为见 §5.4） |

```json
{"keep_staging": false, "pending_max_gb": 5, "pending_ttl_days": 7}
```

- **三者语义不同类（勿混用）**：`keep_staging` 是「**保留**」开关，**不得**把它的语义混进清理规则；只有 `pending_max_gb` / `pending_ttl_days` 两个键用于**保守清理**。
- **与 staging TTL 的关系**：§5.1.5 的 staging TTL 是**固定 7 天常量**，与 `pending_ttl_days` 的**缺省值**同值但**不共用键**，也**不是**第四个键——它不受任何配置键控制。
- **读取规则（写死）**：文件不存在 / 不可解析 / 键缺失 / 类型不符（如 `keep_staging` 不是布尔）⇒ **一律按默认值**，只记一条 `WARNING` 日志，**绝不**让本次转换 / 上传失败。
- **写入**：沿用 `tasks.json` 的**原子写**习惯（`settings.json.tmp` → `flush` + `os.fsync` → `os.replace`，§5.3.4）。
- **三者都不进协议**：不发给服务端、**不参与**配置导出 / 导入（§5.2.8）、**不改** `server.json` / `tasks.json` 的既有结构。

#### §5.3.4 原子写与损坏恢复

| 规则 | 做法 |
| --- | --- |
| 原子写 | 先写 `tasks.json.tmp` → `flush` + `os.fsync` → `os.replace` 覆盖 `tasks.json` |
| 备份 | 每次成功写入前把旧文件复制为 `tasks.json.bak` |
| 损坏恢复 | 启动时解析失败 → 尝试 `tasks.json.bak` → 仍失败则把损坏文件**改名**为 `tasks.json.corrupt.<ts>`（**不删除**）并重建空台账，同时提示用户 |
| 并发 | 台账读写集中在主线程的 Store 单例；轮询线程通过信号回主线程更新 |
| 服务端已清理 | `GET /jobs/{job_id}` 返回 404 `JOB_NOT_FOUND`（§3.3）时把该条标为 `status="orphaned"`（本地视角），UI 显示「服务端已无该任务」，**保留记录** |
| 迁移 | `schema_version` 变更时按版本号做字段补齐，**不删旧字段** |

**`orphaned` 的归属写死**：它属 `records` 的本地 `status` 取值，**不是** `pending_uploads` / `pending_submissions` 的 `phase` 取值（`phase` 只有 §5.3.2 的六个）。

#### §5.3.5 复用清单

| 复用对象 | 位置（**符号锚点**） | 用途 |
| --- | --- | --- |
| `launch_model_validation` 的惰性单实例模式 | `anylabeling/custom/model_validation/launcher.py` | 新 `launch_remote_training` 的模板（§5.1.2） |
| 包入口只导出 launcher | `anylabeling/custom/model_validation/__init__.py` | 新包 `anylabeling/custom/remote_training/__init__.py` |
| `QDialog` + `QStackedWidget` 页面栈与页面切换 | `anylabeling/custom/model_validation/ui/dialog.py`（`ModelValidationDialog` / `show_config` / `show_results`） | 四页窗口骨架（§5.1.3） |
| 只读路径 + 「浏览…」三件套 / `QFileDialog` 选择 | `anylabeling/custom/model_validation/ui/config_page.py`（`_with_button`） | 数据集目录 / 类别表 / 模型路径输入（§5.1.3） |
| 只读面板 | `anylabeling/custom/model_validation/ui/progress_page.py`（`setReadOnly(True)`） | 详情页事件日志面板与结果页 `debugInfoEdit`（§5.1.5） |
| 结果表格与逐行操作 | `anylabeling/custom/model_validation/ui/results_page.py`（`ResultsPage` / `RecordItem`） | 产物清单 / 任务列表的行控件思路 |
| 本地记录（dataclass + `to_dict` / `from_dict`） | `anylabeling/custom/model_validation/records.py` | 台账记录的序列化风格（§5.3.2） |
| 数据集扫描与临时目录 | `anylabeling/custom/model_validation/dataset.py`（`collect_pairs` 的返回结构 / `sha256_file` / `create_staging_root` / 取消检查） | 配对结构、sha256、staging 与取消（§5.2.2 / §5.1.5） |
| 图片扩展名白名单 | `anylabeling/custom/model_validation/labelme_io.py`（`IMAGE_EXTENSIONS`） | 本地扫描与 zip 打包共用（§5.2.3） |
| 标注转换器 | `anylabeling/views/labeling/label_converter.py`（`LabelConverter` / `custom_to_yolo`） | `.json` → YOLO `.txt`（hbb / seg，§5.2.6） |
| 任务→转换模式映射与参数默认值 | `anylabeling/services/auto_training/ultralytics/config.py`（`TASK_LABEL_MAPPINGS` / `DEFAULT_TRAINING_CONFIG`） | 模式取值与表单初值（`optimizer` 初值 = 第 0 项、不发键；`DEFAULT_TRAINING_CONFIG` 里的 `"optimizer":"auto"` 只是本地训练默认，**不驱动远程表单初值**，§5.2.2） |
| 服务器地址 + Token 头写法 | `anylabeling/services/auto_labeling/remote_server.py`（`remote_server_settings` / `XANYLABELING_SERVER_URL` / `{"Token": api_key}`） | 与既有远程推理一致的配置与鉴权风格（§3.1） |
| 工作目录与 HTTP 客户端 | `anylabeling/config.py`（`get_work_directory()`）；上游核心依赖 `requests` | 台账落点与全部训练接口调用（§5.3.1 / §5.3.6） |

#### §5.3.6 新增目录骨架

```text
anylabeling/custom/remote_training/
  __init__.py          # 只导出 launch_remote_training（§5.1.2）
  launcher.py          # 惰性单实例 launcher（§5.1.2）
  api_client.py        # /custom/train/* 的 requests 封装（Token 头、超时、错误 → 异常映射；契约见 §3）
  store.py             # server.json / tasks.json / settings.json 的读写（原子写、损坏恢复、pending 台账、owner marker 扫描）（§5.1.5 / §5.3.1–§5.3.4）
  scanner.py           # 根目录扫描 + N1 校验矩阵 + 完整 schema 校验（§5.2.3–§5.2.5）
  converter.py         # LabelConverter 封装 + 计数与警告（逐文件 try/except，§5.2.4 / §5.2.6）
  splitter.py          # 按类别分层划分（§5.2.7 伪码的唯一实现）
  packer.py            # 图片 sha256 + 标签冻结与 sha256 + zip 打包（§5.2.6 / §5.2.9）
  uploader.py          # plan / upload 两阶段（MultipartEncoder 流式、进度、可唤醒取消）（§4.1 / §5.4）
  poller.py            # 轮询与事件增量拉取（QThread + 信号 + generation 代次）（§5.1.4 / §5.5）
  worker.py            # 扫描 / 转换 / 打包 / 上传的后台线程（关闭状态机、finally 清理）（§5.1.4 / §5.1.5）
  ui/
    __init__.py
    dialog.py          # RemoteTrainingDialog（四页，§5.1.3）
    config_page.py     # 配置页（含状态行、连接测试、划分预览、导入 / 导出，§5.1.3 / §5.2.8）
    jobs_page.py       # 任务列表页（§5.1.3）
    detail_page.py     # 任务详情页（§5.1.3）
    results_page.py    # 结果页（含 debugInfoEdit，§5.1.5）
    widgets.py         # 只读路径框 + 浏览按钮等复用控件（§5.1.3）
    close_guard.py     # ApplicationCloseGuard：应用退出路径的事件过滤器（§5.1.4）
```

**fork 自有依赖文件**：新增 `requirements/custom/remote_training_client.txt`，内容为一行：

```text
requests-toolbelt>=1.0.0
```

- 用途：upload 阶段用 `MultipartEncoder` 做**流式** multipart（大 zip 不进内存、进度可读、取消可唤醒），编排见 §5.4。
- **不改**上游 `pyproject.toml`。
- **运行时缺失必须显式报错并给出安装命令**（例如 `pip install -r requirements/custom/remote_training_client.txt`），**禁止静默回退 `files=`**——`requests` 的 `files=` 会把整个 zip 读进内存，并让「进度 + 可唤醒取消」失效。
- 唯一上游改动仍是 §5.1.1 的四处挂载点；除此之外不修改任何上游文件。


### §5.4 两阶段上传与提交

本节只写**客户端语义与流程**：路由与错误码集合见 §3.2 / §3.3；`plan` / `upload` 的**服务端**协议见 §4.1；台账**字段格式**见 §5.3。上传阶段逐 (HTTP, code) 的语义以 §5.4.4 的**唯一失败转移表**为准。

#### §5.4.1 pending 台账与崩溃对账

**phase 枚举（唯一，6 个取值）**：`pending_uploads` / `pending_submissions` 里的条目，`phase` **只取下列六个**；`orphaned` 只出现在 `records`，**不是 phase**、也不使用 `void_reason`。

| phase | 属于 | 语义（一句话） |
| --- | --- | --- |
| `planned` | `pending_uploads` | **plan 成功响应已落盘**（含 token）、**尚未**发出 upload。**不变式**：条目里 `upload_token` 与 `expires_at` 必定同时存在 |
| `uploading` | `pending_uploads` | upload 请求**已发出、结果未知**（`archive.zip` 与 manifest 均已持久化，§5.4.2） |
| `committed` | `pending_uploads` | upload **已拿到 `dataset_id`**（结果已知），**只需提交任务**——**不再重放 upload** |
| `submitting` | `pending_submissions` | `POST /jobs` **已发出、结果未知** |
| `submitted` | `pending_submissions` → `records` | 已拿到 `job_id`，随即原子移入 `records`（瞬时态，落账后即消失） |
| `void` | 两者 | **已作废**（token 过期 / 内容不一致 / 服务端返回确定的失败响应 / 用户显式作废）；条目保留到下一次对账扫描时清除，**清除前不参与任何重放**。**用户取消上传不产生 `void`**（§5.4.4「取消的三种情形」） |

**三条不能丢的结论**：

1. **先落盘、后发请求**：**没有落盘的 token，就没有 `planned` 条目**；plan 自身的失败（400 / 413 / 503）与「结果未知」**都不写台账**（plan 是只读预检、不创建任何实体，重发一次没有副作用）。
2. 三个哈希字段 `plan_request_hash` / `upload_request_hash` / `submit_request_hash` **各自只描述自己阶段的请求体**；进入下一阶段是**新增**下一个字段而**不是**覆盖上一个，三者**写入后不再被改写**。
3. **唯一合法的跨阶段关系**是 `plan_request_hash == upload_request_hash`（两者描述的是同一份 manifest 原文的字节）；**禁止**用 A 阶段的哈希字段去校验 B 阶段的请求体，也**禁止**把三者当作协议字段发给服务端（它们只存在本地台账）。

**规范化字节函数 `canonical_json_bytes`（跨仓同源，逐字保留）**——**本处是全文唯一定义**（§3.3 的 `MANIFEST_MISMATCH` 与 §4.1.4 的清单指纹都引用它，客户端篇 §5.4.1 即本节）：

~~~python
json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
~~~

键序固定（`sort_keys=True`）、UTF-8、**不写 BOM**（禁止 `utf-8-sig` / `codecs.BOM_UTF8`）；`separators=(",", ":")` + `ensure_ascii=False` 是「同一规范化函数」逐字节可比所需的确定性写法；`allow_nan=False` 遇 `NaN` / `Infinity` / `-Infinity` **直接抛错**，绝不写出非标准 JSON。**落盘副本一律写这份规范化字节**：`pending_dir/manifest.json`、zip 内 `manifest.json`、`pending_dir/submit_request.json`。

**状态转移表（唯一口径；`→` 为正常推进，`⇢` 为异常分支）**：

| 起点 | 事件 | 终点 | 落盘内容（与 `records` 同一次原子写，§5.3.4） |
| --- | --- | --- | --- |
| （无） | **开始 plan**（请求发出之前） | **不落任何 pending 条目** | 只在内存里保留本次请求参数与 `plan_request_hash`；**崩溃时不留半成品**（服务端可能已建 `tmp/uploads/<token>/`，但它随 TTL 自行清理；没有 token 就无需也无法重放，直接重新 plan 即可） |
| （无） | **plan 成功响应落盘**（二次 plan 场景下须等**用户确认并完成二次 plan**，§5.4.3） | `planned` | **一次性**写入 `plan_request_hash` + `dataset_dir` / `classes_file` / `task` / `val_ratio` / `seed` / `params` 摘要 + `upload_token` / `expires_at` + **完整的 `missing_images[]`**（选图与崩溃重放都靠它，**不能只存摘要**）+ `server_url` / `pending_dir` / `staging_dir`；同时把**最终** plan 请求体原文写入 `pending_dir/manifest.json` 并 `fsync`（**与进入 `planned` 同一次动作**，此时 `pending_dir/` 本身也随之创建） |
| `planned` | **upload 请求发出之前** | `uploading` | `upload_request_hash`（= zip 内 `manifest.json` 规范化字节的 sha256）+ `archive_path` / `manifest_path`（**先落盘、后发请求**） |
| `uploading` | upload 响应到达 | `committed` | 回填 `dataset_id`；**此时才允许**进入提交任务步骤 |
| `uploading` | upload 返回**确定的错误响应** | 按 §5.4.4 **唯一失败转移表**逐行判定 | 判为 `void` 者记该行的 `void_reason`；判为保持 `uploading` 者保留 `archive.zip` 与 `pending_dir` |
| `planned` | 未发出 upload 且已过 `expires_at` | `void` | `void_reason=token_expired_unused`（信息级，不打扰用户） |
| `committed` | 用户点「提交任务」 | `submitting` | `{client_submission_id, submit_request_hash, dataset_id, request_path, server_url, pending_dir}`（**先落盘、后发请求**，§5.4.5） |
| `submitting` | `POST /jobs` 响应到达 | `submitted` → `records` | 回填 `job_id`，**原子**完成三件事：① 该条移入 `records`、② 删除对应 `pending_uploads` 条目、③ 把 `pending_dir` 标为可回收（= §5.4.2「清理时机（唯一）」①） |
| `submitting` | 409 `VALIDATION_FAILED`（同 id 异 body） | `void` | `void_reason=submission_conflict`，要求用户确认后重新提交 |
| `uploading` | **用户取消**（三种情形，§5.4.4） | **`uploading`（不变）** | `archive_path` / `manifest_path` / `pending_dir` **全部保留**：取消只保证「不再等这次响应」，**不能**保证服务端没 commit |
| 任意 pending | 用户显式「作废这一条」 | `void` | `void_reason=user_void`（**唯一**能主动回收仍可用条目的路径） |
| `void` | 下一次对账扫描 | 条目被删除 | 同时回收其 `pending_dir`（§5.4.2 清理时机 ②）；**自动转 `void` 的各条路径**（`token_expired_unused` / `submission_conflict` / §5.4.4 表中判为 `void` 的各行）由这一次扫描回收，**不挑来源** |

**崩溃对账（启动时 + 每次进入任务页时各执行一次；每个 phase 的恢复动作写死）**：

| # | phase | 恢复动作 | 失败 / 异常分支 |
| --- | --- | --- | --- |
| 1 | `planned` | **继续本次上传**：`now < expires_at` ⇒ 用 `pending_dir/manifest.json`（plan 请求体原文）与持久化的 `missing_images[]` 打包，再 upload（**不重发 plan**）；否则 ⇢ `void`（`token_expired_unused`，信息级，不打扰用户） | — |
| 2 | `uploading` | **重放**：同一 `upload_token` + **同一份 `archive_path`**（zip 与 manifest 都还在），按服务端判定表 ⓪–⑤ 分支 | ⓪ 命中 + 未过期 + manifest 与 plan 副本在**同一规范化函数下字节一致** ⇒ 走正常路径；③ 已提交表命中且在保留期内 ⇒ **同 body 幂等拿回原 `dataset_id`**；异 body ⇒ 409 `VALIDATION_FAILED` ⇢ `void`；④ / ② 超期 ⇒ 400 `TOKEN_EXPIRED` ⇢ `void` + 提示重新预检；① 两张表都没有（含已被清理）⇒ 400 `UNKNOWN_UPLOAD_TOKEN` ⇢ `void`；⑤ 未 commit 的旧 token + 新 manifest ⇒ 400 `MANIFEST_MISMATCH` ⇢ `void`；429 先做一次过期判定（§5.4.4 例外 ③） |
| 3 | `committed` | **不重放 upload**：已经拿到 `dataset_id`，**只需提交任务** ⇒ 用同一 `dataset_id` 与持久化的提交快照进入 `submitting` 流程 | `dataset_id` 已被服务端清理 ⇒ 提交时 404 `DATASET_NOT_FOUND` ⇢ `void` + 提示重新上传。**这里不含** 409 `JOB_ARTIFACTS_EXPIRED`——该码只属于**已有任务的 resume**，`POST /jobs` 从不返回它 |
| 4 | `submitting` | **重放**同一 `client_submission_id` + **同一请求体快照**（`request_path`） | 同 id 同 body ⇒ 拿回**原 `job_id`**（正常路径）⇒ 按上表原子落账；409（同 id 异 body）⇢ `void`（`submission_conflict`）+ 要求用户确认 |
| 5 | `submitted` | 只应瞬时存在；若崩溃后仍看到该 phase ⇒ 按落账中断处理：重放一次同 id 提交拿回 `job_id`，再原子移入 `records` | — |
| 6 | `void` | 清除该条目并回收其 `pending_dir`（清理时机 ②，自动转 `void` 的条目走同一条）；**不重放、不提示** | — |

对账完成后，pending 里只应剩下**确实未完成**（`planned` / `uploading` / `committed` / `submitting`）的条目；UI 在任务列表顶部提示「有 N 个未完成的上传/提交，正在自动对账」。**所有对账重放都不得产生新实体**（CT11 / CT14 / CT20）。`server.json` 的 Token **不**写入 pending 台账。

#### §5.4.2 `pending/<id>/` 持久化与清理

| 项 | 规则（写死） |
| --- | --- |
| 落点 | `<get_work_directory()>/xanylabeling_data/remote_training/pending/<id>/`（`id` = `ut_...` / `sub_...` 的短哈希 + 前 8 位，目录名安全）。**不在系统临时目录**，因此不受 `xal_remote_training_*` 前缀扫描与 TTL 回收影响（§5.1.5） |
| 内容（可重放数据四文件） | ① `archive.zip`（**原样字节**，重放必须逐字节一致）；② `manifest.json`（**最终 plan 请求体原文**，与 zip 内那一份**逐字节相等**）；③ `submit_request.json`（提交请求体快照）；④ `meta.json`：`{"server_url", "upload_token", "client_submission_id", "phase", "created_at", "expires_at", "staging_dir"}`——**原始 `server_url` 必须持久化**（将来支持多服务器、或用户在重放前改了配置，都不能把重放发到错误的服务器） |
| 哈希字段与落盘文件的对应 | `plan_request_hash` ↔ `manifest.json`；`upload_request_hash` ↔ `archive.zip` 内的 `manifest.json`；`submit_request_hash` ↔ `submit_request.json`。**唯一合法的跨阶段关系**：`plan_request_hash == upload_request_hash` |
| 写入顺序 | **先落盘、后发请求**（三处时机）：① `pending/<id>/` 目录本身与其中的 `manifest.json` 在**进入 `planned` 的同一次动作里**创建 / 写好并 `fsync`；② `archive.zip` 在进入 `uploading` **之前**写好并 `fsync`；③ `submit_request.json` 在进入 `submitting` **之前**写好并 `fsync`。zip 用「先写 `.tmp` 再 `os.replace`」，不留半截文件 |
| 目录创建时机 | **没有 `pending_dir/` 就没有 `planned` 条目**。因此恢复动作 1 与恢复动作 4 在**任意崩溃点**都有目录可用 |
| 清理时机（唯一） | **穷尽且互斥的三条路径**（日志 `reason` 分别记 `submitted` / `void` / `user_void`）：① 条目推进到 `submitted`（**提前回收**）；② 对账扫描**删除**该条目时（`void` 的**任何来源**、或已落账但回收失败 / 崩溃遗留的重试，由同一次扫描清除并回收，属**兜底回收**）；③ 用户显式「作废」时**立即**回收。三者按当前 phase 互斥，任何目录**至少在 ② 被覆盖一次**；**其余任何路径都不得删除它**（含取消、结果未知、对账未完成、TTL 到期） |
| 与 staging 的关系 | `staging_dir` 是**打包工作区**（`tempfile.mkdtemp(prefix="xal_remote_training_")`，**可再生**——丢失只需在重放前重新转换 / 打包，图片按 sha256 命中缓存），与 `pending_dir` **成对落盘**（`meta.json` 同值）；`pending_dir` 是**不可变的重放数据**，**重放只允许复用 `pending_dir/archive.zip`**。仍被 pending 条目引用的 staging **免于 TTL 回收**；条目缺 `staging_dir` ⇒ 按「**无 staging 引用**」处理，且**不得**因此回收其 `pending_dir` |
| `pending_max_gb` / `pending_ttl_days`（默认 **5** / **7**） | **唯一作用对象是「台账无任何引用的孤儿目录」**（`pending/<id>/` 的 id **既不在 `pending_uploads`、也不在 `records`**）：孤儿目录超期或 `pending/` 总量超 `pending_max_gb` 时按**最旧优先**回收并记日志；**回收前必须先扫 `tasks.json` 确认无引用**，仍被引用则**跳过并记 WARN**。**这两个键不是第四条回收路径**：**有台账引用的 `pending_dir` 绝不受它们影响**，只走「清理时机（唯一）」的三条路径（否则会在用户完成对账前毁掉重放数据）；容量 / 超期压力下最多提示用户去任务页对账。两键都**不进协议、不发给服务端**，纯本地策略 |
| 日志 | 每次回收写一条「回收 pending 目录 `<id>`（原因：`submitted` / `void` / `user_void`），释放 X MB」 |


#### §5.4.3 plan 与二次 plan

**请求体（客户端构造；字段含义、校验与响应字段见 §4.1.2）**：

```json
{
  "schema_version": 1,
  "task": "detect",
  "val_ratio": 0.2,
  "seed": 20260101,
  "split_strategy": "per_class",
  "split_stats": {"person": {"train": 960, "val": 240}},
  "classes": ["person", "car"],
  "images": [
    {"name": "road_0001.jpg", "sha256": "<64hex>", "size": 184320,
     "split": "train", "label_sha256": "<64hex>", "label_size": 96}
  ]
}
```

- `<64hex>` 是占位符，实际值恒为 64 位小写十六进制；`images[]` 只有六项、**无类别字段** ⇒ 「整类被条目级剔除」**不可检测**（服务端已如实降级为此口径）。
- `split_strategy`（缺省 `per_class`）与 `split_stats`（每类 train/val **实际**计数）**总是上报**，便于服务端与运维确认类别覆盖；服务端**不重算**划分，只做结构 + 计数自洽性校验并把结果记进 `warnings[]`。
- 拿到响应后展示**冻结文案**的预检摘要（逐字）：

```text
共 N 张，命中缓存 M 张（省 Y GB），需上传 K 张 / B MB
```

**三个占位符的唯一取法（此处是唯一口径）**：`K = len(missing_images)`（需上传张数）、`B = upload_bytes`、`Y` = **客户端本地对命中图片（即 `missing_images[]` 之外的图片）的 `size` 求和**（「省 Y GB」由客户端本地计算，v1 **不要求服务端新增字段**）。`N = total_images`、`M = blob_hits`。

| 响应字段 | 客户端用法 |
| --- | --- |
| `total_images` / `blob_hits` / `upload_bytes` | 填充冻结文案（口径见上） |
| `missing_images[]` | **决定 zip 里放哪些图片**（打包发生在 plan **之后**）：为空时 zip 内**完全不含 `images/`**，但仍**必须**发起 upload（标签永不缓存，服务端正是靠这次 upload 落盘标签并生成 `dataset_id`）。仅 `total_images == 0` 时既不打包也不上传（空数据集在客户端预检已被阻断） |
| `rejected[]` | **条目级**拒绝（`UNSUPPORTED_EXTENSION` / `INVALID_SHA256` / `INVALID_SPLIT` / `NAME_EMPTY`）：非空时弹警告对话框列出「文件名 + 原因」，用户确认后走**二次 plan**；重名 / 同 stem 冲突是**整包 400**，不会出现在本列表 |
| `upload_token` / `expires_at` | 保存 token；倒计时展示（剩余 < 5 分钟提示「上传凭证即将过期」） |

**失败级别（按阶段限定）**：

| 级别 | plan 阶段 | upload 阶段 |
| --- | --- | --- |
| 条目级 | 不支持的扩展名、条目元数据非法（`INVALID_SHA256` / `INVALID_SPLIT` / `NAME_EMPTY`）⇒ 进 `rejected[]`，不阻断整包 | `labels/` 下未被 manifest 引用的**孤儿标签** ⇒ 忽略并计入 `warnings[]`（`ORPHAN_LABELS`）——**孤儿标签是 upload 阶段的条目级告警，不是 plan 阶段的原因** |
| 整包级 | 阻断整包、不产生 `dataset_id`（如「全部条目都被拒」⇒ 400 `VALIDATION_FAILED`） | 同一类问题（扩展名 / `split` 非法 / 条目元数据非法）只要出现在 zip / manifest 中即整包 400；另有未知 / 过期 token、manifest 与 zip 内容不一致、同 split 重名或同 stem 冲突、图片缺失或 sha256 校验失败、**标签 sha256 校验失败**、缺标签文件、路径安全与白名单失败、解压超限、配额超限 ⇒ 按 §5.4.4 的表置 `void` 或保持 `uploading` |

**二次 plan（`rejected[]` 非空时的唯一路径；服务端零改动）**：

| 步 | 内容 |
| --- | --- |
| 1 剔除 | 从本地图片集合移除被拒条目（`rejected[].name`），重算 `split_stats` 与总计数；被剔除条目的 `label_sha256` / `label_size` 随 manifest 一并剔除，**不得**留在新 manifest 里 |
| 2 本地复检 | ① 划分两侧非空（val / train 都不能为空）；② 同 split 内重名；③ 同 split 内同 stem（不同扩展名）。**任一不通过即停在这里**（红色状态行说明原因，**不发起任何网络请求**） |
| 3 二次 plan | 用复检后的集合重新 `POST /datasets/plan`，得到**新 `upload_token`**、新 `missing_images[]`、新 `total_images` / `blob_hits` / `upload_bytes`，并**用本次响应重建冻结文案**；**第 1 次 plan 的这四个数值一律作废**（不得沿用、不得与之混算） |
| 4 打包 | zip 内 `manifest.json` = **本次（第 2 次）plan 的请求体原文**（不再本地重建不一致的副本）；`images/` 只放新的 `missing_images[]`；`labels/` 仍全量（沿用已冻结的标签字节，**不重新转换、不重算哈希**） |
| 5 upload | 用**新 token** 上传：服务端以 plan 副本为权威、按**同一规范化函数下的字节一致**做自校验，因此必然一致 |
| 6 旧 token | **客户端立刻丢弃**（不保存、不重放）。服务端**没有** token 撤销 / supersede 契约：未 commit 且未过期时用旧 token 上传**新 manifest** ⇒ 400 `MANIFEST_MISMATCH`（判定表 ⑤，**不是** 409、**不是** `TOKEN_EXPIRED`）；已 commit ⇒ 同 body 幂等 / 异 body 409；确实超期 ⇒ 400 `TOKEN_EXPIRED`（判定表 ② / ④）。四种情形**都不产生第二个 `dataset_id`** |

**四条边界**：① 被拒的是 val 侧**唯一**图片 ⇒ 步骤 2 的「两侧非空」不通过 ⇒ 阻断（不 upload）；② **全部条目都被拒** ⇒ **不进入本路径**：首次 plan 即 400 `VALIDATION_FAILED`，客户端立即失败（红色状态行 + 本地阻断清单）、**不 upload、不落任何 pending 条目**，且**不得假设 400 携带 `rejected[]`**（只展示 `details` 中**实际存在**的字段；逐项列表需服务端先在 `ErrorResponse.details` 定义结构，§7.2）；③ 剔除后产生同 stem 冲突 ⇒ 步骤 2 阻断并列出冲突的 stem 与全部文件名；④ 旧 token + 被剔除后的新 manifest ⇒ **400 `MANIFEST_MISMATCH`**（判定表 ⑤，**不是** 409 / `TOKEN_EXPIRED`）；已 commit ⇒ 同 body 幂等 / 异 body 409（③）；确实超期 ⇒ 400 `TOKEN_EXPIRED`（② / ④）。四种情形都**不产生第二个 `dataset_id`**。

**状态提示**：「二次 plan」对用户只表现为一次额外的预检（只有元数据，通常 < 1 s），摘要区显示「已剔除 N 个被拒条目，重新预检中…」。

#### §5.4.4 upload

| 项 | 做法 |
| --- | --- |
| 请求 | `multipart/form-data`，两个部件：`upload_token`（文本）+ `archive`（zip 文件） |
| 编码方式（**必须**） | `requests-toolbelt` 的 **`MultipartEncoder`** + `MultipartEncoderMonitor`（回调里读 `monitor.bytes_read`）。**禁止** `requests` 的 `files=` 写法——它会**先把整个 multipart body 编码进内存**，包装对象的回调在**预编码**阶段就走完，既不代表网络发送进度、也保不住内存上界 |
| 内存上界 | 客户端持有 = **一个读取块（8 KiB 级）+ socket 缓冲**，即**常数级（< 1 MB）**，与 zip 大小无关（`MultipartEncoder.read(size)` 每次只从文件读出 `size` 字节，HTTP 栈按 socket 可写量拉取 = 背压） |
| 进度 | `monitor.bytes_read`（已发送字节）/ `encoder.len`；`Content-Length` 取自 **`encoder.len`**（不是本地文件大小统计），进度即**网络实际发送字节**；显示「已上传 X / Y MB（Z%）· 速度 · 预计剩余」。上传在 `QThread` worker 中执行，进度 / 完成 / 失败经 Qt 信号回主线程 |
| 依赖缺失 | 运行时检测不到 `requests_toolbelt` ⇒ **显式报错并给出安装命令**（`pip install -r requirements/custom/remote_training_client.txt`），**不得**静默回退到 `files=` 的整包编码路径 |
| 取消（**可唤醒**） | 置取消标志 → **在 `MultipartEncoderMonitor` 回调里抛出取消异常**（每发送一块就被调用，因此**发送中**的取消在一个块的时间内生效，不必等 600 s read timeout）→ 捕获异常后**关闭底层连接**（`response.close()` / 丢弃该 `Session`）。`Session.close()` **不是**可靠的中断原语（不打断正在阻塞的 socket 写），**不得**只靠它 |
| 超时 | `connect=10s`、`read=600s`（大文件）；不设置过短的总超时。read timeout 只兜「服务端不响应」，**不**兜「网络很慢」——慢网下的可中断性由上面的 monitor 取消保证 |

**取消的三种情形（三种的台账动作完全相同）**：

| # | 情形 | 实现 | 台账动作 |
| --- | --- | --- | --- |
| ① | 取消发生在**发送中** | monitor 回调仍在产生 ⇒ 抛取消异常 + 关闭连接（body 只发了一部分，服务端按处理顺序第 2 步失败，**不可能 commit**） | 条目**留在 `uploading`**（**不**置 `void`）、`archive.zip` / `manifest.json` / `pending_dir` **全部保留** |
| ② | 取消发生在 **body 已发完、正在等服务端响应** | monitor **不再产生任何回调**（`bytes_read == encoder.len`）⇒ 该窗口改用**可中断的读**（`stream=True` + 分块 `iter_content`，在每块之间检查取消标志；命中即 `response.close()` + 丢弃 `Session`），不等 `read=600s` | 同上 |
| ③ | 取消与**服务端 commit 竞态** | 服务端已走完落盘（`dataset_id` 已写、已提交记录已落）而响应在网络上被丢弃，客户端已放弃等待 | 同上：客户端只能保证「不再等这次响应」，**不能**保证服务端没 commit |

三种情形都：下次对账用**同一 token + 同一 `archive.zip`** 重放——命中判定表 ⓪ ⇒ 本次正常上传并拿回 `dataset_id`；命中 ③ 的**同 body** 分支 ⇒ **幂等拿回原 `dataset_id`**（**不产生第二个 dataset**）。拿回后条目推进到 `committed`、自动进入提交步骤，但 `POST /jobs` 仍**必须由用户点「提交任务」触发**；用户若不想提交，走「显式作废这一条」（`user_void`）。UI 文案：「已停止本次上传；服务端可能已收到，客户端会在对账时用同一凭证核对，**不会重复创建数据集**」。

**重试（唯一规则：不自动重试创建型请求；三个例外，且只有这三个）**：

| 例外 | 码 | 动作 |
| --- | --- | --- |
| ① | 409 `UPLOAD_IN_PROGRESS` | **5 s 后自动重试一次**（复用同一 token、同一 body）；再失败转手动 |
| ② | 400 `TOKEN_EXPIRED` / `UNKNOWN_UPLOAD_TOKEN` | **强制动作、不是「重放一次」**：原样重放同一 token **不可能成功** ⇒ 把对应 pending 条目置 `void`（`void_reason=token_expired`）+ **重新 plan、用新 token + 新 manifest 上传**（图片按 sha256 命中缓存、不重传） |
| ③ | 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED` | **必须先做一次过期判定再决定**：`wait` = 响应头 `Retry-After`（缺省取 `details.retry_after_seconds`）、`expires_at` = 该条目在 **plan 阶段记录的** token 到期时间。**`now + wait < expires_at`** ⇒ 到点后**原样重试同一 token、同一 body**（不作废条目、不重新 plan、不换 token）；**`now + wait >= expires_at`（含本地无 `expires_at`）** ⇒ 置 `void`（`void_reason=token_expired`）+ **重新 plan**。等待不阻塞 UI（定时器驱动）；跨退出 / 重启由对账接管 |

其余情况**一律不自动重试**——尤其 **409 `VALIDATION_FAILED`**（判定表 ③ 的异 body 分支）与 **400 `MANIFEST_MISMATCH`**（判定表 ⑤）都**不是**「重放就能好」的情形。**401 / 500 不改 phase、不自动重试**：401 由用户更新 Token 后用**同一 token + 同一 body** 手动重试；500 走通用错误路径并把条目留在原 phase 交由对账。

**唯一失败转移表（上传阶段 (HTTP, code) → phase 的唯一口径）**：

| HTTP / code | 出现阶段 | 条目 phase（转移后） | `archive.zip` / `pending_dir` | 重试语义 | 是否产生 dataset / job |
| --- | --- | --- | --- | --- | --- |
| 200，`rejected[]` **非空** | plan | **不落 `planned`**（首次 plan 的响应连同旧 token 一并丢弃） | 尚未创建 | 用户确认 → 剔除 → 本地复检 → **二次 plan**；拿到最终响应才落 `planned` | 否 |
| 400 `VALIDATION_FAILED`（`details.field` = `split` / `split_strategy` / `classes`，或 `details.files[]` 列出同 split 同 stem 冲突；**含「全部条目都被拒」**） | plan / upload | **plan：不落条目**；**upload：`uploading` → `void`**（`void_reason=validation_failed`） | plan：不适用；upload：回收 | 回配置页复查（两侧非空 / `classes` / 同 stem 冲突）后**重新扫描 → 重新 plan**；「全部被拒」时服务端在首次 plan 就 400，该响应**不含 `rejected[]`** | 否 |
| 400 `CHECKSUM_MISMATCH` | upload | `uploading` → `void`（`checksum_mismatch`） | 回收 | 本地图片在上传前被改动 ⇒ 重新扫描 → 重新转换 → 重新 plan | 否 |
| 400 `LABEL_CHECKSUM_MISMATCH` | upload | `uploading` → `void`（`label_checksum_mismatch`） | 回收 | 标签字节在冻结后被改写 ⇒ 重新转换 → 重新 plan；逐条展示 `details.files[]`（`name` / `label` / `declared` / `actual`） | 否 |
| 400 `MISSING_LABELS` | upload | `uploading` → `void`（`missing_labels`） | 回收 | 回到配置页复查配对后重新 plan | 否 |
| 400 `INVALID_LABEL_FORMAT` | upload | `uploading` → `void`（`invalid_label_format`） | 回收 | 用本工具重新转换后重新 plan | 否 |
| 400 `UNSUPPORTED_EXTENSION` | upload（zip 内） | `uploading` → `void`（`unsupported_extension`） | 回收 | 剔除非白名单文件后重新 plan（**同一问题在 plan 阶段只是条目级 `rejected[]`**） | 否 |
| 400 `MANIFEST_MISMATCH`（判定表 ⑤：未 commit 的旧 token + 新 manifest；或图片集合 / 多余条目不符） | upload | `uploading` → `void`（`manifest_mismatch`） | 回收 | **不得原样重放**：重新预检（二次 plan）后用**新 token + 新 manifest** 上传 | 否 |
| 400 `TOKEN_EXPIRED`（判定表 ② 在用表超期 / ④ 保留期届满）；400 `UNKNOWN_UPLOAD_TOKEN`（判定表 ①） | upload | `uploading` → `void`（`token_expired`） | 回收 | **不得原样重放**：重新 plan、换新 token；图片按 sha256 命中缓存、不重传 | 否 |
| 409 `VALIDATION_FAILED`（`details.field=upload_token`，已 commit + 异 body，判定表 ③ 异 body 分支） | upload | `uploading` → `void`（`upload_token_reused`） | 回收 | 该 token 已提交过**别的**内容：重新 plan；原 dataset 仍可按该 token 幂等取回 | 否（**绝不**产生第二个 dataset） |
| 409 `UPLOAD_IN_PROGRESS` | upload | **保持 `uploading`** | **保留** | 例外 ①：5 s 后自动重试一次（同 token、同 body）；再失败转手动 | 否 |
| 413 `QUOTA_EXCEEDED` | plan / upload | **plan：不落条目**；**upload：保持 `uploading`**（**不作废**） | **保留**（配额是服务端外部状态，与本次上传内容无关） | **不自动重试**：**优先**提示「服务端已触发自动回收，请稍后重试」；**仅当回收无法解除**才提示联系管理员手工清理（§4.1.7、§5.6）；用户点「重试」⇒ **复用同一 token + 同一 `archive.zip`**；若此时已过 `expires_at`，服务端按判定表 ②/④ 回 400 `TOKEN_EXPIRED` ⇒ 按上一行处理 | 否 |
| 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED` | upload | **先过期判定、再分流**：① `now + wait < expires_at` ⇒ **保持 `uploading`**（不作废）；② `now + wait >= expires_at`（含本地无 `expires_at`）⇒ `uploading` → `void`（`void_reason=token_expired`） | ① **保留**；② 按清理时机 ② 回收 | 例外 ③（口径见上）：① 到点后**自动重试同一 token、同一 body**（不重新 plan、不换 token）；② 置 `void` + **重新 plan**（新 token + 新 manifest 副本，图片按 sha256 命中缓存） | 否（① 服务端在**任何副作用之前**返回；② 走新 plan 后用的是新 token） |
| 401 `UNAUTHORIZED` | plan / upload | **不改 phase**（`planned` / `uploading` 原样保留） | **保留** | 用户在配置页更新 Token 后，用**同一 token + 同一 body** 手动重试（**该失败不消耗 `upload_token`**） | 否 |
| 500 `INTERNAL_ERROR` | plan / upload | **不改 phase**（`planned` / `uploading` 原样保留） | **保留** | 通用错误路径：记录日志与 `details.error_id`、提示稍后**手动**重试（**`POST` 不自动重试**）。服务端未就「是否已产生副作用」给出承诺 ⇒ 按「**结果未知**」保留条目、交由对账重放：对账时若已完成 commit ⇒ 判定表 ③ 同 body（幂等拿回**原 `dataset_id`**）；若未 commit ⇒ 判定表 ⓪ | 否（③ 命中时拿回的是原 dataset） |
| 503 `TRAINING_DISABLED` | **只出现在 plan**（**upload 阶段不出现**） | **不落条目** | 不适用 | 管理员启用训练子系统后重新 plan。**依据**：`enabled=false` ⇒ 503 定义在**阶段 1 的同步校验**里，而 upload 的前置条件就是**持有 plan 发放的 `upload_token`** ⇒ `enabled=false` 时 plan 已经 503、客户端永远拿不到 token，upload 阶段在协议上不可能出现 503。若实际仍收到「**无 code 的 503**」（网关 / 反向代理 / 未预期故障），按「其它 5xx」通用兜底：**不改 phase、保留 `archive.zip` 与 `pending_dir`**、按上一行同口径交由对账重放 | 否 |
| （无响应）连接失败 / read 超时 / 用户取消 / 进程被杀 / 响应丢失 | 任意 | **不改 phase**（`planned` 保持 `planned`、`uploading` 保持 `uploading`） | **保留** | 对账：同一 token + 同一 `archive.zip` 重放（判定表 ⓪ → 正常上传；③ 同 body → 幂等拿回**原 `dataset_id`**） | 否（③ 命中时拿回的是原 dataset） |

**本表的行数口径（写死）**：共 **17 行数据** = **16 行带 (HTTP, code)** + **1 行「（无响应）」（无 code，末行）**。逐 (HTTP, code) 分解：`200` × 1 行、`400` × 8 行（承载 **9 个 code**）、`409` × 2 行、`413` × 1 行、`429` × 1 行、`401` × 1 行、`500` × 1 行、`503` × 1 行（只在 plan）。**先分流、再查表**：收到确定的错误响应（有 HTTP 状态码与 `error.code`）才按本表决定 phase 与保留语义；「请求已发出、结果未知」一律不改 phase。

**`void_reason` 的取值集合（唯一，12 个）**：`token_expired` / `token_expired_unused` / `manifest_mismatch` / `validation_failed` / `checksum_mismatch` / `label_checksum_mismatch` / `missing_labels` / `invalid_label_format` / `unsupported_extension` / `upload_token_reused` / `submission_conflict` / `user_void`。`void` 与 `orphaned` 是**两种不同的结局**：`orphaned` 不属于 `void`、也不使用 `void_reason`（只表示服务端已无该任务）。`void` 的落盘含义：记 `void_reason` 后条目**保留到下一次对账扫描时清除**，**清除前不参与任何重放**，其 `pending_dir` 按清理时机 ② 回收（**不挑 `void` 的来源**）。

**关键口径（两类失败不得混为一谈）**：① **收到确定的错误响应**（上表判为 `void` 的那些行）⇒ 才作废条目并**重新 plan + upload**（图片按 sha256 命中缓存、不重传）；② 「**请求已发出但结果未知**」（连接失败 / read 超时 / 用户取消 / 进程被杀，含与服务端 commit 竞态）⇒ **一律保留条目与 `archive.zip`**，用同一 token + 同一 manifest/zip 重放（⓪ 或 ③，拿回原 `dataset_id`），**绝不重新 plan**——那会创建第二个 dataset；③ 413 / 409 `UPLOAD_IN_PROGRESS` 下服务端**没有**消耗 token（token 仍在用表），因此同一 token + 同一 manifest 仍合法，作废只会白丢一个可用 token；429 的原样重试**只在 `Retry-After` 到点后原 token 仍在有效期内**时合法。

#### §5.4.5 提交任务

**提交前的 capabilities 校验（本地能判定的错误不发请求；服务端仍会复核并返回 422 族）**：

| 判据 | 动作 |
| --- | --- |
| 任务在 `tasks`、家族 `available`、preset 在**所选家族的** `model_families[].presets` 内 | 不满足 ⇒ 置灰 / 阻止提交 |
| **`weights_ready[file] == true` 或 `allow_weight_download == true`** | 两者都为假才把该权重置灰并阻止提交；仅 `weights_ready=false` 而 `allow_weight_download=true` ⇒ **允许提交**，提交前提示一次（不阻断）：「首次训练将自动下载并缓存权重到服务端 `weights/`，请确认服务端可联网；下载期间任务停留在 `preparing`」 |
| 参数在 `param_schema`（23 项，§3.8.2）内且落在范围内 | 超范围 ⇒ 本地拦截并高亮对应输入框 |
| `allow_auto_batch` / `param_schema.batch.type` | `allow_auto_batch=true` 时 batch 额外提供两个 auto 取值——`-1`（按 **60% 显存**自动选）与比例值（`0 < r < 1`）；否则只接受 `1..128` 的整数 |
| 设备上限 `vram_table.entries[].max_batch` | 输入框上方展示「本机实测上限 N」；用户填的整数超过它时**不本地拦截**（服务端自动收敛并在 `warnings[]` 返回 `CONVERGED_TO_DEVICE_MAX`），提交后提示「**已按该卡上限调整为 N**」 |
| 显存基线来源 `vram_table.entries[].source` | `auto` = 本机已实测（带 `calibration_at`）；`manual` / `default` = 仍是起点值（`max_batch` 可能为 `null`）。两种状态在配置页与详情页**如实区分展示**，不把起点值说成实测值 |
| OOM 预期 `oom_retry.enabled` | 为真时训练遇 CUDA OOM **不会**直接判失败，而由训练侧自行降 batch 重试；客户端遇到 `log` 事件 `code=OOM_BATCH_DOWNGRADE` 时提示「检测到显存不足，已自动降低 batch 重试（X → Y）」，**不**当错误弹窗 |

**参数表单分组（除日志与产物记录类外全部可配）**：**常用参数** / **数据增强参数** / **学习率与优化器** / **训练控制**，分组与控件**一律由 `param_schema` 驱动**；**日志与产物记录类不暴露**（当前 23 项里对应 `save_period`，将来加入 `verbose` / `plots` 等纯记录参数同样不暴露）。`params.optimizer` 的选项来自**按所选家族过滤后的** preset 列表（含 `auto`）；**默认停在第 0 项** ⇒ 不发 `optimizer`，由服务端 `preset_policy` 决定（出货策略 `type:"auto"` ⇒ 生效 `auto`，§3.8.4）；用户选 `auto` 或具体 preset 时按**显式选择**发送；`default_preset` 仅用于第 0 项提示文案，`type:"iterations_threshold"` 时才参与 `select_preset`（§3.8.4）；**绝不**取全局 `default_preset`（跨家族）。**未主动设置的参数一律不发送**（请求体 `params` 里不出现该键），由服务端 / ultralytics 走默认值。

**提交幂等（`POST /jobs` 是创建型请求，与服务端共同保证至多一次实体创建）**：

| 步 | 客户端动作 |
| --- | --- |
| 1 | 生成 `client_submission_id = "sub_" + secrets.token_hex(16)`（**与 `job_id` 无关**，本地唯一即可） |
| 2 | **先落盘、后发请求**：先把**提交请求体快照** `submit_request.json` 写到 `pending_dir` 下并 **`fsync`**；**`fsync` 成功之后**才把 `{client_submission_id, phase: submitting, submit_request_hash, dataset_id, request_path, server_url, pending_dir}` 持久化进 `pending_submissions`（与 `records` 同一次原子写）。**`fsync` 失败或字段不齐 ⇒ 不进入 `submitting`**（条目留在 `committed`，向用户报错，**不发请求**） |
| 3 | 发起 `POST /jobs`，请求体里**带上** `client_submission_id` |
| 4 | 拿到 `job_id` ⇒ 与 `records` 的写入**同一次原子写**完成三件事：① 该 pending 条目移入 `records`、② 删除对应 `pending_uploads` 条目、③ 把 `pending_dir` 标为可回收（= 清理时机 ①） |
| 5 | 未拿到响应（超时 / 连接断开 / 进程被杀）⇒ 条目留在 `pending_submissions`，下次启动或进入任务页时**重放同一 `client_submission_id` + 同一请求体**，拿回**原 `job_id`**（服务端按该字段去重） |

- 同 id 同 body ⇒ 返回**原 `job_id`**（200）；同 id **异 body** ⇒ **409 `VALIDATION_FAILED`**（`details.field=client_submission_id`）；保留期 **24 小时**（见 §3.11）。**超过保留期**后服务端不再保有该映射 ⇒ 重放会被当作**一次新提交**并可能新建 job，因此应**尽早完成对账**；此时生成**新的** `client_submission_id`，避免与可能已存在的旧任务混淆。
- 用户点「重试提交」时**复用同一个 `client_submission_id`**（幂等的前提）；只有用户**明确修改了参数或数据集**时才生成新 id 并作废旧条目。**不允许**在同一 `client_submission_id` 下改变请求体。
- `POST` **一律不自动重试**；401 / 500 时条目**留在 `submitting`**（不改 phase、不自动重试），由对账用**同一 `client_submission_id` + 同一请求体快照**重放。
- 提交接口的失败码**只处理**在 §5.6.4 有文案的那些：400 / 409 `VALIDATION_FAILED`、401 `UNAUTHORIZED`、404 `DATASET_NOT_FOUND`、422 族、500 `INTERNAL_ERROR` 与 503（`TRAINING_DISABLED` / `NO_DEVICE_AVAILABLE`）。**不含** 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED`（该码只由 upload 阶段的容量准入返回）与 409 `JOB_ARTIFACTS_EXPIRED`（只属已有任务的 resume）。

**降级行为（按已批准方案）**：

1. **数据增强参数不在 `param_schema`**（23 项不动）⇒ 该组显示「**服务端未声明，走 ultralytics 默认值**」，**客户端不得自行提交未定义的字段**；服务端扩充 schema 后再由它驱动控件。
2. **无 `POST /jobs/preflight` 路由**（§3.2.1 的 16 条路由里没有）⇒ **不做本地显存预估**；权威的 `vram_estimate_mb` / `batch_assumed` / `resolved_params` / `warnings[]`（含 `CONVERGED_TO_DEVICE_MAX` / `ADAMW_LR0_HIGH`）**取自 `POST /jobs` 的响应**，客户端只展示与黄条提示；参数错误留到提交时由 422 族兜底。


### §5.5 任务监控

本节覆盖任务提交之后的全部网络行为。终端判据 `is_terminal` 的**唯一定义在 §5.5.4**，本节其余位置与结果页一律引用它，不再重复。

#### §5.5.1 执行载体与轮询间隔

**执行载体（与 §5.1.4 的关窗判据同源）**：轮询必须在 **`QThread`** 中执行（`QThread` 子类，或 `moveToThread` 到 `QThread` 的 worker），与上传 worker 同一模式（worker 持有取消标志 + **单一 `threading.Event`**，进度 / 结果经 Qt 信号回主线程）。§5.1.4 步骤 4 的关窗**唯一判据**是 `all(w.isFinished() for w in workers)`，而 `isFinished()` **只存在于 `QThread`** 上——**不得**用裸 `threading.Thread`（它没有该方法，会让收尾判据恒为假或在求值时抛 `AttributeError`）。

**所有等待**（正常间隔、60 s 空转档、失败退避）**必须**写成 `threading.Event.wait(timeout)`，**禁止 `time.sleep`**：关窗收尾用**同一个 `Event`** 的 `Event.set()` 唤醒轮询线程，而 `Event.set()` 只能让 `Event.wait()` 立刻返回、**无法**打断 `time.sleep`（写成 `time.sleep` 关窗时最长要等满 60 s）。唤醒后**先检查取消标志**再决定是否发下一次请求；若线程正阻塞在一次 HTTP 请求里，由该请求自身的连接 / 读取超时兜底（**不依赖** `Session.close()`）。

| 场景 | 间隔 | 说明 |
| --- | --- | --- |
| 任务列表页可见 | **10 s**（可配 5–60 s） | 用 `GET /jobs?ids=...` 一次拉多个任务（§5.5.5） |
| 任务详情页可见 | **3 s**；该任务 `is_terminal == true` 时停止 3 s 高频轮询，改 §5.5.2 的 60 s 终态兜底 | 只拉当前任务 + 增量事件（§5.5.3） |
| 结果页可见 | **15 s**；该任务 `is_terminal == true` 时停止刷新产物清单，改 §5.5.2 的 60 s 兜底 | 仅刷新产物清单；与详情页、列表页**共用 §5.5.4 的那一个公式**，不得为结果页另定判据 |
| 窗口最小化 / 页面不可见 | **暂停** | 由窗口可见性 + 当前页判定；恢复可见时立即拉一次 |
| 台账中不存在非终态任务（无任何活动任务） | **60 s** 低频空转 | **刷新台账中的全部记录**（终态与非终态**都刷**）——只刷非终态等于不刷，且会让终态任务的跃迁不可见 |

#### §5.5.2 终态兜底通道（发现「外部手动恢复」）

详情页 / 结果页在该任务终态时都停止自动请求，而**手动 resume 可能由外部发起**（另一位运维 / 另一个客户端 / 直接打 API）：服务端把该任务置回排队、`finished_at` 置 `null`、`is_terminal` 回到 `false`。若停留在这些页面的客户端**永久停止请求**，它永远发现不了这次恢复。因此**不采用**「终态即永久停刷」，而写死一条**低频兜底通道**：

| 页面 | 活动态间隔 | 终态后的兜底轮询 | 兜底轮询拉什么 | 发现 `is_terminal` 回到 `false` 之后 |
| --- | --- | --- | --- | --- |
| 任务详情页 | 3 s | **60 s**（与「无任何活动任务」的空转档**同一档**） | **只拉 `GET /jobs/{id}` 的 job 对象**（不拉事件增量、不刷新产物） | **立即**恢复 3 s 高频轮询（当前任务 + 增量事件，`after=<last_seq>` 照常续拉） |
| 结果页 | 15 s | **60 s**（同上） | 先拉 `GET /jobs/{id}` 的 job 对象（判定用）；**仅当本次 tick 判定出 `is_terminal` 由 `true` 回到 `false` 时，在同一 tick 内补发一次 `GET /jobs/{id}/files`**；`is_terminal` 仍为 `true` 时**不发** `files` 请求 | **立即**恢复 15 s 的产物清单刷新，并**立刻**拉一次 `files`——**就是**上一列「补发的一次」：**同一次请求**，**不得**发两次 |

- **7 字段白名单（逐字保留）**：兜底轮询在 `is_terminal` 仍为 `true` 时，响应**只允许**更新 `status` / `is_terminal` / `needs_attention` / `needs_attention_reason` / `resume_cycles` / `resume_mode_available` / `partial_available`，**不得**重绘产物区 / 进度区（该响应**不含 `files[]`**）。`resume_mode_available` 驱动恢复按钮态（§5.5.7）、`partial_available` 驱动「部分结果」提示（§5.6.2）——少任何一个，这条链在兜底通道上永远不刷新。**产物清单的唯一例外**：本次 tick 判定出跃迁时按上表补发一次 `files`，该次响应允许刷新产物清单（属**跃迁后恢复**动作）。
- **可见性门控同样生效**：窗口最小化 / 页面不可见时兜底轮询一并暂停；恢复可见时立即拉一次，这一次同样参与「外部恢复」的发现。
- 与兜底**并发成立**的两条快路径：① 手动刷新按钮；② 重新进入页面时立即拉一次；三条路径（兜底 / 手动 / 重进）**必须走同一个判据**（§5.5.4）。**列表页不受本机制影响**（它本来就刷新全部记录，是「外部恢复」的第一发现者）。与「无任何活动任务 ⇒ 60 s 空转」**同为 60 s 档、不叠加**（一次 tick 同时服务两件事），但**请求不合并、不互相替代**：兜底通道是「1 次 `GET /jobs/{id}` ＋ 跃迁时补发 1 次 `files`（仅结果页）」，空转档走列表批量查询（§5.5.5）。

#### §5.5.3 事件增量

- 每个任务在台账里保存 `last_seq`；轮询时 `GET /jobs/{id}/events?after=<last_seq>`，响应里的 `last_seq` 写回台账（协议见 §3.5）。

| 事件 `type` | UI 处理 |
| --- | --- |
| `progress` | 进度条 + 「epoch 12/100」+ 依据 `eta_seconds` 显示预计剩余 |
| `metrics` | 指标卡片刷新（`mAP50` / `mAP50-95` / losses）；详情页折线可后置 |
| `log` | 追加到日志面板（`level` 为 warning / error 时着色；warning 同时触发黄条）；带扩展字段时一并展示，尤其 `code=OOM_BATCH_DOWNGRADE` ⇒ 「显存不足，已自动降 batch 重试（`from_batch` → `to_batch`）」 |
| `done` | 立即拉一次 `GET /jobs/{id}` 与 `GET /jobs/{id}/files`，刷新终态与产物 |
| `manual_resume` | 人工恢复留痕（`{"attempt": 1, "mode": "resume", "resume_cycles": 1}`）：事件流追加信息行「第 1 次人工恢复（mode=resume），本轮重试预算已重置」，并把 `attempt` 与 `resume_cycles` 写回台账 |
| **未知 `type`（前向兼容）** | **不得丢弃、不得报错**：**降级为 `log` 展示**（`level` 取 `info`，`data` 以 JSON 原样打印），`seq` 游标照常推进——服务端后续新增事件类型时无需改客户端 |

客户端既有训练 UI 的事件分支是 `training_started` / `training_log` / `training_completed` / `training_error` / `training_stopped`；远程训练在**适配层**把服务端事件翻译成这些既有语义（`done` + `status=completed` ⇒ `training_completed`，`log` ⇒ `training_log`，`manual_resume` ⇒ 信息行），以复用日志面板的渲染与配色。

#### §5.5.4 终态判据：唯一的 `is_terminal` 公式

> **本节是全篇（列表页 / 详情页 / 结果页 / 空转降频 / 跃迁检测）唯一的终态判据**，只此一条公式，其余位置一律引用。

```text
is_terminal := (job.is_terminal)                 # ① 优先：服务端下发的权威字段
               若该字段缺失（老服务端）:
is_terminal := (job.finished_at != null)         # ② 回退：等价公式（服务端只在终态写 finished_at）
```

| 项 | 口径 |
| --- | --- |
| 权威来源 | **服务端 job 对象的 `is_terminal`**（§3.4.4）：服务端计算、与状态机同源，客户端**直接采用**，**不得**再自行推理 |
| 回退公式 | `finished_at != null`——服务端只在终态写 `finished_at`（状态机不变量），因此两者**等价**；字段缺失时（老版本服务端 / 代理裁剪）才走这条 |
| 覆盖哪些状态 | `completed` / `cancelled` / 本轮预算耗尽的 `failed` / `interrupted`（`auto_resume=false` 或本轮预算耗尽）⇒ `true`；`queued` / `preparing` / `running`、以及 `failed` 但 `attempt < max_attempts`（服务端会自动重排队尾）⇒ `false` |
| `failed` 但预算未耗尽 | **不是终态**（`is_terminal=false`）：服务端自动重排队尾（`failed` → `queued`、同一 `job_id`、`attempt + 1`）⇒ 客户端**必须继续按正常间隔轮询**，状态行显示「正在自动重试（第 attempt/max_attempts 次）」（分子分母都取服务端值，不硬编码） |
| `interrupted` 已自动重入队 | `status` 变为 `queued`、`resume_mode_available == []` ⇒ **同样不是终态**，继续按正常间隔轮询 |
| resume 之后 | 服务端手动 resume 会把 `finished_at` 置回 `null` ⇒ `is_terminal` 回到 `false`；客户端**必须重新按活动任务轮询**该任务（发现途径见 §5.5.2：详情页 / 结果页最迟 60 s 内发现，手动刷新 / 重新进入则立即） |
| **禁止事项** | **禁止**再用「`status` 属于某集合 **AND** `finished_at` / 预算」「`status` 属于某集合 **OR** `finished_at`」这类**第三方写法**——前者会把「`failed` 但会自动重排队尾」误判成终态，后者会把「`interrupted` 已自动重入队（`queued`）」误判成终态。也**禁止**为结果页 / 空转档另立第二套判据 |
| 终态后的轮询口径（唯一规则） | 详情页停止高频轮询（保留手动刷新、再进入时立刻拉一次）；结果页停止刷新产物清单；列表页按 §5.5.1 的间隔统一刷新全部记录；详情页与结果页同时按 §5.5.2 保留 60 s 终态兜底轮询（该兜底**不拉事件**、平时**只读 job 对象**，与「停止高频轮询」不矛盾） |

#### §5.5.5 列表批量查询

```text
GET {client_base_url}/jobs?ids=job_a,job_b,...,job_n&limit=50   # 每批 ≤ limit（默认 50，见 §3.11）
```

- 台账里**所有**任务 ID 都要刷新（> 50 条是常态），而 `GET /jobs` **没有 `offset`**、不做服务端分页 ⇒ 按 `ids=` **分批查询**：把待刷新 ID 按每批 ≤ `limit`（默认 50）切块，串行或最多 2 个并发发出，再按 `job_id` 合并回台账（**不做本地翻页**）。
- **批量规则（4 条）**：① 响应 `jobs[]` **按请求 `ids` 顺序**返回，客户端**不**需要自行排序；② 未找到的 id 放在同级的 **`not_found_ids[]`**（HTTP 仍 200、**不报错**）；③ 重复 id 服务端**已去重**，客户端可照原样传（或本地先去重以减少请求体积）；④ **每批传入的 id 数量不得超过该批 `limit`**——超过时服务端返回 **400 `VALIDATION_FAILED`**（`details.field=ids` / `details.limit` / `details.provided`），**不做静默截断**，因此客户端必须自行按 `limit` 切块。
- **`not_found_ids[]` 里的 `job_id`**（**不是**「响应里没出现的」，因为未出现的可能只是被 `?status=` 过滤）视为服务端已无该任务 ⇒ 台账标记 **`orphaned`**（本地保留记录；必要时再逐条 `GET /jobs/{id}` 确认 404）。未传 `ids=` 而用 `?status=` 过滤时，**不得**把未返回的 id 当孤儿。
- `?status=queued,running` 只作为可选的轻量优化（例如只想刷新非终态任务时），**不能**替代 `ids=` 的全量刷新。

#### §5.5.6 网络失败与重试调度（全篇唯一一张调度表）

| 项 | 口径 |
| --- | --- |
| 首次处理 | **第 1 次连续失败——无论它是「无 HTTP 响应」还是 HTTP 5xx——都＝静默重发一次**：**不**显示红条、**不**等待、同一 tick 内立刻重发同一请求。（静默重发优先于按档位等待，两者**不可能同时命中**：这一档没有等待动作） |
| 红条阈值 | **连续 3 次「无 HTTP 响应」的失败**（第 1 次的静默重发计为第 2 次尝试；**5xx 不计入**该计数）⇒ 顶部红条「与服务端失去联系，正在重试…」（红条**不**阻断页面、**不**弹框） |
| 退避序列 | 两类失败（连接错误 / 超时 与 5xx）**共用同一条序列、同一个「当前档位」状态**：`5 → 10 → 20 → 30 s`（上限 **30 s**）；**档位不因失败种类切换而重置**（连接错误 → 5xx 或反向，都沿用当前档位继续升级）。按连续失败计数 `c` 定档：`c == 1` ⇒ 等 **0 s**（静默重发）；`c == 2` ⇒ **5 s**；`c == 3` ⇒ **10 s**；`c == 4` ⇒ **20 s**；`c >= 5` ⇒ **恒 30 s** |
| 两个计数 | ① **连续失败计数 `c`** = 唯一的定档输入：每发生一次失败（**无 HTTP 响应** 或 **5xx**）`c += 1`；收到 **2xx 或 4xx（401 除外）**时 `c = 0`；**失败种类切换不重置 `c`**。② **连续无响应计数** = 只统计「**无 HTTP 响应**」的失败，**任意 HTTP 响应到达时清零**（含 4xx / 5xx），**只决定红条显隐、不参与定档**。两个计数**不得混用** |
| 归零 / 保留档位 | **回到该页正常间隔并归零档位**：收到 **2xx**，或收到 **4xx（401 除外）**（服务端已给出答复、连接已恢复；例：404 `JOB_NOT_FOUND` ⇒ 标记 `orphaned`，**不再按网络失败退避**）。**5xx 是「有响应但服务端不健康」**⇒ **保留当前档位并继续升级**（**不**归零、**不**从 5 s 重新起步） |
| 两个终止出口 | ① **收到 401** ⇒ **立即停止轮询**并提示「Token 无效或已过期，请到配置页更新」（属**终止**，不重试）；② **用户关窗** ⇒ worker 在 `Event.wait()` 返回后按取消标志退出，**不再发新请求**，档位状态随 worker 销毁（关窗不需要、也不产生「归零 / 复位」动作）。**除此之外没有任何次数上限：30 s 封顶、永不放弃** |
| 请求类型 | 本调度**只**作用于**安全（幂等）请求**（本节的轮询请求全部是 `GET`）。`POST /jobs`（提交）、`POST /jobs/{id}/cancel`、`POST /jobs/{id}/resume` 的失败**不走**本调度（§5.6.4 表脚注） |
| 等待方式 | 退避等待同样**必须**写成 `threading.Event.wait(timeout)`（§5.5.1），以便关窗时秒级唤醒 |

**跃迁检测（与上表同一节，不构成第二张调度表）**：台账保存上次的 `status` / `is_terminal` / `needs_attention` / `needs_attention_reason` / `resume_cycles`；发现 `needs_attention` false→true、`needs_attention_reason` 变化、`resume_cycles` 变化或首次进入 `failed` 时**按 reason 弹提示**（三值，环境信息**不进入**该字段）：`attempts_exhausted` ⇒「任务 <名称> **本轮**自动重试已耗尽（`attempt` / `max_attempts` 均取服务端值），可恢复并**重置本轮重试预算**（最多**再**自动重试 `max_attempts − 1` 次）」；`artifact_suspect` ⇒「任务 <名称> 产物可疑，使用前请人工确认」；`resume_anomaly` ⇒「任务 <名称> 的恢复/重试流程异常（检查点或接管失败），请人工检查后再恢复」。

- **环境信息条**：环境指纹与告警**不进入** `needs_attention`，只按 §3.9 的三通道归属在配置页 / 详情页显示**信息性**提示条（含 `OOM_RETRY_UNAVAILABLE` 的「由 runner 自行兜底降 batch」语义），**不弹「需人工介入」提示、不阻断提交**。
- **文案区分**：5xx 是「有响应」⇒ **不**触发红条，显示「服务端暂时不可用（HTTP <status>），正在重试…」；只有连接错误 / 超时才显示红条。
- **任务完成**：弹**非阻塞**通知（不抢焦点）：「任务 <名称> 训练完成，可下载结果」；`artifact_suspect` 置位时详情页与结果页显示黄条（§5.6.2）。

#### §5.5.7 取消与恢复

**按钮可见性完全由服务端状态字段驱动（`status` / `resume_mode_available`），客户端不自己推断**；`resume_mode_available` 是**字符串数组**，为空即不可恢复。

| status | 「取消」 | 「恢复」 | 按钮文案与交互 |
| --- | --- | --- | --- |
| `queued` | ✅ | ❌ | 「取消」→ 二次确认「任务还在排队，取消后不会开始训练，**也不会产出任何产物**」→ `POST /jobs/{id}/cancel` |
| `preparing` | ✅ | ❌ | 同上，提示「任务正在准备，取消会终止准备过程」 |
| `running` | ✅ | ❌ | 「取消训练」→ 确认框说明：取消会终止本轮训练；**若训练已产出检查点**，服务端会把已产出的部分结果落到 `partial/`（如 `weights/last.pt`、已产出的 `results.csv` / `args.yaml` / `train.log` / `events.jsonl`）。**是否真的有 `weights/last.pt` 取决于取消发生在训练哪个时刻**，以 `GET /jobs/{id}/files` 为准；**「部分结果可用」的显示与否只由 `partial_available` 决定**（§5.6.2），客户端**不得**承诺一定存在 `last.pt` |
| `interrupted` | ❌ | 按 `resume_mode_available` | **自动重入队完成后服务端状态变为 `queued`**，此时数组为 `[]`（按钮**隐藏**，显示「已中断，正在重新排队」）；检查点存在时仍返回 `["resume"]`，按钮按数组可用性显示。`auto_resume=false`（或本轮预算耗尽）时 `interrupted` 是**终态**，显示「已中断（等待人工恢复）」并按 §5.5.4 停止详情页高频轮询 |
| `failed` | ❌ | 按 `resume_mode_available` | 「恢复」；`needs_attention` 为真时顶部黄条按 `needs_attention_reason` 给文案；数组为 `[]` 时按钮**置灰**并提示「检查点或数据集已过期」。本轮预算未耗尽时服务端自动重排队尾（`failed` → `queued`、同一 `job_id`、`attempt + 1`）⇒ **不是终态**，状态行显示「正在自动重试（第 attempt/max_attempts 次）」 |
| `cancelled` | ❌ | 按 `resume_mode_available` | 「恢复」；确认框按数组内容提示「将从已有进度继续（`["resume"]`）」或「未找到检查点，将从零重训（`["restart"]`）」 |
| `completed` | ❌ | ❌ | 显示「已完成」；不提供恢复入口（服务端 `resume_mode_available` 为 `[]`） |

**恢复按钮的可用性判定（服务端实时给出，客户端不推断；判定表见 §3.4）**：下表**只适用于「可恢复状态」**（`failed` / `interrupted` / `cancelled`）；`queued` / `preparing` / `running` / `completed` 四行按上表**直接隐藏**恢复按钮（`completed` 显示「已完成」），**不适用**下面的置灰规则。

- `resume_mode_available == []` ⇒ 按钮**置灰**（不隐藏，便于用户理解为何不能恢复），tooltip 与状态行文案：「检查点或数据集已过期，无法恢复」。
- `["resume"]` ⇒ 正常可用；确认框提示「将从上次进度继续训练（`run/train/weights/last.pt`）」。
- `["restart"]` ⇒ 正常可用；确认框提示「未找到可用检查点，将从零重训（服务端当前允许从零重训）」。
- 点「恢复」后若仍收到 409 `JOB_ARTIFACTS_EXPIRED`（判定在点击瞬间发生变化），按 §5.6.4 的文案提示并立即刷新详情。

**恢复交互（5 步）**：

| 步 | 内容 |
| --- | --- |
| 1 | 点击「恢复」→ 确认对话框显示任务名、当前 `attempt`（本轮计数）、`resume_cycles`（第几次人工恢复），以及**本次的实际行为**（由 `resume_mode_available` 决定：`["resume"]` = 从上次进度继续；`["restart"]` = 从头重训），并明确写出**重置语义**：`max_attempts` **含首次**，「恢复后本轮重试预算将重置，`attempt` 重置为 1 后本轮内仍可自动重试 `max_attempts` 次，即手动恢复后最多**再**自动重试 `max_attempts − 1` 次」。确认后请求体固定为 `{"mode": <所选行为>}`（`resume` / `restart`），取值必须与 `resume_mode_available` 一致，客户端**不发送任何隐式覆盖参数** |
| 2 | 发起 `POST /jobs/{id}/resume`，用响应的 `mode` / `attempt` / `resume_cycles` 给出明确反馈（响应 `mode` 应与点击前的数组一致；服务端在入队前会重新判定，不一致时以接口结果为准）；收到响应后**立即把 `attempt` / `resume_cycles` / `needs_attention=false` / `needs_attention_reason=null` 写回台账**，任务行的「需关注」徽标同步清除；`artifact_suspect` **保留**（它是产物属性，不因重新入队消失） |
| 3 | 409 `JOB_NOT_RESUMABLE` ⇒ **按 `details.reason` 分支**：① 无 `reason`（对 `completed` 恢复、或 `status` 不属于可恢复集合）⇒「该任务当前状态不允许恢复（已完成的任务不能恢复）」（按钮本不应出现，属兜底）；② `resume_in_progress` ⇒「上一次恢复尚未收尾，请稍后重试」，稍后重试；③ `lock_timeout` ⇒ 提示等锁超时并**直接重试**；④ `process_alive` ⇒「该任务仍有存活进程，请先取消或等它结束」，**不**重试。② / ③ / ④ 均刷新一次详情，恢复按钮保持原状 |
| 4 | 409 `JOB_ARTIFACTS_EXPIRED` ⇒ 按 `details.reason` 分支：`checkpoint_missing` ⇒「没有可用的训练检查点（last.pt），且服务端配置为不自动重训，请联系管理员」；`dataset_expired` ⇒「数据集已过期或被删除，无法继续训练；请重新上传数据集并提交新任务」。随后立即刷新一次详情（`resume_mode_available` 已变为 `[]`，按钮置灰） |
| 5 | 恢复成功后立即触发一次轮询刷新 |

**取消交互**：

- 运行中取消后，界面先显示「正在停止…（等待进程退出，最长 <capabilities.cancel_grace_seconds> 秒，默认 15）」：该值**取自 `capabilities`，不得硬编码**（§3.11）；随后由轮询确认 `cancelled`，客户端**不自行判定超时失败**。取消是**幂等**的：重复点击不会报错（服务端返回当前状态）。取消后任务卡片保留。
- **`partial_available` 的判据只在此处定义一次（§5.6.2 据此给文案）**：**是否显示「部分结果」一律由服务端的 `partial_available` 决定**——`files[]` 是否非空**不是**判据、**不得**与之并列（否则会留下未定义的中间态：`partial_available == false` 但 `files[]` 非空）。两分支**穷尽且互斥**：① `true` ⇒ 显示「部分结果可用」，产物树按 §5.6.1 渲染；② `false`（**含 `files[]` 为空**）⇒ **不显示**任何「部分结果可用」文案 / 黄条，产物区只按清单渲染——`files[]` 为空时显示「该任务没有可下载的产物」并**禁用**「下载结果」；**仅 `summary.json` 存在**（`files[]` 非空）时允许查看摘要但禁用「下载结果」并提示「没有可下载的权重文件」。**最典型的 ② 是「排队中取消」**（服务端明定该阶段**无产物**），因此文案**不得**预设存在 `last.pt`。

- 状态徽标取自 §5.6.2 的**两行状态徽标**（`status == "cancelled"` ⇒「已中止（用户取消）」；`status == "failed"` ⇒「训练失败」），**不受 `partial_available` 约束**——徽标是状态标识、黄条是提示，两者**可以同时出现**。


### §5.6 结果与错误

本节只写**客户端侧的渲染、文案与动作**；产物下载安全与 `file_id` 的服务端口径见 §3.10，错误码的服务端语义集合见 §3.3。

#### §5.6.1 产物清单

`GET /jobs/{id}/files` 的 `files[]` 渲染为树（5 列）：

| 列 | 说明 |
| --- | --- |
| 文件 ID | `file_id`（**不可变**，下载只用它；§3.10）。实现上**不要**把它显示成主列，只在 tooltip / 调试信息里展示即可 |
| 路径 | **恒为相对 `artifacts/<job_id>/`** 的路径（**仅用于展示与分组，不再用于拼接下载 URL**）；**部分结果条目统一带 `partial/` 前缀**（如 `partial/weights/last.pt`）。展示时按前缀分组为「**完整结果 / 部分结果**」两组 |
| 大小 | 人类可读（KB / MB / GB） |
| 时间 | `mtime` |
| 标记 | `partial: true` ⇒ 灰色徽标「上一轮中止的产物」（**文件级来源标注**：**与任务当前状态无关**，适用于 `status` 的全部取值）；`artifact_suspect` ⇒ 黄条「产物可疑」。两条互不替代 |

#### §5.6.2 部分结果

**判据在 §5.5.7 定义（唯一）**：本表只写文案；为 `false` 时不出现任何「部分结果可用」提示。为 `true` 时按 `status` **单值枚举**给唯一文案（**穷尽且互斥**，按下列顺序判定，每条状态命中且**仅命中一支**）：

| # | 命中条件（`partial_available == true`） | 文案 |
| --- | --- | --- |
| ① | `status == "queued"` | 「该任务已重新排队，将继续训练」（**文案去「自动」**：成因有三种、文案**不预设**其中任何一种——`interrupted` 已自动重入队、`failed` 但预算未耗尽被服务端自动重排队尾、**人工恢复**；三种成因都 `is_terminal == false`） |
| ② | `status == "interrupted"` | 「该任务已中断（等待人工恢复），仅保留部分结果」 |
| ③ | `status == "cancelled"` | 「该任务已中止，仅保留部分结果（`weights/last.pt` 与已产出的日志 / 曲线）」 |
| ④ | `status == "failed"` | 「训练失败，已保留部分结果」 |
| ⑤ | 兜底（其余取值：`preparing` / `running` / `completed` 等） | **状态中立文案**：「该任务保留了部分结果（`partial/` 目录下的中间产物），可按需查看或下载」 |

- **⑤ 不预设成因、也绝不断言任务当前处于中止 / 失败态**：「取消 → 人工恢复 → 训练完成」后 `status == "completed"` 与 `partial_available == true` **可以同时成立**（恢复链**不清理** `partial/`，产物默认永久保留）⇒ `completed` / `running` **一律不得**出现「已中止」「训练失败」这类**当前态**措辞。
- **状态徽标（两行，独立于 `partial_available`）**：`status == "cancelled"` ⇒「已中止（用户取消）」；`status == "failed"` ⇒「训练失败」。**徽标是状态标识、黄条是提示，两者可同时出现、互不替代**：`failed` 且 `needs_attention` 为真时徽标仍是「训练失败」，黄条另按 `needs_attention_reason` 给文案（§5.5.6）；预算未耗尽时状态行**并行**显示「正在自动重试（第 attempt/max_attempts 次）」。

- **可恢复性后缀**（**不单独成行文案、不单独命中状态**；只覆盖「不可恢复」这一支）：**只适用 ③ `cancelled` / ④ `failed` 两支**（即 `resume_mode_available` 的语义有效状态集合 `{failed, interrupted, cancelled}`，§3.4）；② 已自带「等待人工恢复」语义、**⑤ 兜底一律不追加**——服务端对 `preparing` / `running` / `completed` 恒返回 `[]`，原因是「`status` 不属于可恢复集合」，**与「检查点 / 数据集已过期」无关**（若把 ⑤ 纳入判据，`== []` 会无条件命中，与该支「状态中立」矛盾）。判据：**非空** ⇒ 追加「；如需继续训练请点击『恢复』」；**`== []` 且属于 ③ / ④** ⇒ 追加「；检查点或数据集已过期，无法恢复」。本行**不产生第二条「部分结果」黄条**。
- **仅 `summary.json` 存在**（`files[]` **非空**但**无权重文件**）：允许查看摘要，但「下载结果」按钮**禁用**并提示「没有可下载的权重文件」。**本行优先于**「`partial_available == false` 且 `files[]` 为空」的通用按钮描述：后者「该任务没有可下载的产物」**只在 `files[]` 为空时**出现；两行**互斥、二选一**，同一情形不得同时命中。
- **两条黄条的显示顺序（写死，自上而下）**：「**部分结果**」黄条在前、「**产物可疑**」黄条在后——前者与 `status` 绑定、必须紧跟状态徽标（属同一状态块），后者是对**已有产物可信度**的附加警告。两个判据**正交**：`partial_available` 只决定「部分结果」黄条是否出现、`artifact_suspect` 只决定「产物可疑」黄条是否出现，因此两者同时为真时结果页**同时有两条黄条**，实现者**不得**以「只有一条黄条」为由压掉其中任何一条。

#### §5.6.3 下载

| 项 | 做法 |
| --- | --- |
| 打包下载 | `GET /jobs/{id}/download?include_partial=true`（流式 zip）；保存用 `QFileDialog.getSaveFileName`，默认文件名 `<job_id>.zip`；不允许覆盖系统关键路径 |
| 单文件下载 | `GET /jobs/{id}/files/{file_id}`：`file_id` 取自 `files[]` 条目，**不拼 `path`**（§3.10） |
| 响应分流 | `files/{file_id}` 与 `download` 的**成功响应是裸二进制 / `application/zip`，不包 JSON envelope**；失败仍返回标准 envelope ⇒ **必须按 `Content-Type` 分流**：`application/json` ⇒ 错误解析路径；其余 ⇒ **流式写盘**路径 |
| 进度分母 | **只按响应自身长度计算**：zip 是**压缩流**、`files[]` 的 `size` 汇总是**未压缩字节**，二者不可比（用后者当分母会让进度条超过 100%）。① 带 `Content-Length` ⇒ 进度 = 已下载字节 / `Content-Length`；② **无 `Content-Length`**（`Transfer-Encoding: chunked`）⇒ 只显示已下载量 + **不定进度（忙碌指示器）**，不显示百分比 / 总大小、不用 `files[]` 汇总当分母 |
| 完成判定 | 下载完成以**流读完（EOF）**判定，**不**以「已达到某个总大小」判定；过程可取消 |
| 完成后 | 提示「已保存到 <路径>」；提供「**打开所在目录**」；把路径写回台账 `download_path` |
| 解压 | **不自动解压**：zip 只保存到用户指定的保存路径并提供「打开所在目录」，**不**自动解包、**不**在工作目录里另落一份 |
| 摘要区 | 结果页「摘要」区直接展示 `summary.json` 的关键字段（`final_metrics` / `verified_metrics` / `duration_seconds` / `resolved_params` / `training_env` 纯指纹）；`resolved_params` 各字段口径见 §3.8.5，客户端只读展示、不传该参数 |
| 续传 | **v1 不做断点续传**：不发 `Range` / `If-Range`、不保留任何分片记录；中断后重下即放弃旧数据、从 0 开始 |
| 可疑产物 | `artifact_suspect == true` ⇒ 黄条：「`best.pt` 的复算指标与训练日志不一致，或恢复后 loss 异常，请人工确认后再使用」 |

#### §5.6.4 错误处理（30 对码的客户端文案表）

错误封装统一为 `{"success": false, "error": {"code", "message", "details"}}`（§3.1）；客户端按 `code` 映射文案，`message` 原样展示在详情里，`details.files[]` 等清单**必须逐条列出**。本表只写**文案 / 动作 / 是否自动重试**三列；每个码的服务端触发条件见 §3.3。

| 码 | UI 文案（主） | 动作与是否自动重试 |
| --- | --- | --- |
| 401 `UNAUTHORIZED` | 「Token 无效或已过期，请在配置页更新后重试」 | 高亮服务器设置区；❌ |
| 400 `VALIDATION_FAILED` | 按 `details.field` 分支：`split` ⇒「上传内容校验失败：split 取值非法，或划分结果某一侧为空」；`split_strategy` ⇒「…划分策略取值非法（当前只支持 per_class）」；`mode`（resume mode 非法）⇒「恢复方式不被接受：当前只允许 <`details.allowed`>」；`files[]` 非空（同 split 内 stem 冲突等）⇒「上传内容校验失败」+ 冲突的 stem 与全部文件名；`field` 缺省 ⇒「请求体校验失败」 | 数据 / 上传类 ⇒ 回配置页复查后**重新预检**，**不得继续上传**；`mode` 非法 ⇒ 刷新详情（重新读 `resume_mode_available`）后按允许的取值重发；❌ |
| 400 `MISSING_LABELS` | 「服务端发现缺少标注文件」+ 文件清单 | 回配置页复查数据集；❌ |
| 400 `CHECKSUM_MISMATCH` | 「上传文件与本地声明不一致（可能传输损坏）」+ 文件清单 | 重新预检并上传；❌ |
| 400 `MANIFEST_MISMATCH` | 「上传内容与预检结果不一致」+ 缺失 / 多余清单 | **不得**本地重建 manifest 后用旧 token 上传：走**二次 plan**（§5.4.3）；对账时把该 pending 条目置 `void`（`manifest_mismatch`）；❌ |
| 400 `LABEL_CHECKSUM_MISMATCH` | 「标注文件内容与本地声明不一致（可能传输损坏或被改写）」+ `details.files[]`（含 `name` / `label` / `reason` / `declared` / `actual`，**逐条列出**） | 回配置页复查后重新预检并上传（**不要**把它当成图片 `CHECKSUM_MISMATCH` 的同义词，两者可分别提示）；❌ |
| 400 `INVALID_LABEL_FORMAT` | 「标签格式非法」+ 文件清单 | 提示用本工具重新转换后重新预检；❌ |
| 400 `UNSUPPORTED_EXTENSION` | 「存在不支持的图片扩展名」+ 清单 | 剔除非白名单文件后重试；❌ |
| 400 `TOKEN_EXPIRED` / `UNKNOWN_UPLOAD_TOKEN` | 「上传凭证已过期或无效，请重新预检」 | **不得原样重放该 token**（`TOKEN_EXPIRED` = 确已过期；`UNKNOWN_UPLOAD_TOKEN` = 服务端根本不知道它（含已被清理），**重放不可能成功**）：把对应 pending 条目置 `void`（`void_reason=token_expired`）+ **重新走 plan + upload（换新 token）**，图片按 sha256 命中缓存、不重传；❌（不重放，直接重新 plan） |
| 409 `VALIDATION_FAILED` | 按 `details.field` 分支：`upload_token` ⇒「该上传凭证已提交过且内容不同，将重新预检并上传」；`client_submission_id` ⇒「该提交请求已存在且内容不同，请确认后重新提交」 | **不得**复用该 token / id 提交不同内容：作废对应 pending 条目 → 重新预检（upload）或让用户确认（submit）；❌ |
| 404 `JOB_NOT_FOUND` / `DATASET_NOT_FOUND` | 「服务端已不存在该任务 / 数据集」 | 台账标记 `orphaned`，刷新列表；❌ |
| 404 `ARTIFACT_NOT_FOUND` | 「产物文件已不存在或不可下载」（`file_id` 在清单内但目标不存在 / 是目录 / 是符号链接 / 是设备文件；响应**不回显文件系统路径**） | 刷新产物清单（`file_id` 已不在清单中时同理）；❌ |
| 409 `DATASET_IN_USE` | 「数据集正被排队 / 运行中的任务使用，无法删除」 | 展示引用任务列表（客户端 v1 **不消费**数据集接口，本行只作兜底解析）；❌ |
| 409 `JOB_NOT_RESUMABLE` | 按 `details.reason` 分支：无 `reason` ⇒「该任务当前状态不允许恢复（已完成的任务不能恢复）」；`resume_in_progress` ⇒「上一次恢复尚未收尾，请稍后重试」；`lock_timeout` ⇒「服务端正忙，请重试」；`process_alive` ⇒「该任务仍有存活进程，请先取消或等它结束」 | 无 `reason` ⇒ 刷新状态、置灰 / 隐藏恢复按钮；`resume_in_progress` / `lock_timeout` ⇒ **可重试**（前者稍后、后者直接重试），只刷新状态、不动按钮；`process_alive` ⇒ **不重试**，先取消或等它结束；❌ |
| 409 `JOB_ARTIFACTS_EXPIRED` | 「无法恢复：检查点或数据集已过期」+ `checkpoint_missing` ⇒「没有可用的训练检查点且服务端未启用自动重训」；`dataset_expired` ⇒「数据集已过期或被删除」 | 立即刷新详情（`resume_mode_available` 变 `[]`、恢复按钮置灰）；数据集需重新上传；❌ |
| 409 `UPLOAD_IN_PROGRESS` | 「同一上传凭证正在上传中」 | 等待当前上传结束；**✅ 5 s 后一次**（**仅限「同一 `upload_token` + 同一 body」这一契约幂等前提**） |
| 413 `QUOTA_EXCEEDED` | 「超出服务端配额」+ `details` 数值 | **优先**提示「服务端已触发自动回收，请稍后重试」（§4.1.7）；**仅当回收无法解除**（对象全被引用）才提示**联系管理员**清理服务端数据集 / 配额（客户端 v1 **无数据集管理页、不消费数据集接口**）；❌ |
| 429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED` | 「服务端上传凭证保留区已满（保留期内不淘汰旧记录），请稍后重试」+ `details.limit` / `current` / `retry_after_seconds` | **保留本地 pending 条目**，**必须先做一次过期判定**（§5.4.4 例外 ③）：`now + wait < expires_at` ⇒ **✅ 到点后原样重试同一 token + 同一 body**（不作废、不重新 plan）；否则 ⇒ 置 `void`（`token_expired`）+ **重新 plan**（**不**原样重试） |
| 422 `PARAM_OUT_OF_RANGE` | 「参数 X 超出范围（min–max）」；若 `details.field=batch` 且服务端 `allow_auto_batch=false` ⇒「服务端不允许 batch 使用自动取值（-1 / 比例），请填 1–128 的整数」 | 高亮对应输入框；batch 类错误同时隐藏 / 禁用「自动」选项；❌ |
| 422 `PARAM_NOT_OVERRIDABLE` | 「该参数由服务端注入，客户端不可指定」+ 字段清单 | 属客户端 bug，记日志；❌ |
| 422 `OPTIMIZER_UNSUPPORTED` | 「当前环境不支持该优化器预设」 | 刷新 capabilities 并重选 preset；❌ |
| 422 `MODEL_FAMILY_UNSUPPORTED` | 「服务端 ultralytics 版本低于该模型家族要求（需要 ≥ X）」 | 提示改选家族；❌ |
| 422 `WEIGHT_NOT_AVAILABLE` | 「服务端没有该权重文件且未开启联网下载」 | 该权重置灰（`weights_ready=false` **且** `allow_weight_download=false`）；提示联系管理员预置或改选已就绪权重。若 `allow_weight_download=true` 时仍收到该码，属服务端异常（不应出现），记录日志并提示重试；❌ |
| 422 `INSUFFICIENT_VRAM` | 按 `details.reason` 分支：① `insufficient_capacity` ⇒「估算显存 X MB 超过本机最小单卡容量 Y MB（已扣推理预留 Z MB），本机永远跑不了该配置」（容量字段是 `min_device_total_mb` = 各卡 `total_mb` **最小值**，异构多卡按最小卡算；「当前无卡可用」**不**产生本码，任务会正常排队）；② `ratio_cap_below_one` / `table_cap_below_one` ⇒「该配置的自动 batch 折算上限小于 1，服务端判定为容量不足」+ 展示 `details.batch_cap_by_ratio` / `details.batch_cap_by_table` / `details.suggestion` | ① 建议降低 batch / imgsz 或换小模型，并预填建议值；② 建议改为**显式整数 batch**（如 1–4）、减小 `imgsz`，或提高服务端 `auto_batch_vram_ratio`；❌ |
| 422 `VRAM_ESTIMATE_UNAVAILABLE` | 「该模型与任务组合在服务端本机不可调度」+ 按 `details.reason` 细分（无任何可用显存表行 ⇒「缺少显存数据」；`VRAM_CALIBRATION_FAILED` ⇒「该组合在服务端本机的显存标定中全部点位 OOM」） | 置灰该（模型, 任务）组合（来源 `vram_table.unschedulable[]`），提示改用更小的模型 / 更大的卡，或联系管理员检查服务端 GPU 与标定日志；❌ |
| 503 `TRAINING_DISABLED` | 「服务端未启用远程训练」 | 显示管理员提示；❌ |
| 503 `NO_DEVICE_AVAILABLE` | 「服务端当前没有可用 GPU」 | 本码**只在提交任务**（`POST /jobs`）时产生：提示用户稍后**手动**重新提交；❌ |
| 500 `INTERNAL_ERROR` | 「服务端内部错误（错误号 <id>）」 | 记录日志与 `error_id`；**✅ 仅安全请求 `GET`**：与下一行「其它 5xx」**共用同一台退避状态机**（`5 → 10 → 20 → 30 s`、上限 **30 s**、**永不放弃**，**没有「最多 3 次」这类次数上限**；完整口径见 §5.5.6，本行**只引用不重述**）；落在 `POST` 上时**不**自动重发 |
| **其它 5xx**（`501` / `502` / `504`，以及**不带码的 503**：网关 / 反向代理 / 未预期的服务端故障——**不属于** §3.3 的 (HTTP, code) 码表集合，仅作**通用兜底**） | 「服务端暂时不可用（HTTP <status>），正在重试…」 | 记录日志与响应体（若有 `error_id` 一并记录）；**✅ 仅安全请求 `GET`**：退避同为 `5 → 10 → 20 → 30 s`、上限 30 s、永不放弃，与上一行 `500` **同一台状态机、同一个档位**（两行不得各写一套序列）；落在 `POST` 上时**不**自动重发 |
| 网络层（超时 / 连接失败） | 顶部红条「与服务端失去联系，正在重试…」 | **✅ 仅 `GET` 类**：**第 1 次**失败（无论「无 HTTP 响应」还是 `5xx`）**等 0 s**、同一 tick 内**静默重发一次**（静默重试优先于按档位等待）；**自第 2 次连续失败起**按 `5 → 10 → 20 → 30 s` 退避（上限 **30 s**、永不放弃）；**连续 3 次「无 HTTP 响应」失败**（**仅**该类计入，`5xx` **不**计入、只进退避档位）显示红条，**收到任意 HTTP 响应即清零「连续无响应计数」并隐藏红条**（退避档位由 **2xx 与 4xx（401 除外）归零**、**5xx 沿用当前档位继续升级**；**仅有的两个终止出口**是收到 401 立即停止轮询与用户关窗）；完整调度见 §5.5.6 |

> **统计口径（唯一）**：本表 **30 对 (HTTP, code) / 29 个不同 code**——`VALIDATION_FAILED` 同时以 400 与 409 出现 ⇒ **只算 1 个 code**，两个分支都必须实现；`INTERNAL_ERROR` 的 HTTP 配对写作 **500**；「其它 5xx」与「网络层」两行**不含 code**（属通用兜底行）。按 HTTP 分布：400 类 **9**、401 **1**、404 **3**、409 **5**、413 **1**、422 **7**、429 **1**、503 **2**、500 **1**（合计 **30**）。
>
> **自动重试只适用于安全（幂等）请求**：✅ 允许——所有 `GET` 查询，以及契约上幂等的两个上传重放场景（409 `UPLOAD_IN_PROGRESS`、429 `COMMITTED_TOKEN_CAPACITY_EXCEEDED` 且判定为可原样重试）；❌ 一律不自动重试——`POST /jobs`、`POST /jobs/{id}/cancel`、`POST /jobs/{id}/resume`，只提示用户**手动**重试。因此本表 `500` / 其它 5xx / 503 `NO_DEVICE_AVAILABLE` 等行的「自动重试」列**只对 `GET` 生效**；同一码落在 `POST` 上时客户端**不**自动重发。


---

## §6 验收

本章的客户端用例保留 **CT1–CT45** 编号（写测试函数名时直接用），服务端冒烟用新的 **S1–S25** 编号；两者**正文其余位置不引用 S 编号**。客户端解析的字段一律以 §3 各契约表为准（本节不重复接口假设）。

### §6.1 客户端验收 CT1–CT45

| # | 用例 | 期望 |
| --- | --- | --- |
| CT1 | 划分确定性 | 同数据集 / 同 `seed` 连跑两次：`images[].split` **逐图一致**，导出配置的 `seed` / `val_ratio` / `split_strategy` 与本次一致 |
| CT2 | 换 `seed` ⇒ 划分变化 | 只换 `seed` 重划（≥ 200 张）：至少 1 张图 `split` 改变（概率性断言，不构成保证） |
| CT3 | 每类两侧都有代表（带例外） | 含稀有类：各类 val 配额先扣除已在 val 的该类图再补新图，`n_val_c ≤ \|I_c\| − 1` 夹取生效，安全网 ≤ 3 轮 |
| CT4 | val 占比与 `val_ratio` | 单标签 val 占比 ≈ `val_ratio`；多标签全重叠时 **`\|V\| = max_c n_val_c ≈ N × val_ratio`**（`Σ_c n_val_c` 仅为名义配额上界，不是收敛值） |
| CT5 | 单图类别只有 1 张 | 该图归 train；给「类别 c 未出现在 val」警告；**上传不阻断**；`split_stats` 中该类 `val = 0` |
| CT6 | 多标签图片不重复出现在两侧 | 跨 2 类的多标签图在 manifest 里**只有一个** `split`；「先稀后多」优先级生效 |
| CT7 | val 为空阻止上传 | 全背景图 / 所有类别仅 1 张图 ⇒ 红色状态行列出原因并**阻止上传**（不发任何请求） |
| CT8 | seed 缺失时生成并展示 | `seed` 留空后扫描：生成随机种子并**回填**输入框，导出配置带该值，按该值重划一致 |
| CT9 | 配置导入复现划分 | 导出 → 改 `seed` 再导出 → 依次导入：各自与导出时一致；导入**不带出 Token** |
| CT10 | 服务端回执提示 | 含单图类别的数据集 upload 后：`SPLIT_CLASS_MISSING_VAL` 以**信息提示行**展示（不弹框、不阻断提交） |
| CT11 | 创建响应丢失 → 重放拿回同一 `dataset_id` | upload 已 commit、响应未到时强杀并重启（或造 `phase=committed` 条目）：同一 token + 同一 zip 重放 ⇒ 拿回**原 `dataset_id`**，不产生第二个 dataset |
| CT12 | 提交流程崩溃 → 同 `client_submission_id` 去重 | `POST /jobs` 已成功、响应丢失时强杀；重启后再提交 ⇒ 复用同一 id + 同请求体，返回**原 `job_id`** |
| CT13 | 流式上传的内存上界与可唤醒取消 | 慢速服务端上传接近 1 GB zip 并采样 RSS：峰值**不随 zip 线性增长**（< 1 MB 量级）；取消在一个块内生效 |
| CT14 | 二次 plan 路径 | val 侧唯一图被拒 / 全部被拒 / 剔除后同 stem / 旧 token 重放：前两者本地复检阻断或首次 plan 即 400（不落条目）；旧 token + 新 manifest ⇒ 400 `MANIFEST_MISMATCH`；都**不产生第二个 `dataset_id`** |
| CT15 | 提交前不带 Token / 缺 key 服务端 | 错误 / 缺失 Token ⇒ 401 文案与「高亮服务器设置区」正确；更新 Token 后用**同一 `client_submission_id`** 手动重试成功 |
| CT16 | 终态含 `interrupted` | `auto_resume=false` 且 `finished_at` 非空 ⇒ 判终态：详情页停高频轮询、列表 10 s（无活动 60 s）、结果页停刷 |
| CT17 | 关闭窗口不崩线程 | 上传 / 轮询中用 `Esc` 与关闭按钮各关一次（含「取消关闭」）：二次确认后无 `QThread` 销毁告警，`isFinished()` 全为真 |
| CT18 | 崩溃遗留 staging 回收 + `keep_staging` 保留 | (a) 强杀后重启 ⇒ 按 TTL 回收并记日志；(b) `keep_staging: true` 正常结束多次 ⇒ 目录**全部保留（无上限）**且路径可见；超过 **7 天**（固定 TTL 常量）才被回收（§5.1.5） |
| CT19 | 按 `file_id` 下载（**已按「v1 不做续传」改写**） | 用清单 `file_id` 下载嵌套产物与 `best.pt`：整文件流式成功、进度与 `Content-Length` 一致（**不做断点续传**）；穿越串 ⇒ 400 |
| CT20 | 网络断开后重放成功且 zip 仍在（关键） | 正常网络断开且服务端已 commit：重启后同一 token + 同一 zip 重放成功、拿回原 `dataset_id`，`archive.zip` 仍在 |
| CT21 | phase 枚举唯一 + `committed` 不重放 upload | 造四种 phase 触发对账：`phase` 只用六个取值；`committed` 条目**不重放 upload**、直接进提交流程 |
| CT22 | 仍被 pending 引用的目录免于 TTL 回收 | staging 与 `pending/<id>/` 时间戳改到 TTL 之外且台账有引用 ⇒ **都不回收**；无引用的孤儿才最旧优先回收 |
| CT23 | 唯一 `is_terminal` 公式与三页复用 | 预算未耗尽的 `failed` / 终态 `interrupted` / 手动 resume 后三例：三页同一公式；跃迁回 `false` 后立即恢复高频轮询 |
| CT24 | 请求超等待上限且不响应取消时不销毁窗口与线程 | 假服务端接受连接后挂住 > 600 s：关窗后窗口不销毁、无 `QThread` 告警，`isFinished()` 仍能为真 |
| CT25 | `points` 基数约束 | 空 `points` 的 rectangle、单点 rectangle、2 点 polygon：按 §5.2.4 判非法并给可读原因，**不上传** |
| CT26 | 冻结字段清单与服务端逐字段对齐 | 用真实响应核对各契约表：解析覆盖 `calibration` / `resolved_params` / `files[].file_id` / `not_found_ids[]`，无凭空造字段 |
| CT27 | 根目录扫描与 N1 阻断矩阵 | 同置子目录图、缺标签图、非白名单扩展名、重名 / 同 stem：按矩阵逐条给原因，**不写数据集目录** |
| CT28 | 缓存命中时「零图片 + 全量标签」上传 | 建缓存后重传：`missing_images == []`、`upload_bytes == 0`，zip 不含 `images/` 但含全部 `labels/`，得**新 `dataset_id`** |
| CT29 | `label_sha256` 生成与错误展示 | 复算一致；改一个标签 ⇒ 400 `LABEL_CHECKSUM_MISMATCH`，`details.files[]` 逐条列出 `name` / `label` / `declared` / `actual` |
| CT30 | 各 (HTTP, code) 的 phase 转移 | 对 `phase=uploading` 条目逐行注入 §5.4.4 表：每行得到写死的 phase；判 `void` 者记对应 `void_reason`，保持者留 zip |
| CT31 | 429 的过期判定两分支 | `now + wait < expires_at` ⇒ 保留并按点重试同一 token + 同一 body；`>=`（含无 `expires_at`）⇒ 置 `void` + 重新 plan |
| CT32 | plan 响应前崩溃 | 在 plan 已发出、响应未落盘的窗口强杀：台账**无 `planned` 条目**、无 `pending_dir/`；重启后可直接重新 plan |
| CT33 | 取消与 commit 竞态 | 发送中 / body 已发完等响应 / 已 commit 但响应被丢弃三种取消：条目都留在 `uploading`，zip 保留，对账拿回原 `dataset_id` |
| CT34 | 全部条目被拒 ⇒ 首次 plan 即失败 | 假服务端回 400 `VALIDATION_FAILED`（**不含 `rejected[]`**）：立即失败，只展示 `details` 实际字段 + 本地阻断清单，**不 upload、不落条目** |
| CT35 | 事件增量契约 | 按序返回四类必需事件 + `manual_resume` + 一个未知 `type`：`after=` 与 `last_seq` 正确；`manual_resume` 写回台账；未知 `type` **降级为日志行** |
| CT36 | 批量查询规则与孤儿判定 | 120 条记录 + `not_found_ids[]` + 一次超 `limit` 请求：按 ≤ `limit` 切块、按请求顺序合并、`not_found_ids[]` 标 `orphaned`、超限按 400 **不静默截断** |
| CT37 | 静默重试、退避与重连 | 按请求时刻断言：首失 0 s 静默重发，此后 `5 → 10 → 20 → 30 s`（`c ≥ 5` 恒 30 s）；连续 3 次无响应显示红条、任意响应隐藏 |
| CT38 | 取消 / 恢复的错误分支与响应写回 | `resume` 的 409 `JOB_NOT_RESUMABLE`（三 reason）与 409 `JOB_ARTIFACTS_EXPIRED`（两 reason）逐支文案正确；成功响应写回台账并清徽标 |
| CT39 | 终态页发现「外部手动恢复」 | 外部 resume 置回 `queued`：最迟 **60 s**（手动刷新 / 重进即刻）发现并恢复高频轮询；结果页**跃迁那次 tick 补发一次** `files` |
| CT40 | 数据集源目录严格只读 + 工作区零写入 | 数据集目录前后哈希（含 `mtime`）**逐字节未变**；除台账 / `pending/` / 配置外不写工作区与用户数据目录 |
| CT41 | 上传中退出应用不留「线程仍运行」 | 上传中关主窗口 / `QApplication.quit()`：`Event.set()` 唤醒并等 worker `isFinished()`，无 `QThread` 告警 |
| CT42 | 磁盘写入失败 / 半写 `owner.json` 容错 | 原子写前强杀或人为写盘失败：残留目录被回收并记日志；写盘失败**不进入下一阶段**并报错 |
| CT43 | 调试产物路径可见性（不新增挂载点） | `keep_staging: true` 后正常结束 3 次：UI 给出**可复制的完整路径**（保留目录**无份数上限**，只受 7 天固定 TTL 约束，§5.1.5） |
| CT44 | `*_request_hash` 的按阶段命名与不变式 | 走完全链路：三个字段只描述各自阶段请求体且**未被改写**；`plan_request_hash == upload_request_hash`；三者**未出现在请求体** |
| CT45 | 两类失败交替时的等待值唯一确定 | `5xx` → 连接错误 → `5xx`：定档只由 `c` 决定（`c == 1` 等 0 s；`2 / 3 / 4` 等 5 / 10 / 20 s；`c ≥ 5` 恒 30 s），**种类切换不重置档位** |

### §6.2 服务端冒烟 S1–S25

人工可执行的最小冒烟清单（每条「动作 → 期望」）。前置：单台 Linux 服务器、`work_dir` 可写、已按 §2.6 的**七步顺序**启动。

| # | 主题 | 动作 → 期望 |
| --- | --- | --- |
| S1 | fork 安全性 | `git merge upstream/main` **无冲突**；`git diff upstream/main --stat` **只显示新增目录**，上游文件零改动 |
| S2 | 七步启动顺序 | 按 §2.6 启动：第 ② 步是**唯一**可写步骤；**标定先于推理模型加载**；任一步失败即**拒绝启动**、端口不监听 |
| S3 | 鉴权 fail-closed | 未开鉴权却启用训练（无逃生开关）⇒ 启动**拒绝**、端口**不监听**；加逃生开关 + 回环后才可达 |
| S4 | health 四态鉴权表 | 按 §3.7 四行演练：`false/*` 无 Token 也 200；`true/*` 无 Token ⇒ 401；`true/false` 带 Token ⇒ 200 + `enabled=false`；其它路由 ⇒ 503 |
| S5 | 冷缓存 | 首次上传：`blob.written == total`、`blob.hit == 0`；物化为**硬链接**（`st_nlink > 1`） |
| S6 | 热缓存 | 再传一次：`blob_hits == total_images`、`missing_images == []`、`upload_bytes == 0`，仍生成**新 `dataset_id`** |
| S7 | 标签永不缓存 | 只改一张标签重传：图片**全命中**、zip **仍含全部 `labels/`**、得到**新 `dataset_id`** |
| S8 | 判定表 ⓪ / ③ | ⓪ 同 token 同 body 未 commit ⇒ 正常落盘拿 `dataset_id`；③ 已 commit 后同 body 重放 ⇒ **幂等拿回原 `dataset_id`**；异 body ⇒ 409 |
| S9 | 判定表 ①②④⑤ | 未知 token ⇒ 400 `UNKNOWN_UPLOAD_TOKEN`；在用表超期 / 保留期届满 ⇒ 400 `TOKEN_EXPIRED`；旧 token + 新 manifest ⇒ 400 `MANIFEST_MISMATCH` |
| S10 | 校验失败码 | 缺标签 / 改图片 / 改标签 / 改 manifest ⇒ 400 `MISSING_LABELS` / `CHECKSUM_MISMATCH` / `LABEL_CHECKSUM_MISMATCH` / `MANIFEST_MISMATCH`，都**不产生 `dataset_id`** |
| S11 | 提交期预检拒绝 | 超范围 batch / 不支持 preset / 显存不足 ⇒ 422 `PARAM_OUT_OF_RANGE` / `OPTIMIZER_UNSUPPORTED` / `INSUFFICIENT_VRAM`，都**不入队** |
| S12 | 严格 FCFS | 连提 3 个（并发上限 2）：第 3 个排队且**不插队**；手动恢复的任务**重新入队尾**（`queue_position` 变大） |
| S13 | 选卡与 `queued_reason` 三值 | `queued_reason` 落在三值集合内；派出后 `device_index` 从 `null` 变为实际卡号，同卡并发不超上限 |
| S14 | 取消（`queued`） | 排队中取消 ⇒ `cancelled`、**无产物**（`files[]` 空、`partial_available=false`）；重复调用仍 200（**幂等**） |
| S15 | 取消（`running`） | SIGTERM → 宽限 `cancel_grace_seconds` → SIGKILL；中间产物保留在 `partial/`（`partial_available=true`）；重复取消幂等 |
| S16 | 手动恢复 | 预算耗尽的 `failed` 恢复 ⇒ `attempt` 重置 **1**、`resume_cycles` **+1**、`status=queued`；`completed` ⇒ 409 `JOB_NOT_RESUMABLE` |
| S17 | 自动重试耗尽 | 失败至 `attempt == max_attempts` ⇒ `needs_attention_reason=attempts_exhausted`，**不再自动重排队尾** |
| S18 | 服务重启接管 | 训练中重启：终态任务被消费、进程状态按既定分流处理、进程存活判 `interrupt`；重启后状态与落盘一致 |
| S19 | 事件增量 | `?after=<seq>` 只返回 `seq` **严格大于** `after` 的事件，`last_seq` 为当前最大 `seq`；连续两次**无重复事件** |
| S20 | 未知事件 type 降级 | 追加未知 `type` 事件 ⇒ 接口照常返回、`last_seq` 推进；客户端按日志行展示（**不报错、不丢弃**） |
| S21 | `file_id` 清单即白名单 | 清单外 ⇒ 400 `VALIDATION_FAILED`；清单内但非普通文件（目录 / 符号链接）⇒ 404 `ARTIFACT_NOT_FOUND`；都不回显路径 |
| S22 | `ETag` / 416 / 可复算 | `If-Range` + `Range` ⇒ 206 + `Content-Range`；不可满足区间 ⇒ 416；按 §3.10.1 从路径**复算** `file_id` 与清单逐字相同 |
| S23 | preset 选择 | 出货策略 `type:"auto"`：未显式传 `optimizer` ⇒ `optimizer="auto"` / `optimizer_preset=null` / `optimizer_source="server_auto"`；策略为 `iterations_threshold` 时家族 `default_preset` 生效（`yolo26` ⇒ `yolo26-default`、`yolo11` ⇒ `yolo11-sgd`），未显式选择 `auto` 时 `resolved_params.optimizer` 为家族 preset 决定的优化器名；显式选择 `auto` 时 `optimizer="auto"` / `optimizer_preset=null` / `optimizer_source="auto"`（§3.8.4 取值来源表） |
| S24 | 标定失败 ⇒ 不可调度 | 组合全部点位 OOM ⇒ `vram_table.unschedulable[]` 含它；提交该组合 ⇒ 422 `VRAM_ESTIMATE_UNAVAILABLE` |
| S25 | TTL 与软删除 | `DELETE` ⇒ 目录**先进 `.trash/`**；`artifact_ttl_days: 0` ⇒ **不清理**；正值越期 ⇒ 到期清理 |

## §7 已裁决、降级行为与未决项

### §7.1 已裁决事项（每条只写决议）

| # | 决议 |
| --- | --- |
| 1 | 「省 Y GB」**由客户端本地计算**（命中图片的 `size` 汇总），不要求服务端新增字段。 |
| 2 | **不做分片 / 断点续传**：依据「单数据集常态 < 1 GB」——该依据**只在真正流式的前提下成立**，因此 upload **必须**用 `MultipartEncoder`（`files=` 会整包驻留内存）。 |
| 3 | 结果 zip **不自动解压**：只保存到用户指定路径并提供「打开所在目录」。 |
| 4 | **不做数据集管理页**：`GET /datasets`、`DELETE /datasets/{dataset_id}`、`GET /cache/stats` **不消费**；413 **优先**提示「服务端已触发自动回收，请稍后重试」；**仅当回收无法解除**才提示联系管理员手工清理（§4.1.7、§5.6）。 |
| 5 | 参数表单**除日志与产物记录类外全部可配**，分四组（常用 / 数据增强 / 学习率与优化器 / 训练控制）由 `param_schema` 驱动；**未主动设置的参数不发送**。 |
| 6 | **不做本地显存预估**，改为**服务端预估**；客户端只展示服务端给出的 `vram_estimate_mb` / `batch_assumed` / `resolved_params` / `warnings[]`。 |
| 7 | **单服务器**：`server.json` 只保存一个 `server_url`，暂不考虑多服务器切换（`pending_dir` 仍持久化原始 `server_url`）。 |

### §7.2 服务端未定义项的降级行为

| 未定义项 | 客户端降级行为 |
| --- | --- |
| 数据增强参数不在 `param_schema`（23 项不动） | 该组显示「服务端未声明，走 ultralytics 默认值」；**不得自行提交未定义的字段**。 |
| 无 `POST /jobs/preflight` 路由（§3.2.1 的 16 条里没有） | **不做任何本地显存预估**；权威预估字段取自 `POST /jobs` 响应，参数错误由 422 族兜底。 |
| 400 响应不保证携带 `rejected[]` | 「全部条目被拒」时只展示 `details` 中**实际存在**的字段 + 本地阻断清单，**不解析不存在的逐项列表**。 |

### §7.3 明确不做清单（防止实现者顺手做）

- **断点续传 / 分片上传**（客户端与服务端都不做，见 §7.1 第 2 条）。
- **`systemd-run` 独立 cgroup**（每任务独立 scope 属后续版本，v1 只用既定进程模型）。
- **OBB / Pose / Classify**（`tasks`、标签模式映射与 `data.yaml` 生成都不扩展；任务字段已预留）。
- **SSE / WebSocket**（v1 用 `events?after=<seq>` 轮询，已具备增量语义）。
- **数据集整包校验**（只做逐图片 / 逐标签 sha256）；**标定键加 device 维度**（v1 只支持同构多卡，异构按各卡 `total_mb` 最小值保守折算、**不引入专门告警码**）。
- **`Idempotency-Key` 头**与**多租户 / owner 隔离**（只做最小版幂等：upload token 幂等重放 + `client_submission_id` 去重）。**owner 隔离是【用户已拍板，内部信任模型】下的显式非目标**：不引入用户体系与按用户配额；同一把 key 的使用者可见彼此的任务，实践上客户端按本地 `tasks.json` 台账的 `ids` 查询（`GET /jobs?ids=`）各看各的（§1.3）。
- **结果侧两项**：训练指标可视化（不在服务端聚合 `results.csv` 下发曲线点）与「结果自动注册为推理模型」（产物只供下载）。

### §7.4 未决项

| # | 未决点 | 当前默认口径 |
| --- | --- | --- |
| 1 | 事件类型命名差异 | 服务端固定四类必需事件 + 可选 `manual_resume`，由客户端**适配层**映射到既有 `training_*` 语义；若要求端到端同名需另行澄清。 |
| 2 | 数据增强参数的 `param_schema` 扩充 | 服务端仍是 23 项、无数据增强字段；客户端该组留空并注明「服务端未声明，走 ultralytics 默认值」，**不登记非法字段**。 |
| 3 | 无副作用参数预检入口 | 接口集合仍是 **16 条**、**无 preflight**；客户端不做本地估算，参数错误由提交时 422 兜底。 |
| 4 | 上传 token 的撤销 / supersede 契约 | **不新增撤销契约**：旧 token **仅客户端丢弃**，服务端按判定表 ② / ③ / ⑤ 的自然行为处理。 |
| 5 | 保留期届满后的重放语义（过期 tombstone） | **不引入 tombstone**：保留期外回落为 400 `UNKNOWN_UPLOAD_TOKEN`（判定表 ①，**有意如此**）；客户端在保留期外**重新 plan**。 |
| 6 | `ErrorResponse.details` 的逐项拒绝列表（`details.rejected[]`） | 服务端未定义结构 ⇒ 客户端**不做任何假设**，只展示实际存在的 `details` + 本地阻断清单（与 §7.2 第 3 条同源）。 |
| 7 | 「某类别被条目级剔除而整类消失」不可检测 | plan 请求体 `images[]` 无类别字段 ⇒ 该情形**不产生告警**；客户端在**本地预检**阶段尽量阻断，服务端由 plan 校验表既有判据兜底。 |
