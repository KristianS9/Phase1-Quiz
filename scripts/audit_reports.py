#!/usr/bin/env python3
"""
audit_reports.py — Autonomous quiz question audit loop.

Fetches "New" reports from the Notion database, analyses each with Claude,
applies valid fixes to script.js, commits to main, and updates Notion.

Usage:
    python3 scripts/audit_reports.py            # normal run
    python3 scripts/audit_reports.py --dry-run  # analyse only, no commits
"""

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT  = Path(__file__).resolve().parent.parent
SCRIPT_JS  = REPO_ROOT / "script.js"
AUDIT_LOG  = REPO_ROOT / "scripts" / "audit_log.jsonl"
ENV_FILE   = REPO_ROOT / ".env"


# ─── Environment ──────────────────────────────────────────────────────────────
def load_env():
    """Parse .env file and populate os.environ."""
    if not ENV_FILE.exists():
        print(f"[warn] .env not found at {ENV_FILE} — relying on existing environment variables")
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


# ─── HTTP helpers ─────────────────────────────────────────────────────────────
def _http(method: str, url: str, payload: dict | None, headers: dict) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {body[:300]}") from e


def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {require_env('NOTION_TOKEN')}",
        "Content-Type":  "application/json",
        "Notion-Version": "2022-06-28",
    }


def anthropic_headers() -> dict:
    return {
        "x-api-key":         require_env("ANTHROPIC_API_KEY"),
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }


# ─── Notion: fetch reports ─────────────────────────────────────────────────────
def fetch_new_reports() -> list[dict]:
    """Return all Notion pages with Status = 'New'."""
    db_id  = require_env("NOTION_DB_ID")
    url    = f"https://api.notion.com/v1/databases/{db_id}/query"
    hdrs   = notion_headers()
    pages  = []
    cursor = None

    while True:
        payload: dict = {"filter": {"property": "Status", "status": {"equals": "New"}}}
        if cursor:
            payload["start_cursor"] = cursor

        data   = _http("POST", url, payload, hdrs)
        pages += data.get("results", [])

        if data.get("has_more"):
            cursor = data["next_cursor"]
        else:
            break

    return pages


def parse_page(page: dict) -> dict:
    """Extract structured fields from a raw Notion page object."""
    props = page["properties"]

    def rich(prop_name: str) -> str:
        parts = props.get(prop_name, {}).get("rich_text", [])
        return "".join(p["text"]["content"] for p in parts)

    def title() -> str:
        parts = props.get("Name", {}).get("title", [])
        return "".join(p["text"]["content"] for p in parts)

    return {
        "page_id":    page["id"],
        "name":       title(),                           # stem ≤200 chars
        "issue_type": (props.get("Issue Type") or {}).get("select", {}).get("name", "Other"),
        "description": rich("Description"),              # "reason — detail"
        "question_id": rich("Question ID"),              # "lecture · Block N · QN · source"
        "email":       (props.get("Reporter Email") or {}).get("email") or "",
    }


def fetch_page_children(page_id: str) -> dict:
    """
    Retrieve child blocks and extract full question stem, all 5 options,
    and the correct answer letter.

    Block structure (written by worker.js):
        heading_3 "Question"
        paragraph  <full stem text>
        heading_3 "Options"
        paragraph  "A: opt text"          (or "A: opt text  ✓" for correct)
        paragraph  "B: ..."
        ...
    """
    url  = f"https://api.notion.com/v1/blocks/{page_id}/children"
    hdrs = notion_headers()
    data = _http("GET", url, None, hdrs)

    blocks = data.get("results", [])

    stem    = ""
    options = []
    correct = ""
    section = None

    for block in blocks:
        btype = block.get("type", "")
        if btype == "heading_3":
            text = "".join(
                t["text"]["content"]
                for t in block["heading_3"]["rich_text"]
            )
            section = text.strip().lower()
            continue

        if btype == "paragraph":
            text = "".join(
                t["text"]["content"]
                for t in block["paragraph"]["rich_text"]
            )
            text = text.strip()
            if section == "question":
                stem = text
            elif section == "options" and text:
                is_correct = text.endswith("  ✓")
                clean = re.sub(r"\s+✓$", "", text).strip()
                # strip "A: " prefix
                option_text = re.sub(r"^[A-E]:\s*", "", clean)
                letter = text[0] if text and text[0] in "ABCDE" else ""
                options.append(option_text)
                if is_correct:
                    correct = letter

    return {"stem": stem, "options": options, "correct_letter": correct}


# ─── Deduplication ────────────────────────────────────────────────────────────
def deduplicate_reports(reports: list[dict]) -> list[dict]:
    """
    Group reports that describe the same question (same stem prefix).
    Merges descriptions and page_ids; keeps a single entry per unique question.
    """
    groups: dict[str, dict] = {}
    for r in reports:
        key = r["stem"][:100] if r.get("stem") else r["name"][:100]
        if key not in groups:
            groups[key] = dict(r)
            groups[key]["page_ids"] = [r["page_id"]]
            groups[key]["all_descriptions"] = [r["description"]]
        else:
            groups[key]["page_ids"].append(r["page_id"])
            groups[key]["all_descriptions"].append(r["description"])

    # Merge multi-report descriptions
    for g in groups.values():
        if len(g["all_descriptions"]) > 1:
            merged = "\n---\n".join(
                f"Report {i+1}: {d}" for i, d in enumerate(g["all_descriptions"])
            )
            g["description"] = merged
        else:
            g["description"] = g["all_descriptions"][0]
        del g["all_descriptions"]

    return list(groups.values())


# ─── Reason parsing ───────────────────────────────────────────────────────────
def parse_original_reason(description: str) -> str:
    """
    The Description field is "reason — detail" (from submitReport() in script.js).
    Extract the reason prefix (before " — ").
    """
    return description.split(" — ")[0].strip()


REASON_OVERRIDES = {
    "Poor distractor quality": "Poor distractor quality",
}


def effective_reason(report: dict) -> str:
    """
    Notion maps "Poor distractor quality" -> Issue Type "Other".
    Recover the original reason from the Description field where needed.
    """
    if report["issue_type"] == "Other":
        original = parse_original_reason(report["description"])
        return REASON_OVERRIDES.get(original, "Other")
    return report["issue_type"]


# ─── Question lookup in script.js ─────────────────────────────────────────────
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
            # Exact match
            if data[0] == stem_norm:
                return i, data
            # Prefix fallback: Notion truncates Name to 200 chars
            if len(stem_norm) >= 100 and data[0].startswith(stem_norm[:100]):
                return i, data

    return -1, None


# ─── Claude: analyse report ────────────────────────────────────────────────────
ANALYSIS_SYSTEM = (
    "You are an expert medical education auditor reviewing student-submitted reports "
    "about MCQ quiz questions used in UK medical school (Phase 1 / pre-clinical). "
    "Your task is to determine whether each report identifies a genuine problem and, "
    "if so, provide a precise fix. Respond ONLY with valid JSON — no prose, no markdown fences."
)

ANALYSIS_USER = """\
## Report
Issue Type: {issue_type}
User Description: {description}
Reporter Email: {email}

## Question
Stem: {stem}

Options:
A: {opt_a}
B: {opt_b}
C: {opt_c}
D: {opt_d}
E: {opt_e}
Correct answer: {correct_letter} — {correct_text}

## Instructions
Analyse this report and respond with a JSON object matching EXACTLY this schema:

{{
  "verdict": "fix" | "escalate" | "dismiss",
  "confidence": "high" | "medium" | "low",
  "issue_summary": "<one concise sentence describing the problem or why it was dismissed>",
  "fix_type": "incorrect_answer" | "rewrite_distractors" | "clarify_wording" | null,
  "new_correct_letter": "<A|B|C|D|E>" | null,
  "new_options": ["<opt A>", "<opt B>", "<opt C>", "<opt D>", "<opt E>"] | null,
  "new_stem": "<rewritten stem>" | null,
  "explanation_needs_review": true | false,
  "notion_note": "<brief explanation for the audit log, 1-2 sentences>"
}}

Rules:
1. verdict="fix" only when you can provide a clear, medically accurate correction.
2. "incorrect_answer": provide new_correct_letter AND new_options (reflecting all 5 updated options). Set explanation_needs_review=true.
3. "rewrite_distractors": replace the 4 wrong options only. The correct answer must remain at its current letter position. new_options must have exactly 5 items.
4. "clarify_wording": provide new_stem only; options and correct_letter unchanged.
5. "Outdated Information" reports: ALWAYS verdict="escalate". Do not provide fix fields.
6. verdict="escalate" or "dismiss": do NOT include fix fields (leave them null).
7. All new option strings must be medically accurate, similar length, and plausible distractors.
8. If the fix would require consulting recent medical literature, set verdict="escalate".
9. explanation_needs_review=true only when the correct answer letter changes.
"""

VERIFY_SYSTEM = (
    "You are an independent medical examiner verifying a proposed change to a UK pre-clinical MCQ. "
    "Respond ONLY with valid JSON — no prose, no markdown fences."
)

VERIFY_USER = """\
A previous analysis proposes changing the correct answer of this question.
Please independently verify whether the proposed new correct answer is definitively correct
according to current standard medical knowledge (UK medical school level).

## Question
Stem: {stem}

Proposed updated options:
A: {opt_a}
B: {opt_b}
C: {opt_c}
D: {opt_d}
E: {opt_e}
Proposed correct answer: {new_correct_letter} — {new_correct_text}

Original correct answer was: {original_correct_letter} — {original_correct_text}

Respond with:
{{
  "verified": true | false,
  "reasoning": "<one sentence explaining your determination>"
}}
"""


def _claude(system: str, user: str) -> dict:
    payload = {
        "model":      "claude-sonnet-4-6",
        "max_tokens": 1024,
        "system":     system,
        "messages":   [{"role": "user", "content": user}],
    }
    resp = _http("POST", "https://api.anthropic.com/v1/messages", payload, anthropic_headers())
    raw  = resp["content"][0]["text"].strip()
    # Strip accidental markdown fences
    raw  = re.sub(r"^```(?:json)?\s*", "", raw)
    raw  = re.sub(r"\s*```\s*$", "", raw)
    return json.loads(raw)


def analyse_report(report: dict) -> dict:
    opts = report["options"]
    user = ANALYSIS_USER.format(
        issue_type    = effective_reason(report),
        description   = report["description"],
        email         = report.get("email") or "anonymous",
        stem          = report["stem"],
        opt_a=opts[0], opt_b=opts[1], opt_c=opts[2], opt_d=opts[3], opt_e=opts[4],
        correct_letter = report["correct_letter"],
        correct_text   = opts["ABCDE".index(report["correct_letter"])] if report["correct_letter"] in "ABCDE" else "",
    )
    return _claude(ANALYSIS_SYSTEM, user)


def verify_answer_change(report: dict, analysis: dict) -> bool:
    """
    Second independent Claude call to verify a proposed correct-answer change.
    Returns True only if Claude confirms the new answer is correct.
    """
    new_opts  = analysis["new_options"]
    new_letter = analysis["new_correct_letter"]
    new_idx    = "ABCDE".index(new_letter)
    orig_opts  = report["options"]
    orig_letter = report["correct_letter"]
    orig_idx    = "ABCDE".index(orig_letter) if orig_letter in "ABCDE" else 0

    user = VERIFY_USER.format(
        stem              = report["stem"],
        opt_a=new_opts[0], opt_b=new_opts[1], opt_c=new_opts[2],
        opt_d=new_opts[3], opt_e=new_opts[4],
        new_correct_letter = new_letter,
        new_correct_text   = new_opts[new_idx],
        original_correct_letter = orig_letter,
        original_correct_text   = orig_opts[orig_idx] if orig_idx < len(orig_opts) else "",
    )
    result = _claude(VERIFY_SYSTEM, user)
    return bool(result.get("verified", False))


# ─── Apply fix to script.js ────────────────────────────────────────────────────
def apply_fix_to_js(
    js_path: Path,
    line_idx: int,
    original_q: list,
    analysis: dict,
) -> None:
    """
    Replace the question at line_idx in script.js.
    Preserves original indentation and trailing comma.
    Explanation field (index 3) is NEVER modified.
    """
    text  = js_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    original_line = lines[line_idx]
    indent_len    = len(original_line) - len(original_line.lstrip())
    indent        = original_line[:indent_len]
    has_comma     = original_line.rstrip("\n").rstrip().endswith(",")

    stem        = analysis.get("new_stem")       or original_q[0]
    options     = analysis.get("new_options")    or original_q[1]
    correct     = analysis.get("new_correct_letter") or original_q[2]
    explanation = original_q[3]  # never touch this

    new_q    = [stem, options, correct, explanation]
    new_line = indent + json.dumps(new_q, ensure_ascii=False)
    if has_comma:
        new_line += ","
    new_line += "\n"

    lines[line_idx] = new_line
    js_path.write_text("".join(lines), encoding="utf-8")


# ─── Version bump ──────────────────────────────────────────────────────────────
def bump_version(js_path: Path) -> tuple[str, str]:
    """
    Increment the minor version in QUIZ_VERSION.
    Returns (old_version, new_version).
    """
    content = js_path.read_text(encoding="utf-8")
    match   = re.search(r'const QUIZ_VERSION = "(v(\d+)\.(\d+))"', content)
    if not match:
        raise ValueError("QUIZ_VERSION not found in script.js")

    old    = match.group(1)
    major  = int(match.group(2))
    minor  = int(match.group(3))
    new    = f"v{major}.{minor + 1}"

    js_path.write_text(
        content.replace(f'const QUIZ_VERSION = "{old}"', f'const QUIZ_VERSION = "{new}"'),
        encoding="utf-8",
    )
    return old, new


# ─── Audit log ────────────────────────────────────────────────────────────────
def append_audit_log(entry: dict) -> None:
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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


def revert_script_js() -> None:
    try:
        git("checkout", "--", "script.js")
        print("[git] Reverted script.js changes")
    except subprocess.CalledProcessError:
        print("[git] Could not revert script.js — manual check needed")


def git_commit_push(new_version: str, summaries: list[str]) -> str:
    """
    Pull, stage script.js + audit_log.jsonl, commit, push.
    Returns the commit hash.
    """
    git("fetch", "origin")
    git("pull", "--ff-only", "origin", "main")

    git("add", "script.js", "scripts/audit_log.jsonl")

    n   = len(summaries)
    msg = f"Auto-fix {n} report(s) — {new_version}\n\n"
    msg += "\n".join(f"- {s}" for s in summaries)
    msg += "\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"

    git("commit", "-m", msg)
    git("push", "origin", "main")

    return git("rev-parse", "--short", "HEAD")


# ─── Gist update ──────────────────────────────────────────────────────────────
def update_gist(new_version: str) -> None:
    gist_id = require_env("GIST_ID")
    token   = require_env("GITHUB_TOKEN")
    payload = {
        "files": {
            "quiz_version.json": {
                "content": json.dumps({"version": new_version})
            }
        }
    }
    _http(
        "PATCH",
        f"https://api.github.com/gists/{gist_id}",
        payload,
        {
            "Authorization": f"Bearer {token}",
            "Accept":        "application/vnd.github+json",
            "User-Agent":    "phase1-quiz-audit",
        },
    )


# ─── Notion: update pages ─────────────────────────────────────────────────────
def update_notion_page(page_id: str, new_status: str, note: str) -> None:
    hdrs = notion_headers()

    # Update Status property
    _http(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        {"properties": {"Status": {"status": {"name": new_status}}}},
        hdrs,
    )

    # Append audit note as a child paragraph
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    _http(
        "PATCH",
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        {
            "children": [{
                "object": "block",
                "type":   "paragraph",
                "paragraph": {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": f"[Audit {timestamp}] {note}"}
                    }]
                },
            }]
        },
        hdrs,
    )


def post_weekly_summary(stats: dict) -> None:
    """Append a run summary block to the configured AUDIT_RUNS_PAGE_ID."""
    audit_page = os.environ.get("AUDIT_RUNS_PAGE_ID")
    if not audit_page:
        print("[notify] AUDIT_RUNS_PAGE_ID not set — skipping weekly summary")
        return

    now  = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    text = (
        f"Run: {now}\n"
        f"Reports processed: {stats['total']}\n"
        f"Fixed: {stats['fixed']}"
        + (f" (committed as {stats.get('new_version','')})" if stats['fixed'] else "")
        + f"\nEscalated: {stats['escalated']}\n"
        f"Dismissed: {stats['dismissed']}\n"
        f"Errors: {stats['errors']}"
    )
    if stats.get("explanation_flags"):
        text += f"\n\nExplanation review needed:\n" + "\n".join(
            f"  - {q}" for q in stats["explanation_flags"]
        )

    _http(
        "PATCH",
        f"https://api.notion.com/v1/blocks/{audit_page}/children",
        {
            "children": [
                {
                    "object": "block",
                    "type":   "heading_3",
                    "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"Audit Run — {now}"}}]},
                },
                {
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": text}}]
                    },
                },
            ]
        },
        notion_headers(),
    )


# ─── Orchestration ─────────────────────────────────────────────────────────────
STATUS_MAP = {
    "fix":      "Fixed",
    "escalate": "Needs Human Review",
    "dismiss":  "Dismissed",
    "error":    "Needs Human Review",
}


def main(dry_run: bool = False) -> None:
    load_env()

    run_id = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M")
    print(f"[audit] Run {run_id} — dry_run={dry_run}")

    # 1. Fetch & parse new reports
    raw_pages = fetch_new_reports()
    if not raw_pages:
        print("[audit] No new reports. Exiting.")
        return

    print(f"[audit] {len(raw_pages)} raw report(s) fetched")

    reports = [parse_page(p) for p in raw_pages]

    # 2. Enrich with child block data (full stem + options)
    for r in reports:
        children = fetch_page_children(r["page_id"])
        r.update(children)

    # 3. Deduplicate by question stem
    reports = deduplicate_reports(reports)
    print(f"[audit] {len(reports)} unique question(s) after deduplication")

    # 4. Load script.js once
    js_content = SCRIPT_JS.read_text(encoding="utf-8")

    # Tracking
    fixed_entries    : list[dict]  = []   # entries ready to commit
    notion_updates   : list[dict]  = []   # deferred Notion status updates
    explanation_flags: list[str]   = []

    stats = {"total": len(reports), "fixed": 0, "escalated": 0, "dismissed": 0, "errors": 0}

    for r in reports:
        page_ids = r.get("page_ids", [r["page_id"]])
        stem_preview = (r.get("stem") or r["name"])[:60]

        print(f"[audit] Processing: {stem_preview!r}  ({effective_reason(r)})")

        try:
            # Hard escalation: Outdated Information
            if effective_reason(r) == "Outdated Information":
                note = "Outdated Information reports require medical literature verification — escalated automatically."
                for pid in page_ids:
                    notion_updates.append({"page_id": pid, "status": "Needs Human Review", "note": note})
                append_audit_log({
                    "run_id": run_id, "notion_page_ids": page_ids,
                    "question_stem_preview": stem_preview, "issue_type": "Outdated Information",
                    "verdict": "escalate", "confidence": None, "notion_note": note,
                })
                stats["escalated"] += 1
                continue

            # Find question in script.js
            line_idx, original_q = lookup_question_in_js(js_content, r.get("stem") or r["name"])
            if line_idx == -1:
                note = "Audit could not locate this question in script.js — it may have already been updated."
                for pid in page_ids:
                    notion_updates.append({"page_id": pid, "status": "Needs Human Review", "note": note})
                append_audit_log({
                    "run_id": run_id, "notion_page_ids": page_ids,
                    "question_stem_preview": stem_preview,
                    "verdict": "error", "notion_note": note,
                })
                stats["errors"] += 1
                continue

            # Call Claude for analysis
            analysis   = analyse_report(r)
            verdict    = analysis.get("verdict", "escalate")
            confidence = analysis.get("confidence", "low")

            # Downgrade incorrect-answer fixes with non-high confidence
            if (
                analysis.get("fix_type") == "incorrect_answer"
                and verdict == "fix"
                and confidence != "high"
            ):
                verdict = "escalate"
                analysis["notion_note"] = (
                    f"Confidence was '{confidence}' for answer change — escalated for human review. "
                    + analysis.get("notion_note", "")
                )

            # Two-pass verification for correct-answer changes
            verified = None
            if verdict == "fix" and analysis.get("fix_type") == "incorrect_answer":
                print(f"[audit]   → Running verification pass for answer change...")
                verified = verify_answer_change(r, analysis)
                if not verified:
                    verdict = "escalate"
                    analysis["notion_note"] = (
                        "Verification pass disagreed with proposed answer change — escalated for human review. "
                        + analysis.get("notion_note", "")
                    )

            if verdict == "fix":
                if dry_run:
                    print(f"[dry-run] Would apply fix: {analysis.get('fix_type')} — {analysis.get('issue_summary')}")
                    note = f"[DRY RUN] Would apply: {analysis.get('fix_type')}. {analysis.get('notion_note','')}"
                    for pid in page_ids:
                        notion_updates.append({"page_id": pid, "status": "Needs Human Review", "note": note})
                    stats["escalated"] += 1
                else:
                    apply_fix_to_js(SCRIPT_JS, line_idx, original_q, analysis)
                    # Reload content so subsequent lookups use updated line numbers
                    js_content = SCRIPT_JS.read_text(encoding="utf-8")

                    if analysis.get("explanation_needs_review"):
                        explanation_flags.append(stem_preview)

                    entry = {
                        "run_id":               run_id,
                        "notion_page_ids":      page_ids,
                        "question_stem_preview": stem_preview,
                        "issue_type":           effective_reason(r),
                        "original_q":           original_q,
                        "verdict":              "fix",
                        "confidence":           confidence,
                        "verified":             verified,
                        "applied_fix": {
                            "fix_type":          analysis.get("fix_type"),
                            "new_correct_letter": analysis.get("new_correct_letter"),
                            "new_options":        analysis.get("new_options"),
                            "new_stem":           analysis.get("new_stem"),
                        },
                        "explanation_needs_review": analysis.get("explanation_needs_review", False),
                        "notion_note":          analysis.get("notion_note", ""),
                    }
                    fixed_entries.append(entry)
                    stats["fixed"] += 1

            else:  # escalate or dismiss
                notion_status = STATUS_MAP.get(verdict, "Needs Human Review")
                note = analysis.get("notion_note", "")
                for pid in page_ids:
                    notion_updates.append({"page_id": pid, "status": notion_status, "note": note})
                append_audit_log({
                    "run_id":               run_id,
                    "notion_page_ids":      page_ids,
                    "question_stem_preview": stem_preview,
                    "issue_type":           effective_reason(r),
                    "verdict":              verdict,
                    "confidence":           confidence,
                    "notion_note":          note,
                })
                stats["escalated" if verdict == "escalate" else "dismissed"] += 1

        except Exception as exc:
            import traceback
            print(f"[error] {exc}")
            traceback.print_exc()
            note = f"Audit script error: {type(exc).__name__}: {str(exc)[:200]}"
            for pid in page_ids:
                notion_updates.append({"page_id": pid, "status": "Needs Human Review", "note": note})
            append_audit_log({
                "run_id": run_id, "notion_page_ids": page_ids,
                "question_stem_preview": stem_preview,
                "verdict": "error", "notion_note": note,
            })
            stats["errors"] += 1

    # 5. Commit fixes
    commit_hash  = None
    new_version  = None
    commit_error = None

    if fixed_entries and not dry_run:
        try:
            old_v, new_version = bump_version(SCRIPT_JS)
            summaries = [
                f"{e['issue_type']}: {e['notion_note'][:80]}" for e in fixed_entries
            ]
            commit_hash = git_commit_push(new_version, summaries)
            update_gist(new_version)
            stats["new_version"] = new_version
            print(f"[git] Committed {len(fixed_entries)} fix(es) as {new_version} ({commit_hash})")
        except subprocess.CalledProcessError as exc:
            commit_error = exc.stderr or str(exc)
            print(f"[git] Push failed: {commit_error}")
            revert_script_js()
            # Escalate all pending fixes
            for entry in fixed_entries:
                note = f"Fix was generated but git push failed: {commit_error[:200]}"
                for pid in entry["notion_page_ids"]:
                    notion_updates.append({"page_id": pid, "status": "Needs Human Review", "note": note})
                entry["verdict"]     = "error"
                entry["notion_note"] = note
                append_audit_log(entry)
            fixed_entries = []
            stats["fixed"]  = 0
            stats["errors"] += len(fixed_entries)

    # 6. Write audit log entries for committed fixes
    for entry in fixed_entries:
        entry["commit_hash"] = commit_hash
        entry["new_version"] = new_version
        append_audit_log(entry)
        # Queue Notion update
        fix_note = (
            f"Fixed in {new_version} (commit {commit_hash}). "
            f"{entry.get('explanation_needs_review') and 'Explanation may need review. ' or ''}"
            f"{entry.get('notion_note','')}"
        )
        for pid in entry["notion_page_ids"]:
            notion_updates.append({"page_id": pid, "status": "Fixed", "note": fix_note})

    # 7. Update all Notion pages
    print(f"[notion] Updating {len(notion_updates)} page(s)...")
    for upd in notion_updates:
        try:
            update_notion_page(upd["page_id"], upd["status"], upd["note"])
        except Exception as exc:
            print(f"[error] Failed to update Notion page {upd['page_id']}: {exc}")

    # 8. Post weekly summary
    stats["explanation_flags"] = explanation_flags
    try:
        post_weekly_summary(stats)
    except Exception as exc:
        print(f"[warn] Could not post weekly summary: {exc}")

    # 9. Done
    print(
        f"[audit] Done. Fixed: {stats['fixed']}, Escalated: {stats['escalated']}, "
        f"Dismissed: {stats['dismissed']}, Errors: {stats['errors']}"
    )


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
