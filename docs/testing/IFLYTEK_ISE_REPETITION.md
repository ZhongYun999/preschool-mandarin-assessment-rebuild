# ISE 跟读原始评价合同（离线验证）

来源 `iflytek-ise/1` 仅支持正式 Repetition Submission 的 `read_sentence`；参考文本取冻结 TaskRevision 的 `reference_answer`。请求显式固定 `group`、可选 `grade`、音频处理规格和协议版本，不按文本长度决定题型，不对 QA、Speaking 或 Dialogue 触发 ISE。`adult` 可用于成人录音的本地技术验证；`pupil`、`youth` 等供应商选项不代表已验证学前儿童适用性。

录音由 RecordingStore 校验身份和摘要，使用 AudioTools 按冻结 AudioSpec 准备 16 kHz、16 位、单声道 PCM。讯飞[流式协议](https://www.xfyun.cn/doc/Ise/IseAPI.html)使用签名 WebSocket，依次发送参数帧、音频帧和结束帧；普通版 HTTP 接口已下线。凭据从 `IFLYTEK_ISE_APP_ID`、`IFLYTEK_ISE_API_KEY`、`IFLYTEK_ISE_API_SECRET` 私有环境读取，签名 URL 不进入持久化结果或日志。

成功的 RawEvaluation 记录供应商原始分、可选维度、逐层字段、XML、请求参数和音频来源；缺失维度保持空值，不换算正式成绩。拒识及非零 `except_info` 为 FAILED，不保存可评分 payload；对应执行的不可变 `EvaluationDiagnosticEvidence` 保存拒识原因、逐层信息、必要 XML 和来源，与 RawEvaluation 和 Job 同事务提交。供应商明确报错记为 FAILED，非法结果为 INVALID，外发结果不明为 UNKNOWN。UNKNOWN 仅允许明确接受不确定性后重试；重试追加执行证据。诊断证据不参与评分资格，不能向儿童公开完整 XML。

迁移 `0028_evaluation_diagnostics` 为诊断证据建立一对一外键和不可变触发器；存在证据时禁止降级丢弃历史。当前代码和测试没有自动执行真实供应商调用，适配器只在调用方明确领取并执行 Job 时发送请求；真实模型可用性与适龄性尚未验证。
