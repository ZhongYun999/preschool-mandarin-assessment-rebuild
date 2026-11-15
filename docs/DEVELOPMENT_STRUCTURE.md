# VNext｜项目目录与模块归属约定（建议稿）

> 状态：**开发目录建议，不是业务合同或立即重构指令**。编写于 2026-11-15，审视基线为新仓库 `main` `e98ef734716f277658bfb28186cf229c1c927faa`（SESSION-02A 已合并）。
>
> 适用仓库：`ZhongYun999/preschool-mandarin-assessment-rebuild`。旧仓库只供只读参考。
>
> 决策优先级：先遵守 `AGENTS.md`、`docs/DESIGN_INDEX.md` 和本任务相关的**已确认专项设计 / C1.0**；本文件仅说明代码**放在哪里、如何控制依赖和迁移成本**。发现业务矛盾时不得通过改变目录来重新定义业务。

## 1. 为什么现在需要约定

目前 `backend/app/` 下的题库、Excel 导入、组卷、发布、TTS、Session、Submission、鉴权与教师 HTTP API 均为同级模块。其中 `teacher_api.py` 已混合题库管理、导入和音频接口。随着五种题型、评测、评分及前端加入，继续在根目录扩展将提高跨会话改动冲突与定位成本。

本约定目标只有三点：**同一业务领域的代码尽量相邻；依赖方向明确；未实现的功能不提前建空架子**。它不要求每一个模块都具备 Model、Repository、Service、Controller 四件套，也不要求全部代码搬入一个笼统的 `services/` 目录。

## 2. 全仓库目标形态（逐步形成，非一次性创建）

以下 `【现】` 表示当前仓库已有，`【迁】` 表示建议从已有根目录文件归位，`【需】` 表示对应业务开发时按需出现。缩进展示归属，不承诺全部成为独立 Python 模块或 Vue 文件。

```text
preschool-mandarin-assessment-rebuild/
├── AGENTS.md                               【现】协作、质量与安全纪律
├── README.md                               【现】快速开始与开发命令
├── VNEXT_INITIAL_ARCHITECTURE_DESIGN.md     【现】早期总体参考，非最新细则
├── PROBLEMS.md                             【现】历史故障与回归线索
├── .github/
│   └── workflows/
│       └── pr.yml                          【现】轻量分层 PR 检查
├── docs/
│   ├── DESIGN_INDEX.md                     【现】现行设计入口
│   ├── DEVELOPMENT_STRUCTURE.md            【本文件】目录规范
│   ├── design/                             【现】C1.0 与九份专项业务设计
│   ├── testing/                            【需】跨模块测试计划、验收说明
│   ├── api/                                【需】已实施的公开接口合同
│   └── operations/                         【需】真实部署或运维时再创建
├── backend/                                【现】FastAPI + PostgreSQL 16
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                         【现】应用创建、实际 Router 挂载
│   │   ├── database.py                     【现】基础 Engine、ORM Base 与连接
│   │   ├── api/                            【迁】HTTP 接口与权限/DTO 边界
│   │   │   ├── teacher/                    【迁】教师管理入口，按已存在功能分文件
│   │   │   │   ├── taskbanks.py
│   │   │   │   ├── imports.py
│   │   │   │   ├── papers.py               【需】出现真实试卷 HTTP API 时
│   │   │   │   └── audio.py                【迁】已有固定音频管理/试听接口
│   │   │   ├── assessment/                 【需】施测 Session、题目、结果接口
│   │   │   └── dependencies.py             【迁/需】实际公共数据库与授权依赖
│   │   ├── taskbank/                       【迁】题库身份、修订、导入、图片
│   │   │   ├── models.py                   【迁】TaskBank/Identity/Revision/Asset
│   │   │   ├── excel.py                    【迁】T1.5 XLSX 模板与结构预检
│   │   │   ├── import_service.py           【迁】预检对比、原子导入业务
│   │   │   ├── import_batch.py             【迁】ImportBatch 审计模型
│   │   │   ├── preview_store.py            【迁】预览临时存储技术实现
│   │   │   └── image_store.py              【迁】图片对象字节与资源保存
│   │   ├── papers/                         【迁】组卷、发布与冻结执行合同
│   │   │   ├── models.py                   【迁】Paper、PaperEntry、EvaluationPlan
│   │   │   ├── drafts.py                   【迁】草稿增删改序、复制等业务
│   │   │   └── publication.py              【迁】发布预检、版本/需求冻结
│   │   ├── sessions/                       【迁】Session 与作答提交权威状态
│   │   │   ├── models.py                   【迁】Session、SessionTask、Attempt 等
│   │   │   ├── lifecycle.py                【迁】创建、启动、重做等事务
│   │   │   ├── submission.py               【迁】播放事实和正式提交
│   │   │   └── close.py                    【需】CloseManifest、输入关闭事务
│   │   ├── task_types/                     【需】仅已实现的题型 Handler
│   │   │   ├── instruction.py              【需】选择题
│   │   │   ├── repetition.py               【需】跟读题
│   │   │   ├── qa.py                       【需】问答题
│   │   │   ├── dialogue.py                 【需】对话题
│   │   │   └── speaking.py                 【需】说话题
│   │   ├── media/                          【迁】公共语音与文件技术能力
│   │   │   ├── fixed_audio.py              【迁】已发布 E/F 固定语音及存取
│   │   │   ├── tts_attempt.py              【迁】单次合成执行与状态
│   │   │   ├── tts_worker.py               【迁】TTS 作业领取与恢复
│   │   │   └── dashscope_tts.py            【迁】供应商合成适配器
│   │   ├── evaluation/                     【需】按需评价需求、作业及原始证据
│   │   │   ├── jobs.py                     【需】持久评价作业与依赖
│   │   │   ├── raw.py                      【需】RawEvaluation 版本化事实
│   │   │   └── providers/                  【需】实际用到的 ASR/ISE/LLM 适配
│   │   └── scoring/                        【迁/需】评分算法及人工复核
│   │       ├── registry.py                 【迁】已部署插件/版本注册
│   │       ├── automatic.py                【需】score_task / score_paper 协调
│   │       ├── review.py                   【需】教师采纳分/复核决策
│   │       └── finalization.py             【需】正式结果原子固化
│   ├── migrations/                         【现】Alembic env.py、历史版本
│   │   └── versions/                       【现】迁移序列，仅追加新版本
│   ├── tests/                              【现】pytest，按风险/功能逐渐分组
│   │   ├── conftest.py
│   │   ├── test_*.py                        【现】现有定向和集成测试
│   │   └── integration/                    【需】确有大量跨域测试再迁入
│   ├── pyproject.toml                      【现】Python 依赖、Ruff、pytest
│   └── .env.example                       【现】仅示例变量
├── frontend/                               【需】教师管理前端；框架确定后建立
│   ├── src/
│   │   ├── app/                           【需】入口、路由、壳层
│   │   ├── features/                      【需】题库、试卷、复核、结果等页面能力
│   │   ├── api/                           【需】教师侧 HTTP 客户端
│   │   └── shared/                        【需】确有复用的 UI/工具/样式
│   └── tests/                             【需】前端测试，位置依实际工具决定
├── miniprogram/                            【需】微信小程序，实施时建立
│   ├── pages/                             【需】入口、准备、五题型、结果等页面
│   ├── components/                        【需】可复用组件，如音频与题目导航
│   ├── services/                          【需】后端 API、上传及播放接入
│   ├── state/                             【需】客户端页面/会话状态缓存
│   └── utils/                             【需】与业务裁决无关的纯工具
├── scripts/                                【现】文档、分类等工程工具
└── deploy/                                 【需】真实部署方案获准后才建立
```

**重要**：上述未来文件名均为建议性**归属示例**，不是新增文件清单。具体实现可合并相邻小文件，亦可在单一职责变复杂后拆分。绝不为满足示意树而建空类、空服务或提前决定无依据的评分/题型限制。

## 3. 后端各领域的责任与调用边界

| 归属 | 负责 | 不负责 |
|---|---|---|
| `api/` | HTTP 路由、请求与响应投影、调用方权限、错误映射、实际依赖注入 | 自行维护 Session 状态机、评分公式、长事务业务 |
| `taskbank/` | 题库内身份、不可变修订、T1.5、资源引用、导入事务 | 修改已发布试卷的引用、决定测评提交资格 |
| `papers/` | 草稿、组卷标准选择、PaperEntry 冻结、发布准备和 EvaluationPlan | 运行时变更已发布题目、替 Worker 执行评分 |
| `sessions/` | Session/Attempt/Submission/CloseManifest 身份、当前候选和业务原子事务 | 自行发起不需要的付费评测、篡改题目版本 |
| `task_types/` | 各题型 Handler 的交互事实、断点和合法提交判断 | 自行计算插件分、覆盖 Session 权威状态 |
| `media/` | 固定 TTS、音频资源、录音基础能力和相关 Worker 技术执行 | 决定题型状态转换、复核、整场结束 |
| `evaluation/` | 读取冻结需求、按依赖发起实际所需的 ASR/ISE/LLM、保存 RawEvaluation | 通用扣分、整卷汇总、将缺失伪造为 0 |
| `scoring/` | 版本化评分插件 `score_task()` / `score_paper()`、系统分、复核、正式成绩固化 | 重新生成题型交互、重新执行不需要的模型请求 |

**依赖规则（轻量约定）**：

1. `api` 可以调用所属领域的公开业务函数；不得将教师完整 DTO 直接用于儿童接口。前端必须只经授权的 HTTP API 获取后台数据。
2. 领域间以稳定业务身份（`task_revision_id`、`paper_entry_id`、`session_task_id`、`submission_id` 等）交接；避免另一领域私自复制或维护可变的“最新题干/当前成绩”。
3. `sessions` 主导提交、重做和关闭的业务裁决；`task_types` 提供题型自己的合法性判断；基础音频/模型适配仅提供可验证事实。**允许现实中必要的双向协作通过明确函数接口组织，不为追求绝对无环引入无用接口层。**
4. `evaluation` 依据已发布试卷冻结的需求调度；`scoring` 消费冻结作答与已保存评价。人工改采用分只重新运行 `score_paper()`，不重新调用 ASR/ISE/LLM 或 `score_task()`。
5. 外部 SDK 适配器应尽量放在实际消费它的领域内，如 `media/dashscope_tts.py`；不要预先建立一个容纳所有供应商的巨大 `integrations/`。
6. ORM 模型可以与直接相关的事务代码共处一个领域包，**不强制**额外加 `repositories/`、`domain/`、`application/`、`infrastructure/` 四层。如果出现真实的技术复用需求，再作局部抽取。
7. 技术 Worker 的可执行入口尽量归在所服务领域附近（如 `media/tts_worker.py`、`evaluation/worker.py`）；独立部署方式出现后再决定统一的启动脚本位置。Worker 不应成为另一个业务权威。

## 4. 现有文件的候选迁移清单（STRUCTURE-01）

此表按 2026-11-15 `main` 快照编制。**只作归属参考，真正迁移时必须重新读取最新 `main`**；如果源码职责已变化，应根据实际 import/调用关系更新迁移计划。

| 当前文件（`backend/app/` 下） | 建议归属 | 注意 |
|---|---|---|
| `main.py`、`database.py` | **保留根目录** | 稳定应用入口与数据库基础 |
| `taskbank.py` | `taskbank/models.py`（或领域内简洁等价文件） | 包名 `app.taskbank` 将与旧 `taskbank.py` 重名；迁移须整体完成 |
| `taskbank_excel.py` | `taskbank/excel.py` | T1.5 结构预检不混入 DB 事务 |
| `taskbank_import.py` | `taskbank/import_service.py` | 保持预览基线与原子提交行为 |
| `import_batch.py` | `taskbank/import_batch.py` | Alembic 元数据导入同步调整 |
| `import_preview_store.py` | `taskbank/preview_store.py` | 临时文件只服务导入预览 |
| `taskbank_storage.py` | `taskbank/image_store.py` | 资源保存与 TaskRevision 不可变引用分离 |
| `paper.py` | `papers/models.py` + `papers/drafts.py`（按实际职责拆） | 不因迁移改变发布或草稿版本语义 |
| `publication.py` | `papers/publication.py` | 发布冻结与评分版本保持不变 |
| `assessment_session.py` | `sessions/models.py` + `sessions/lifecycle.py`（按实际职责拆） | 事务锁顺序及当前 Attempt 候选不可变 |
| `submission.py` | `sessions/submission.py` | 封存 Attempt/Submission 与播放事实的原子事务不变 |
| `fixed_audio.py` | `media/fixed_audio.py` | E/F 固定 TTS 与资源不可覆盖 |
| `tts_attempt.py` | `media/tts_attempt.py` | 与固定音频 Worker 的版本/租约关联保持 |
| `tts_worker.py` | `media/tts_worker.py` | 更新 CLI、计划任务与测试入口 |
| `dashscope_tts.py` | `media/dashscope_tts.py` | 不扩大供应商接口表面 |
| `scoring_registry.py` | `scoring/registry.py` | 已发布插件身份及版本不能变化 |
| `teacher_auth.py` | `api/dependencies.py` 或 `api/teacher/auth.py` | 保持已有教师身份验证和权限边界 |
| `teacher_api.py` | `api/teacher/taskbanks.py`、`imports.py`、`audio.py` 等 | 只按当前真实 HTTP 功能拆分；路由路径、依赖覆写和响应 schema 不变 |

**不要求**每个源文件拆成两个目标文件：例如 `assessment_session.py` 保持单文件也可先放在 `sessions/core.py`，如果拆分没有降低复杂度就不拆。示意目标必须以**最少有意义改动**为准。

## 5. 前端与小程序的边界（未来开发参考）

### 5.1 教师管理前端 `frontend/`

目前新仓库还没有 `frontend/`。若采用现有熟悉的 Vue / TypeScript 技术栈，可按**实际功能**在 `features/` 下逐步建立 `taskbanks/`、`papers/`、`sessions/`、`reviews/`、`results/`、`settings/` 等模块。UI 层只负责交互、数据展示和本地表单状态；后端负责身份授权、导入事务、评分决策及历史版本冻结。

复用组件放在 `shared/` 的条件是已经存在两个以上真实调用方，或清晰的公共技术用途。不要在首个页面开发前建立空的 `hooks/`, `stores/`, `composables/`, `types/`, `constants/` 全家桶。前端具体框架、路由及状态库由其实施任务另行确定。

### 5.2 微信小程序 `miniprogram/`

按页面与实际可复用组件组织，而不是复制后端领域模型。五种题型各自拥有 UI/交互状态；共用录音上传、播放事实确认、进度查询等服务。`state/` 缓存仅用于页面恢复，**不作为 Attempt、Submission、重听次数或正式成绩的权威来源**。

儿童普通模式和高级模式可以共享页面/组件，但高级模式只是显示模式，不能绕过后端鉴权；高级模式只读取经筛选的诊断事实，不显示未固化自动分、参考答案或完整内部 Prompt。题型可执行动作必须服从后端状态投影。

### 5.3 部署、文件与私有数据

需要上线后，再根据服务器实际拓扑增加 `deploy/`（例如容器或服务管理脚本）和 `docs/operations/`。不预先承诺必须使用 Docker、Kubernetes、Celery、Redis 或对象存储。后端数据库为 PostgreSQL 16；图片、语音和缓存可采用经实际验收的可靠存储位置，但必须与题目/Session 不可变引用及权限合同一致。

**以下不得入 Git 仓库**：真实 `.env`、API 密钥、真实儿童身份资料与录音、生产数据库、日志、临时上传、音频缓存与备份。独立机器部署时，通过受控渠道配置私有环境变量，只核验必需值存在，不打印密钥。

## 6. 测试、迁移、CI 与工作流不变性

- **当前 `backend/tests/` 保留即可**。单个领域的测试继续按 `test_taskbank_*.py`、`test_session_*.py` 等命名。只有当测试数量造成定位困难时，再按领域或 `integration/` 分组；不要求先整体搬迁测试。
- 新目录应让现有 pytest 继续发现测试；移动生产代码后修订测试 import、monkeypatch 的实际对象路径以及依赖覆写入口，不保留无人使用的旧路径兼容壳。
- **Alembic 历史迁移文件不可重排/改写/重编号**；更新 `migrations/env.py` 的模型元数据导入，验证 `Base.metadata` 包含现有全部表，并检查不会生成无依据的删除或重建迁移。
- FastAPI 的现有 URL、方法、请求与响应合同、路由前缀、错误码和教师权限不变；`main.py` 仅负责真实 Router 注册，不回填业务处理细节。
- TTS Worker 的真实启动命令、模块导入路径、音频文件可追溯性与并发租约恢复保持兼容；README 的开发命令需要同步更新。
- **STRUCTURE-01 这种横跨多个模块的纯迁移 PR**：除 Ruff、现有后端 pytest 与实际 PostgreSQL 迁移检查外，应执行相关 Worker/HTTP/集成测试，确认功能等价。之后普通功能 PR 恢复轻量 CI 分层，不让每次修改都跑无关测试。
- 新前端/小程序开发完成后，才为 CI 增加其真实构建和定向测试；调整 `scripts/classify_changes.py` 的路径分类，继续维持单一稳定的 `PR result`，避免“新增前端就所有 PR 全量回归”的旧模式。

## 7. 目录实施节奏与多会话协作

### STRUCTURE-01：一次受控的既有代码归位

1. 基于**最新 `main`** 开独立分支，先核实没有会与本次移动冲突的进行中 PR。优先保护刚合并的 SESSION-02A。
2. **只移动已存在的后端模块**，按实际消费关系更新 import、Alembic、Router、TTS Worker 入口和测试；拆分 `teacher_api.py` 的现有不同业务接口。不要顺便优化业务规则或重写大段函数。
3. 尽量保留关键业务函数签名、事务实现和现有测试语义。若发现当前实现本身有缺陷，记录为单独后续任务，不混进纯目录调整。
4. 用真实 PostgreSQL 16 运行完整后端测试，并验证 Alembic、启动与主要管理接口；`git diff --check`、Ruff 与 PR CI 正常。
5. Codex 普通提交并推送；GPT 远端审计**变更性质确实为搬迁**、测试及关键接口行为后创建 PR；用户检查 CI 手动合并。不要自动监控、自动合并或部署。

### 后续会话的文件所有权原则

- 题库会话优先维护 `taskbank/` 及对应教师题库/导入 API。
- 试卷会话维护 `papers/`、发布与固定音频资源业务；底层 TTS 技术可与 `media/` 会话协调。
- Session 会话维护 `sessions/`，各题型会话实现自己的 `task_types/` Handler，但**Submission 权威事务仍属于 sessions**。
- 评测/Worker 会话维护 `evaluation/` 和来源适配，评分/复核会话维护 `scoring/`。
- 教师前端与小程序分别维护 `frontend/` 和 `miniprogram/`；公共 API 变更须先与后端负责人对齐。

这只是默认归属，不禁止合法跨模块 PR。只要会影响共享实体、字段、状态、契约或目录迁移，就**先协调，再实施**；不要两个 Codex 分支各自独立重构同一文件。

## 8. 新开发任务的最小自检

开发者或新会话在新增文件前回答：

1. 这份代码解决的是哪个**已经明确存在**的业务或技术问题？哪个模块是它的权威所有者？
2. 能否直接放在该领域现有文件中，还是确有复用/复杂度理由需要新文件？
3. 是否跨越提交、重做、关闭、评分或评价来源等业务边界？若跨越，先核对 C1.0 与专项设计。
4. 是否新增了不会在当前 PR 实际使用的接口、状态、配置、基类或兜底？若是，删掉。
5. 是否改变了对外 API、历史数据库迁移、音频入口、公开数据投影或 CI 检查？若改变，不能以“只整理目录”申报。

**总原则：按领域聚合，按实际职责拆分；可移而不重写，可简而不牺牲数据一致性。**
