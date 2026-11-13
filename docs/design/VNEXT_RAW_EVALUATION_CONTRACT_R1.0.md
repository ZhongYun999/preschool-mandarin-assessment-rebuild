# VNext｜五题型 RawEvaluation 原始评分字段与最终评分交接合同 R1.0

> 设计讨论稿。基线：旧仓库 `ZhongYun999/preschool-mandarin-assessment` 的 `pronunciation_assessment_service.py`、`llm_service.py`、`domain/ai_scoring_rules.py`、`open_response_scoring_service.py`、`open_response_acoustic_service.py`、`choice_service.py`、`score_resolution_service.py`、`trial_scoring.py`、`summary_service.py`；另参照讯飞开放平台 ISE 官方文档。不是已实施代码，不代表新的固定评分权重。

## 1. 边界和原则

1. **题型 Handler**：可靠完成作答、Submission 和录音／选择事实；不计算评分插件所负责的转换分。
2. **评测适配层（ISE／LLM／本地声学提取）**：遵守各自的原始输出合同，持久化原始响应及可验证的结构化字段；不扣重播或求助分、不混入五分制转换分。
3. **评分插件**：试卷发布时固定插件 ID 和实现版本；按 S1.0 双阶段评分：`score_task()` 读取冻结题目、Submission 事实和 RawEvaluation，计算并保存自动单题转换分；`score_paper()` 汇总采用的单题分，初始汇总及复核期间可按需重复计算。必要复核完成后固化正式结果。
4. **ScoreResult**：正式结果在必要复核完成后原子固化；正式成绩页面与导出只读取已固化结果，不在查询时重新评分。受控诊断可见范围遵循第 9 节。
5. **缺失不等于零分**：真实 0 是分数；尚未生成、外部失败、技术无效、缺少插件需要的证据，各有机器状态。用户端等待或提示成绩暂不可用；确需显示分数字段时用 `N/A`，不展示伪造的 0。
6. 评分说明（Excel 的 N 列）是该题目提供给 LLM 评分的**具体提示词资料**；它不是评分插件代码、教师人工评语，也不能覆盖系统固定的输出结构合同。参考答案、材料说明、AI 追问范围按各自题型职责消费。

**字段消费补充**：Excel T1.5 中 M 是统一的 `reference_answer`；选择题 M 解析为已存在的选项身份，其他题型按各自评价合同作为参考文本使用。固定 TTS 只生成 E 引导语（允许为空）和 F 题目内容，不取 M；LLM 题目级评分说明来自 N。儿童及高级模式接口不得暴露选择题正确答案或其他题型的参考答案。

## 2. 不应混淆的三套数据

| 类别 | 持久化对象 | 示例 | 写入者 | 允许读取者 |
|---|---|---|---|---|
| 儿童作答事实 | Attempt / Submission / Turn / Playback / Audio | 已选 B、录音 ID、真实 ASR、成功重播 2 次、求助 1 次 | 题型业务与录音服务 | 引擎上下文、最终插件、复核 |
| 原始评价结果 | RawEvaluation（每来源一份） | ISE `total_score`、LLM `semantic_score`、声学 RMS/时长 | 各评价适配器 | 最终插件、教师诊断 |
| 正式换算结果 | ScoreResult（完整不可变） | 单题 4.25、总分 4.10、等级良好 | 试卷绑定的插件及结果提交事务 | 小程序、教师后台、正式导出 |

重要：允许同一 Submission 拥有**多条** RawEvaluation；例如跟读的 ISE，一道问答的 LLM＋本地声学指标。不要把多个来源预先合成唯一的 `raw_task_score`；那可能已经包含换算或扣分，历史旧代码有此问题。

## 3. RawEvaluation 公共外壳——统一交接，不统一数值维度

| 字段建议 | 具体用途 | 约束说明 |
|---|---|---|
| `evaluation_id` | 唯一评分作业结果身份，幂等／复核／结果溯源 | 内部身份 |
| `submission_id` | 精确关联已接受的本次 Submission（对应 Attempt 已 SEALED） | 不关联整个可变 Session 当前题目 |
| `source_kind` | 区分 `iflytek_ise` / `llm_text` / `local_acoustic_metrics` 等 | 未来可扩展；选择题事实不一定需伪装成引擎调用 |
| `contract_id`、`contract_version` | 告诉插件 `raw_payload` 应按何种结构读取 | 例如 `llm_qa_text_v1`；不可仅凭题型猜字段 |
| `provider`、`model`、`engine_version` | 标识真实引擎来源，支持故障定位与历史解释 | 仅保存实际已知值，不凭空填默认模型 |
| `status` | `PENDING / READY / FAILED / INVALID` 等评价操作状态 | 仅 READY 才有可供插件消费的有效载荷 |
| `raw_payload` | **完整已确认的原始结构化结果**，包含原生字段和维度 | 不插入应用层转换总分；可附供应商原文资源引用 |
| `input_refs` | 引用本次已冻结的音频、ASR、题目修订和评测参考文本 | 可追踪来源，避免复核时用到最新题目 |
| `error_code`、必要诊断 | 让老师区分未返回、请求失败、格式错误和被拒 | 只在出错时写入；不当作数字 0 |
| `created_at`、`completed_at` | 处理时间与历史追踪 | 操作身份稳定，允许技术重试但不覆盖已确认结果 |

不必为每个评价来源把全部维度展平成 SQL 表列；通用外壳 + 经相应合同校验的结构化 `raw_payload` 更适合字段随来源和合同变化的现实。供应商原始 XML/JSON 可以保留为独立的不可变证据或资源引用，解析后的投影以 `raw_payload` 交接。原始响应及音频访问遵守原系统数据权限与保护要求。

### 3.1 强烈不建议的字段

- `raw_task_score`: 如果内容实际已经是多维融合、重播扣分后的数值，名称会误导未来插件。
- `assisted_score_5` / `mapping_rule`: 旧 ISE 适配器将 0～100 转五分，这是插件工作；不能再当成 ISE 的原始输出。
- `final_score`：不应出现于 RawEvaluation 的**系统正式成绩**语义里；若 LLM 模型自身真的返回某个名为 final_score 的字段，只能当作模型输出、按其合同处理，不得冒充 ScoreResult。
- `default_zero`: 缺少真实来源时永远不能代替有效原始分。

## 4. 讯飞 ISE：外部固定协议，按实际类别可选字段

旧系统 `PronunciationAssessmentResult` 可读取：

| 原字段 | 用途 | 注意 |
|---|---|---|
| `total_score` | 供应商总体评价原始分 | 旧实装要求它存在；若缺失应标记失败／无有效结果，不当作 0 |
| `accuracy_score` | 准确度 | 并非每种中文评测场景都返回 |
| `fluency_score` | 流利度 | 按中文任务类型和可用能力判断 |
| `integrity_score` | 完整度 | 同上 |
| `phone_score` | 声韵相关分 | 某些任务／全维度能力下才返回 |
| `tone_score` | 声调／调型相关分 | 同上 |
| `emotion_score` | 朗读表达相关维度 | 旧解析器支持；是否返回须根据实际返回报文 |
| `is_rejected`、`reject_message`、`except_info` | 供应商拒绝／异常事实 | 不能将拒绝误判为正常零分 |
| `dp_message`、`perr_msg`、`is_yun` 等 | 增漏／替换／回读及声韵调错误线索 | 旧代码从嵌套 XML 汇总 `error_counts`、`error_items`；若以后用于评价／复核应保留原始细节 |

评测请求上下文：`category`（`read_syllable`、`read_word`、`read_sentence`、`read_chapter`）、`group`、`grade`（当配置存在）、准确的参考文本或引用、实际引擎版本。注意这些是**请求参数或解析上下文**，不要冒充供应商返回的评分字段。

讯飞官方文档确认中文题型的字段集合不同，并说明部分维度取决于 `extra_ability=multi_dimension` 等能力。设计不能把上表全部强制为非空，也不能把某项未返回的维度值随意补零。判断哪些字段是**某个已选评测合同**所必需，需要结合当前类别／权限／真正的输出格式进行适配。

外部参考：
- https://www.xfyun.cn/doc/voiceservice/ise/API.html
- https://www.xfyun.cn/doc/voiceservice/ise/ise_protocol.html
- https://www.xfyun.cn/doc/Ise/IseAPI.html

## 5. LLM：输出字段由独立的评测合同规定

与 ISE 不同，LLM 可以通过**我们编写的系统级评价提示词**要求 JSON 返回指定评分维度。**题目 Excel 的“评分说明”作为题目数据参与模型判断，但不能更改正式评测合同字段、类型和数值范围。**

旧系统已有三套具体示例（均是历史评分合同，不是 VNext 必须永久采用的分档）：

### 5.1 QA（问答题）历史文本评分合同

- `grammar_score`：语法分，旧档位 `0.25/0.5/0.75/1`
- `semantic_score`：语义分，旧档位 `0.5/1/1.5/2`
- `pragmatic_score`：语用分，旧档位 `0.1/0.3/0.5`
- `completeness_score`：完整性分，旧档位 `0/0.25/0.5`
- `dimension_reasons`：各维文本依据
- `review_recommended`、`review_reasons`：建议复核及原因

旧版另由本地声学代理计算 `speech_clarity_fluency_score`，属于**另外一个评价来源**，不能伪称 LLM 返回。

### 5.2 Dialogue（对话题）历史文本评分合同

- `participation_interaction_score`：参与互动，旧档位 `0.25/0.5/1/1.5`
- `vocabulary_grammar_score`：词汇语法，旧档位 `0.25/0.5/0.75/1`
- `relevance_pragmatics_score`：切题／语用，旧档位 `0.25/0.5/1/1.5`
- `dimension_reasons`、`review_recommended`、`review_reasons`

历史 Dialogue 的 `speech_clarity_fluency_score` 来自另外的本地声学计算；每轮 `completed_play_count` 和扣分也由旧后端实现，不是 LLM 输出。在 VNext，应读取稳定的 Submission/Turn 重播事实，不照搬旧版 `max(0, completed_play_count-2)` 的业务语义。

### 5.3 Speaking（说话题）历史文本评分合同

- `content_coherence_score`：内容组织连贯，旧档位 `0.5/1/1.5/2`
- `vocabulary_grammar_score`：词汇语法，旧档位 `0.25/0.5/0.75/1`
- `expression_naturalness_score`：文本可体现的自然表达，旧档位 `0.25/0.5/0.75/1`
- `dimension_reasons`、`review_recommended`、`review_reasons`

历史 Speaking 的 `speech_clarity_fluency_score` 是本地声学代理分；求助扣分属于旧版 `open_response_scoring_service.py` 的加工，不应该提前扣进原始文本分。

### 5.4 LLM 输出版本的设计要求

- 比如 `llm_qa_text_v1`、`llm_dialogue_text_v1`、`llm_speaking_text_v1`；每套合同规定维度键名、类型、必要性、可接受的原始范围／枚举和复核信息。
- 格式不合法、必要字段缺失、类型错误、JSON 解析失败，属于**技术无效**，执行有边界的技术恢复，耗尽则 FAILED／INVALID；**不是直接填 0**。
- 不审查回答的主观语言质量以决定是否推进对话；对话题下一问生成 JSON 与**评分 JSON** 是不同的合同，不能混用。
- 新增评分插件如果需要当前 LLM 从未返回的维度，不可能通过换算凭空得到：应显式新增／升级评价合同并检查插件与已发布试卷的兼容关系。保留对应评测提示词版本或快照，才能复现历史原始分的产生背景。

## 6. 本地声学事实与 ASR

旧 `open_response_acoustic_service.py` 能提取：

| 原始指标 | 数据意义 |
|---|---|
| `audio_duration_seconds` | 本轮／片段总录音时长 |
| `active_audio_duration_seconds` | 依据选定声学算法估计的有效活动时长 |
| `active_audio_ratio` | 有效活动比例 |
| `mean_active_rms_dbfs` | 活动段音量统计 |
| `long_silence_count` | 按该算法判定的长静音次数 |
| `asr_confidence` | 已接入 ASR 所报告／换算的置信度，必须保留来源 |

这类指标是**应用内音频分析算法得出的评价事实**，不应假装讯飞 ISE 返回。指标提取算法版本需要记录；原始音频不允许被插件修改。

如果本地声学分析提前产出依据固定评分规则算出的四分制、五分制代理分，则又会发生“提前换算”——VNext 应保留所需基础指标，将面向正式成绩的分档／权重交给最终插件。若某一独立评测引擎天然输出一个分数，则该分数属于它的真正原始结果，可以原样保留。

ASR 的原文、无可识别语音、置信度和音频引用是题型作答／识别事实，不等于 LLM 分数；无可识别文本可以作为正常 ASR 结果并进入评分处理，ASR 服务故障不得伪装成儿童沉默。

## 7. 五题型最小原始输入清单

| 题型 | Submission 业务事实 | 原始评价来源 | 交给插件的来源与元数据 |
|---|---|---|---|
| `instruction`（选择题） | 所选选项的稳定身份、有效重播 | 无须 LLM／ISE；服务端按题型解析冻结题目修订中的 M 参考答案并确定选择是否正确 | 选择事实、题目修订、重播事实；插件决定给分与扣分 |
| `repetition`（跟读） | 录音、重播事实 | ISE 原始评分及错误明细；按评测合同使用冻结题目修订中的 M 参考文本，可选其他已接入评价源 | `iflytek_ise` 的原始字段和类别、供应商异常状态；不预设 0～5 转换 |
| `qa`（问答） | 录音、ASR | LLM 文本维度；本地声学指标或已批准的声学评测来源 | 按合同读取各来源，不提前融合 |
| `dialogue`（对话） | 冻结完整轮次的 Q-A 音频／ASR、各轮有效重播 | LLM 对全轮文本给维度；声学分析逐轮／整体原始指标 | `target_rounds`、轮次身份和评价来源；不预设扣重播公式 |
| `speaking`（说话） | 音频片段／累计真实录音、ASR、有效求助和重播 | LLM 文本维度；本地声学指标或其他评测来源 | 原始维度＋已确认事实；不提前扣求助分 |

**必须注意：题目内容 F、材料说明 L、参考答案 M、评分说明 N、AI 追问范围 O 不是“最终计分插件配置”。** 它们分别由题型或评价生成阶段在需要时消费；任何针对题目本身的文本更改生成新 TaskRevision。评分插件绑定在 Paper，不能回写它们。

## 8. 示例：一次 QA 的原始证据（示意，不是最终表结构）

```json
{
  "submission_id": "SUB-1001",
  "raw_evaluations": [
    {
      "evaluation_id": "EV-LLM-1",
      "source_kind": "llm_text",
      "contract_id": "llm_qa_text",
      "contract_version": "v1",
      "status": "READY",
      "raw_payload": {
        "grammar_score": 0.75,
        "semantic_score": 1.5,
        "pragmatic_score": 0.5,
        "completeness_score": 0.5,
        "dimension_reasons": {
          "grammar": "句式基本完整",
          "semantic": "回答切题",
          "pragmatic": "回应得体",
          "completeness": "包含必要信息"
        },
        "review_recommended": false,
        "review_reasons": []
      }
    },
    {
      "evaluation_id": "EV-ACOUSTIC-1",
      "source_kind": "local_acoustic_metrics",
      "contract_id": "acoustic_metrics",
      "contract_version": "v1",
      "status": "READY",
      "raw_payload": {
        "audio_duration_seconds": 7.2,
        "active_audio_duration_seconds": 4.1,
        "active_audio_ratio": 0.569,
        "mean_active_rms_dbfs": -24.5,
        "long_silence_count": 1,
        "asr_confidence": 0.86
      }
    }
  ]
}
```

此示例中不存在 `formal_task_score`、`final_task_score` 或自动生成的五分制总分。数值为**虚构示例**，不能当作真实测评数据。真正的 Submission 事实另以独立身份关联，插件最终拿到的是已冻结的完整评分输入。

## 9. 双阶段评分和对外展示（以 S1.0 为准）

1. 题型 Handler 先保存已接受的 Submission；按 E1.0 冻结的 EvaluationPlan 准备必要 RawEvaluation，失败仅恢复技术处理，不抹除已接受作答。
2. 某题**评分插件必需的原始证据**齐全后，`score_task()` 读取冻结题目、Submission 事实和各来源 RawEvaluation，生成并持久化 `AutomaticTaskScore`。没声明需要的评价源不要求存在；缺失的必需证据不冒充真实 0 分。
3. 全题自动单题转换分齐备后，第一次执行同版本 `score_paper()`，保存 `AutomaticPaperScore` 供教师查看初始建议成绩；按冻结复核策略确定待复核题。
4. 教师只确认或覆盖命中题的**单题总分**，保留版本化 `ReviewDecision`，不修改原始来源、`AutomaticTaskScore`，也不因为复核重新调用 ASR、ISE、LLM、`score_task()`。
5. 每次采用分变化时，同一 `score_paper()` 可按当前全部有效单题分重算整卷结果。必要复核全部完成后，`FinalScoreResult` 与 Session 正式完成标记在同一事务中固化，包含输入身份、插件版本和计算依据。
6. 成绩未齐全时，儿童普通成绩页面只显示“成绩生成中”或“暂不可用”，不得出现伪造的 0 分或看似正式的部分总分；教师诊断可查看标为非正式的 RawEvaluation、自动分、失败信息。儿童高级模式可通过受控诊断接口查看 LLM 实际问题／引导、ASR、评价维度与失败状态；不暴露参考答案、评分说明、正确选项、完整内部 Prompt 或未经筛选的供应商原始数据，高级模式不自动授予后台权限。
7. 已固化的正式结果不因查询、导出或重启而重新运行评分算法；若以后有经授权的正式纠错，按另行确认的追加型修订合同保留历史版本。

## 10. 最小验收案例

- ISE 只返回 `total_score` 且符合该具体评测合同，不因缺少非必需维度失败；未返回该合同必需的评分则明确 FAILED/INVALID，不记零。
- ISE 拒绝评分 `is_rejected=true` 时不能产生 `READY` 的可用评分。
- 同一任务的 ISE 原始分 82.5 保持 82.5；`4.13` 若存在只能出现在最终插件计算结果。
- LLM QA 的评分 JSON 与 Dialogue 追问生成 JSON 分属不同合同，字段不互相混淆。
- LLM 有效 JSON 但缺少必要维度时，不会被视为成功评分。
- Dialogue 第一轮录音已保存、LLM 追问失败时，不会生成 RawEvaluation 的正式计分值或假 0。
- 说话题有效求助 2 次、重播 1 次：RawEvaluation 的 LLM 分数未变；只由插件按选定方案读取作答事实决定如何处理。
- 选择题答错产生 `is_correct=false`，原始事实不等于提前给出正式零分。
- 评分插件缺少某个必需来源时不能悄悄补 0。
- 分数值真实为 0 时，最终结果必须显示 `0` 而不是 `N/A`。
- 评分中断、客户端关闭、页面刷新不会触发新的临时成绩或重新计算。
- 更换前端、更新后端算法代码，不改变已经持久化的历史 ScoreResult。
- 同一 Submission 的重复工作请求不产生两组不同的权威 RawEvaluation。
- 人工复核修正不能覆盖 ISE／LLM 的原始响应；新计算引用明确复核修订。

## 11. 有待独立专题确认（不是预先硬限制）

- VNext 第一批真正要接入的评测来源：沿用旧版 ISE 跟读 + QA/Dialogue/Speaking 的 LLM 文本评分 + 本地声学指标，还是准备替换其中任一来源？不默认让 ISE 为所有开放题作答评分。
- 各题型的 LLM 评分维度是否直接继承旧 `qa_mixed`、`dialogue_mixed`、`speaking_mixed` 提示词，或调整为更贴合老师要求的版本？字段可以通过版本化合同调整，但不能在既有合同里无声变更。
- 人工复核输入究竟覆盖某个计算后的单题分，还是修正原始的某个维度？不同选择会影响“最终阶段执行一次”的边界，下一轮应专门定稿。

本文件未改变任何服务器源码、旧数据库或正式评分策略。
