# Speaking formal Qwen ASR execution

The versioned source `dashscope-speaking-qwen3-asr/1` covers every segment in
the accepted Speaking Submission. Its EvaluationPlan freezes the DashScope model,
region, workspace, language, ITN option, WAV PCM S16LE mono 16 kHz specification,
processing version and actual FFmpeg/FFprobe version strings. The provider is
`dashscope`, model is `qwen3-asr-flash`, and `confirmed_no_text` is false. A
blank response is invalid because this adapter has no confirmed supplier
no-speech evidence. No scoring plugin is registered or enabled by this source.

The trusted server invokes `speaking_qwen_worker.run_job` explicitly with the
database engine, existing recording stores, audio tools, a `SpeakingQwenASR`
provider and a positive lease. The default provider reads the API key from
server-private `DASHSCOPE_API_KEY`; callers can inject a private key provider.
Credentials must not appear in the frozen plan, input references or results. The
ordinary generic worker does not execute this source. No scheduler or child
client retry endpoint is added.

Preparation verifies original bytes, normalizes outside database transactions,
and freezes the complete source, actual audio references, hashes, processing
version and input fingerprint before provider dispatch. Matching WAV inputs can
reuse their original recording. Changed audio is stored separately as a derived
resource. The actual consumed MIME type is WAV, even if the original upload's
declared MIME type differed. The provider enforces the published per-file 5-minute
and 10-MB constraints; for a Base64 request the encoded Data URI must fit the
size limit. They are not Speaking-wide limits. See the
[official Alibaba Cloud API reference](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-asr-api-reference).

Each parent EvaluationJob has one immutable operation identity per source segment
and one execution record for each claim. A claim is `RUNNING` until it finishes;
the attempt records whether its external dispatch boundary was committed.
`READY` segment results cannot be changed. `FAILED` and `INVALID` require an
explicit trusted call to `authorize_retry`; `UNKNOWN` additionally requires
`accept_unknown=True`. An expired claim without dispatch becomes WAITING; after
dispatch it becomes UNKNOWN. A late execution token cannot overwrite a recovered
attempt. Repeated or restarted runs do not resend READY segments. When all segments
are READY, `aggregate` checks the frozen source and fingerprint, then atomically
creates one `speaking-asr-result-v1` RawEvaluation and marks the parent READY.

An external request may be charged even if its result was not saved. Explicitly
accepting UNKNOWN can therefore incur another charge. There is no physical
exactly-once guarantee. This implementation has only been tested with mock
transport and synthetic audio, not with the real provider. It does not implement
process-ASR reuse, downstream scoring, or automatic retry scheduling.
