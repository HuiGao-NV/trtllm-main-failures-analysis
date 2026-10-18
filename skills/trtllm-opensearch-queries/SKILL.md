---
name: trtllm-opensearch-queries
description: Helps users access the OpenSearch database for TRT-LLM CI stage and test data. Use when the user asks about TRT-LLM stages, tests, failures, stage counts by platform, test history, test/stage with nodes, OpenSearch, Open DB, or querying gpuwa.nvidia.com/opensearch.
---

# TRT-LLM OpenSearch Queries

## Connection

- Base URL: `http://gpuwa.nvidia.com/opensearch`
- **No auth needed** — publicly accessible on NVIDIA network.
- All queries use `POST {base_url}/{index}/_search` with JSON body.

## Indexes & Key Fields

| Index | Pattern | Use for |
|-------|---------|---------|
| `stage_info` | `df-swdl-trtllm-infra-ci-prod-stage_info-*` | Stage runs, platform failures, build info, node lookup |
| `test_info` | `df-swdl-trtllm-infra-ci-prod-test_info-*` | Test results, test failures, PR test failures |

**Critical rules:**
- All `s_*` fields are already `keyword` type — **never** append `.keyword` in queries or aggregations
- Use `term` for exact match, `wildcard` for partial match (`*`/`?`). Avoid `match` on keyword fields.
- Always filter `{"term": {"s_stage_type": "TopParallelBranch"}}` on stage_info to exclude sub-stages
- In test_info queries, add `"must_not":[{"term":{"s_test_name":"Stage Failed"}}]` to exclude stage-level markers
- Always include `{"range": {"ts_created": {"gte": ...}}}` to bound the time window
- Timestamps are **epoch milliseconds**: `$(date -u -d "N days ago" +%s)000`

**Most-used fields** (full schema: [references/schema.md](references/schema.md)):

| Field | In | Description |
|-------|----|-------------|
| `s_stage_name` | stage | Stage name (e.g., "DGX_B200-PyTorch-1") |
| `s_status` | both | Stage: SUCCESS/FAILURE/SKIP/ABORTED. Test: PASSED/FAILED/SKIPPED |
| `s_job_name` | both | Jenkins job (e.g., "LLM/main/L0_MergeRequest_PR") |
| `s_build_id` | both | Jenkins build number |
| `s_host_node_name` | both | Execution node (e.g., "dl325g11-0036.ipp2u2.colossus") |
| `s_trigger_mr_id` | both | GitHub PR number |
| `s_test_name` | test | Full test name (keyword — use `wildcard` for partial) |
| `s_gpu_type` | test | GPU: A10, H100_PCIe, V100, L40, DGX_B200, GB200 |
| `s_short_error_msg` | test | Short error message |
| `ni_error_msg` | test | Full error (not indexed — display only) |
| `ts_created` | both | Timestamp (epoch ms) |

**Failure analysis fields** (stage_info only):

| Field | Type | Description |
|-------|------|-------------|
| `s_ai_failure_type` | keyword | High-level: Infra / User / Infra or User (coarse, may be inaccurate) |
| `s_ai_failure_type_long` | keyword | Detailed type: Test assertion failure, Dependency issue, Slurm issue, Network issue, Flaky test, Timeout, etc. |
| `ni_ai_failure_comment` | text | AI-generated root cause analysis (not indexed — display only) |
| `ni_ai_suggestion` | text | AI-generated fix suggestion (not indexed — display only) |

**Analyzing failure reasons**: Do NOT rely solely on `s_ai_failure_type` (Infra/User) — it is often inaccurate. Instead:
1. Aggregate by `s_ai_failure_type_long` for detailed classification
2. Fetch `ni_ai_failure_comment` and `ni_ai_suggestion` for root cause context
3. Cross-reference with `s_job_name` to interpret the failure:
   - **L0_MergeRequest_PR / L0_MergeRequest**: PR-triggered CI — could be PR code issue OR infra problem. Check if the comment says "pre-existing blocker" or "PR-caused".
   - **L0_PostMerge**: main/release branch — if failing, it's either a main branch code regression OR infra. PR code is not a factor.
   - **release/*/L0_PostMerge**: release branch — same as PostMerge but for a release branch.

---

## Query Routing

| User asks about | Index | Key filters |
|-----------------|-------|-------------|
| Stage failures, platform status, CI overview | `stage_info` | `s_stage_type`, `s_status`, `s_stage_name` |
| Test results, failing tests, flaky tests | `test_info` | `s_status`, `s_test_name`, `s_gpu_type` |
| Which node ran a stage/test | either | `s_host_node_name` |
| PR failures | either | `s_trigger_mr_id` |

## Query Recipes

### Recipe 1: Stage failures by platform

**Input**: "How many stage failures per platform today?"
**Index**: `df-swdl-trtllm-infra-ci-prod-stage_info-*`

```json
{"size":0,"query":{"bool":{"must":[
  {"term":{"s_stage_type":"TopParallelBranch"}},
  {"term":{"s_status":"FAILURE"}},
  {"range":{"ts_created":{"gte":"AGO_MS"}}}
]}},"aggs":{"by_platform":{"terms":{"field":"s_stage_name","size":50}}}}
```

**Output**: `aggregations.by_platform.buckets` → table of platform + failure count.

Example (real data):
```
| Platform                                   | Failures |
|--------------------------------------------|----------|
| RTX5090-PackageSanityCheck-PY312-UB2404    | 34       |
| A10-PackageSanityCheck-PY310-UB2204        | 31       |
| DGX_B200-8_GPUs-PyTorch-1                  | 17       |
```

### Recipe 2: Failed tests for a PR

**Input**: "What tests failed in PR #11993?"
**Index**: `df-swdl-trtllm-infra-ci-prod-test_info-*`

```json
{"size":200,"query":{"bool":{"must":[
  {"term":{"s_trigger_mr_id":"11993"}},
  {"term":{"s_status":"FAILED"}}
]}},"sort":[{"ts_created":{"order":"desc"}}],
"_source":["s_test_name","s_stage_name","s_gpu_type","ts_created","s_short_error_msg"]}
```

**Output**: `hits.hits[]._source` → table of test, stage, GPU, error.

### Recipe 3: Stage failure history

**Input**: "Show recent failures for DGX_B200-PyTorch-1"
**Index**: `df-swdl-trtllm-infra-ci-prod-stage_info-*`

```json
{"size":50,"query":{"bool":{"must":[
  {"term":{"s_stage_type":"TopParallelBranch"}},
  {"term":{"s_status":"FAILURE"}},
  {"term":{"s_stage_name":"DGX_B200-PyTorch-1"}},
  {"range":{"ts_created":{"gte":"AGO_MS"}}}
]}},"sort":[{"ts_created":{"order":"desc"}}],
"_source":["s_stage_name","s_build_id","s_job_name","s_host_node_name","s_trigger_mr_id","ts_created"]}
```

**Output**: `hits.hits[]._source` → table of time, build, job, node, PR.

### Recipe 4: Test history (pass/fail timeline)

**Input**: "Show history of test_accuracy[nvfp4-4-attn_dp_on-trtllm]"
**Index**: `df-swdl-trtllm-infra-ci-prod-test_info-*`

Exact name → `term`; partial name → `wildcard`:
```json
{"size":100,"query":{"bool":{"must":[
  {"term":{"s_test_name":"test_accuracy[nvfp4-4-attn_dp_on-trtllm]"}},
  {"range":{"ts_created":{"gte":"AGO_MS"}}}
]}},"sort":[{"ts_created":{"order":"desc"}}],
"_source":["s_test_name","s_status","s_gpu_type","s_stage_name","ts_created"]}
```

For partial match, replace the `term` clause with:
```json
{"wildcard":{"s_test_name":"*test_accuracy*nvfp4*"}}
```

**Output**: `hits.hits[]._source` → timeline table. Calculate pass rate from results.

### Recipe 5: CI status overview

**Input**: "Give me a CI overview for the last 24 hours"
**Index**: `df-swdl-trtllm-infra-ci-prod-stage_info-*`

```json
{"size":0,"query":{"bool":{"must":[
  {"term":{"s_stage_type":"TopParallelBranch"}},
  {"range":{"ts_created":{"gte":"AGO_MS"}}}
]}},"aggs":{
  "by_status":{"terms":{"field":"s_status","size":10}},
  "by_job":{"terms":{"field":"s_job_name","size":20}}
}}
```

**Output** (real data):
```
| Status  | Count | %     |     | Job                         | Count |
|---------|-------|-------|     |-----------------------------|-------|
| SUCCESS | 3075  | 57.3% |     | LLM/main/L0_MergeRequest_PR | 4872  |
| SKIP    | 1006  | 18.7% |     | LLM/main/L0_PostMerge       | 302   |
| ABORTED | 979   | 18.2% |     | LLM/release-1.2/L0_PostMerge| 105   |
| FAILURE | 307   | 5.7%  |     | LLM/main/L0_MergeRequest    | 88    |
```

### Recipe 6: Node lookup for a build/stage

**Input**: "Which node ran build #30432 DGX_B200-PyTorch-1?"
**Index**: `df-swdl-trtllm-infra-ci-prod-stage_info-*`

```json
{"size":5,"query":{"bool":{"must":[
  {"term":{"s_stage_type":"TopParallelBranch"}},
  {"term":{"s_build_id":"30432"}},
  {"term":{"s_stage_name":"DGX_B200-PyTorch-1"}}
]}},"_source":["s_stage_name","s_host_node_name","s_build_id","s_status","s_job_name"]}
```

**Output**: `hits.hits[0]._source.s_host_node_name` → the node name.

### Recipe 7: Top failing tests by GPU

**Input**: "Top 10 failing tests on A10 in the last 7 days?"
**Index**: `df-swdl-trtllm-infra-ci-prod-test_info-*`

```json
{"size":0,"query":{"bool":{"must":[
  {"term":{"s_status":"FAILED"}},
  {"term":{"s_gpu_type":"A10"}},
  {"range":{"ts_created":{"gte":"AGO_MS"}}}
],"must_not":[
  {"term":{"s_test_name":"Stage Failed"}}
]}},"aggs":{"top_tests":{"terms":{"field":"s_test_name","size":10}}}}
```

**Output** (real data):
```
| # | Test                                                  | Failures |
|---|-------------------------------------------------------|----------|
| 1 | test_run_with_different_env[1]                        | 70       |
| 2 | test_unittests_v2[unittest/disaggregated/test_agent..] | 70       |
| 3 | test_unittests_v2[unittest/auto_deploy/singlegpu/smoke]| 42       |
```
