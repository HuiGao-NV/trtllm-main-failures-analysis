# OpenSearch Field Schema

All `s_*` fields are `keyword` type — no `.keyword` suffix needed for aggregations.
Use `term` for exact match; use `wildcard` for partial/pattern match.

## stage_info fields

Index pattern: `df-swdl-trtllm-infra-ci-prod-stage_info-*`

| Field | Type | Description |
|-------|------|-------------|
| `s_stage_name` | keyword | Stage name (e.g., "DGX_B200-PyTorch-1", "A10-PackageSanityCheck-PY310-UB2204") |
| `s_stage_type` | keyword | Stage type — use `"TopParallelBranch"` to filter for actual stage runs |
| `s_status` | keyword | SUCCESS / FAILURE / SKIP / ABORTED |
| `s_job_name` | keyword | Jenkins job name (e.g., "LLM/main/L0_MergeRequest_PR", "LLM/main/L0_PostMerge") |
| `s_build_id` | keyword | Jenkins build number |
| `s_host_node_name` | keyword | Node that ran the stage (e.g., "dl325g11-0036.ipp2u2.colossus") |
| `s_host_node_id` | keyword | Node ID (often empty) |
| `s_gpu_type` | keyword | GPU type |
| `s_platform` | keyword | Platform name |
| `s_trigger_mr_id` | keyword | GitHub PR number that triggered this run |
| `s_trigger_mr_commit` | keyword | Git commit SHA |
| `s_trigger_mr_link` | keyword | URL to the PR/MR |
| `s_trigger_mr_user` | keyword | User who triggered the run |
| `s_failure_reason` | keyword | Failure reason category |
| `s_blue_ocean_link` | keyword | Blue Ocean URL for this stage |
| `s_blue_ocean_log_link` | keyword | Blue Ocean log URL |
| `b_is_PR_job` | boolean | Whether this is a PR-triggered job |
| `b_is_post_merge` | boolean | Whether this is a post-merge job |
| `ts_created` | date (epoch ms) | Stage start timestamp |
| `l_e2e_time_ms` | long | End-to-end time in milliseconds |
| `l_run_time_ms` | long | Run time in milliseconds |
| `l_wait_time_ms` | long | Wait time in milliseconds |
| `s_ai_failure_type` | keyword | High-level AI classification: Infra / User / Infra or User (coarse, may be inaccurate) |
| `s_ai_failure_type_long` | keyword | Detailed AI classification: Test assertion failure, Dependency issue, Slurm issue, Network issue, Configuration error, Flaky test, Timeout, Machine issue, Disk issue, Import error, Jenkins issue, Model conversion error, Compilation error, Artifactory issue, CUDA error, CUDA OOM, Performance regression, Docker issue, Unknown, Tests not run |
| `ni_ai_failure_comment` | text | AI-generated root cause analysis with evidence (not indexed) |
| `ni_ai_suggestion` | text | AI-generated fix suggestion (not indexed) |
| `ni_failure_reason_details` | text | Detailed failure reason from CI pipeline (not indexed) |
| `ts_failure_analyzed` | date | When AI analysis was performed |

## test_info fields

Index pattern: `df-swdl-trtllm-infra-ci-prod-test_info-*`

| Field | Type | Description |
|-------|------|-------------|
| `s_test_name` | keyword | Full test name (e.g., "test_accuracy[nvfp4-4-attn_dp_on-trtllm]") |
| `s_turtle_name` | keyword | Alternative test name field |
| `s_class_name` | keyword | Test class name |
| `s_stage_name` | keyword | Stage that ran this test |
| `s_status` | keyword | PASSED / FAILED / SKIPPED |
| `s_gpu_type` | keyword | GPU model: A10, H100_PCIe, V100, L40, RTX5080, GB200, DGX_B200 |
| `s_build_id` | keyword | Jenkins build ID |
| `s_job_name` | keyword | Jenkins job name |
| `s_trigger_mr_id` | keyword | PR/MR number |
| `s_trigger_mr_commit` | keyword | Git commit SHA |
| `s_trigger_mr_user` | keyword | User who triggered the run |
| `s_host_node_name` | keyword | Execution node |
| `s_short_error_msg` | keyword | Short error message |
| `ni_error_msg` | text | Full error message (not indexed) |
| `ni_error_stack_trace` | text | Error stack trace (not indexed) |
| `ni_stderr` | text | Stderr output (not indexed) |
| `ts_created` | date (epoch ms) | Test execution timestamp |
| `l_e2e_time_ms` | long | Test duration in milliseconds |
| `l_gpu_count` | long | Number of GPUs used |
| `b_is_unit_test` | boolean | Whether this is a unit test |

## Field naming conventions

- `s_*` — String/keyword fields (all keyword, no `.keyword` suffix needed)
- `ts_*` — Timestamp fields (epoch milliseconds)
- `b_*` — Boolean fields
- `l_*` — Long integer fields
- `ni_*` — Not-indexed text fields (display only, cannot aggregate/filter)
