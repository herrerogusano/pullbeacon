from __future__ import annotations

from typing import Any

from pullbeacon.core.diff import build_file_diff
from pullbeacon.core.findings import evidence_in_diff, validate_findings
from pullbeacon.core.scope import ReviewScope
from pullbeacon.domain.models import Severity

PATCH = """@@ -1,3 +1,5 @@
 def delete_account(request, user_id):
-    check_owner(request, user_id)
+    require_user(request)
+    db.users.delete(user_id)
+    return {"deleted": user_id}
     log("done")"""


def scope() -> ReviewScope:
    diff = build_file_diff("src/users.py", "modified", PATCH)
    return ReviewScope(diffs=(diff,), skipped=(), truncated=False, total_files=1)


def raw(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "severity": "critical",
        "category": "security",
        "title": "No ownership check",
        "explanation": "Any user can delete any account.",
        "impact": "Data loss",
        "evidence": "require_user(request)",
        "path": "src/users.py",
        "line": 3,
        "side": "RIGHT",
        "suggested_fix": "Check ownership.",
        "confidence": 0.9,
        "can_apply": True,
    }
    data.update(overrides)
    return data


def test_valid_finding_is_kept_inline() -> None:
    outcome = validate_findings([raw()], scope(), max_inline=10)
    assert outcome.dropped == ()
    (finding,) = outcome.findings
    assert finding.id == "PB-001"
    assert finding.inline
    assert finding.line == 3
    assert finding.can_apply


def test_invented_path_is_dropped() -> None:
    outcome = validate_findings([raw(path="src/other.py")], scope(), max_inline=10)
    assert outcome.findings == ()
    assert "not part of the reviewed diff" in outcome.dropped[0]


def test_line_outside_diff_is_downgraded_not_attributed() -> None:
    outcome = validate_findings([raw(line=40)], scope(), max_inline=10)
    (finding,) = outcome.findings
    assert finding.inline is False
    assert finding.line is None
    assert finding.can_apply is False


def test_wrong_side_is_not_inline() -> None:
    # Line 3 on the LEFT side does not exist (old file has lines 1..3 but 3 is context "log").
    outcome = validate_findings([raw(line=2, side="LEFT")], scope(), max_inline=10)
    (finding,) = outcome.findings
    assert finding.inline is True  # old line 2 is the deleted check_owner line
    assert finding.can_apply is False  # fixes only target the new side


def test_evidence_must_exist_in_diff() -> None:
    outcome = validate_findings([raw(evidence="os.system(user_input)")], scope(), max_inline=10)
    assert outcome.findings == ()
    assert "evidence not found" in outcome.dropped[0]


def test_evidence_with_copied_coordinates_is_accepted() -> None:
    diff = scope().diffs[0]
    assert evidence_in_diff("| R3 +     require_user(request)", diff)
    assert evidence_in_diff('R5 L3   log("done")', diff)


def test_short_evidence_needs_exact_line() -> None:
    diff = scope().diffs[0]
    assert not evidence_in_diff("user", diff)


def test_invalid_enum_range_and_missing_fields_are_dropped() -> None:
    items = [
        raw(severity="blocker"),
        raw(confidence=1.7),
        raw(evidence=""),
        raw(title="   "),
        {"severity": "high"},
        "not an object",
    ]
    outcome = validate_findings(items, scope(), max_inline=10)
    assert outcome.findings == ()
    assert len(outcome.dropped) == len(items)


def test_unknown_category_maps_to_other() -> None:
    (finding,) = validate_findings([raw(category="Style!!")], scope(), max_inline=10).findings
    assert finding.category.value == "other"


def test_duplicates_are_merged_keeping_most_severe() -> None:
    items = [raw(severity="medium", confidence=0.6), raw(severity="critical", title="Dup")]
    outcome = validate_findings(items, scope(), max_inline=10)
    assert len(outcome.findings) == 1
    assert outcome.findings[0].severity is Severity.CRITICAL
    assert any("duplicate" in d for d in outcome.dropped)


def test_general_finding_without_path_requires_evidence() -> None:
    kept = validate_findings([raw(path=None, line=None, side=None)], scope(), max_inline=10)
    assert kept.findings[0].inline is False
    assert kept.findings[0].path == "src/users.py"
    dropped = validate_findings(
        [raw(path=None, line=None, evidence="something unrelated entirely")], scope(), max_inline=10
    )
    assert dropped.findings == ()


def test_ordering_ids_and_inline_cap() -> None:
    items = [
        raw(severity="low", evidence='return {"deleted": user_id}', line=5, confidence=0.7),
        raw(severity="high", evidence="db.users.delete(user_id)", line=4),
        raw(severity="critical"),
    ]
    outcome = validate_findings(items, scope(), max_inline=2)
    assert [f.severity.value for f in outcome.findings] == ["critical", "high", "low"]
    assert [f.id for f in outcome.findings] == ["PB-001", "PB-002", "PB-003"]
    assert [f.inline for f in outcome.findings] == [True, True, False]


def test_fingerprint_ignores_model_wording_and_order() -> None:
    a = validate_findings(
        [raw(), raw(severity="low", evidence="db.users.delete(user_id)", line=4)],
        scope(),
        max_inline=10,
    )
    b = validate_findings(
        [
            raw(severity="low", evidence="db.users.delete(user_id)", line=4, title="Other words"),
            raw(title="Changed"),
        ],
        scope(),
        max_inline=10,
    )
    assert {f.fingerprint for f in a.findings} == {f.fingerprint for f in b.findings}
