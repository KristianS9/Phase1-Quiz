#!/usr/bin/env python3
"""
audit_reports.py — Deterministic "hands" for the quiz question triage pipeline.

Classification and drafting of fixes happens for free inside the Claude Code
scheduled task `phase1-quiz-daily-audit` (Notion MCP, no billed API calls).
This script is the code-enforced safety layer that task shells out to: it
independently re-checks the auto-deployability checklist (never trusts the
task's own verdict alone), then — only if eligible — applies the fix to
script.js, bumps the version, commits + pushes to main, updates the version
Gist, and logs a full before/after diff, atomically with the deploy.

Usage:
    python3 scripts/audit_reports.py --apply-fix '<json>' [--dry-run]
    python3 scripts/audit_reports.py --apply-fix '<json>' --human-approved
    python3 scripts/audit_reports.py --changelog-rollup [--dry-run]

--apply-fix JSON fields:
    question_stem            str   — stem text to locate the question in script.js
    fix_type                 str   — spelling_grammar | punctuation | formatting |
                                      broken_link | mismatched_label | (anything else)
    checklist                dict  — changes_correct_answer, changes_option_claim,
                                      changes_explanation_claim,
                                      label_confirmed_by_explanation (bools)
    new_stem, new_options, new_correct_letter, new_explanation   — drafted fix
                                      (omit any that don't change)
    notion_page_id            str  — for the audit log's notion_page_ids
    reasoning, checklist_result   str — carried into the audit log

Without --human-approved, the fix is only applied if is_auto_deployable()
passes. With --human-approved, the checklist gate is skipped (a person has
already reviewed and approved it), but the mechanical apply/commit/log steps
are identical.

Prints a single JSON object to stdout:
    {"deployed": true,  "commit_hash": ..., "new_version": ..., "old_version": ..., "diff": ...}
    {"deployed": false, "reason": "..."}
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_JS = REPO_ROOT / "script.js"
AUDIT_LOG = REPO_ROOT / "scripts" / "audit_log.jsonl"
GIST_ID   = "e9043cd2ebc9585bc29a4725d7c1949b"  # not a secret — public gist id


# ─── GitHub auth (reuses git's own stored credential, no separate secret) ─────
def get_github_token() -> str | None:
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="url=https://github.com\n\n",
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("password="):
                return line.partition("=")[2].strip()
    except Exception:
        pass
    return None


# ─── HTTP helper ────────────────────────────────────────────────────────────────
def _http(method: str, url: str, payload: dict | None, headers: dict) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {body[:300]}") from e


# ─── Question lookup / patch ────────────────────────────────────────────────────
def lookup_question_in_js(js_content: str, stem: str) -> tuple[int, list | None]:
    """
    Find the line in script.js that contains a JSON array whose first element
    matches stem. Returns (line_index, parsed_array) or (-1, None).

    Each question is a single line:
        ["Stem text", ["A","B","C","D","E"], "C", "Explanation..."],
    """
    stem_norm = stem.strip()
    lines     = js_content.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip().rstrip(",")
        if not stripped.startswith("["):
            continue
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue

        if (
            isinstance(data, list) and len(data) == 4
            and isinstance(data[0], str)
            and isinstance(data[1], list) and len(data[1]) == 5
            and isinstance(data[2], str) and data[2] in "ABCDE"
            and isinstance(data[3], str)
        ):
            if data[0] == stem_norm:
                return i, data
            if len(stem_norm) >= 100 and data[0].startswith(stem_norm[:100]):
                return i, data

    return -1, None


def write_question_line(js_path: Path, line_idx: int, new_q: list) -> None:
    """Overwrite the question array at line_idx, preserving indentation and trailing comma."""
    text  = js_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    original_line = lines[line_idx]
    indent_len    = len(original_line) - len(original_line.lstrip())
    indent        = original_line[:indent_len]
    has_comma     = original_line.rstrip("\n").rstrip().endswith(",")

    new_line = indent + json.dumps(new_q, ensure_ascii=False)
    if has_comma:
        new_line += ","
    new_line += "\n"

    lines[line_idx] = new_line
    js_path.write_text("".join(lines), encoding="utf-8")


def build_new_q(original_q: list, analysis: dict) -> list:
    """Compose the drafted 'after' array from an analysis dict, falling back to original values."""
    return [
        analysis.get("new_stem") or original_q[0],
        analysis.get("new_options") or original_q[1],
        analysis.get("new_correct_letter") or original_q[2],
        analysis.get("new_explanation") or original_q[3],
    ]


def build_diff_string(before: list, after: list) -> str:
    """Human-readable before/after diff for the Notion 'Proposed Fix' field."""
    parts = []
    if before[0] != after[0]:
        parts.append(f"Stem:\n- {before[0]}\n+ {after[0]}")
    for i, letter in enumerate("ABCDE"):
        if before[1][i] != after[1][i]:
            parts.append(f"Option {letter}:\n- {before[1][i]}\n+ {after[1][i]}")
    if before[2] != after[2]:
        parts.append(f"Correct answer: {before[2]} → {after[2]}")
    if before[3] != after[3]:
        parts.append(f"Explanation:\n- {before[3][:200]}\n+ {after[3][:200]}")
    if not parts:
        return "(no changes drafted)"
    return "\n\n".join(parts)[:1900]


# ─── Deterministic checklist gate ──────────────────────────────────────────────
SAFE_AUTO_TYPES = {"spelling_grammar", "punctuation", "formatting", "broken_link", "mismatched_label"}


def is_auto_deployable(analysis: dict) -> tuple[bool, str]:
    """
    The actual auto-deploy decision. Does NOT trust the caller's verdict or
    confidence alone — fix_type must be in the safe list AND the checklist
    booleans must confirm no clinical-substance change, checked in code.
    Returns (eligible, reason) — reason is only meaningful when not eligible.
    """
    fix_type = analysis.get("fix_type")
    if fix_type not in SAFE_AUTO_TYPES:
        return False, f"fix_type '{fix_type}' is not in the auto-deployable checklist"
    checklist = analysis.get("checklist") or {}
    if checklist.get("changes_correct_answer"):
        return False, "checklist: changes the correct answer"
    if checklist.get("changes_option_claim"):
        return False, "checklist: changes an option's factual claim"
    if checklist.get("changes_explanation_claim"):
        return False, "checklist: changes the explanation's factual claim"
    if fix_type == "mismatched_label" and not checklist.get("label_confirmed_by_explanation"):
        return False, "mismatched_label but explanation does not unambiguously confirm the correct option"
    return True, ""


# ─── Version bump ──────────────────────────────────────────────────────────────
def bump_version(js_path: Path) -> tuple[str, str]:
    """Increment the minor version in QUIZ_VERSION. Returns (old_version, new_version)."""
    content = js_path.read_text(encoding="utf-8")
    match   = re.search(r'const QUIZ_VERSION = "(v(\d+)\.(\d+))"', content)
    if not match:
        raise ValueError("QUIZ_VERSION not found in script.js")

    old   = match.group(1)
    major = int(match.group(2))
    minor = int(match.group(3))
    new   = f"v{major}.{minor + 1}"

    js_path.write_text(
        content.replace(f'const QUIZ_VERSION = "{old}"', f'const QUIZ_VERSION = "{new}"'),
        encoding="utf-8",
    )
    return old, new


def current_quiz_version(js_path: Path) -> str:
    match = re.search(r'const QUIZ_VERSION = "(v[\d.]+)"', js_path.read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


# ─── Audit log ────────────────────────────────────────────────────────────────
def append_audit_log(entry: dict) -> None:
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit_log() -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    entries = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def rewrite_audit_log(entries: list[dict]) -> None:
    with AUDIT_LOG.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ─── Git ──────────────────────────────────────────────────────────────────────
def git(*args, cwd: Path = REPO_ROOT) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args],
            output=result.stdout, stderr=result.stderr
        )
    return result.stdout.strip()


def revert_to(pre_head: str) -> None:
    """
    Undo a failed apply attempt. Uses `reset --hard` (not `checkout -- <files>`)
    because git_commit_push may have already created a local commit before the
    push itself failed (e.g. a remote race, not just a pre-commit pull conflict)
    — a file-level checkout would leave that commit sitting locally, to be
    silently swept into the next run. Resetting to the pre-attempt HEAD undoes
    both a dirty working tree (nothing committed yet) and an unpushed commit.
    """
    try:
        git("reset", "--hard", pre_head)
        print(f"[git] Reset to {pre_head} — reverted any local commit and working-tree changes")
    except subprocess.CalledProcessError:
        print("[git] Could not revert changes — manual check needed")


def git_commit_push(title: str, summaries: list[str]) -> str:
    """Pull, stage script.js + audit_log.jsonl, commit, push. Returns the commit hash."""
    git("fetch", "origin")
    git("pull", "--ff-only", "origin", "main")

    git("add", "script.js", "scripts/audit_log.jsonl")

    msg = f"{title}\n\n"
    msg += "\n".join(f"- {s}" for s in summaries)
    msg += "\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

    git("commit", "-m", msg)
    git("push", "origin", "main")

    return git("rev-parse", "--short", "HEAD")


# ─── Gist update (best effort — only affects the update-nag banner) ───────────
def update_gist(new_version: str) -> None:
    token = get_github_token()
    if not token:
        print("[gist] No GitHub token available via git credential helper — skipping gist update")
        return
    payload = {"files": {"quiz_version.json": {"content": json.dumps({"version": new_version})}}}
    try:
        _http(
            "PATCH",
            f"https://api.github.com/gists/{GIST_ID}",
            payload,
            {
                "Authorization": f"Bearer {token}",
                "Accept":        "application/vnd.github+json",
                "User-Agent":    "phase1-quiz-audit",
            },
        )
    except RuntimeError as exc:
        print(f"[gist] Update failed (non-fatal): {exc}")


# ─── Apply a single fix ─────────────────────────────────────────────────────────
def apply_fix(analysis: dict, human_approved: bool = False, dry_run: bool = False) -> dict:
    if not human_approved:
        eligible, reason = is_auto_deployable(analysis)
        if not eligible:
            return {"deployed": False, "reason": reason}

    js_content = SCRIPT_JS.read_text(encoding="utf-8")
    stem = analysis.get("question_stem", "")
    line_idx, original_q = lookup_question_in_js(js_content, stem)
    if line_idx == -1:
        return {"deployed": False, "reason": "question not found in script.js"}

    new_q = build_new_q(original_q, analysis)
    diff  = build_diff_string(original_q, new_q)

    if diff == "(no changes drafted)":
        return {"deployed": False, "reason": "no actual change in drafted fix — nothing to deploy"}

    if dry_run:
        return {"deployed": False, "dry_run": True, "diff": diff}

    write_question_line(SCRIPT_JS, line_idx, new_q)
    old_v, new_version = bump_version(SCRIPT_JS)

    today = datetime.date.today().isoformat()
    append_audit_log({
        "notion_page_ids": [analysis.get("notion_page_id", "")],
        "question_id": stem[:80],
        "issue_type": analysis.get("fix_type", ""),
        "before": original_q, "after": new_q,
        "classification": "auto-deployable" if not human_approved else "needs-approval",
        "checklist_result": analysis.get("checklist_result", ""),
        "reasoning": analysis.get("reasoning", ""),
        "status": "Approved" if human_approved else "Auto-deployed",
        "deployed_at": today,
        "reviewer": analysis.get("reviewer", ""),
        "fix_type": analysis.get("fix_type", ""),
        "new_version": new_version,
        "changelog_batched": False,
    })

    pre_head = git("rev-parse", "HEAD")
    try:
        commit_hash = git_commit_push(
            f"Auto-fix: {analysis.get('fix_type', 'question fix')} — {new_version}",
            [f"{analysis.get('fix_type', 'fix')}: {stem[:80]}"],
        )
    except subprocess.CalledProcessError as exc:
        revert_to(pre_head)
        return {"deployed": False, "reason": f"git push failed: {exc.stderr or exc}"}

    update_gist(new_version)

    return {
        "deployed": True,
        "commit_hash": commit_hash,
        "new_version": new_version,
        "old_version": old_v,
        "diff": diff,
    }


# ─── Weekly changelog rollup ────────────────────────────────────────────────────
def changelog_rollup(dry_run: bool = False) -> None:
    """Roll up unbatched Auto-deployed/Approved log entries into a single new CHANGELOG entry."""
    entries   = read_audit_log()
    unbatched = [e for e in entries if e.get("status") in ("Auto-deployed", "Approved") and not e.get("changelog_batched", True)]

    if not unbatched:
        print("[changelog] Nothing to roll up.")
        return

    type_counts = Counter((e.get("fix_type") or "other").replace("_", " ") for e in unbatched)
    parts = ", ".join(f"{n} {t}" for t, n in type_counts.items())
    summary_line = (
        f"Content auto-corrections: {len(unbatched)} fix(es) ({parts}) "
        f"applied automatically this period — see scripts/audit_log.jsonl for detail."
    )

    if dry_run:
        print(f"[dry-run] Would roll up changelog: {summary_line}")
        return

    version    = current_quiz_version(SCRIPT_JS)
    js_content = SCRIPT_JS.read_text(encoding="utf-8")

    marker = "const CHANGELOG = [\n"
    idx = js_content.find(marker)
    if idx == -1:
        print("[changelog] Could not find CHANGELOG array in script.js — skipping rollup")
        return

    today_human = datetime.date.today().strftime("%d %b %Y")
    new_entry = (
        f'  {{ version:"{version}", date:"{today_human}", '
        f'summary:"Content auto-corrections from automated report triage",\n'
        f'    changes:[{json.dumps(summary_line)}] }},\n'
    )
    insert_at = idx + len(marker)
    SCRIPT_JS.write_text(js_content[:insert_at] + new_entry + js_content[insert_at:], encoding="utf-8")

    unbatched_ids = {id(e) for e in unbatched}
    for e in entries:
        if id(e) in unbatched_ids:
            e["changelog_batched"] = True
    rewrite_audit_log(entries)

    pre_head = git("rev-parse", "HEAD")
    try:
        commit_hash = git_commit_push(
            f"Changelog rollup — {len(unbatched)} auto-correction(s) ({version})",
            [summary_line],
        )
        print(f"[changelog] Rolled up {len(unbatched)} entries into CHANGELOG ({version}, {commit_hash})")
    except subprocess.CalledProcessError as exc:
        print(f"[changelog] Commit/push failed: {exc.stderr or exc}")
        revert_to(pre_head)


# ─── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply-fix", metavar="JSON", help="Apply one drafted fix (JSON string)")
    parser.add_argument("--human-approved", action="store_true", help="Skip the checklist gate — a human already approved this fix")
    parser.add_argument("--changelog-rollup", action="store_true", help="Roll up unbatched auto-deployed/approved entries into CHANGELOG")
    parser.add_argument("--dry-run", action="store_true", help="Analyse/print only, no writes")
    args = parser.parse_args()

    if args.apply_fix:
        try:
            analysis = json.loads(args.apply_fix)
        except json.JSONDecodeError as exc:
            print(json.dumps({"deployed": False, "reason": f"invalid JSON: {exc}"}))
            return
        result = apply_fix(analysis, human_approved=args.human_approved, dry_run=args.dry_run)
        print(json.dumps(result))
    elif args.changelog_rollup:
        changelog_rollup(dry_run=args.dry_run)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
