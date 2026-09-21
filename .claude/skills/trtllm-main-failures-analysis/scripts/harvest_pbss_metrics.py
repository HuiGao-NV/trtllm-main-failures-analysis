#!/usr/bin/env python3
"""Harvest a per-test metric from the PBSS per-test stdout logs of one CI stage across MANY builds.

Why: the dashboard's test-history only lists failures. For accuracy/threshold tests
(and any test whose *value* matters, not just pass/fail) the passing builds carry
the signal that decides whether an onset follows the `main` base commit or the
run time (Method D in SKILL.md). PBSS keeps every per-test stdout.log, for PR
builds and post-merge builds alike, so the full value timeline can be rebuilt
offline in minutes.

PBSS layout (object-store listing, no auth):
  https://pbss.s8k.io/v1/AUTH_svc_tensorrt/trtllm-ci-logs?prefix=main/<job>/<build>/&delimiter=/
      -> one prefix per stage, e.g. main/L0_MergeRequest_PR/60068/DGX_H100-4_GPUs-PyTorch-GptOss-1/
         (stage names may carry a suffix such as -cbts; match on a substring)
  <stage prefix>/<stage>_<test path with / :: [ ] -> _>_-<8 hex>-<epoch>/stdout.log
      -> <epoch> is the per-test start time in US-Pacific-as-epoch (add 7 h for UTC),
         and a build that ran the test twice (stage retry) has two directories.
  <job> is the short Jenkins job name: L0_MergeRequest_PR (PR builds) or L0_PostMerge.
  (ci_report / get_base_commit.py want the full name: LLM/main/L0_MergeRequest_PR, LLM/main/L0_PostMerge.)

Output: a TSV with one row per (build, stage, per-test log):
  job  build  stage  test  attempt_utc  attempt_epoch  host  <metric columns...>  log_path
plus rows with test="<no-stage>" / "<no-logs>" for builds that did not run the stage,
so the sweep coverage is explicit.

Metrics: --metric NAME=REGEX (repeatable). The first capture group is the value; the
LAST match in the log wins (accuracy tests print the final table last). Defaults
cover lm-eval accuracy tests:
  gpqa    = gpqa_diamond[a-z_]* average accuracy: ([0-9.]+)
  gsm8k   = gsm8k [a-z_,-]* accuracy: ([0-9.]+)
  mmlu    = mmlu[a-z_]* average accuracy: ([0-9.]+)
  evaluated = Evaluated accuracy: ([0-9.]+)
  got     = Expected accuracy >= threshold, but got ([0-9.]+)
Pass any other regex for other tests (e.g. --metric 'tps=Throughput: ([0-9.]+)').

Example (today's GPT-OSS case):
  harvest_pbss_metrics.py --job L0_MergeRequest_PR --builds 59500-60160 \
      --job L0_PostMerge --builds 2950-2963 \
      --stage-substring DGX_H100-4_GPUs-PyTorch-GptOss-1 \
      --test-regex 'test_w4_4gpus_v[12]_kv_cache' \
      --log-dir <run_dir>/pbss --out <run_dir>/metrics_<slug>.tsv
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import os
import re
import sys
import urllib.parse
import urllib.request

PBSS = "https://pbss.s8k.io/v1/AUTH_svc_tensorrt/trtllm-ci-logs"
DEFAULT_METRICS = {
    "gpqa": r"gpqa_diamond[a-z_]* average accuracy: ([0-9.]+)",
    "gsm8k": r"gsm8k [a-z_,-]* accuracy: ([0-9.]+)",
    "mmlu": r"mmlu[a-z_]* average accuracy: ([0-9.]+)",
    "evaluated": r"Evaluated accuracy: ([0-9.]+)",
    "got": r"Expected accuracy >= threshold, but got ([0-9.]+)",
}
DIR_RE = re.compile(r"/([^/]*?)_-([0-9a-f]{8})-(\d{9,11})/stdout\.log$")


def http_get(url: str, timeout: int = 60, retries: int = 3) -> bytes:
    last = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"GET {url} failed: {last}")


def pbss_list(prefix: str, delimiter: bool = False) -> list[str]:
    url = f"{PBSS}?prefix={urllib.parse.quote(prefix)}" + ("&delimiter=/" if delimiter else "")
    return http_get(url).decode().split()


def parse_builds(spec: str) -> list[str]:
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(str(i) for i in range(int(a), int(b) + 1))
        elif part:
            out.append(part)
    return out


def pacific_epoch_to_utc(epoch: int) -> str:
    # PBSS dir timestamps are US-Pacific wall clock encoded as epoch: +7 h = UTC (verified against Blue Ocean).
    return dt.datetime.fromtimestamp(epoch + 7 * 3600, dt.UTC).strftime("%Y-%m-%d %H:%M")


def harvest_build(job: str, build: str, stage_sub: str, test_re: re.Pattern, metrics: dict[str, re.Pattern],
                  log_dir: str) -> list[dict]:
    rows: list[dict] = []
    base = dict(job=job, build=build)
    try:
        stages = [s for s in pbss_list(f"main/{job}/{build}/", delimiter=True) if stage_sub in s]
    except RuntimeError as e:
        return [dict(base, stage="", test="<list-error>", note=str(e)[:120])]
    if not stages:
        return [dict(base, stage="", test="<no-stage>")]
    for st in stages:
        stage_name = st.rstrip("/").split("/")[-1]
        try:
            paths = [p for p in pbss_list(st) if p.endswith("stdout.log")]
        except RuntimeError as e:
            rows.append(dict(base, stage=stage_name, test="<list-error>", note=str(e)[:120]))
            continue
        hits = []
        for p in paths:
            m = DIR_RE.search(p)
            if not m:
                continue
            test_dir, tag, epoch = m.group(1), m.group(2), int(m.group(3))
            test = test_dir[len(stage_name) + 1:] if test_dir.startswith(stage_name + "_") else test_dir
            if test_re.search(test):
                hits.append((p, test, tag, epoch))
        if not hits:
            rows.append(dict(base, stage=stage_name, test="<no-logs>"))
            continue
        for p, test, tag, epoch in hits:
            fn = os.path.join(log_dir, f"{job}_{build}_{test}_{tag}{epoch}.log")
            try:
                if not os.path.exists(fn):
                    data = http_get(f"{PBSS}/{p}")
                    with open(fn, "wb") as f:
                        f.write(data)
                text = open(fn, "rb").read().decode("utf-8", "replace")
            except Exception as e:  # noqa: BLE001
                rows.append(dict(base, stage=stage_name, test=test, attempt_epoch=epoch,
                                 attempt_utc=pacific_epoch_to_utc(epoch), note=f"fetch-error {str(e)[:80]}"))
                continue
            row = dict(base, stage=stage_name, test=test, attempt_epoch=epoch,
                       attempt_utc=pacific_epoch_to_utc(epoch), log_path=fn)
            hm = re.search(r"'HOSTNAME': '([^']+)'", text) or re.search(r"\b(pool\d-\d{5})\b", text)
            row["host"] = hm.group(1) if hm else ""
            for name, rx in metrics.items():
                ms = rx.findall(text)
                row[name] = ms[-1] if ms else ""
            rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", action="append", required=True,
                    help="short job name, e.g. L0_MergeRequest_PR or L0_PostMerge (repeatable, paired with --builds)")
    ap.add_argument("--builds", action="append", required=True,
                    help="build range/list for the paired --job, e.g. 59500-60160 or 2950,2957-2963")
    ap.add_argument("--stage-substring", required=True, help="substring of the stage name, e.g. DGX_H100-4_GPUs-PyTorch-GptOss-1")
    ap.add_argument("--test-regex", default=".", help="regex on the per-test dir name (pytest id with / :: [ ] -> _)")
    ap.add_argument("--metric", action="append", default=[], help="NAME=REGEX with one capture group (repeatable); "
                                                                 "replaces the lm-eval defaults when given")
    ap.add_argument("--log-dir", required=True, help="where per-test stdout logs are cached")
    ap.add_argument("--out", required=True, help="output TSV")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if len(a.job) != len(a.builds):
        ap.error("--job and --builds must be paired")
    metrics = {k: re.compile(v) for k, v in (m.split("=", 1) for m in a.metric)} if a.metric else \
        {k: re.compile(v) for k, v in DEFAULT_METRICS.items()}
    test_re = re.compile(a.test_regex)
    os.makedirs(a.log_dir, exist_ok=True)
    jobs = [(j, b) for j, spec in zip(a.job, a.builds) for b in parse_builds(spec)]
    print(f"sweeping {len(jobs)} builds for stage *{a.stage_substring}* ...", file=sys.stderr)
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(harvest_build, j, b, a.stage_substring, test_re, metrics, a.log_dir) for j, b in jobs]
        for i, f in enumerate(cf.as_completed(futs), 1):
            rows.extend(f.result())
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} builds", file=sys.stderr)
    cols = ["job", "build", "stage", "test", "attempt_utc", "attempt_epoch", "host", *metrics.keys(), "log_path", "note"]
    rows.sort(key=lambda r: (r["job"], int(r["build"]), r.get("attempt_epoch") or 0))
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    with_logs = [r for r in rows if r.get("log_path")]
    print(f"wrote {len(rows)} rows ({len(with_logs)} per-test logs from "
          f"{len({(r['job'], r['build']) for r in with_logs})} builds) to {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
