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

## PR 检查

PR 工作流始终运行文档检查及稳定的 `PR result` 汇总检查。纯 Markdown 修改不安装后端依赖；其他文件（包括依赖、迁移和 CI 配置）保守触发 Ruff、pytest 与 PostgreSQL 迁移检查。未来增加前端或其他模块时应扩展变更分类和对应任务；当前没有空任务。CI 不调用外部付费服务，也不运行全量端到端回归。
