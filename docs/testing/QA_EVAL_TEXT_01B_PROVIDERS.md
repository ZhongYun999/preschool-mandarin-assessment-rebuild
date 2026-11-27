# QA-EVAL-01B 全局文本评价服务商配置

管理员使用临时共享 `require_teacher` 管理凭据调用 `/teacher/llm-provider`：GET 查询当前配置，PUT 原子保存并激活配置，GET `/credentials` 查询凭据身份及可用状态，POST `/credentials/{credential_id}/disable` 显式停用。PUT 使用 `provider`、`model`、可选 `region`、`workspace`，以及新 `api_key` 或已保存 `credential_id` 两者之一。接口不接受 Endpoint，且不返回凭据或密文；目前 Qwen 支持 `qwen-plus`、`qwen-turbo`，DeepSeek 支持 `deepseek-flash`、`deepseek-v4-pro`。正式登录启用后应改用管理员专属权限。

`VNEXT_LLM_PROVIDER_ENCRYPTION_KEY` 是环境提供的私有 Fernet 主密钥。创建凭据按供应商递增版本，加密内容绑定凭据身份、来源及版本；重新选择需验证归属、状态与可解密性。配置与审计追加保存，停用不可撤销；禁用或无法解密时不回退旧凭据。全局事务锁将配置变更、凭据状态和领取配置快照串行化。

QA 试卷只冻结 `qa-text-evaluation/1`、原 Prompt 身份及正式 ASR 依赖。Worker 领取时从当前配置创建独立快照，包含来源、模型、执行合同、连接位置、请求参数、配置和凭据版本及领取时间；密钥仅在执行期间存在内存。快照不参与业务输入指纹，并在请求前与租约身份共同持久化。READY、失败和 UNKNOWN 原始记录保存对应快照；已派发的 UNKNOWN 需要明确授权重试，重试重新选择配置。既有 Speaking 合同不受此配置影响。

本阶段不注册生产评分插件或自动故障转移。后续生产部署应安全管理主密钥、替换临时共享鉴权，并通过受控的人工授权验证真实服务商；MockTransport 不能证明模型质量、计费或服务可用性。
