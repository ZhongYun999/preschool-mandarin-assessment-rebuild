# VNext 设计文档索引（第一次仓库基线）

## 阅读顺序与权威边界

1. `../AGENTS.md`：代码、Git 和安全协作边界。
2. `../VNEXT_INITIAL_ARCHITECTURE_DESIGN.md`：总体架构**早期初稿**。其中已被后续用户确认的决策修正的表述，不再直接作为最新实现依据。
3. 本目录各专题最新版本。**专题新版本只覆盖同一主题中已明确确认的旧定义**；遇到真正的业务原则冲突应明确列出，由用户决策，不得自行任选。

本仓库首次初始化只收录最新专项文档；旧版 T1.0–T1.3、P1.0、SW1.0、Speaking 初稿等暂不提交，以降低误用。历史原稿仍在原会话中。

## 当前收录的九份专项设计

| 范围 | 现行稿件 | 主要边界 |
|---|---|---|
| 题库与 Excel | [T1.4](design/VNEXT_TASKBANK_VERSION_IMPORT_DESIGN_T1.4.md) | TaskIdentity/不可变 TaskRevision，16 列 Excel |
| 试卷与冻结 | [P1.1](design/VNEXT_PAPER_PUBLICATION_SCORING_DESIGN_P1.1.md) | 复制原样继承历史版本，发布后不可变 |
| Speaking | [v1.1](design/VNEXT_SPEAKING_STATE_MACHINE_DESIGN_v1.1.md) | 求助、录音、提交与恢复 |
| Dialogue | [v1.0](design/VNEXT_DIALOGUE_STATE_MACHINE_DESIGN_v1.0.md) | 逐轮问答、独立播放与提交 |
| 简单题型 | [ST1.0](design/VNEXT_SIMPLE_TASK_STATE_MACHINES_ST1.0.md) | 选择/跟读/问答与主动重听事实 |
| 原始评价 | [R1.0](design/VNEXT_RAW_EVALUATION_CONTRACT_R1.0.md) | 原始证据与转换分分离，ISE/LLM 独立字段 |
| 评分与复核 | [S1.0](design/VNEXT_SCORING_PLUGIN_DUAL_STAGE_DESIGN_S1.0.md) | `score_task()` 单题折算，`score_paper()` 可反复汇总 |
| 按需评测 | [E1.0](design/VNEXT_EVALUATION_REQUIREMENTS_SCHEDULING_E1.0.md) | 插件声明 ISE/LLM 需求，未声明不调用收费服务 |
| Session / Worker | [SW1.1](design/VNEXT_SESSION_WORKER_LIFECYCLE_SW1.1.md) | 重做失效、输入关闭不可撤回、抽样复核及可靠作业 |

## 已确认的新决策：纠正早期文档中的旧表述

- **显式重做：**一旦新 Attempt 已开始，旧 Submission 退出该题当前有效候选；新 Attempt 未正式提交就视为未完成。不能根据早期总体报告“保留旧有效提交”的表述做兜底；以 SW1.1 与题型专项为准。
- **结算输入关闭：**`INPUT_CLOSED` 成功后，不存在恢复儿童操作或撤回的正常业务路径；后台可以继续评分、复核和正式结果固化。
- **评分插件双阶段：**插件不是一个同时执行所有步骤的单一 `calculate()`；单题转换结果先落库，人工复核只覆盖采用分，整卷汇总算法允许再次运行。详见 S1.0。
- **评测源按需调度：**评分插件声明所需原始评价；不需要开放题 ISE 时，不应产生 ISE 请求；旧系统的普通话规范评测旁路不能擅自触发。详见 E1.0。
- **选择性复核：**仅复核策略命中或经规则强制命中的题目进入队列；比例在 Session 创建时冻结。详见 SW1.1。
- **题库字段：**采用 T1.4 的 16 列方案，O“评分说明”是 LLM 评分指令素材，不是只读教师备注。

## 开发前尚需集中审计的接口/业务缺口

以下是提醒，不是授权开发者单方面定案。

1. 跨模块实体身份与引用：`TaskRevision → PaperEntry → SessionTask → Attempt → Submission → RawEvaluation → TaskScore → PaperScore`，以及 EvaluationPlan 的冻结引用关系。
2. 不同状态机对公共播放成功回执、轮次事实、显式重做和终止后拒绝写入的语义是否一致。
3. 评分插件版本、原始评价合同版本、可选评测源失败时的成绩生成资格；在任何情况下均不能把缺失冒充零分。
4. 正式结果的原子可见性、复核修订后整卷算法再计算的版本控制与审计字段。
5. 普通新建试卷草稿遇到题库新 revision 时是否自动更新、已发布且无引用的试卷是否允许物理删除等尚未确认细节。
6. 需要开放题 ISE 时的参考文本/类别策略尚需供应商能力验证，不得直接假设 ASR 文本回填 ISE 合法可靠。

## 首次推送完成标准

- 文件清单仅为协作约束、总体设计、九份最新专项、索引、README、`.gitignore`。
- 不添加示例真实音频、儿童个人信息、Excel 原始样卷、私有 `.env`、生产数据库、日志、API 密钥、旧仓库源码。
- 校验所有文档链接、文件名、编码、`git diff --check`。
- 不将本次文档推送说成业务模块已完成，更不执行生产部署。
