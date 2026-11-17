# EVAL-02E：QA 评价合同与联调预检

## 验证边界与技术事实

- 本文基线为 main `533cf4ba797a36d69db4bcfe6ad567bd369e7819`。PR #36 的 [PR checks](https://github.com/ZhongYun999/preschool-mandarin-assessment-rebuild/actions/runs/44096437949) 已完成且成功，包含后端测试、迁移和文档检查。
- 两个模型的真实在线请求尚未执行，地域、工作空间的模型权限及真实响应兼容性尚待验证；不能用离线替身证明供应商可用或评分质量。
- ASR 为 `qwen3-asr-flash`，将音频转换为 16 kHz 单声道 PCM WAV 后以 Base64 Data URI 调用。适配器检查 5 分钟、编码后 Data URI 10 MB 的模型请求兼容限制；不改写儿童作答合同。
- 文本模型为 `qwen-plus`，使用非思考 JSON Object 模式；服务端验证候选字段、档位、重复键、非有限值与字段类型。合同 `qwen-qa-text-candidate` / `test-v1` 和 Prompt `qa-text-candidate-v1` 是候选测试身份，不是生产评分标准。
- Worker 冻结 Submission、题目版本、Prompt、配置及 ASR 证据身份。ASR 的 `TEXT` 才可进入当前候选文本合同；`NO_TEXT` 使下游等待并记录诊断。技术失败不视为无语音，未知外发结果不自动重试。
- [ASR 文档](https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference)、[音频规格](https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide)、[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)和[地域](https://help.aliyun.com/zh/model-studio/regions)由供应商维护，实际权限和接口行为须在线确认。

## 授权后的可执行计划

先由用户明确授权模型和上限。一次链路最多调用 `qwen3-asr-flash` 1 次、`qwen-plus` 1 次；仅 ASR 返回 `TEXT` 且有效时调用 LLM。入口拒绝重试和额外外发。两个单模型冒烟命令若另行使用，每个命令分别增加至多一次对应模型调用，须单独纳入授权次数；ASR 静音只检验连通性，不证明语音识别质量。

1. 私下配置 `DASHSCOPE_API_KEY`，确认该地域工作空间拥有两种模型权限；不要把密钥输入聊天、命令历史或版本库。
2. 使用单独的本地 PostgreSQL 16 测试库 `vnext_eval02e_test`，通过 `TEST_DATABASE_URL` 配置。入口只接受本地主机上的该库名，并在随机临时 schema 内创建测试事实，结束后清理。
3. 准备 WAV 格式的已获授权成人录音或合法合成语音，内容可为“小猫在睡觉”；不得使用真实儿童录音。入口的上传声明为 `audio/wav`，实际格式仍由音频准备模块检测。通过私有路径配置 `EVAL02E_AUDIO_FILE`，并确认来源。
4. 在同一受保护进程中配置 `EVAL02E_REGION`、`EVAL02E_WORKSPACE`、`EVAL02E_NON_CHILD_AUDIO=confirmed`。如需人工核查识别文字和候选评价理由，可额外把 `EVAL02E_PRIVATE_REVIEW_FILE` 指向仓库外的全新绝对路径，所在目录须由操作系统权限保护；该文件含测试转写和结构化评价，不得提交或公开。只有获得本次一 ASR 一 LLM 明确授权后，才设置 `EVAL02E_PAID_CALLS=ASR1_LLM1`。入口不属于默认 pytest 文件匹配，普通 CI 不会调用供应商。
5. 在仓库根目录执行：

   ```powershell
   python -m pytest backend/tests/manual/real_qa_chain.py -q -s --tb=no --show-capture=no
   ```

入口使用现有测试 Registry、录音存储、音频准备和 Worker；不会封卷或运行正式结算。输出仅包含调用次数、作业状态、请求 ID、实际请求模型、用量、证据关联和是否存在正式结果，不打印录音、识别文本、完整 Prompt、密钥或原始供应商响应。若 ASR 返回 `NO_TEXT`，LLM 不外发；如返回失败或 UNKNOWN，应停下诊断，不自行增加次数或重试。

已存在的单模型入口是 `python -m app.evaluation.asr_smoke --region <REGION> --workspace <WORKSPACE> --confirm-paid-call` 和 `python -m app.evaluation.qa_text_smoke --region <REGION> --workspace <WORKSPACE> --confirm-paid-call`。后者使用虚构回答，而不是上游真实识别结果；二者不能单独证明 ASR→LLM 的关联。

## 待业务确认的规则

- **四维指标与档位**：可保留候选档位并验证教师一致性，或重新定义维度和档位后版本化；前者便于与历史样例比较，后者更能符合 VNext 教学目标，但均需真实教学证据验证。
- **NO_TEXT**：可保持等待人工处理，避免虚构分数；也可定义独立的非数字“无法评价”结果及后续流程，减少长期等待，但需先确认消费和复核规则。
- **ASR 误识别**：可在识别歧义时建议复核，降低误判；也可引入经授权的人工核对或其他证据，但会增加工作量和数据权限要求。
- **文本与声学组合**：可作为独立证据由版本化插件组合，或分别展示并由教师判断。权重、适用范围和缺失证据处理均未确认。
- **总分及重播事实**：可由已发布试卷绑定的评分插件单独定义，或先只提供维度结果；重播如何影响正式结果尚未确认，不能依据旧系统推断。

## 虚构案例的验证范围

离线受控测试覆盖简短及不同措辞回答、不同学段、ASR 错字、空参考答案、仅有文字描述的图片题、冲突指令、非法 JSON 和 `NO_TEXT` 等输入与合同边界。真实供应商尚未运行这些案例；若要比较答非所问、不同学段和评分理由，需要另行明确每个模型的请求数、成本授权和人工评价方法。如配置仓库外私有审查文件，用户可人工检查一次链路的转写与评价理由；未配置时只输出状态摘要，不保留具体理由。合法 JSON 与候选数字不构成科学效度或生产验收结论。
