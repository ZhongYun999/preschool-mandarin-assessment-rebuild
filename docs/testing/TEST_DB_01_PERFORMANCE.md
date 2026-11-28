# TEST-DB-01｜测试数据库性能记录

采样环境：同一 Windows 工作站、Python 3.12.7、PostgreSQL 16，独立本机测试数据库；所有回归均阻断外部模型网络。`--durations=25` 的结果是单次运行数据，不视为 CI 性能保证。

| 指标 | 原始基线 | 共享 Schema 后 |
|---|---:|---:|
| 收集数 / 结果 | 843；842 通过、1 跳过 | 862；861 通过、1 跳过 |
| 本机完整回归 | 453.70 秒（首次）；446.34 秒（补采迁移计时） | 284.22 秒 |
| fixture 初始化迁移 | 642 次、181.92 秒 | 共享 1 次、0.29 秒；独立 28 次（并入下行耗时） |
| 测试内及独立升级 | 13 次、1.20 秒 | 独立合计 41 次、8.57 秒 |
| 普通测试清理 | 每项销毁独立 Schema；未单独记录累计销毁耗时 | 624 次、0 次失败、99.79 秒 |

补采基线使用同一提交的独立检出，附加轻量计时插件。与补采基线相比，完整回归减少约 36.3%；与首次本机基线相比减少约 37.4%。用户提供的 CI 基线为 534.08 秒，因机器环境不同仅作参考。原始基线实际共调用 Alembic upgrade 655 次；优化后主测试进程共 42 次。故障清理验证会单独启动一个子 Pytest Session，另外初始化一次共享 Schema 并清理两项测试，该子进程不包含在主进程统计中。独立测试中自建 Schema 的 `test_migrations` 计入独立升级次数，不计入 fixture 初始化次数。

## 基线最慢 25 项

```text
3.04s setup    tests/test_taskbank.py::test_asset_update_racing_with_first_reference_is_rejected
2.54s setup    tests/test_speaking_text_providers.py::test_invalid_model_response_is_never_ready[content8-stop-deepseek]
2.19s setup    tests/test_speaking_submission.py::test_submission_competing_operations[record]
1.88s setup    tests/test_speaking_process_asr.py::test_failure_retry_without_rerecording[INVALID]
1.82s call     tests/test_speaking_formal_asr.py::test_subset_consumption_keeps_complete_source_identity
1.49s setup    tests/test_session_credential_revocation.py::test_revoke_and_writes_serialized[write-close]
1.45s setup    tests/test_review_policy.py::test_sampled_tasks_require_review_and_snapshot_immutable
1.44s setup    tests/test_speaking_checkpoint_a.py::test_failed_segment_transaction_leaves_recording_unconfirmed
1.19s call     tests/test_flow01c_settlement.py::test_worker_restart_in_fresh_process
1.10s setup    tests/test_qwen_qa_integration.py::test_asr_failure_blocks_text_call
0.96s setup    tests/test_qa_http.py::test_qa_http_recording_flow_and_recovery
0.93s call     tests/test_flow01c_settlement.py::test_last_review_and_worker_race
0.90s call     tests/test_dashscope_asr_integration.py::test_late_asr_after_redo_cannot_contaminate_result
0.89s setup    tests/test_evaluation_dispatcher.py::test_old_unstarted_job_is_skipped_and_never_rescanned
0.88s setup    tests/test_paper.py::test_excel_import_selected_revision_stays_pinned
0.87s call     tests/test_speaking_acoustic.py::test_full_job_creates_one_traced_result[4]
0.83s setup    tests/test_integ_01.py::test_worker_competition_restart_and_corrupted_audio
0.82s call     tests/test_migrations.py::test_postgres_connection_and_migration_cycle
0.82s setup    tests/test_evaluation_worker.py::test_frozen_plan_idempotency_and_zero_value
0.78s call     tests/test_speaking_qwen_worker.py::test_three_segments_failure_and_explicit_recovery
0.78s call     tests/test_speaking_qwen_worker.py::test_every_segment_succeeds_and_input_is_frozen[3]
0.75s setup    tests/test_fixed_audio.py::test_copy_preserves_config_and_archive_is_not_ready
0.71s call     tests/test_speaking_acoustic.py::test_full_job_creates_one_traced_result[3]
0.71s setup    tests/test_assessment_session.py::test_creation_serializes_with_paper_archive
0.68s setup    tests/test_score_review.py::test_legacy_unverified_pending_blocks_finalization
```

## 优化后最慢 25 项

```text
1.56s call     tests/test_db_fixture_lifecycle.py::test_failed_test_cleans_and_releases_schema
1.19s call     tests/test_flow01c_settlement.py::test_worker_restart_in_fresh_process
0.83s call     tests/test_speaking_acoustic.py::test_full_job_creates_one_traced_result[4]
0.82s call     tests/test_migrations.py::test_postgres_connection_and_migration_cycle
0.80s call     tests/test_speaking_qwen_worker.py::test_three_segments_failure_and_explicit_recovery
0.75s call     tests/test_speaking_qwen_worker.py::test_every_segment_succeeds_and_input_is_frozen[3]
0.69s call     tests/test_speaking_acoustic.py::test_full_job_creates_one_traced_result[3]
0.67s call     tests/test_recording_submission_idempotency.py::test_recorded_submission_retry_after_close_and_completion[qa]
0.65s call     tests/test_speaking_acoustic.py::test_redo_preserves_old_result_and_anchors_new_submission
0.61s setup    tests/test_evaluation_dispatcher.py::test_waiting_dependency_can_become_ready_from_persistent_fact
0.60s call     tests/test_speaking_qwen_worker.py::test_formal_asr_handoff_preserves_evidence_identity
0.59s call     tests/test_speaking_qwen_worker.py::test_actual_dashscope_sdk_transport_is_intercepted_in_full_chain
0.59s call     tests/test_recording_submission_idempotency.py::test_recorded_submission_retry_after_close_and_completion[repetition]
0.59s call     tests/test_speaking_qwen_worker.py::test_every_segment_succeeds_and_input_is_frozen[2]
0.58s call     tests/test_dashscope_asr_integration.py::test_late_asr_after_redo_cannot_contaminate_result
0.58s call     tests/test_speaking_qwen_worker.py::test_aggregation_failure_does_not_repeat_provider
0.55s call     tests/test_speaking_acoustic.py::test_full_job_creates_one_traced_result[2]
0.52s call     tests/test_speaking_submission.py::test_manifest_frozen_counts_and_idempotence
0.51s call     tests/test_qwen_qa_integration.py::test_old_llm_result_cannot_change_new_final_result
0.50s setup    tests/test_evaluation_worker.py::test_downgrade_refuses_existing_evidence
0.50s call     tests/test_speaking_checkpoint_a.py::test_checkpoint_and_recovery[guide]
0.49s call     tests/test_speaking_guidance_delivery.py::test_unavailable_void_serializes_with_playback_and_redo[redo]
0.49s call     tests/test_speaking_formal_asr.py::test_redo_cannot_use_previous_formal_recognition
0.49s call     tests/test_speaking_guidance_delivery.py::test_unavailable_void_serializes_with_playback_and_redo[playback]
0.48s call     tests/test_flow01c_settlement.py::test_http_review_nonlinear_and_idempotency
```
