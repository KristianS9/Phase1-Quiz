#!/usr/bin/env python3
"""
improve_distractors.py — Improve quiz distractors + fix answer position bias
(script.js port — operates on the live `const BLOCKS = {...}` data structure)

Two modes of improvement:

1. Distractor rewrite (default): for questions where one or more options stand
   out by length (bidirectional), regenerates the 4 distractors so that all
   options are broadly comparable in length.

2. Stem rewrite (--rewrite-stems): for questions where ALL options are
   uniformly short (average < SHORT_ANSWER_AVG_THRESHOLD chars), rewrites
   both the question stem and all answer options. This handles cases where the
   correct answer is an inherently short label (e.g. "SCN", "oxytocin",
   "alpha-synuclein") that cannot be equalised by distractor rewrite alone.

Optionally (--redistribute-positions, OFF by default): redistributes correct
answer positions across ALL processed questions to achieve an even A/B/C/D/E
spread (~20% each), fixing stored positional bias. Off by default because it
touches every question in scope (not just flagged/patched ones), producing a
much larger diff — only enable when you deliberately want to rebalance a
block's letter distribution.

Usage:
  python3 scripts/improve_distractors.py --block 1                     # API: distractors only
  python3 scripts/improve_distractors.py --block all --rewrite-stems   # API: + stem rewrites
  python3 scripts/improve_distractors.py --dry-run --block 1           # report flagged (no API)
  python3 scripts/improve_distractors.py --export-flagged --block 1,2,3,4  # export to JSON (no API)
  python3 scripts/improve_distractors.py --apply-patch --block 1,2,3,4     # apply patch JSON (no API)

No-API workflow (--export-flagged / --apply-patch):
  1. Run --export-flagged  → writes scripts/flagged_questions.json
  2. Claude reads that file and writes scripts/distractor_patch.json
  3. Run --apply-patch     → applies patch to script.js

Requirements (API mode only):
  pip3 install anthropic
  export ANTHROPIC_API_KEY=sk-...

Block keys in script.js are not purely numeric (e.g. "1".."5", "f1".."f4",
"ppt") — --block accepts any of these, comma-separated, or "all".
"""

import json
import re
import sys
import argparse
import os
import random

# ── Flagging thresholds ──────────────────────────────────────────────────────

# Flag if ANY option is more than this multiple of the average of all 5 options
MAX_OPTION_RATIO = 1.7

# Flag if ANY option is shorter than this fraction of the average of all 5 options
MIN_OPTION_RATIO = 0.5

# Maximum API attempts per question before giving up
MAX_RETRIES = 3

# If the average of all 5 options is below this, the question is a "short-answer"
# type where distractor rewrite alone won't help — a stem rewrite is needed instead
SHORT_ANSWER_AVG_THRESHOLD = 20


# ── Question parsing ─────────────────────────────────────────────────────────

def parse_question_line(line: str):
    """
    Try to parse a single line as a question array:
      [question_text, [opt0..opt4], "A"-"E", explanation]
    Returns the list on success, or None.
    """
    stripped = line.strip().rstrip(',')
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None

    if (
        isinstance(data, list) and len(data) == 4
        and isinstance(data[0], str)
        and isinstance(data[1], list) and len(data[1]) == 5
        and all(isinstance(o, str) for o in data[1])
        and isinstance(data[2], str) and len(data[2]) == 1 and data[2] in "ABCDE"
        and isinstance(data[3], str)
    ):
        return data
    return None


# ── Flagging logic ───────────────────────────────────────────────────────────

def is_flagged(q) -> bool:
    """
    Return True if any option stands out by length — either direction.
    Checks all 5 options against the average of all 5 (bidirectional).
    """
    _, options, _, _ = q
    lens = [len(o) for o in options]
    avg = sum(lens) / 5
    if avg == 0:
        return False
    return any(l > MAX_OPTION_RATIO * avg or l < MIN_OPTION_RATIO * avg for l in lens)


def is_short_answer(q) -> bool:
    """
    Return True if ALL options are so uniformly short that distractor rewrite
    alone cannot fix the question — the stem itself needs to be rewritten to
    elicit a longer, more descriptive answer.
    """
    _, options, _, _ = q
    avg = sum(len(o) for o in options) / 5
    return avg < SHORT_ANSWER_AVG_THRESHOLD


# ── Position redistribution (opt-in) ─────────────────────────────────────────

def redistribute_position(q, target_idx: int):
    """
    Move the correct answer to target_idx, shuffling the other options
    randomly into the remaining slots.
    """
    text, options, correct_letter, explanation = q
    correct_idx = ord(correct_letter) - ord("A")
    correct_text = options[correct_idx]
    distractors = [options[i] for i in range(5) if i != correct_idx]
    random.shuffle(distractors)
    new_options = distractors[:target_idx] + [correct_text] + distractors[target_idx:]
    new_correct = "ABCDE"[target_idx]
    return [text, new_options, new_correct, explanation]


def pick_target_letter(counts: dict) -> int:
    """
    Return the index (0-4) of the least-used letter so far.
    Ties are broken randomly.
    """
    min_count = min(counts.values())
    candidates = [i for i, letter in enumerate("ABCDE") if counts[letter] == min_count]
    return random.choice(candidates)


# ── API call ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are an experienced medical education specialist who writes "
    "high-quality multiple-choice questions for preclinical medical students."
)

USER_TEMPLATE = """Below is a multiple-choice question with its correct answer and the source notes it was drawn from.

Question: {question}
Correct answer: {correct}
Medical topic: {lecture}
Source context: {explanation}

Your task: write exactly 4 plausible WRONG answer options (distractors).

Rules:
1. The correct answer is {correct_len} characters long. Write each distractor to be \
between {target_min} and {target_max} characters. No option should be so much shorter \
or longer that a student could identify the answer by scanning lengths alone.
2. Ground each distractor in the same conceptual area as the source context — \
use related but incorrect mechanisms, wrong numerical values, or misapplied concepts \
from the same topic. Do not invent entirely unrelated facts.
3. Each distractor must be plausible to a student who has NOT studied this topic, \
and clearly wrong to a student who HAS studied it.
4. No trivially dismissible options (wrong organ system entirely, completely \
unrelated drug class, obviously impossible physiology).
5. Match the grammatical style of the correct answer exactly — \
if the correct answer is a noun phrase, all distractors must be noun phrases; \
if it is a complete sentence, all distractors must be complete sentences.
6. Do NOT paraphrase, repeat, or closely echo the correct answer.

Return ONLY a JSON array of exactly 4 strings and nothing else:
["distractor 1", "distractor 2", "distractor 3", "distractor 4"]"""

STEM_REWRITE_TEMPLATE = """Below is a multiple-choice question where every answer option is a very short label \
(single word, abbreviation, or brief name). This makes the question trivially easy and \
fails to assess clinical reasoning.

Question: {question}
Correct answer: {correct}
Medical topic: {lecture}
Source context: {explanation}

Your task: rewrite the question stem AND all 5 answer options so the question tests \
understanding rather than mere recall of a label.

Rules:
1. Test the same underlying fact, but ask for a mechanism, definition, or clinical \
significance — not just a name.
2. All 5 options must be between {target_min} and {target_max} characters each. No option \
should be so dramatically shorter or longer than the others that a student could identify \
the correct answer by scanning lengths.
3. The rewritten correct answer must remain factually correct and unambiguous.
4. The 4 distractors must be plausible to an unstudied student and clearly wrong to a \
studied one. Ground them in the same topic area.
5. All options must share the same grammatical style (all noun phrases OR all complete \
sentences — pick whichever fits the new stem).
6. Do NOT echo or closely paraphrase the correct answer in any distractor.

Return ONLY a JSON object with exactly these three keys and nothing else:
{{
  "stem": "rewritten question stem",
  "correct": "rewritten correct answer ({target_min}–{target_max} chars)",
  "distractors": ["distractor 1", "distractor 2", "distractor 3", "distractor 4"]
}}"""


def improve_question(client, q, lecture_name: str):
    """Call Claude to regenerate distractors; return updated question list."""
    text, options, correct_letter, explanation = q
    correct_idx = ord(correct_letter) - ord("A")
    correct_answer = options[correct_idx]
    correct_len = len(correct_answer)
    target_min = max(int(correct_len * 0.75), 20)
    target_max = int(correct_len * 1.35)

    prompt = USER_TEMPLATE.format(
        question=text,
        correct=correct_answer,
        lecture=lecture_name,
        explanation=explanation,
        correct_len=correct_len,
        target_min=target_min,
        target_max=target_max,
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    distractors = json.loads(raw)

    if not isinstance(distractors, list) or len(distractors) != 4:
        raise ValueError(f"Expected 4 distractors, got: {distractors!r}")
    if not all(isinstance(d, str) for d in distractors):
        raise ValueError(f"Non-string distractor in: {distractors!r}")

    # Rebuild options, keeping correct answer in its original slot
    new_options = list(options)
    di = 0
    for i in range(5):
        if i != correct_idx:
            new_options[i] = distractors[di]
            di += 1

    return [text, new_options, correct_letter, explanation]


def rewrite_question_stem(client, q, lecture_name: str):
    """
    Call Claude to rewrite both the stem and all options for short-answer questions.
    The correct answer stays at the same index; only its text changes.
    """
    text, options, correct_letter, explanation = q
    correct_idx = ord(correct_letter) - ord("A")
    correct_answer = options[correct_idx]

    avg_len = sum(len(o) for o in options) / 5
    target_min = max(int(avg_len * 2.5), 50)
    target_max = max(int(avg_len * 9), 180)

    prompt = STEM_REWRITE_TEMPLATE.format(
        question=text,
        correct=correct_answer,
        lecture=lecture_name,
        explanation=explanation,
        target_min=target_min,
        target_max=target_max,
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1536,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    result = json.loads(raw)

    if not isinstance(result, dict):
        raise ValueError(f"Expected a JSON object, got: {result!r}")
    for key in ("stem", "correct", "distractors"):
        if key not in result:
            raise ValueError(f"Missing key '{key}' in response: {result!r}")
    if not isinstance(result["distractors"], list) or len(result["distractors"]) != 4:
        raise ValueError(f"Expected 4 distractors, got: {result['distractors']!r}")

    new_options = list(options)
    new_options[correct_idx] = result["correct"]
    di = 0
    for i in range(5):
        if i != correct_idx:
            new_options[i] = result["distractors"][di]
            di += 1

    return [result["stem"], new_options, correct_letter, explanation]


# ── Serialisation ─────────────────────────────────────────────────────────────

def serialize_question(q) -> str:
    """Compact single-line JSON matching the original file format."""
    return json.dumps(q, ensure_ascii=False, separators=(",", ":"))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Improve quiz distractors via Claude API (script.js)")
    parser.add_argument(
        "--block",
        default="1",
        help='Block(s) to process: "1", "1,2,f1,ppt", or "all" (default: 1)',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print flagged questions only; do not call the API or modify script.js",
    )
    parser.add_argument(
        "--rewrite-stems",
        action="store_true",
        help=(
            "Also rewrite question stems for short-answer questions "
            f"(avg option length < {SHORT_ANSWER_AVG_THRESHOLD} chars). "
            "Without this flag, short-answer questions are reported but skipped."
        ),
    )
    parser.add_argument(
        "--export-flagged",
        action="store_true",
        help="Export flagged questions to scripts/flagged_questions.json (no API, no edits)",
    )
    parser.add_argument(
        "--apply-patch",
        action="store_true",
        help="Apply scripts/distractor_patch.json to script.js (no API calls)",
    )
    parser.add_argument(
        "--redistribute-positions",
        action="store_true",
        help=(
            "Also rebalance correct-answer letter (A-E) distribution across ALL "
            "questions in scope, not just flagged/patched ones. OFF by default — "
            "produces a much larger diff. Matches the original tool's default "
            "behaviour if enabled."
        ),
    )
    args = parser.parse_args()

    js_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "script.js")
    )

    if not os.path.exists(js_path):
        sys.exit(f"ERROR: script.js not found at {js_path}")

    scripts_dir = os.path.dirname(os.path.abspath(__file__))

    # Set up API client (only needed for normal improvement mode)
    client = None
    no_api = args.dry_run or args.export_flagged or args.apply_patch
    if not no_api:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError:
            sys.exit("ERROR: anthropic not installed. Run: pip3 install anthropic")

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("ERROR: ANTHROPIC_API_KEY environment variable not set")

        client = anthropic.Anthropic(api_key=api_key)

    # Load patch map if apply-patch mode
    patch_map: dict[int, dict] = {}
    if args.apply_patch:
        patch_path = os.path.join(scripts_dir, "distractor_patch.json")
        if not os.path.exists(patch_path):
            sys.exit(f"ERROR: distractor_patch.json not found at {patch_path}")
        with open(patch_path, encoding="utf-8") as f:
            patch_data = json.load(f)
        patch_map = {entry["line_idx"]: entry for entry in patch_data}
        print(f"Loaded {len(patch_map)} patches from distractor_patch.json")

    with open(js_path, encoding="utf-8") as f:
        lines = f.readlines()

    target_blocks = None if args.block == "all" else set(args.block.split(","))

    current_block: str | None = None
    current_lecture: str | None = None

    # Per-block letter distribution counters (reset per block)
    letter_counts: dict[str, int] = {l: 0 for l in "ABCDE"}

    total_q = 0
    flagged_q = 0
    short_answer_q = 0
    improved_q = 0
    stem_rewritten_q = 0
    retry_count = 0
    patched_q = 0
    repositioned_q = 0
    flagged_export: list[dict] = []
    new_lines = []

    for line_idx, line in enumerate(lines):
        # ── Track current block (2-space indent, quoted key: e.g. "1", "f1", "ppt")
        bm = re.match(r'^  "([^"]+)":\s*\{', line)
        if bm:
            new_block = bm.group(1)
            if new_block != current_block:
                # Reset letter counts when entering a new block
                letter_counts = {l: 0 for l in "ABCDE"}
                current_block = new_block

        # ── Track current lecture (6-space indent, string key → array)
        lm = re.match(r'^      "([^"]+)":\s*\[', line)
        if lm:
            current_lecture = lm.group(1)

        # ── Skip blocks not in scope
        if target_blocks and current_block not in target_blocks:
            new_lines.append(line)
            continue

        # ── Process question lines (8-space indent starting with [)
        if re.match(r"^        \[", line):
            q = parse_question_line(line)
            if q:
                total_q += 1
                lecture_label = current_lecture or "Unknown"
                modified = False

                # ── Export-flagged mode: collect and move on (no edits)
                if args.export_flagged:
                    if is_flagged(q):
                        flagged_q += 1
                        short = is_short_answer(q)
                        if short:
                            short_answer_q += 1
                        text, options, correct_letter, explanation = q
                        correct_idx = ord(correct_letter) - ord("A")
                        correct_len = len(options[correct_idx])
                        target_min = max(int(correct_len * 0.75), 20)
                        target_max = int(correct_len * 1.35)
                        flagged_export.append({
                            "block": current_block,
                            "lecture": lecture_label,
                            "line_idx": line_idx,
                            "question": text,
                            "options": options,
                            "correct": correct_letter,
                            "correct_len": correct_len,
                            "target_min": target_min,
                            "target_max": target_max,
                            "is_short_answer": short,
                            "explanation": explanation,
                        })
                    new_lines.append(line)
                    continue

                # ── Flag detection (used by dry-run and API mode)
                if is_flagged(q):
                    flagged_q += 1
                    lens = [len(o) for o in q[1]]
                    avg_all = sum(lens) / 5
                    correct_idx = ord(q[2]) - ord("A")
                    short = is_short_answer(q)
                    if short:
                        short_answer_q += 1

                    if args.dry_run:
                        tag = " [STEM]" if short else ""
                        labels = [f"{'*' if i == correct_idx else ' '}{chr(65+i)}:{lens[i]}" for i in range(5)]
                        print(
                            f"[Block {current_block}] {lecture_label}{tag}\n"
                            f"  Q: {q[0][:90]}{'…' if len(q[0]) > 90 else ''}\n"
                            f"  avg={avg_all:.0f}  options: {', '.join(labels)}\n"
                        )
                    elif not args.apply_patch:
                        # ── API improvement mode
                        if short and args.rewrite_stems:
                            print(
                                f"✎ [{current_block}] {lecture_label[:40]:40s}  "
                                f"{q[0][:55]}{'…' if len(q[0]) > 55 else ''}",
                                flush=True,
                            )
                            try:
                                for attempt in range(1, MAX_RETRIES + 1):
                                    candidate = rewrite_question_stem(client, q, lecture_label)
                                    if attempt > 1:
                                        retry_count += 1
                                    if not is_flagged(candidate) or attempt == MAX_RETRIES:
                                        if is_flagged(candidate):
                                            print(f"  ⚠ still flagged after {MAX_RETRIES} attempts — keeping best result", file=sys.stderr)
                                        q = candidate
                                        stem_rewritten_q += 1
                                        modified = True
                                        break
                            except Exception as exc:  # noqa: BLE001
                                print(f"  ⚠ ERROR ({exc}) — keeping original question", file=sys.stderr)
                        elif not short:
                            print(
                                f"→ [{current_block}] {lecture_label[:40]:40s}  "
                                f"{q[0][:55]}{'…' if len(q[0]) > 55 else ''}",
                                flush=True,
                            )
                            try:
                                for attempt in range(1, MAX_RETRIES + 1):
                                    candidate = improve_question(client, q, lecture_label)
                                    if attempt > 1:
                                        retry_count += 1
                                    if not is_flagged(candidate) or attempt == MAX_RETRIES:
                                        if is_flagged(candidate):
                                            print(f"  ⚠ still flagged after {MAX_RETRIES} attempts — keeping best result", file=sys.stderr)
                                        q = candidate
                                        improved_q += 1
                                        modified = True
                                        break
                            except Exception as exc:  # noqa: BLE001
                                print(f"  ⚠ ERROR ({exc}) — keeping original distractors", file=sys.stderr)

                # ── Apply patch (apply-patch mode): substitute options from patch file
                if args.apply_patch and line_idx in patch_map:
                    patch = patch_map[line_idx]
                    text, options, correct_letter, explanation = q
                    correct_idx = ord(correct_letter) - ord("A")
                    new_options = list(options)
                    if "new_correct" in patch:
                        new_options[correct_idx] = patch["new_correct"]
                        text = patch.get("new_stem", text)
                    di = 0
                    for i in range(5):
                        if i != correct_idx:
                            new_options[i] = patch["new_distractors"][di]
                            di += 1
                    q = [text, new_options, correct_letter, explanation]
                    patched_q += 1
                    modified = True

                # ── Redistribute correct answer position (opt-in only)
                if not args.dry_run and args.redistribute_positions:
                    target_idx = pick_target_letter(letter_counts)
                    q = redistribute_position(q, target_idx)
                    letter_counts[q[2]] += 1
                    repositioned_q += 1
                    modified = True

                if not args.dry_run and modified:
                    trailing = "," if line.rstrip("\n").endswith(",") else ""
                    new_lines.append(f"        {serialize_question(q)}{trailing}\n")
                    continue

        new_lines.append(line)

    # ── Summary
    pct_flagged = f"{100 * flagged_q / total_q:.0f}%" if total_q else "n/a"
    print(f"\n{'─' * 50}")
    print(f"Block:             {args.block}")
    print(f"Questions seen:    {total_q}")
    print(f"Flagged:           {flagged_q} ({pct_flagged})")
    print(f"  ├─ distractor fix: {flagged_q - short_answer_q}")
    print(f"  └─ stem rewrite:   {short_answer_q} {'(skipped — use --rewrite-stems)' if not args.rewrite_stems else ''}")

    if args.export_flagged:
        export_path = os.path.join(scripts_dir, "flagged_questions.json")
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(flagged_export, f, ensure_ascii=False, indent=2)
        print(f"\n✓ Wrote {len(flagged_export)} flagged questions to {export_path}")
    elif args.apply_patch:
        print(f"Patches applied:   {patched_q}")
        if args.redistribute_positions:
            print(f"Positions shuffled:{repositioned_q}")
            print(f"Letter distribution: { {l: letter_counts[l] for l in 'ABCDE'} }")
        if patched_q > 0 or repositioned_q > 0:
            with open(js_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"\n✓ Wrote updated script.js")
    elif not args.dry_run:
        print(f"Distractors fixed: {improved_q}")
        print(f"Stems rewritten:   {stem_rewritten_q}")
        print(f"Retries used:      {retry_count}")
        if args.redistribute_positions:
            print(f"Positions shuffled:{repositioned_q}")
            print(f"Letter distribution: { {l: letter_counts[l] for l in 'ABCDE'} }")
        if improved_q > 0 or stem_rewritten_q > 0 or repositioned_q > 0:
            with open(js_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"\n✓ Wrote updated script.js")


if __name__ == "__main__":
    main()
