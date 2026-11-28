# Speaking formal ASR result v1

This is an input and result contract, not a registered production evaluation source.
The existing worker anchors `speaking-source-v1` for the whole submission and
fingerprints the source, the evaluation requirement, the adapter's consumption
references and payload. The contract declares an ordered, non-empty subset in
`speaking_consumption.segment_ids`, with one `audio_inputs` entry per segment.
Each entry identifies the segment, recording, position, storage reference,
media type, input SHA-256 and the processing version frozen by the requirement.
The offline adapter reads the actual stored synthetic recordings. A derived input must also appear
in `derived_recordings`, whose storage reference, media type and digest are
checked by the worker. The source retains each original storage reference,
media type and digest; processing parameters and tool versions must be frozen
by any future concrete contract that uses them.

An offline result has the following shape (identifiers abbreviated):

```json
{
  "schema": "speaking-asr-result-v1",
  "submission_id": "submission-id",
  "speaking_source_sha256": "full-source-digest",
  "contract": {"source": "offline-multisegment-asr", "version": "1"},
  "segments": [{
    "segment_id": "segment-id", "recording_id": "recording-id", "position": 1,
    "status": "TEXT", "text": "离线测试文本",
    "input_sha256": "input-digest", "processing_version": "identity-v1"
  }]
}
```

Provider and model are persisted in RawEvaluation and validated against the
frozen requirement. Each result must match its corresponding consumed input,
including order; a missing, duplicated or foreign segment is invalid. `NO_TEXT`
requires an explicitly enabled contract and a null text. A future real adapter
must establish vendor-confirmed no-speech semantics before returning `NO_TEXT`;
failure, invalid output and unknown outcomes are not no-speech evidence.

Downstream adapters receive `formal_speaking_asr` with the ordered segment
identities, statuses and original recognition texts, plus an overall status.
The dependency reference binds the upstream RawEvaluation, fingerprint,
complete source digest and consumption. No transition text, score or prompt is
added. Process ASR remains independent: its present persisted evidence does not
establish the actual normalized audio digest and all preprocessing identities,
so equivalence to a future formal contract cannot be proved. A future reuse
design must also verify original digest, processing details, model, provider,
parameters, READY outcome, confirmed empty semantics and submission coverage;
any additional immutable evidence or migration needs separate approval.
