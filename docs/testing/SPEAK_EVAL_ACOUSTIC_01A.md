# Speaking local acoustic metrics v1

`local-speaking-acoustic/1` is an independently declared evaluation source. It
does not depend on ASR, does not register a scoring plugin, and is scheduled
only by a published plan that explicitly requires it. It produces measurements,
not a language ability, fluency, pronunciation, or final score.

The requirement freezes the `energy-pcm-v1` algorithm and an independent audio
specification: mono, 16 kHz, PCM signed 16-bit WAV, processing version and
actual FFmpeg/FFprobe versions. Analysis uses 320-sample (20 ms) non-overlapping
frames. RMS is `sqrt(sum(sample²)/frame_sample_count)` with a threshold of
0.01 × 32768; values at the threshold are active. The last frame uses its
actual sample count. A contiguous run of low-energy frames, including a leading
or trailing run, is counted if it contains at least 24000 samples (1.5 s).
These are measurement definitions, not scoring cutoffs.

Each segment reports actual PCM duration and active duration in seconds,
active duration divided by duration, and the pooled RMS of active samples in
dBFS (20 × log10(RMS / 32768), or null when no samples are active). It also
reports the count and duration in seconds of qualifying low-energy runs. Runs
are measured independently for each segment; neither elapsed wall-clock time
between recordings nor a segment boundary creates a gap. Overall durations and
counts are sums; the overall activity ratio is total active duration divided by
total audio duration. No segment is dropped for being short.

The worker derives `speaking-source-v1` from the accepted Submission and saved
Segment/Recording facts. The source anchors all originals, their ordering,
storage references, SHA-256, media type, Submission, Attempt, PaperEntry and
TaskRevision. Preparation verifies the original bytes and full decode, reuses
matching audio or writes a content-addressed derivative; the exact output
reference, SHA-256 and processing version are included in consumed input. The
worker validates the whole source and consumption and fingerprints those facts,
the frozen requirement and payload before measuring outside the DB transaction.
On recovery the source and derivative must still verify, and the same fingerprint
must be obtained. Missing or corrupted input is a technical failure, never
silence or a zero score. A local expired lease can be recovered without the
external-provider UNKNOWN billing semantics; an old lease cannot commit.
The trusted local execution entry is `app.evaluation.speaking_acoustic.run_job`;
it prepares the audio outside the transaction and delegates claim, dispatch and
result persistence to the shared worker. Registering the contract alone does not
start a daemon or create a job.

An abbreviated synthetic result has this shape (values are illustrative):

```json
{"schema":"speaking-acoustic-metrics-v1", "submission_id":"…",
 "speaking_source_sha256":"…", "contract":{"source":"local-speaking-acoustic","version":"1"},
 "algorithm":"energy-pcm-v1", "audio_spec":{"container":"wav", "codec":"pcm_s16le", "sample_rate":16000, "channels":1, "processing_version":"acoustic-prep-v1", "ffmpeg_version":"…", "ffprobe_version":"…"},
 "segments":[{"segment_id":"…", "recording_id":"…", "position":1,
   "original_sha256":"…", "input_sha256":"…", "processing_version":"acoustic-prep-v1",
   "audio_duration_seconds":1.0, "energy_active_duration_seconds":1.0,
   "energy_active_ratio":1.0, "mean_active_rms_dbfs":-6.02,
   "long_low_energy_gap_count":0, "long_low_energy_gap_duration_seconds":0.0}],
 "aggregate":{"segment_count":1, "total_audio_duration_seconds":1.0,
   "total_energy_active_duration_seconds":1.0, "overall_energy_active_ratio":1.0,
   "total_long_low_energy_gap_count":0, "total_long_low_energy_gap_duration_seconds":0.0}}
```

The legacy `speaking_acoustic_proxy_v2` used energy measurements together with
ASR confidence and heuristic deductions. This source deliberately does not
reuse those deductions, infer absent confidence, or treat 30 seconds as an
acoustic scoring threshold. Its sample-weighted active RMS and exact partial
frame durations are explicit definitions, not claims of equivalence to the old
implementation. A future confirmed, versioned scoring plugin may consume these
facts but must specify its own approved semantics. Background noise, quiet
speech, microphone gain and natural thinking can affect these measurements;
they cannot establish pronunciation accuracy or a child's competence.
