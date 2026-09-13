#!/usr/bin/env python3
"""Get the full, untruncated error message/callstack for one failing test
execution, given its (job, build, stage, entity_name) - the same identifying
info already present in each `fetch_execution_details.py` execution record.

`fetch_execution_details.py`'s `short_error_msg` (`s_short_error_msg` from
ci_report's `/api/job/<job>/<build>`) is frequently truncated at the source.
This script gets the real thing, tried in this order:

  1. `_pbss_log` on the test's own record (within that job's
     tests_by_stage[stage]) - the full console/pytest log for the build,
     already embedded in the same ci_report job JSON `fetch_execution_details.py`
     fetches. This is usually enough; no second network call needed. A
     sub-test entity_name's own record (e.g. one whose `s_turtle_name` is
     `<file>::<Class>::<test>`) does NOT carry its own `_pbss_log` - only the
     file-level record (`s_turtle_name` == the bare file path, the one
     pytest invocation that ran every test in that file) does, since they
     all share one underlying log. For a sub-test entity_name, this script
     falls back to that file-level record's `_pbss_log` automatically
     (confirmed empirically: only the file-level record in
     `tests_by_stage[stage]` has a non-empty `_pbss_log` key at all).
  2. If that record has no `_pbss_log`, or it doesn't contain a pytest
     failure block matching the test filter, fall back to fetching the raw
     Jenkins Blue Ocean log directly at the stage's `log_link` (resolved via
     `fetch_execution_details.find_stage_log_link()`, the same "Log" URL
     ci_report's own UI shows for that stage) and search that instead.

Either way, a pytest failure block is found by matching pytest's own
underscore-delimited header line (e.g. `___ TestClass.test_name ___` or
`___ ERROR at setup of TestClass.test_name ___`) against a test-name filter
derived from --entity-name (the part after "::"), and extracting everything
up to the next such header line.

For a file-level entity_name (no "::", e.g. a whole test file rolled up into
one Main Break group), there's no single test to filter by - ALL failure
blocks found in the log are returned instead, since a file-level group can
aggregate several different sub-tests' distinct failures (seen in practice:
`test_trtllm_serve_e2e.py`'s file-level group rolled up both an unrelated
NameError in the Flux sub-tests and a separate server-startup timeout in the
Wan sub-tests).

Only if neither `_pbss_log` nor `log_link` yields a matching block does this
script come up empty (`blocks: []`) - that's a signal to fall back further to
manual Jenkins/Blue Ocean node navigation (per-CI-quirk enough - nested
pipeline stages, Blue Ocean's node-id indirection - that it isn't worth
hardcoding here), not a bug in this script.

  3. Independent of 1-2, and merged with whatever they found (not just a
     fallback for when they come up empty): the case's own JUnit XML from the
     stage's uploaded test-results artifact. This exists because pytest's
     console output truncates/loses information the structured XML keeps
     intact, and for a whole-directory/file-level stage invocation (many
     sub-tests run under one pytest call, e.g. a `-k <expr>` directory sweep)
     the console-log regex match can miss a specific sub-test's block
     entirely even though the XML always has it, keyed cleanly by test name -
     confirmed in practice: this recovered a sub-test's failure that steps
     1-2 found nothing for at all.

     Resolved by walking the Blue Ocean pipeline graph forward from the
     stage's own node (the same node `log_link` above points at) through its
     `edges` until reaching a node named "Submit Test Result" or "Generate
     Report" - which one shows up depends on the stage's pipeline shape (a
     plain unittest stage: `<stage node> -> "Initialize Test" -> "[<stage>]
     Run Pytest" -> "Submit Test Result"`; an accuracy/perf stage instead
     routes through "Generate Report"). Whichever node is reached, every
     "Upload artifacts" step under it is checked (there can be more than
     one - "Generate Report" in particular uploads an unrelated rerun-report
     HTML from one of them and the actual test-results archive from
     another), parsing each step's log for the artifact URL it deployed to
     Artifactory (an unauthenticated `.tar.gz`, e.g.
     `.../test-results/results-<stage>.tar.gz`). That archive contains one
     JUnit XML per pytest invocation on that stage (`results-sub-unittests-
     <sanitized-case>.xml`, one `<testcase>` per test actually run). The
     target XML's exact filename is read straight out of the console text
     already fetched in steps 1-2 (pytest's local-variable dump on failure
     includes a literal `output_xml = '<path>'` line - use its basename); if
     that hint isn't available, every XML in the archive is scanned for a
     `<testcase>` whose name/classname matches instead, since guessing the
     filename purely from `--entity-name` is unreliable for a directory-level
     stage invocation (the sanitized name is derived from the invocation's
     `case` expression, e.g. `unittest/_torch/multimodal -k "not ..."`, not
     from any one sub-test's own name).

     The final `blocks[].text` is the **merge** of both sources when the XML
     match succeeds: the console-log text (if any) followed by the XML
     `<failure>`/`<error>` element's text, clearly separated - never just one
     replacing the other, since either can carry detail the other lacks.
"""
import argparse
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_execution_details as fed  # noqa: E402

# Matches a Blue Ocean node-log or node-page URL, e.g.
#   https://<host>/<instance>/blue/rest/organizations/jenkins/pipelines/<job-path>/runs/<run>/nodes/<node>/log/
#   https://<host>/<instance>/blue/rest/organizations/jenkins/pipelines/<job-path>/runs/<run>/nodes/<node>/
BLUE_OCEAN_NODE_URL_RE = re.compile(
    r"^(?P<base>https?://[^/]+/[^/]+/blue/rest/organizations/jenkins/pipelines/.+?/runs/(?P<run>[^/]+))/nodes/(?P<node>[^/]+)/"
)

ARTIFACT_URL_RE = re.compile(r"Deploying artifact:\s*(\S+\.(?:tar\.gz|tgz|zip))")

# The literal local-variable dump pytest prints on a traceback that reaches
# the harness's `output_xml` variable, e.g.:
#   output_xml = '/home/.../results-sub-unittests-unittest-_torch-....xml'
OUTPUT_XML_HINT_RE = re.compile(r"output_xml\s*=\s*'([^']+\.xml)'")

# Pytest's own failure-block delimiter, e.g.:
#   ___________________________ TestClass.test_name ___________________________
#   ___ ERROR at setup of TestClass.test_name ___
HEADER_RE = re.compile(r"^_{10,}\s*(.*?)\s*_{10,}$")

# Pytest's "warnings summary" section delimiter, e.g.:
#   ==================================== warnings summary =====================================
# This section is appended by pytest *after* the failure traceback(s) - it's a
# collected list of unrelated warnings (deprecation notices, unknown marks,
# etc.) from the whole test run, not part of the actual failure. It uses "="
# delimiters, not the "_" ones HEADER_RE matches, so it never ends a block on
# its own; it has to be trimmed separately.
WARNINGS_SUMMARY_RE = re.compile(r"^=+\s*warnings summary\s*=+$", re.IGNORECASE)

# Jenkins' raw Blue Ocean node-log API (the `log_link` fallback source)
# prepends a timestamp to *every* line, e.g.:
#   [2026-09-09T06:43:44.279Z] _____________ test_openai_chat_harmony _____________
# `_pbss_log` (the primary source) has no such prefix. Without stripping it,
# HEADER_RE/WARNINGS_SUMMARY_RE never match anything in a log_link-sourced
# log (the line never starts with "_"/"=" after stripping whitespace) - this
# was a real bug here: every log_link fallback silently returned 0 blocks
# regardless of whether the log actually contained a FAILURES section.
JENKINS_TS_PREFIX_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[\d:.]+Z\]\s?")


def strip_jenkins_ts_prefix(line: str) -> str:
    return JENKINS_TS_PREFIX_RE.sub("", line, count=1)


def extract_failure_blocks(log_text: str, test_filter: str | None) -> list[dict]:
    lines = [strip_jenkins_ts_prefix(l) for l in log_text.splitlines()]
    headers = []
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line.strip())
        if m:
            headers.append((i, m.group(1).strip()))
    blocks = []
    for idx, (start, title) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        if test_filter and test_filter not in title and test_filter.replace("::", ".") not in title:
            continue
        block_lines = lines[start:end]
        for i, line in enumerate(block_lines):
            if WARNINGS_SUMMARY_RE.match(line.strip()):
                block_lines = block_lines[:i]
                break
        blocks.append({"title": title, "text": "\n".join(block_lines).rstrip()})
    return blocks


def guess_test_filter(entity_name: str) -> str | None:
    """None for a file-level entity_name (no '::') - caller then returns
    every failure block found rather than filtering to one test."""
    if "::" not in entity_name:
        return None
    return entity_name.split("::", 1)[1]


def fetch_text(url: str, timeout: int = 30, retries: int = 2) -> str:
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"Accept": "text/plain"})
        try:
            with fed._DIRECT_OPENER.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            e.close()
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < retries:
            import time
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url)
    with fed._DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def fetch_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with fed._DIRECT_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


ARTIFACT_UPLOAD_NODE_NAMES = ("Submit Test Result", "Generate Report")


def find_artifact_upload_node(base: str, run: str, start_node: str, max_hops: int = 15) -> str | None:
    """Walk the Blue Ocean node graph forward from `start_node` via each
    node's `edges`, looking for a node named "Submit Test Result" or
    "Generate Report" - which of the two shows up depends on the stage's
    pipeline shape (a plain unittest stage goes straight to "Submit Test
    Result"; an accuracy/perf stage routes through "Generate Report"
    instead, which contains its own "Upload artifacts" step(s) - one of
    which, in practice, uploads an unrelated rerun-report HTML rather than
    the test-results archive, so every "Upload artifacts" step found under
    whichever node matches here needs to be checked, not just the first).
    Follows only the first edge at each hop (a single chain in every stage
    shape observed so far, not a branch); a stage whose graph genuinely
    branches before reaching either node won't be found, which surfaces as
    this function returning None, same as any other lookup miss."""
    node = start_node
    for _ in range(max_hops):
        try:
            data = fetch_json(f"{base}/nodes/{node}/")
        except Exception:
            return None
        if data.get("displayName") in ARTIFACT_UPLOAD_NODE_NAMES:
            return node
        edges = data.get("edges") or []
        if not edges:
            return None
        node = edges[0].get("id")
        if not node:
            return None
    return None


def find_upload_artifacts_steps(base: str, run: str, node: str) -> list[str]:
    try:
        steps = fetch_json(f"{base}/nodes/{node}/steps/")
    except Exception:
        return []
    return [step.get("id") for step in steps if step.get("displayName") == "Upload artifacts"]


def find_artifact_urls(base: str, run: str, node: str, steps: list[str]) -> list[str]:
    urls = []
    for step in steps:
        try:
            log_text = fetch_text(f"{base}/nodes/{node}/steps/{step}/log/?start=0")
        except Exception:
            continue
        urls.extend(ARTIFACT_URL_RE.findall(log_text))
    return urls


def extract_testcase_failure_text(testcase: ET.Element) -> str | None:
    """Return a pytest-style combined failure/error text for one <testcase>
    element, or None if it has neither (i.e. it passed)."""
    parts = []
    for tag in ("failure", "error"):
        for el in testcase.findall(tag):
            msg = el.get("message")
            if msg:
                parts.append(f"{tag}: {msg}")
            if el.text and el.text.strip():
                parts.append(el.text.strip())
    if not parts:
        return None
    return "\n\n".join(parts)


def find_matching_testcases(xml_bytes: bytes, test_filter: str | None) -> list[dict]:
    """Parse one JUnit XML file's bytes and return every <testcase> that
    failed/errored and (if test_filter is given) whose name or classname
    contains it - the same "no filter -> return everything" convention
    extract_failure_blocks() uses for a file-level entity_name."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out = []
    for tc in root.iter("testcase"):
        text = extract_testcase_failure_text(tc)
        if text is None:
            continue
        name = tc.get("name") or ""
        classname = tc.get("classname") or ""
        if test_filter and test_filter not in name and test_filter not in classname:
            continue
        out.append({"name": name, "classname": classname, "text": text})
    return out


def artifact_cache_path(artifacts_dir: Path, job: str, build: str, url: str) -> Path:
    """Deterministic local filename for a given artifact URL, scoped by
    build - the URL's own basename alone isn't unique enough (the same
    stage name's artifact filename, e.g. `results-DGX_B200-PyTorch-4.tar.gz`,
    recurs across every build of that stage), but (job, build, basename)
    is."""
    job_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", job)
    basename = Path(urllib.parse.urlparse(url).path).name
    return artifacts_dir / f"{job_slug}_{build}_{basename}"


def fetch_or_reuse_artifact(url: str, artifacts_dir: Path, job: str, build: str) -> tuple[bytes, Path, bool]:
    """Downloads `url` into `artifacts_dir` (creating it if needed), unless a
    file for this exact (job, build, url) is already there, in which case
    that's read and reused instead - the common case in practice, since one
    stage's artifact is shared by every sub-test's fetch_full_error.py call
    for that stage within a run."""
    local_path = artifact_cache_path(artifacts_dir, job, build, url)
    if local_path.exists():
        return local_path.read_bytes(), local_path, True
    data = fetch_bytes(url)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    return data, local_path, False


def fetch_junit_xml_matches(
    job_data: dict, job: str, build: str, stage: str, test_filter: str | None,
    output_xml_hint: str | None, artifacts_dir: Path,
) -> tuple[list[dict], list[dict]]:
    """Steps 3 of the module docstring. Returns (matches, artifacts_tried) -
    matches is a list of {name, classname, text} dicts, empty if nothing
    could be resolved/matched at any point (never raises for that - this is
    an enhancement over steps 1-2, not something that should fail the whole
    script if the pipeline shape doesn't match what's expected). artifacts_tried
    is a list of {url, local_path, reused} for every artifact actually
    downloaded/reused while looking for a match - useful even on a miss, to
    see what was checked."""
    artifacts_tried: list[dict] = []

    log_link = fed.find_stage_log_link(job_data, stage)
    m = BLUE_OCEAN_NODE_URL_RE.match(log_link or "")
    if not m:
        return [], artifacts_tried
    base, run, node = m.group("base"), m.group("run"), m.group("node")

    upload_node = find_artifact_upload_node(base, run, node)
    if not upload_node:
        return [], artifacts_tried
    upload_steps = find_upload_artifacts_steps(base, run, upload_node)
    if not upload_steps:
        return [], artifacts_tried
    artifact_urls = find_artifact_urls(base, run, upload_node, upload_steps)
    if not artifact_urls:
        return [], artifacts_tried

    for artifact_url in artifact_urls:
        try:
            archive_bytes, local_path, reused = fetch_or_reuse_artifact(artifact_url, artifacts_dir, job, build)
        except Exception:
            continue
        artifacts_tried.append({"url": artifact_url, "local_path": str(local_path), "reused": reused})

        members: dict[str, bytes] = {}
        try:
            if artifact_url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                    for name in zf.namelist():
                        if name.endswith(".xml"):
                            members[Path(name).name] = zf.read(name)
            else:
                with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as tf:
                    for member in tf.getmembers():
                        if member.isfile() and member.name.endswith(".xml"):
                            extracted = tf.extractfile(member)
                            if extracted:
                                members[Path(member.name).name] = extracted.read()
        except Exception:
            continue

        if output_xml_hint and output_xml_hint in members:
            matches = find_matching_testcases(members[output_xml_hint], test_filter)
            if matches:
                return matches, artifacts_tried

        # Hint missing/didn't pan out - scan every XML in the archive.
        all_matches = []
        for xml_bytes in members.values():
            all_matches.extend(find_matching_testcases(xml_bytes, test_filter))
        if all_matches:
            return all_matches, artifacts_tried

    return [], artifacts_tried


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--job", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--entity-name", required=True, help="Full entity_name from fetch_failures.py, e.g. 'unittest/x/test_foo.py::TestClass::test_case', or just the file path for a file-level entity")
    parser.add_argument("--test-filter", default=None, help="Override the substring used to pick failure block(s) out of the log (default: derived from --entity-name's part after '::'; unset -> return ALL blocks found, for file-level entities)")
    parser.add_argument("--out", default=None, help="Output JSON path (default: print to stdout)")
    parser.add_argument(
        "--artifacts-dir", default=None,
        help="Directory to download/reuse step-3 test-results artifacts in (default: an 'artifacts' "
             "subfolder next to --out, i.e. <run_dir>/artifacts/; falls back to './artifacts' if "
             "--out wasn't given). A file already present there for a given (job, build, url) is "
             "reused instead of re-downloaded.",
    )
    args = parser.parse_args()
    artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else (Path(args.out).parent if args.out else Path(".")) / "artifacts"

    limiter = fed.RateLimiter(0.5)
    job_data = fed.fetch_job_data_uncached(args.job, args.build, limiter)
    if not job_data:
        print("error: could not fetch job data for this (job, build)", file=sys.stderr)
        sys.exit(1)

    record = fed.find_test_record(job_data, args.stage, args.entity_name)
    test_filter = args.test_filter if args.test_filter is not None else guess_test_filter(args.entity_name)

    result = {
        "job": args.job,
        "build": args.build,
        "stage": args.stage,
        "entity_name": args.entity_name,
        "test_filter": test_filter,
        "short_error_msg": (record or {}).get("s_short_error_msg"),
        "source": None,
        "log_link": None,
        "junit_xml_artifacts_tried": [],
        "blocks": [],
    }

    # Steps 1-2: console-log based blocks (also the source of the
    # output_xml hint step 3 looks for).
    console_text = ""
    console_source = None
    pbss_log = (record or {}).get("_pbss_log") or ""
    if not pbss_log and "::" in args.entity_name:
        file_level_name = args.entity_name.split("::", 1)[0]
        file_record = fed.find_test_record(job_data, args.stage, file_level_name)
        pbss_log = (file_record or {}).get("_pbss_log") or ""
    if pbss_log:
        console_text, console_source = pbss_log, "_pbss_log"

    log_link = fed.find_stage_log_link(job_data, args.stage)
    result["log_link"] = log_link
    if not console_text and log_link:
        try:
            console_text = fetch_text(log_link)
            console_source = "log_link"
        except Exception as e:
            print(f"warning: failed to fetch log_link: {e}", file=sys.stderr)

    console_blocks = extract_failure_blocks(console_text, test_filter) if console_text else []

    # Step 3: JUnit XML artifact, merged with the console blocks above -
    # never just a fallback for when they're empty.
    hint_match = OUTPUT_XML_HINT_RE.search(console_text) if console_text else None
    output_xml_hint = Path(hint_match.group(1)).name if hint_match else None
    xml_matches, artifacts_tried = fetch_junit_xml_matches(
        job_data, args.job, args.build, args.stage, test_filter, output_xml_hint, artifacts_dir,
    )
    result["junit_xml_artifacts_tried"] = artifacts_tried
    for a in artifacts_tried:
        print(
            f"{'reused' if a['reused'] else 'downloaded'} artifact {a['url']} -> {a['local_path']}",
            file=sys.stderr,
        )
    matched_artifact_url = artifacts_tried[-1]["url"] if xml_matches and artifacts_tried else None

    merged_blocks = []
    used_xml_indices = set()
    for block in console_blocks:
        xml_text = None
        for i, m in enumerate(xml_matches):
            if i in used_xml_indices:
                continue
            if m["name"] and (m["name"] in block["title"] or block["title"].endswith(m["name"])):
                xml_text = m["text"]
                used_xml_indices.add(i)
                break
        text = block["text"]
        if xml_text and xml_text.strip() not in text:
            text = f"{text}\n\n=== JUnit XML artifact ({matched_artifact_url}) ===\n{xml_text}"
        merged_blocks.append({"title": block["title"], "text": text})
    for i, m in enumerate(xml_matches):
        if i in used_xml_indices:
            continue
        title = f"{m['classname']}.{m['name']}" if m["classname"] else m["name"]
        merged_blocks.append({"title": title, "text": m["text"]})

    result["blocks"] = merged_blocks
    sources = [s for s in (console_source, "junit_xml" if xml_matches else None) if s]
    result["source"] = "+".join(sources) if sources else None

    out_text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(out_text)
        print(f"Wrote {len(result['blocks'])} failure block(s) (source: {result['source']}) to {args.out}", file=sys.stderr)
    else:
        print(out_text)

    if not result["blocks"]:
        print(
            "No matching failure block found in _pbss_log, log_link, or the JUnit XML artifact - "
            "fall back to manual Jenkins/Blue Ocean navigation (per SKILL.md Step 4's last-resort path).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
