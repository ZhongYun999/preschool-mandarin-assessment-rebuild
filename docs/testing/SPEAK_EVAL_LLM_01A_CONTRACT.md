# Speaking text evaluation v1

This document describes the frozen business contract and offline validation.
`speaking-text-evaluation/1` is **not registered as a production provider or
scoring plugin**. The offline adapter lives in tests; it does not call a model.

The system prompt identity is `speaking-text-v1` and its UTF-8 SHA-256 is
`ffb275cf5b46d1e10f4ba8753df0d4316f7a7fa9e105abbc2d68783c74cb77af`.
The frozen requirement carries both values, and validation also hashes the
deployed prompt. A changed prompt cannot silently execute a plan with the old
digest; a future semantic change must retain the old version for historical
plans and introduce a distinct version. The formal scoring prompt is independent
of the runtime guidance prompt.

The prompt asks for age-appropriate qualitative evaluation: short and naturally
elliptical speech is acceptable for younger children; for older children it
considers content development and relationships. It sets no fixed sentence,
word, character, conjunction or sentence-pattern minimum, cap or automatic
deduction. ASR punctuation and spelling are not automatically attributed to the
child. It prohibits inferring pronunciation, prosody, pauses, loudness, audio
clarity or psychological confidence from text. The model must not guess image
contents or missing guidance context, and question and child data cannot override
the system contract. Offline tests verify these instructions and structural
behavior, not actual model teaching quality.

For sufficient evidence, the strictly validated raw result has exactly these
fields:

```json
{
  "content_coherence_score": 1.5,
  "vocabulary_grammar_score": 0.75,
  "expression_naturalness_score": 0.75,
  "dimension_reasons": {
    "content_coherence": "内容围绕主题展开。",
    "vocabulary_grammar": "词句能够表达意思。",
    "expression_naturalness": "措辞符合儿童口语特点。"
  },
  "review_recommended": false,
  "review_reasons": []
}
```

This is an offline example, not a child's assessment. The allowed values are
`0.5/1/1.5/2` for content coherence and `0.25/0.5/0.75/1` for each of the other
two dimensions. The parser rejects duplicate keys, non-finite numbers, imprecise
out-of-band decimal values, missing or extra fields, boolean scores, invalid
reasons and incorrect review types. It never rounds a score into an allowed band.
The values are raw text observations, not a final task or paper score.

The worker obtains task fields from the immutable TaskRevision and injects the
validated upstream READY ASR identity and `formal_speaking_asr` ordered segments.
The contract requires all frozen segment IDs and the corresponding recording
IDs and positions; a `NO_TEXT` segment remains `NO_TEXT` with null text. The
complete source, ASR evaluation ID, ASR job ID and fingerprint, task revision,
prompt and actual context are bound by the worker's input references and
fingerprint. Neither process ASR nor guidance, TTS, replay or support counts are
part of the child response. The frozen material description is text only; the
model is told it cannot see the image.

For explicit model evidence insufficiency the parser recognizes
`{"status":"INSUFFICIENT_EVIDENCE","reasons":["原因"]}` separately from a
score. The test adapter records `LLM_INSUFFICIENT_EVIDENCE` as a non-READY
diagnostic under the existing worker, without storing a score. This is not a
decision that a non-scored result may settle, nor authorization for a teacher
score to replace it. A future provider adapter must implement the same boundary;
future business design must decide the durable review and settlement semantics.
Before any future provider call, an all-`NO_TEXT` input must not be converted to
a scored response; unavailable, mismatched or incomplete ASR input must be
blocked before dispatch. Partial `NO_TEXT` is retained without fabricating text;
whether sufficient content remains is a separate evidence judgment.
ASR technical failure, missing source, invalid JSON and evidence insufficiency
remain distinct from a genuinely low-quality but evaluable response. Current
Qwen Speaking ASR does not attest legal no-speech outcomes; mixed or all-NO_TEXT
tests use controlled offline contracts only.

An EvaluationPlan must explicitly declare the ASR dependency and this text
contract. No ISE, acoustic, TTS or additional ASR call is inferred. The eventual
plugin may consume the raw dimensions and the Submission's independent help and
replay facts. The confirmed initial SPEAK-SCORE reference formula subtracts
`min(expression_naturalness_score - 0.25, 0.25 * effective_help_count)` from
naturalness, retaining a floor of `0.25`; **this module does not execute the
formula**. No replay deduction or final scoring is implemented here.

Real provider authentication, execution adapters, provider budgets, protected
diagnostics and a registered scoring plugin remain future work. Real model
reliability, age suitability, image-dependent questions and ambiguity handling
require a separately authorized educator-reviewed evaluation on appropriate data.
