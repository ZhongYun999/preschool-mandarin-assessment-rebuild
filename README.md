# 学前儿童普通话测评系统 VNext（重构版）

独立于旧系统的重新设计项目，当前包含设计基线和后端工程骨架，尚未实现业务功能。

旧仓库 `ZhongYun999/preschool-mandarin-assessment` 仅用于只读业务对照；不得把生产旧系统数据、密钥或历史兼容约束未经确认复制到新仓库。

## 从哪里开始

- [项目协作约束](AGENTS.md)：编码、仓库操作与公开仓库安全要求。
- [总体架构初稿](VNEXT_INITIAL_ARCHITECTURE_DESIGN.md)：早期目标及核心概念。
- [现行专项设计索引](docs/DESIGN_INDEX.md)：**实施前必须检查此索引的版本、已更新决策与待解决冲突**。

## 当前阶段

- 已讨论题库/试卷、五种题型状态机、原始评价、评分插件与复核、Session 和后台调度的业务合同。
- 现有设计文档包含【确定】、【设计建议】和【待确认】条目，不得把全部文字等同于已获批准的实现约束。
- 下一步：先进行跨模块合同一致性审计，冻结最小数据身份与接口约定，然后按模块实现、测试、PR 审核。

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

访问 `http://127.0.0.1:8000/health` 应返回 `{"status":"ok"}`。该接口仅确认应用进程可响应，不表示数据库就绪。迁移只建立 Alembic 版本记录，不创建业务表。

在另一个已激活虚拟环境的终端，从仓库根目录执行：

```powershell
ruff check backend
python -m pytest backend/tests
python scripts/check_docs.py
python -m unittest discover -s scripts -p 'test_*.py'
```

迁移测试在 `TEST_DATABASE_URL` 指定的数据库中建立随机临时 schema，测试完成后清理；未设置该变量会明确失败。新建迁移可用 `alembic -c backend/alembic.ini revision -m "说明"`，执行迁移使用上面的 `upgrade head`。迁移前应检查目标数据库，现阶段没有业务迁移。

## Excel 模板与预检

`app.taskbank_excel.create_template()` 返回 T1.5 模板的 XLSX 字节；`preflight_xlsx(data)` 读取 XLSX 字节，返回行号、题号、`RevisionContent`、按槽位分组的图片字节及 SHA-256 和带行列位置的问题列表。消费方应先检查 `Preview.valid`；存在错误时不能把部分解析结果当作可提交批次。相同内容与排序的重复题号合并并给出警告，冲突会报错。本阶段不访问数据库、不保存图片，也不判断题目在题库中是新增还是更新。

### 题库导入服务

`preview_import(session, bank_id, xlsx_bytes)` 只读解析并比较题库，返回每题变化及提交基线；预览包含错误时不能提交。`commit_import(engine, ImageStore(root), xlsx_bytes, preview, operation_id)` 先按内容摘要可靠保存图片，再在同一数据库事务中校验基线、写入题目及导入审计。操作 ID 重试返回原结果，输入变更或基线过期会报错。`ImageStore()` 默认使用仓库内被 Git 忽略的 `uploads/taskbank`；可以传入其他本地路径，部署时应确保路径位于仓库外或被 Git 忽略，并保持配置稳定。数据库回滚后可能保留未引用图片，后续可清理。

## 教师题库管理 API

设置私有环境变量 `TEACHER_MANAGEMENT_TOKEN` 后，使用 `Authorization: Bearer <token>` 访问 `/teacher` 下的题库管理、题目历史、图片、模板和导入报告接口。未配置 Token 时管理接口不可用；不要把 Token 放入 URL、日志或仓库。此临时共享凭据授予全部教师管理接口访问权，后续可替换为正式身份体系；`/health` 仅返回服务存活状态。

设置 `TASKBANK_ASSET_ROOT` 可以指定 DATA-02B 使用的图片根目录，必须与导入服务保持一致；不配置时使用被 Git 忽略的 `uploads/taskbank`。图片按题库、题目修订和槽位读取并校验内容摘要，接口不返回本地文件路径。题目列表的 `content_summary` 为题干前 120 个字符，查询支持题号子串、学段、题型、`offset` 和 `limit`。

### HTTP 两阶段 Excel 导入

教师凭据适用于所有接口。向 `POST /teacher/banks/{bank_id}/import-previews` 以 multipart `file` 上传 XLSX；有效预览返回 `preview_id`、行级变化及问题，错误文件返回 422 和行列信息。可通过 `GET /teacher/banks/{bank_id}/import-previews/{preview_id}` 恢复预览；确认时向对应的 `/confirm` 发送 JSON `{"operation_id":"调用方稳定生成的标识"}`。确认返回既有导入报告；基线过期和操作 ID 冲突分别以 409 及不同错误代码返回。

原始 XLSX 与预览基线暂存在 `TASKBANK_PREVIEW_ROOT` 指定的私有持久目录，默认 `uploads/import-previews`，被 Git 忽略；配置目录应在所有服务实例间保持一致并限制操作系统访问权限。预览不会自动过期或清理，以支持重启与重试；后续需要运维安排清理，但不要删除仍需确认或重试的预览。

## PR 检查

PR 工作流始终运行文档检查及稳定的 `PR result` 汇总检查。纯 Markdown 修改不安装后端依赖；其他文件（包括依赖、迁移和 CI 配置）保守触发 Ruff、pytest 与 PostgreSQL 迁移检查。未来增加前端或其他模块时应扩展变更分类和对应任务；当前没有空任务。CI 不调用外部付费服务，也不运行全量端到端回归。

## 固定语音存储

固定语音对象默认保存在 Git 忽略的 `uploads/fixed-audio`，可通过 `FIXED_AUDIO_ROOT` 指定私有目录。发布时调用方必须显式提供供应商、模型、音色、版本和语速；本阶段没有生产 TTS 默认配置。服务只冻结请求，音频由后续注入的合成器准备；未生成或文件校验失败时不可用于新 Session。

DashScope 固定音频 Worker 使用私有环境变量 `DASHSCOPE_API_KEY`，启动命令为 `python -m app.tts_worker`（从 `backend` 目录执行）。发布调用应显式冻结 `provider=dashscope`、模型、音色、`version=cosyvoice-sdk-v2`、语速、`format=wav` 和 `sample_rate`；可选地域和业务空间也必须由调用方明确提供。本阶段只支持 WAV，其他配置会明确拒绝，不会静默忽略。合成不会修改冻结的原文。

教师管理接口 `GET /teacher/papers/{paper_id}/audio` 查询任务和诊断，`POST /teacher/papers/{paper_id}/audio/prepare` 确认已由 Worker 扫描，`POST /teacher/papers/{paper_id}/audio/{audio_id}/retry` 授权重试，`GET /teacher/papers/{paper_id}/audio/{audio_id}/content` 校验后试听。所有接口需要教师 Bearer 凭据。外呼结果不明确时需在重试请求中明确传入 `accept_unknown=true`，因为可能已经产生费用；不存在外部调用恰好一次的保证。已完成音频的文件损坏或丢失会阻止使用，不会自动重合成或覆盖历史资源。
