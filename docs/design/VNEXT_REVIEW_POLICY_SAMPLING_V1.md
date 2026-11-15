# VNext｜冻结复核策略 V1

本合同确定 REVIEW-01 的策略解释和抽样算法；其他评分、人工复核及结果固化规则继续遵循 S1.0、SW1.1 与 C1.0。

## 策略快照

Session 创建时保存策略，之后不读取外部当前设置。V1 是 JSON 对象，`version` 必须为 `v1`，必须提供 `default_ratio`；`task_type_ratios` 和 `paper_entry_ratios` 可省略，各自为映射。所有比例是有限的 JSON 数值，范围为 0 至 1；布尔值不视为数值。禁止未识别字段。题型键必须是该试卷冻结评分插件注册的题型身份；单题键是该试卷实际 PaperEntry 的规范小写 UUID 字符串。优先使用单题比例，再使用题型比例，最后使用明确的默认比例；不存在系统默认比例或最低抽样题数。

历史 `{"sampling": 0}` 保留零抽样含义；其他无版本旧格式及无法识别或不合法的策略不作推断。`validate_policy` 返回明确错误供诊断；结算保持等待。

## V1 确定性算法

对每个 SessionTask，将字节序列 `UTF8("vnext-review-sampling-v1") + 0x00 + session_id 的 16 字节 UUID + session_task_id 的 16 字节 UUID` 计算 SHA-256。摘要按大端解释为无符号 256 位整数 `bucket`；比例按 Python 对已冻结 JSON 数字的最短十进制表示转换为精确有理数 `numerator / denominator`。当且仅当 `bucket * denominator < numerator * 2^256` 时抽样命中。0 永不命中，1 必然命中。该规则每题独立，不要求每场命中精确比例。哈希输入、数值解释或阈值规则的变更须使用新策略版本。

## 复核和证据

必要 RawEvaluation 就绪、合同和输入身份验证通过后，`requires_review = sampled OR model_recommended`。来源合同的 `review_recommendation` 显式为 `False` 表示来源不提供复核建议；可调用解析器返回严格布尔值表示有或无建议；`None` 表示未知，不得按 False 推断。无法解析、来源合同不可用或必要证据未就绪时不得固化。

同一 Manifest 和 SessionTask 最多一项 PendingReview：原因分别为 `SAMPLED`、`MODEL_RECOMMENDED`、`SAMPLED+MODEL_RECOMMENDED`。教师复核前按当前冻结策略和证据重新核验，不仅凭历史队列存在判断。若旧 PendingReview 的原因与可验证事实不一致，保留历史并阻止固化，另行处理，不自动清洗。教师只处理命中题；其余题使用自动分。已有正式成绩不受策略解释更新影响。
