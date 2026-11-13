# VNext 设计文档索引（现行设计）

## 阅读顺序与权威边界

1. `../AGENTS.md`：代码、Git 和安全协作边界。
2. `../VNEXT_INITIAL_ARCHITECTURE_DESIGN.md`：总体架构**早期初稿**。其中已被后续用户确认的决策修正的表述，不再直接作为最新实现依据。
3. 本目录各专题最新版本。**专题新版本只覆盖同一主题中已明确确认的旧定义**；遇到真正的业务原则冲突应明确列出，由用户决策，不得自行任选。

本仓库保留现行专项文档。旧版 T1.0–T1.4、P1.0、SW1.0、Speaking 初稿等不作为新业务实现依据；历史资料可从原会话追溯。`PROBLEMS.md` 是历史故障/回归清单，其中的旧补救措施若与后续已确认专项冲突，以专项设计为准。

## 当前收录的九份专项设计

| 范围 | 现行稿件 | 主要边界 |
|---|---|---|
| 题库与 Excel | [T1.5](design/VNEXT_TASKBANK_VERSION_IMPORT_DESIGN_T1.5.md) | TaskIdentity/不可变 TaskRevision，15 列 Excel；选择题正确选项并入参考答案 |
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
- **题库字段（T1.5）：**Excel 为 **A:O 共 15 列**；H/I/J 选项图片、K 题目图片、L 材料说明、M 参考答案、N 评分说明、O AI 追问范围。选择题在 M 写 A/B/C，不再保存第二份权威正确选项；N 为题目级 LLM 原始评分指令素材。
- **TTS 消费：**发布时只准备 E（可空）和 F 固定语音，绝不朗读 M 参考答案；Dialogue 首次完整播放 F。旧版本模板及图片位置必须通过列名与模板版本显式迁移。
- **运行时动态 Prompt：**Speaking／Dialogue 每次新引导操作直接读取当前有效系统级 Prompt；已生成并保存的问题不因其后变化而重写。不为管理员在测评期间修改 Prompt 增设复杂兼容层；正式评分 Prompt/插件仍遵守发布冻结合同。
- **ISE 与重做：**ISE 拒识不抹除已接受 Submission；Session 仍 IN_PROGRESS 时高级模式可明确整题重做；INPUT_CLOSED 后禁止回退或新增 Attempt，必要时新建 Session。
- **高级模式投影：**可在受控诊断接口展示 LLM 实际问题/引导、ASR、评价维度与失败状态；不传 M 参考答案、N 评分说明、正确选项、完整内部 Prompt 或未经筛选的供应商原始数据。高级模式仅是 UI 展示，不自动授予后台权限。

## 开发前尚需集中审计的接口/业务缺口

以下是提醒，不是授权开发者单方面定案。

1. 跨模块实体身份与引用：`TaskRevision → PaperEntry → SessionTask → Attempt → Submission → RawEvaluation → TaskScore → PaperScore`，以及 EvaluationPlan 的冻结引用关系。
2. 不同状态机对公共播放成功回执、轮次事实、显式重做和终止后拒绝写入的语义是否一致。
3. 评分插件版本、原始评价合同版本、可选评测源失败时的成绩生成资格；在任何情况下均不能把缺失冒充零分。
4. 正式结果的原子可见性、复核修订后整卷算法再计算的版本控制与审计字段。
5. 普通新建试卷草稿遇到题库新 revision 时是否自动更新、已发布且无引用的试卷是否允许物理删除等尚未确认细节。
6. 需要开放题 ISE 时的参考文本/类别策略尚需供应商能力验证，不得直接假设 ASR 文本回填 ISE 合法可靠。

## 文档维护与后续实施

- 修改 Excel 字段时以 T1.5 为单一权威，其他文档按业务字段语义引用；旧列号仅允许在历史模板迁移说明中出现。
- 涉及未确认的权限细则、具体评分公式、数据库物理表结构、API 路由时，不据历史修复建议自行补造限制。
- 文档更新走独立分支、只包含相关文档变更；检查相对链接、编码、`git diff --check`，经用户审核后合并。
