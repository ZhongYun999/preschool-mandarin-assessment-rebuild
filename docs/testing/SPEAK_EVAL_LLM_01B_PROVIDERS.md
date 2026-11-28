# Speaking text v1 provider execution

The business result contract is `speaking-text-evaluation/1` from 01A. It keeps
the same three raw dimensions, score bands and `speaking-text-v1` prompt with
SHA-256 `ffb275cf5b46d1e10f4ba8753df0d4316f7a7fa9e105abbc2d68783c74cb77af`.
The production Registry exposes two *execution* contracts, not scoring plugins:
`dashscope-speaking-text/1` and `deepseek-speaking-text/1`. Their input and
output validators delegate to the same business contract. A scoring plugin must
explicitly declare the ASR and one of these requirements; no source is selected
merely by registering a provider or opening a page. Existing published plans
and `speaking-text-evaluation/1` retain their previous meaning.

Each requirement freezes the provider, model, API version, endpoint, output
format, temperature, maximum output tokens and prompt version/digest. Qwen also
freezes region, workspace and `enable_thinking=false`; DeepSeek freezes the
official endpoint and `thinking=disabled`. The dependency identifies a
particular upstream ASR source, version and requirement index. Worker snapshots
bind the full speaking source, ordered segments, ASR job and RawEvaluation IDs,
upstream fingerprint, task revision, prompt and task context. No provider key,
help count, replay count or identifying child data is included in these inputs.

Supported execution profiles are deliberately limited to models for which the
documented JSON and non-thinking behavior was checked: Qwen `qwen-plus`,
`qwen3.5-plus`, `qwen3.6-plus`; DeepSeek `deepseek-flash`, `deepseek-v4-pro`.
DeepSeek documents a fixed effective temperature of `1` for non-thinking
`deepseek-flash`, so other requested temperatures are rejected rather than
silently ignored. Further models or parameter modes require capability review
and a compatible execution version. Provider aliases can change upstream even
when the requested model ID is frozen; a provider-reported model is recorded
when available and a conflicting reported ID is rejected. Freezing an alias
alone does not guarantee a provider's internal weights remain unchanged.

Qwen uses the DashScope SDK with `max_retries=0`, JSON object response and
non-streaming output. DeepSeek uses an explicit `httpx` POST to its official
Chat Completions endpoint with no automatic retries, redirects or implicit proxy
configuration. Private server configuration supplies `DASHSCOPE_API_KEY` and
`DEEPSEEK_API_KEY` independently; they are never execution-plan parameters or
raw result fields. A trusted caller supplies the appropriate adapter to the
existing worker. There is no administrator UI or automatic provider switching.

Both adapters pass the ordered formal ASR segments as JSON and never concatenate
guidance or replay text into the child's answer. All-NO_TEXT is stopped without
an external request. Normal completions are parsed by 01A's strict JSON parser;
blank, truncated, refusal, extra fields or invalid scores become INVALID.
Successful RawEvaluation records preserve provider, requested model, any
provider-reported model and request ID, non-sensitive token usage, prompt
identity, business/execution contract IDs and worker input fingerprint. These
are candidate raw observations, not a final score.

`INSUFFICIENT_EVIDENCE` becomes FAILED with the stable diagnostic code
`LLM_INSUFFICIENT_EVIDENCE`, with no READY score or automatic retry. The current
`raw_evaluations` constraint requires `payload IS NULL` for non-READY records;
there is no existing protected free-text diagnostic field. Consequently the
model's individual reason strings are **not persisted**. Preserving them needs
a separately designed protected diagnostic fact, privacy and access rules, and
possibly a migration; it cannot be implemented by mislabeling the result READY.
This does not authorize a manual score to replace failed automatic evaluation.

The existing worker owns leases, the dispatch marker and UNKNOWN recovery. A
timeout or ambiguous server failure may already have incurred cost; UNKNOWN is
not automatically retried and an explicit `accept_unknown` authorization is
required. Exactly-once billing cannot be guaranteed. Full supplier behavior,
region availability, credentials, model quality and educator suitability have
not been verified online in this task. `MockTransport` tests do not constitute a
real provider or paid-model test.

Primary API references: [Qwen structured output](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-structured-output),
[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/),
and [DeepSeek JSON output](https://api-docs.deepseek.com/guides/json_mode/).
