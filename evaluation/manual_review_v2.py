"""Manual review of benchmark v2 disagreements and the adjusted metrics derived from it.

Every finding the deterministic matcher did not count as a true positive or acceptable extra was
read against the case (diff, labels, upstream history for OSS cases) and given one verdict:

- SAME   the labelled defect, reported on another line (e.g. at a deleted line of a revert), with
         another category, or split into a second finding. Counts as a TP when the labelled issue
         was otherwise missed in that sample, as a duplicate when it was already matched.
- VALID  a real, specific issue present in the diff that the labels did not cover.
- MINOR  true but low value: generic "no tests", style, theoretical edge cases, intended behaviour
         changes. Defensible, but noise for most teams.
- SPEC   unsupported or speculative: claims needing code the diff does not show (diff-only
         context), or that are wrong on inspection.

Usage: ``uv run python evaluation/manual_review_v2.py evaluation/results/v2-test-r1.json``
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

H, S = "anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"

# (model, case_id, line or "-") -> verdict. Lines are the finding's line; "-" is unlocated.
VERDICTS: dict[tuple[str, str, str], str] = {
    # ---- Haiku
    (H, "cl_clean_ts_price_format", "-"): "MINOR",
    (H, "cl_clean_ts_price_format", "1"): "MINOR",
    (H, "cl_clean_ts_price_format", "13"): "MINOR",
    (H, "cl_clean_ts_price_format", "17"): "VALID",  # /100 wrong for JPY/KWD (trap label wrong)
    (H, "cl_clean_ts_price_format", "2"): "MINOR",
    (H, "cl_clean_ts_price_format", "23"): "SPEC",
    (H, "cl_clean_ts_price_format", "31"): "MINOR",
    (H, "cl_clean_ts_price_format", "7"): "MINOR",
    (H, "cl_inj_export_traversal", "26"): "MINOR",
    (H, "cl_mut_commands_list_items", "-"): "MINOR",
    (H, "cl_mut_commands_list_items", "64"): "MINOR",
    (H, "cl_mut_diff_header_default", "28"): "SAME",
    (H, "cl_mut_paths_dot_prefix", "-"): "MINOR",
    (H, "cl_mut_paths_dot_prefix", "90"): "SAME",
    (H, "cl_syn_cache_timedelta_seconds", "-"): "MINOR",
    (H, "cl_syn_cache_timedelta_seconds", "41"): "MINOR",
    (H, "cl_syn_policy_fail_open", "-"): "MINOR",
    (H, "cl_syn_policy_fail_open", "30"): "MINOR",
    (H, "cl_syn_release_shell_injection", "-"): "MINOR",
    (H, "cl_syn_release_shell_injection", "33"): "VALID",  # describes HEAD, not the branch
    (H, "cl_syn_release_shell_injection", "44"): "MINOR",
    (H, "cl_syn_reports_page_offset", "-"): "VALID",  # the labelled acceptable, unlocated
    (H, "cl_syn_webhook_timing_compare", "-"): "MINOR",
    (
        H,
        "cl_syn_webhook_timing_compare",
        "28",
    ): "VALID",  # '²'.isdigit() -> int() raises (trap wrong)
    (H, "ext_artifact_path", "13"): "VALID",  # second unsanitised path builder
    (H, "ext_clean_archive", "-"): "MINOR",
    (H, "ext_clean_archive", "18"): "VALID",  # PR claims manifest verification that is absent
    (H, "ext_clean_archive", "22"): "MINOR",
    (H, "ext_clean_archive", "23"): "MINOR",
    (H, "ext_clean_search_sql", "16"): "MINOR",
    (H, "ext_digest_cursor", "-"): "MINOR",
    (H, "ext_digest_cursor", "10"): "VALID",  # public page_rows removed
    (H, "ext_digest_cursor", "109"): "MINOR",
    (H, "ext_digest_cursor", "117"): "MINOR",
    (H, "ext_digest_cursor", "144"): "MINOR",
    (H, "ext_digest_cursor", "150"): "MINOR",
    (H, "ext_digest_cursor", "33"): "VALID",  # Page gains a required field
    (H, "ext_digest_cursor", "39"): "VALID",  # naive/aware compare raises before the tz check
    (H, "ext_digest_cursor", "47"): "MINOR",
    (H, "ext_digest_cursor", "48"): "MINOR",
    (H, "ext_fetch_retry", "13"): "MINOR",
    (H, "ext_fetch_retry", "14"): "SAME",
    (H, "ext_fetch_retry", "15"): "MINOR",
    (H, "ext_lease_release", "25"): "SAME",
    (H, "ext_lease_release", "30"): "MINOR",
    (H, "ext_price_contract", "1"): "SAME",
    (H, "ext_report_authz", "28"): "MINOR",
    (H, "oss_httpx_headers_update", "968"): "SPEC",
    (H, "oss_jinja_xmlattr_keys", "251"): "MINOR",
    (H, "oss_jinja_xmlattr_keys", "290"): "SAME",  # reported at the deleted check
    (H, "oss_nodefetch_localhost_redos", "122"): "MINOR",  # right line, missed the ReDoS
    (H, "oss_requests_redirect_auth_strip", "118"): "MINOR",
    (H, "oss_requests_redirect_auth_strip", "245"): "MINOR",
    (H, "oss_requests_zip_tempfile", "-"): "MINOR",
    (H, "oss_requests_zip_tempfile", "286"): "SAME",
    (H, "oss_requests_zip_tempfile", "287"): "SAME",
    (H, "oss_requests_zip_tempfile", "289"): "SPEC",  # atomic_open is defined outside the diff
    (H, "oss_urllib3_read_zero", "247"): "MINOR",
    (H, "oss_urllib3_target_fragment", "-"): "MINOR",
    (H, "oss_urllib3_target_fragment", "58"): "VALID",  # the revert also reverts the regex
    (H, "oss_ws_protocol_redos", "289"): "MINOR",  # right line, missed the ReDoS
    (H, "ossfix_httpx_headers_update", "-"): "MINOR",
    (H, "ossfix_httpx_headers_update", "970"): "SPEC",
    (H, "ossfix_jinja_xmlattr_keys", "-"): "MINOR",
    (H, "ossfix_jinja_xmlattr_keys", "251"): "MINOR",
    (H, "ossfix_jinja_xmlattr_keys", "293"): "VALID",  # '=' still allowed: later CVE-2024-34064
    (H, "ossfix_nodefetch_localhost_redos", "-"): "MINOR",
    (H, "ossfix_nodefetch_localhost_redos", "122"): "MINOR",
    (H, "ossfix_requests_proxy_bypass_empty", "101"): "MINOR",
    (H, "ossfix_requests_redirect_auth_strip", "118"): "MINOR",
    (
        H,
        "ossfix_requests_redirect_auth_strip",
        "132",
    ): "VALID",  # default ports: fixed upstream later
    (H, "ossfix_requests_zip_tempfile", "286"): "MINOR",
    (H, "ossfix_requests_zip_tempfile", "288"): "MINOR",
    (H, "ossfix_urllib3_read_zero", "247"): "MINOR",
    (H, "ossfix_urllib3_read_zero", "249"): "SPEC",
    (H, "ossfix_urllib3_target_fragment", "-"): "MINOR",
    (H, "ossfix_urllib3_target_fragment", "328"): "MINOR",
    (H, "ossfix_urllib3_target_fragment", "58"): "SPEC",
    (H, "ossfix_ws_protocol_redos", "289"): "MINOR",
    # ---- Sonnet
    (S, "cl_inj_export_traversal", "26"): "MINOR",
    (S, "cl_mut_commands_list_items", "64"): "MINOR",
    (S, "cl_mut_diff_header_default", "28"): "SAME",
    (S, "cl_mut_paths_dot_prefix", "92"): "MINOR",
    (S, "cl_syn_cache_timedelta_seconds", "41"): "MINOR",
    (S, "cl_syn_policy_fail_open", "16"): "MINOR",
    (S, "cl_syn_policy_fail_open", "18"): "VALID",  # project id used in a file path unvalidated
    (S, "cl_syn_release_shell_injection", "33"): "MINOR",
    (S, "cl_syn_release_shell_injection", "46"): "VALID",  # branch can be a git option
    (S, "cl_syn_reports_page_offset", "3"): "VALID",  # the labelled acceptable, other line
    (S, "cl_syn_webhook_timing_compare", "17"): "MINOR",
    (S, "cl_syn_webhook_timing_compare", "25"): "MINOR",
    (S, "ext_artifact_path", "13"): "VALID",
    (S, "ext_clean_archive", "22"): "MINOR",
    (S, "ext_clean_archive", "23"): "VALID",  # PR claims manifest verification that is absent
    (S, "ext_digest_cursor", "39"): "VALID",
    (S, "ext_fetch_retry", "14"): "MINOR",
    (S, "ext_report_authz", "28"): "MINOR",
    (S, "ext_workflow_title", "14"): "VALID",  # second interpolation of the title
    (S, "oss_jinja_xmlattr_keys", "-"): "MINOR",
    (S, "oss_jinja_xmlattr_keys", "291"): "MINOR",
    (S, "oss_nodefetch_localhost_redos", "122"): "MINOR",
    (S, "oss_requests_redirect_auth_strip", "239"): "MINOR",
    (S, "oss_requests_zip_tempfile", "286"): "SPEC",
    (S, "oss_requests_zip_tempfile", "287"): "SAME",
    (S, "oss_requests_zip_tempfile", "289"): "SPEC",
    (S, "oss_urllib3_target_fragment", "331"): "SPEC",  # FRAGMENT_CHARS is defined outside the diff
    (S, "oss_urllib3_target_fragment", "58"): "VALID",
    (S, "oss_ws_protocol_redos", "289"): "MINOR",
    (S, "ossfix_httpx_headers_update", "969"): "MINOR",
    (S, "ossfix_httpx_headers_update", "970"): "MINOR",
    (S, "ossfix_jinja_xmlattr_keys", "251"): "VALID",
    (S, "ossfix_jinja_xmlattr_keys", "290"): "VALID",
    (S, "ossfix_jinja_xmlattr_keys", "291"): "MINOR",
    (S, "ossfix_requests_redirect_auth_strip", "118"): "MINOR",
    (S, "ossfix_requests_redirect_auth_strip", "132"): "VALID",
    (S, "ossfix_requests_zip_tempfile", "286"): "MINOR",
    (S, "ossfix_requests_zip_tempfile", "288"): "MINOR",
}

SUMMARY = re.compile(r"\[(?P<verdict>[^\]]+)\] PB-\d+ \S+ (?P<path>\S+?):(?P<line>\S+) ")


def main(path: Path) -> int:
    run = json.loads(path.read_text(encoding="utf-8"))
    kinds: dict[str, str] = {}
    missing: set[tuple[str, str, str]] = set()
    out: dict[str, dict[str, object]] = {}
    for model, results in run["cases"].items():
        verdicts: Counter[str] = Counter()
        tp = fn = 0
        clean_runs = 0
        clean_spec = clean_valid = clean_minor = 0
        per_case_tp: dict[str, list[int]] = defaultdict(list)
        for r in results:
            kinds[r["case_id"]] = r["kind"]
            sample_tp, sample_fn = r["tp"], r["fn"]
            if r["kind"] == "clean":
                clean_runs += 1
            for summary in r["finding_summaries"]:
                m = SUMMARY.match(summary)
                if m is None:
                    continue
                v = m["verdict"]
                if v.startswith("TP"):
                    verdicts["TP"] += 1
                    continue
                if v == "ACCEPTABLE":
                    verdicts["ACCEPTABLE"] += 1
                    continue
                key = (model, r["case_id"], m["line"])
                manual = VERDICTS.get(key)
                if manual is None:
                    missing.add(key)
                    manual = "UNREVIEWED"
                if manual == "SAME":
                    if sample_fn > 0:
                        sample_tp, sample_fn = sample_tp + 1, sample_fn - 1
                        verdicts["SAME_AS_TP"] += 1
                    else:
                        verdicts["SAME_DUPLICATE"] += 1
                    continue
                verdicts[manual] += 1
                if r["kind"] == "clean":
                    clean_spec += manual == "SPEC"
                    clean_valid += manual == "VALID"
                    clean_minor += manual == "MINOR"
            tp += sample_tp
            fn += sample_fn
            per_case_tp[r["case_id"]].append(sample_fn)
        total = sum(verdicts.values())
        useful = (
            verdicts["TP"] + verdicts["SAME_AS_TP"] + verdicts["VALID"] + verdicts["ACCEPTABLE"]
        )
        out[model] = {
            "findings": total,
            "verdicts": dict(sorted(verdicts.items())),
            "adjusted_recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "adjusted_tp": tp,
            "adjusted_fn": fn,
            "useful_share": round(useful / total, 4) if total else None,
            "minor_share": round(verdicts["MINOR"] / total, 4) if total else None,
            "unsupported_share": round(verdicts["SPEC"] / total, 4) if total else None,
            "duplicate_share": round(verdicts["SAME_DUPLICATE"] / total, 4) if total else None,
            "clean_runs": clean_runs,
            "unsupported_per_clean_pr": round(clean_spec / clean_runs, 4) if clean_runs else None,
            "valid_per_clean_pr": round(clean_valid / clean_runs, 4) if clean_runs else None,
            "minor_per_clean_pr": round(clean_minor / clean_runs, 4) if clean_runs else None,
            "cases_missed_in_every_sample": sorted(
                c for c, fns in per_case_tp.items() if all(f > 0 for f in fns)
            ),
            "cases_missed_in_some_sample": sorted(
                c
                for c, fns in per_case_tp.items()
                if any(f > 0 for f in fns) and not all(f > 0 for f in fns)
            ),
        }
    print(json.dumps(out, indent=2))
    if missing:
        print(f"UNREVIEWED findings: {sorted(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
