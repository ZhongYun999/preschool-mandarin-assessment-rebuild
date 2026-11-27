# QA-EVAL-01B 全局文本评价服务商配置

管理员使用临时共享 `require_teacher` 管理凭据调用 `/teacher/llm-provider`：GET 查询当前配置，PUT 原子保存并激活配置，GET `/credentials` 查询凭据身份及可用状态，POST `/credentials/{credential_id}/disable` 显式停用。PUT 使用 `provider`、`model`、可选 `region`、`workspace`，以及新 `api_key` 或已保存 `credential_id` 两者之一。接口不接受 Endpoint，且不返回凭据或密文；目前 Qwen 支持 `qwen-plus`、`qwen-turbo`，DeepSeek 支持 `deepseek-flash`、`deepseek-v4-pro`。正式登录启用后应改用管理员专属权限。

`VNEXT_LLM_PROVIDER_ENCRYPTION_KEY` 是环境提供的私有 Fernet 主密钥。创建凭据按供应商递增版本，加密内容绑定凭据身份、来源及版本；重新选择需验证归属、状态与可解密性。配置与审计追加保存，停用不可撤销；禁用或无法解密时不回退旧凭据。全局事务锁将配置变更、凭据状态和领取配置快照串行化。

QA 试卷只冻结 `qa-text-evaluation/1`、原 Prompt 身份及正式 ASR 依赖。Worker 领取时从当前配置创建独立快照，包含来源、模型、执行合同、连接位置、请求参数、配置和凭据版本及领取时间；密钥仅在执行期间存在内存。快照不参与业务输入指纹，并在请求前与租约身份共同持久化。READY、失败和 UNKNOWN 原始记录保存对应快照；已派发的 UNKNOWN 需要明确授权重试，重试重新选择配置。既有 Speaking 合同不受此配置影响。

本阶段不注册生产评分插件或自动故障转移。后续生产部署应安全管理主密钥、替换临时共享鉴权，并通过受控的人工授权验证真实服务商；MockTransport 不能证明模型质量、计费或服务可用性。

## 凭据地域与迁移（QA-EVAL-01B-FIX）

DashScope 凭据绑定创建时的地域；复用时要求服务商和地域均匹配，同地域可用于不同 Workspace。DeepSeek 凭据不绑定地域。新密文认证凭据 ID、服务商、版本和地域；解密及每次领取均校验地域，不匹配或凭据不可用时不回退。管理 API 的凭据列表返回地域，不返回明文或密文。

0026 根据历史配置回填仅有一个明确地域的 DashScope 凭据元数据；多地域、未引用或地域不明的凭据保持未知。0025 密文不含经过认证的地域，因此即便元数据能回填，也不能安全证明该地域属于密钥：所有旧格式 DashScope 凭据均需重新录入。旧格式 DeepSeek 凭据保持兼容。迁移不重写密文、配置、审计或评价记录；已有凭据时禁止降级以免丢失地域历史。

供应商切换验证使用真实 Worker 和两个 MockTransport：旧 DeepSeek Claim 在配置切换后执行，模拟请求已接收后超时并持久化 UNKNOWN；明确授权后新 Claim 使用 Qwen，完成 READY，两个执行的业务输入指纹相同。相关测试及完整后端回归使用外部网络阻断和合成凭据，不发送真实供应商请求。

本轮验证：隔离 PostgreSQL 空库升级至 0026 与 ORM 一致性检查通过；0025 历史凭据、配置、审计及既有 READY 评价和执行快照的升级验证通过，含降级保护。凭据、Worker、调度器、迁移和管理 API 专项测试 83 项通过；新增独立网络阻断后切换测试另行重跑通过。使用项目完整测试命令的结果为 842 项通过、1 项跳过；跳过项为未配置专用真实供应商验证数据库。Ruff、文档检查和 `git diff --check` 通过。
