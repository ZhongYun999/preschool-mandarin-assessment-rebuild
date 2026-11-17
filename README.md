# 学前儿童普通话测评系统 VNext（重构版）

独立于旧系统的重新设计项目，已实现题库、试卷发布、固定语音及 Session 作答和封卷的后端基础功能。

旧仓库 `ZhongYun999/preschool-mandarin-assessment` 仅用于只读业务对照；不得把生产旧系统数据、密钥或历史兼容约束未经确认复制到新仓库。

## 从哪里开始

- [项目协作约束](AGENTS.md)：编码、仓库操作与公开仓库安全要求。
- [总体架构初稿](VNEXT_INITIAL_ARCHITECTURE_DESIGN.md)：早期目标及核心概念。
- [现行专项设计索引](docs/DESIGN_INDEX.md)：**实施前必须检查此索引的版本、已更新决策与待解决冲突**。
- [项目目录与模块归属约定](docs/DEVELOPMENT_STRUCTURE.md)：后端领域归属与后续开发参考。

## 当前阶段

- 已讨论题库/试卷、五种题型状态机、原始评价、评分插件与复核、Session 和后台调度的业务合同。
- 现有设计文档包含【确定】、【设计建议】和【待确认】条目，不得把全部文字等同于已获批准的实现约束。
- 后续按已确认合同继续实现题型、评价、评分和正式结果，并逐项测试及审核。

## 本地开发

需要 Python 3.12 或 3.13、PostgreSQL 16，以及可访问的本地开发数据库和独立测试数据库。使用 PostgreSQL 是为了后续事务和并发裁决与实际运行环境一致；当前不维护 SQLite 等第二套数据库实现。数据库和账号需自行创建，测试账号需要在测试数据库内创建和删除临时 schema 的权限。不得将生产数据库作为测试目标。

在仓库根目录（PowerShell）：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e 'backend[dev]'
Copy-Item backend/.env.example backend/.env
```

编辑 `backend/.env`，填写本地 `DATABASE_URL` 和独立的 `TEST_DATABASE_URL`；也可直接通过环境变量配置。环境变量优先，配置文件不提交。然后运行：

```powershell
alembic -c backend/alembic.ini upgrade head
uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

访问 `http://127.0.0.1:8000/health` 应返回 `{"status":"ok"}`。该接口仅确认应用进程可响应，不表示数据库就绪。迁移会创建和更新业务表及其约束。

在另一个已激活虚拟环境的终端，从仓库根目录执行：

```powershell
ruff check backend
python -m pytest backend/tests
python scripts/check_docs.py
python -m unittest discover -s scripts -p 'test_*.py'
```

迁移测试在 `TEST_DATABASE_URL` 指定的数据库中建立随机临时 schema，测试完成后清理；未设置该变量会明确失败。新建迁移可用 `alembic -c backend/alembic.ini revision -m "说明"`，执行迁移使用上面的 `upgrade head`。迁移前应检查目标数据库。

## Excel 模板与预检

`app.taskbank.excel.create_template()` 返回 T1.5 模板的 XLSX 字节；`preflight_xlsx(data)` 读取 XLSX 字节，返回行号、题号、`RevisionContent`、按槽位分组的图片字节及 SHA-256 和带行列位置的问题列表。消费方应先检查 `Preview.valid`；存在错误时不能把部分解析结果当作可提交批次。相同内容与排序的重复题号合并并给出警告，冲突会报错。本阶段不访问数据库、不保存图片，也不判断题目在题库中是新增还是更新。

### 题库导入服务

`preview_import(session, bank_id, xlsx_bytes)` 只读解析并比较题库，返回每题变化及提交基线；预览包含错误时不能提交。`commit_import(engine, ImageStore(root), xlsx_bytes, preview, operation_id)` 先按内容摘要可靠保存图片，再在同一数据库事务中校验基线、写入题目及导入审计。操作 ID 重试返回原结果，输入变更或基线过期会报错。`ImageStore()` 默认使用仓库内被 Git 忽略的 `uploads/taskbank`；可以传入其他本地路径，部署时应确保路径位于仓库外或被 Git 忽略，并保持配置稳定。数据库回滚后可能保留未引用图片，后续可清理。

## 教师题库管理 API

设置私有环境变量 `TEACHER_MANAGEMENT_TOKEN` 后，使用 `Authorization: Bearer <token>` 访问 `/teacher` 下的题库管理、题目历史、图片、模板和导入报告接口。未配置 Token 时管理接口不可用；不要把 Token 放入 URL、日志或仓库。此临时共享凭据授予全部教师管理接口访问权，后续可替换为正式身份体系；`/health` 仅返回服务存活状态。

设置 `TASKBANK_ASSET_ROOT` 可以指定 DATA-02B 使用的图片根目录，必须与导入服务保持一致；不配置时使用被 Git 忽略的 `uploads/taskbank`。图片按题库、题目修订和槽位读取并校验内容摘要，接口不返回本地文件路径。题目列表的 `content_summary` 为题干前 120 个字符，查询支持题号子串、学段、题型、`offset` 和 `limit`。

### HTTP 两阶段 Excel 导入

共享管理凭据适用于以下题库管理接口。向 `POST /teacher/banks/{bank_id}/import-previews` 以 multipart `file` 上传 XLSX；有效预览返回 `preview_id`、行级变化及问题，错误文件返回 422 和行列信息。可通过 `GET /teacher/banks/{bank_id}/import-previews/{preview_id}` 恢复预览；确认时向对应的 `/confirm` 发送 JSON `{"operation_id":"调用方稳定生成的标识"}`。确认返回既有导入报告；基线过期和操作 ID 冲突分别以 409 及不同错误代码返回。

原始 XLSX 与预览基线暂存在 `TASKBANK_PREVIEW_ROOT` 指定的私有持久目录，默认 `uploads/import-previews`，被 Git 忽略；配置目录应在所有服务实例间保持一致并限制操作系统访问权限。预览不会自动过期或清理，以支持重启与重试；后续需要运维安排清理，但不要删除仍需确认或重试的预览。

## PR 检查

PR 工作流始终运行文档检查及稳定的 `PR result` 汇总检查。纯 Markdown 修改不安装后端依赖；其他文件（包括依赖、迁移和 CI 配置）保守触发 Ruff、pytest 与 PostgreSQL 迁移检查。未来增加前端或其他模块时应扩展变更分类和对应任务；当前没有空任务。CI 不调用外部付费服务，也不运行全量端到端回归。

## 固定语音存储

固定语音对象默认保存在 Git 忽略的 `uploads/fixed-audio`，可通过 `FIXED_AUDIO_ROOT` 指定私有目录。发布时调用方必须显式提供供应商、模型、音色、版本和语速；本阶段没有生产 TTS 默认配置。服务只冻结请求，音频由后续注入的合成器准备；未生成或文件校验失败时不可用于新 Session。

DashScope 固定音频 Worker 使用私有环境变量 `DASHSCOPE_API_KEY`，启动命令为 `python -m app.media.tts_worker`（从 `backend` 目录执行）。发布调用应显式冻结 `provider=dashscope`、模型、音色、`version=cosyvoice-sdk-v2`、语速、`format=wav` 和 `sample_rate`；可选地域和业务空间也必须由调用方明确提供。本阶段只支持 WAV，其他配置会明确拒绝，不会静默忽略。合成不会修改冻结的原文。

教师管理接口 `GET /teacher/papers/{paper_id}/audio` 查询任务和诊断，`POST /teacher/papers/{paper_id}/audio/prepare` 返回等待独立 Worker 扫描的提示；202 不表示已扫描、已开始生成或已立即排队。`POST /teacher/papers/{paper_id}/audio/{audio_id}/retry` 授权重试，`GET /teacher/papers/{paper_id}/audio/{audio_id}/content` 校验后试听。这些音频管理接口需要共享管理 Bearer 凭据。外呼结果不明确时需在重试请求中明确传入 `accept_unknown=true`，因为可能已经产生费用；不存在外部调用恰好一次的保证。已完成音频的文件损坏或丢失会阻止使用，不会自动重合成或覆盖历史资源。

## 评价调度入口

`app.evaluation.worker.run_once()` 执行一轮有界调度，调用方需提供数据库 engine、评价合同 Registry、适配器映射、租约时长和批次大小，并按部署需要重复调用。当前没有生产评价适配器；空映射不会产生成功评价。已被重做取代且尚未跨过外发边界的作业会标记为 `SKIPPED`；跨过外发边界后的旧调用可能仍产生费用并保留历史结果。外呼结果不明的作业必须显式授权后才可重试。

## 结算扫描入口

`app.scoring.worker.run_settlement_once(engine, registry=registry, batch_size=100)` 推进数据库中有 `SettlementIntent` 和 `CloseManifest` 且状态为 `INPUT_CLOSED` 的 Session。`batch_size` 是单次查询的分页大小：每轮依次遍历符合条件的场次，逐场使用独立的 `READ COMMITTED` 事务；等待中的场次不会永久占住分页，已锁定场次留待下一轮。返回值包含 `completed`、`pending`、`skipped_locked` 和 `failures`；调用方须检查失败项。失败项包括 Session 身份、异常类型和代码位置，不包含可能敏感的异常内容。历史复核策略不能解释时，`pending` 中提供策略诊断，其他场次仍继续处理。

调用方按需要重复执行评价 Worker 和结算扫描：评价 Worker 根据冻结 `EvaluationPlan` 调度必要来源并持久化 `RawEvaluation`；结算 Worker 不调用外部评价服务，而是使用已保存的证据、单题分及复核事实，推进自动评分和正式固化。必要评价或复核未完成时仍为 `INPUT_CLOSED`；`COMPLETED` 表示正式结果已固化。教师最后一次有效复核也可在复核事务中触发固化。重启后可重新调用扫描入口，已完成结果不会被重算。

当前生产 Registry 尚无正式评分插件，也没有正式评价适配器；本轮验证使用受控测试插件和适配器，仅证明受控环境中的业务闭环，不代表生产上线验收。此入口是供调用方执行的一轮扫描，不包含常驻进程或生产定时任务。

### 教师复核凭据

题库及资源管理继续使用私有 `TEACHER_MANAGEMENT_TOKEN`。复核写入使用独立的个人高熵随机 Bearer Token；服务端私有环境变量 `TEACHER_REVIEW_TOKEN_HASHES` 保存 JSON 对象，键为稳定教师身份，值为对应 Token 的 SHA-256 十六进制摘要。配置结构示例：`{"teacher-id-a":"<sha256-hex>","teacher-id-b":"<sha256-hex>"}`；占位内容不能直接用于认证。每位教师使用不同 Token，任何摘要不得重复，也不得与共享管理 Token 对应的摘要相同。通过安全渠道生成、分发和轮换凭据，不将原始 Token 或摘要提交仓库、写入日志或 URL。

个人复核 Token 可访问教师评分查询及复核决定接口，不能访问题库或资源管理接口；共享管理 Token 可查询但不能作为复核操作人的身份。未配置或配置无效时复核写入失败关闭，不影响共享管理 Token 的既有管理访问。当前凭据不提供班级、学校或组织级细粒度授权，生产评分 Registry 尚无正式插件。

### 施测 Session 与访问凭据

共享管理凭据可调用 `POST /teacher/assessment/sessions`，提交 `paper_id`、稳定的 `create_request_id` 和 V1 `review_policy` 创建 Session。重复创建请求返回同一 Session，不签发或改变访问凭据；不同参数复用请求标识会冲突。新 Session 必须使用可解释的 V1 复核策略，既有历史策略的幂等重传保持原样。

管理方调用 `POST /teacher/assessment/sessions/{session_id}/access-token` 明确签发高熵 Session Bearer Token。明文只在这次响应中返回，数据库仅保存摘要，响应禁止缓存；通过受控渠道将 Token 交给施测端，不将其写入 URL、日志或仓库。每次调用都是**追加签发**：之前的 Token 继续有效，不隐式轮换或限制设备数量。若响应丢失或 Token 遗失，管理方再次签发新的 Token；数据库无法恢复旧明文。签发响应包含 `credential_id`，用于管理该凭据。

凭据泄漏时，管理方使用共享管理凭据调用 `GET /teacher/assessment/sessions/{session_id}/access-tokens` 查询凭据身份、签发与撤销时间和有效状态；响应不包含 Token 或摘要。确认单个泄漏凭据时，调用 `POST /teacher/assessment/sessions/{session_id}/access-tokens/{credential_id}/revoke` 撤销；无法识别泄漏凭据时，调用 `POST /teacher/assessment/sessions/{session_id}/access-tokens/revoke-all` 撤销该场所有当前有效凭据。两种撤销均可重复调用。随后可显式追加签发新 Token，并通过安全渠道交付给施测端。

撤销只改变后续访问资格，不重置 Session 或作答进度；新凭据可恢复原有 Attempt 与 Submission。撤销不会取消已经完成认证并开始读取的在途只读请求，也无法删除客户端下载的资源。不要将 Token 或摘要写入 URL、日志或仓库。

施测端使用对应 Token 调用 `GET /assessment/sessions/{session_id}`，返回当前 Attempt、是否有当前 Submission、可用业务动作及关闭／正式成绩状态。Session Token 不能用于其他 Session 或教师管理接口。所有施测请求（包括音频和图片）通过 `Authorization: Bearer <token>` 传递凭据，不使用 URL 查询参数；响应中的资源路径也需要携带同一凭据。

选择题施测接口以 `/assessment/sessions/{session_id}` 为前缀：

- `GET /tasks/{session_task_id}/content` 返回冻结题干、中性 A/B/C 选项、图片与音频资源路径；`GET` 相应路径读取经摘要校验的二进制内容。
- `GET /tasks/{session_task_id}/attempt` 恢复当前 Attempt 和播放事实；`POST` 同一路径开始或返回当前 Attempt，不会隐式重做。
- `POST /tasks/{session_task_id}/playbacks` 接受 `attempt_id`、稳定 `request_id`、`kind`（`GUIDE`、`INITIAL_PROMPT`、`USER_REPLAY` 或 `TECHNICAL_RETRY`）；技术重试携带原播放的 `retry_of_id`。`POST /tasks/{session_task_id}/playbacks/{playback_id}/finish` 接受 `attempt_id` 和布尔值 `completed`，明确报告完成或失败。刷新后应查询既有播放事实，不自动确认成功。
- `POST /tasks/{session_task_id}/submissions/instruction` 接受 `attempt_id`、稳定 `submit_request_id` 和 `selected_choice_id`（A/B/C）；返回 `ACCEPTED` 只表示正式接收，成绩尚未必生成。
- `POST /tasks/{session_task_id}/redo` 接受稳定 `redo_request_id`，显式切换当前 Attempt。旧播放正在进行时也可重做，客户端应停止旧音频；旧回执不能改变新 Attempt。
- `POST /close` 接受稳定 `close_request_id`；未完成时返回 409 和缺失题目，成功返回 `INPUT_CLOSED` 与 Manifest 身份，后续评分异步进行。

结构非法返回 422，凭据无效返回 401，题目或资源不存在返回 404，状态或幂等请求冲突返回 409。写操作的请求标识在网络重试时应保持不变。

### 跟读与问答录音及正式提交

跟读题和问答题通过同一冻结题目和播放接口取得题干与 GUIDE／INITIAL_PROMPT／USER_REPLAY 事实，并共用以下录音接口。`POST /tasks/{task_id}/recordings` 使用 `attempt_id` 与稳定 `request_id` 开始录音；`GET /tasks/{task_id}/recordings` 恢复当前 Attempt 的录音状态，`POST /tasks/{task_id}/recordings/{recording_id}/cancel` 使用 `attempt_id` 取消尚未完成的操作。取消后可用新请求身份重新录制；显式整题重做会放弃旧 Attempt，旧上传不能作用于新 Attempt。

完成录音后，以原始二进制请求体向 `POST /tasks/{task_id}/recordings/{recording_id}/content` 上传，使用 `X-Attempt-Id` 标识 Attempt、`Content-Type: audio/wav` 等 `audio/*` 类型标识客户端实际媒体格式，并携带 Session Bearer Token。当前服务记录声明的媒体类型并校验非空内容与 SHA-256，尚未验证各客户端编码与后续评价供应商的兼容性，不将 WAV 固定为唯一格式。同一录音操作的相同字节与媒体类型可以重传；不同内容或类型会冲突。已保存音频可通过返回的受保护内容路径读取；上传完成不等于正式提交。

跟读题使用 `POST /tasks/{task_id}/submissions/repetition`，问答题使用 `POST /tasks/{task_id}/submissions/qa`；两者均接受 `attempt_id`、`recording_id`、稳定 `submit_request_id`，验证各自题型、当前 Attempt、播放事实和录音文件后正式提交。播放和录音互斥；提交后可继续下一题，无须等待评价或转写。封卷后不接受新的作答或提交；已接受的相同提交请求仍可幂等重传。Session 查询及录音查询可恢复服务端权威状态。

录音默认保存在私有 `uploads/recordings`，可通过 `RECORDING_ROOT` 指向受操作系统权限保护的私有持久目录，所有服务和评价 Worker 必须访问同一目录；不可公开静态托管。文件按内容摘要不可覆盖保存，数据库只记录引用及摘要；保存成功但数据库事务失败时可能留下未引用文件，当前没有自动清理。必要评价适配器根据 Submission 的录音身份核对所属 Attempt、读取并校验私有录音，再使用冻结题目版本和评价合同；未声明的评价来源不会调度。本轮不包含真实 ISE 适配器或生产媒体兼容性验收。

问答题的 ASR、文本评价和声学评价各自遵循试卷冻结的需求，不因题型自动启用 ISE。评价需求可通过 `depends_on: {"requirement_index": 2, "source": "<asr-source>", "version": "<asr-version>"}` 显式引用同一计划中的一项 ASR 需求；索引只是示例，发布预检会校验索引及来源版本，且该上游必须是独立的识别合同。未声明依赖的来源仍可独立运行。

识别合同的 `recognition_result` 必须是确定性的纯解析函数，将实际原始结果解析为 `{"status":"TEXT","text":"..."}` 或 `{"status":"NO_TEXT","text":null}`；下游合同以 `accepts_no_text` 明确声明是否接受后者。未就绪的上游及不接受的无可信文本使下游保持等待，不生成虚构文本或零分。Worker 在领取下游时校验同一 Submission、冻结题目与录音身份，将上游 Job、RawEvaluation、合同版本和输入指纹写入输入引用，并把识别状态和文本纳入输入指纹；适配器分别通过 `InputSnapshot.refs["asr"]` 和 `InputSnapshot.payload["asr"]` 读取上游身份与识别状态。重试不得更换已冻结输入；身份冲突会拒绝领取并在等待作业中记录 `INPUT_IDENTITY_CONFLICT` 诊断。

ASR 适配器可用 `recording_input()` 取得所属录音身份、摘要、存储引用及声明的媒体类型，并在事务外读取、校验实际文件。媒体类型声明不代表编码、采样率或声道已通过验证；真实供应商适配器须按其合同验证。当前未接入正式 ASR／LLM 适配器，也没有常驻调度服务；受控测试音频不能代表生产兼容性。该依赖能力仅处理显式声明的识别上游，不是通用工作流引擎。

### 录音技术检查与按需转换

原始录音是不可变作答证据，继续按上传时的摘要保存在 `RECORDING_ROOT`；上传和正式提交不依赖音频工具。后台适配器可在 `resolve_inputs()` 事务中仅调用 `recording_input()`，将录音 ID、摘要、声明媒体类型，以及其评价合同要求的 `AudioSpec`（目标 WAV 编码、采样率、声道、处理版本和 FFmpeg/FFprobe 版本）放入冻结输入。领取后，在 `evaluate()` 的事务外调用 `prepare_audio()`；适配器须把返回的 `PreparedAudio` 来源信息保存到自己的 RawEvaluation 合同中，并把 `AudioError.code` 映射为 Worker 的 `KnownFailure`。处理失败只产生技术失败，不代表 ASR 无文本、零分或儿童须重新录音。处理成功本身也不是评价结果。

`AudioTools(ffmpeg=..., ffprobe=..., timeout=..., max_input_bytes=..., max_decoded_bytes=...)` 由服务端可信配置构造；默认从 PATH 查找 `ffmpeg` 和 `ffprobe`，默认超时 60 秒、输入及解码输出技术预算各 1 GiB，部署可调整这些技术预算。工具版本可通过 `tools.version()` 和 `tools.version(probe=True)` 获取；必须与冻结规格一致，升级工具应发布新处理版本并保留旧版本执行环境。调用方自行提供受保护的独立 `RecordingStore` 派生目录，所有相关 Worker 必须能访问相同的原始和派生目录；禁止公开托管。派生副本以内容摘要寻址，另以原始摘要、目标规格和工具版本索引；复用时重新校验摘要及解码结果。原始录音不会被覆盖或删除，也没有自动清理。

检测以私有录音字节而非声明 MIME 或扩展名为准，使用 FFprobe 检查真实容器和音频流，再通过 FFmpeg 完整解码验证；报告已解码采样时长，并单独保留容器报告的时长（若可得）。当前目标转换支持调用方指定采样率及声道的 WAV PCM S16LE／S24LE；匹配且解码有效时直接复用原始文件。FFmpeg 通过仅允许 `pipe` 输入的子进程接收私有副本，不允许音频引用网络资源。需要随机访问的部分容器可能无法经管道处理，会返回技术失败；客户端声明、人工构造的 WAV/MP3 和本地 FFmpeg 测试不能证明真实设备及供应商兼容性。尚未接入正式 ASR、ISE 或 LLM 适配器。
