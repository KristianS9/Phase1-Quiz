#!/usr/bin/env python3
"""
improve_distractors.py — Improve quiz distractors + fix answer position bias

Identifies questions where the correct answer is significantly longer than the
distractors (a telltale giveaway), then regenerates the distractors so that
length is no longer a reliable cue. Distractors are grounded in the Notion
notes context already embedded in each question's explanation.

Additionally, redistributes correct answer positions across ALL questions to
achieve an even A/B/C/D/E spread (~20% each), fixing the stored positional bias.

Usage:
  python3 scripts/improve_distractors.py --block 1          # Pilot Block 1
  python3 scripts/improve_distractors.py --block all        # All blocks
  python3 scripts/improve_distractors.py --dry-run --block 1  # Report only

Requirements:
  pip3 install anthropic
  export ANTHROPIC_API_KEY=sk-...
"""

import json
import re
import sys
import argparse
import os
import random

# ── Flagging thresholds ──────────────────────────────────────────────────────

# Flag if correct answer is >= this multiple of the average distractor length
RATIO_THRESHOLD = 1.5

# Flag if any single distractor is shorter than this fraction of correct answer
MIN_DISTRACTOR_FRACTION = 0.4


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
    """Return True if this question's distractors are suspiciously short."""
    _, options, correct_letter, _ = q
    correct_idx = ord(correct_letter) - ord("A")
    correct_len = len(options[correct_idx])

    if correct_len == 0:
        return False

    distractor_lens = [len(options[i]) for i in range(5) if i != correct_idx]
    avg_d = sum(distractor_lens) / 4
    min_d = min(distractor_lens)

    ratio = correct_len / avg_d if avg_d > 0 else 0
    return ratio >= RATIO_THRESHOLD or min_d < MIN_DISTRACTOR_FRACTION * correct_len


# ── Position redistribution ──────────────────────────────────────────────────

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
1. All options (including the correct answer) should be broadly similar in length. \
A ±50% character count range is acceptable; no option should be so dramatically shorter \
or longer than the others that a student could identify the correct answer by scanning lengths.
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


def improve_question(client, q, lecture_name: str):
    """Call Claude to regenerate distractors; return updated question list."""
    text, options, correct_letter, explanation = q
    correct_idx = ord(correct_letter) - ord("A")
    correct_answer = options[correct_idx]

    prompt = USER_TEMPLATE.format(
        question=text,
        correct=correct_answer,
        lecture=lecture_name,
        explanation=explanation,
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


# ── Serialisation ─────────────────────────────────────────────────────────────

def serialize_question(q) -> str:
    """Compact single-line JSON matching the original file format."""
    return json.dumps(q, ensure_ascii=False, separators=(",", ":"))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Improve quiz distractors via Claude API")
    parser.add_argument(
        "--block",
        default="1",
        help='Block to process: "1", "2", "3", "4", or "all" (default: 1)',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print flagged questions only; do not call the API or modify index.html",
    )
    args = parser.parse_args()

    html_path = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")
    )

    if not os.path.exists(html_path):
        sys.exit(f"ERROR: index.html not found at {html_path}")

    # Set up API client unless dry-run
    client = None
    if not args.dry_run:
        try:
            import anthropic  # noqa: PLC0415
        except ImportError:
            sys.exit("ERROR: anthropic not installed. Run: pip3 install anthropic")

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("ERROR: ANTHROPIC_API_KEY environment variable not set")

        client = anthropic.Anthropic(api_key=api_key)

    with open(html_path, encoding="utf-8") as f:
        lines = f.readlines()

    target_blocks = None if args.block == "all" else {args.block}

    current_block: str | None = None
    current_lecture: str | None = None

    # Per-block letter distribution counters (reset per block)
    letter_counts: dict[str, int] = {l: 0 for l in "ABCDE"}

    total_q = 0
    flagged_q = 0
    improved_q = 0
    repositioned_q = 0
    new_lines = []

    for line in lines:
        # ── Track current block (2-space indent, numeric quoted key)
        bm = re.match(r'^  "(\d+)":\s*\{', line)
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

                # Step 1: Improve distractors for flagged questions
                if is_flagged(q):
                    flagged_q += 1
                    correct_idx = ord(q[2]) - ord("A")
                    correct_len = len(q[1][correct_idx])
                    d_lens = [len(q[1][i]) for i in range(5) if i != correct_idx]
                    avg_d = sum(d_lens) / 4

                    if args.dry_run:
                        print(
                            f"[Block {current_block}] {lecture_label}\n"
                            f"  Q: {q[0][:90]}{'…' if len(q[0]) > 90 else ''}\n"
                            f"  Correct ({q[2]}): len={correct_len}  "
                            f"avg-distractor: {avg_d:.0f}  ratio: {correct_len/avg_d:.2f}\n"
                            f"  Distractors: {d_lens}\n"
                        )
                    else:
                        print(
                            f"→ [{current_block}] {lecture_label[:40]:40s}  "
                            f"{q[0][:55]}{'…' if len(q[0]) > 55 else ''}",
                            flush=True,
                        )
                        try:
                            q = improve_question(client, q, lecture_label)
                            improved_q += 1
                        except Exception as exc:  # noqa: BLE001
                            print(f"  ⚠ ERROR ({exc}) — keeping original distractors", file=sys.stderr)

                # Step 2: Redistribute correct answer position for ALL questions
                if not args.dry_run:
                    target_idx = pick_target_letter(letter_counts)
                    q = redistribute_position(q, target_idx)
                    letter_counts[q[2]] += 1
                    repositioned_q += 1

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
    if not args.dry_run:
        print(f"Distractors fixed: {improved_q}")
        print(f"Positions shuffled:{repositioned_q}")
        print(f"Letter distribution: { {l: letter_counts[l] for l in 'ABCDE'} }")

    if not args.dry_run and repositioned_q > 0:
        with open(html_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"\n✓ Wrote updated index.html")


if __name__ == "__main__":
    main()
