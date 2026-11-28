# AUTH-01 教师身份与权限合同

教师身份使用 PostgreSQL 用户与服务端登录会话。用户名去除首尾空白并转小写，只允许 1—64 位 ASCII 字母、数字、点、下划线和短横线。密码使用 Argon2id，登录令牌仅保存 SHA-256 摘要；不迁移旧系统账号或会话。

| 能力 | teacher | admin |
|---|---|---|
| 题库、试卷、音频、测评、评分查询和人工复核 | 是 | 是 |
| 用户管理、全局 LLM 供应商和 Speaking 运行政策 | 否 | 是 |

`POST /auth/login` 接受 JSON 用户名与密码，必须有 `VNEXT_AUTH_ALLOWED_ORIGINS` 中精确匹配的 Origin，返回用户信息和会话绑定的 `csrf_token`，同时设置 `vnext_teacher_session` Cookie。Cookie 为 HttpOnly、SameSite=Lax、Path=/，默认 Secure，不设置 Domain；有效期由 `VNEXT_AUTH_SESSION_HOURS` 指定，默认 24 小时。本地 HTTP 可明确设置 `VNEXT_AUTH_COOKIE_SECURE=false`；生产须使用 HTTPS 和默认 Secure。可信 Origin 使用逗号分隔，禁止任意跨域凭据访问。

当前应用不启用跨域 CORS；浏览器开发环境应通过同源反向代理访问 API，并将浏览器实际使用的 Origin 加入可信列表。可信 Origin 配置本身不会开放跨域读取或跨域携带凭据。

`GET /auth/me` 返回当前用户和 CSRF Token；所有 Cookie 认证的写请求须提供 `X-CSRF-Token`。`POST /auth/logout` 撤销当前会话并清除 Cookie；`POST /auth/change-password` 验证旧密码、保留当前会话并在同一事务撤销其他会话。认证响应禁止缓存。会话过期或用户停用后无效。

管理员通过 `GET/POST /admin/users`、`PATCH /admin/users/{user_id}` 和 `POST /admin/users/{user_id}/reset-password` 管理用户。角色或启停状态变化及重置密码撤销目标用户会话；管理员不能停用、降级或重置自己。管理员数量变化使用 PostgreSQL 事务锁串行裁决，且在事务中重新确认操作者。

## 初始化和切换

先明确 VNext 数据库并升级到最新迁移，在仓库根目录执行：

```powershell
python backend/scripts/create_admin.py --database-env DATABASE_URL
```

命令交互式读取用户名、显示名称和隐藏密码；不会覆盖同名用户，也不在命令行参数中接收密码。配置可信 Origin 后，通过 `/auth/login` 登录，后续写操作附带响应中的 CSRF Token。`TEACHER_MANAGEMENT_TOKEN` 和 `TEACHER_REVIEW_TOKEN_HASHES` 不再用于认证，应从部署配置中移除；旧值不能访问新的教师接口。旧用户和历史密码不会导入。

历史 `review_decisions.teacher_id` 保持不变；新 HTTP 复核以稳定用户 UUID 写入该字段，同时保存 `reviewer_user_id`，两种身份在幂等检查中区分。供应商管理的 HTTP 操作在审计记录中保存 `actor_user_id`，历史记录和内部无操作者调用保持空值。0027 降级会移除新用户和会话表；如果已有复核或供应商审计的正式用户关联，迁移拒绝降级，防止丢失身份关联。

儿童施测仍只接受相应 Session 的 `Authorization: Bearer as1_...`，与教师 Cookie 独立。教师退出、停用或改密不撤销已签发的儿童施测凭据。组织、班级及资源所有权规则不在本合同内。
