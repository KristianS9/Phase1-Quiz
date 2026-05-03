#!/usr/bin/env python3
"""
find_question.py — search for questions in script.js

Usage:
    python3 scripts/find_question.py "oxidative phosphorylation"
    python3 scripts/find_question.py "thyroid" --block 1
    python3 scripts/find_question.py "glycolysis" --lecture "Energy Metabolism"
    python3 scripts/find_question.py --block 2 --correct A
"""

import re
import sys
import argparse
from pathlib import Path

SCRIPT_JS = Path(__file__).parent.parent / 'script.js'
LETTERS = 'ABCDE'


# ── JS mini-parser ────────────────────────────────────────────────────────────

def read_string(s, i):
    """Read a JS quoted string starting at s[i]. Returns (value, next_i)."""
    q = s[i]
    i += 1
    buf = []
    while i < len(s):
        c = s[i]
        if c == '\\':
            i += 1
            buf.append({'n':'\n','t':'\t','r':'\r','\\':'\\','/':'/'}.get(s[i], s[i]))
        elif c == q:
            return ''.join(buf), i + 1
        else:
            buf.append(c)
        i += 1
    raise ValueError(f'Unterminated string at {i}')


def read_array(s, i):
    """Read a JS array starting at s[i]='['. Returns (list, next_i)."""
    assert s[i] == '[', repr(s[i])
    i += 1
    items = []
    while i < len(s):
        # skip whitespace + commas
        while i < len(s) and s[i] in ' \t\n\r,':
            i += 1
        if i >= len(s):
            break
        c = s[i]
        if c == ']':
            return items, i + 1
        elif c in ('"', "'"):
            v, i = read_string(s, i)
            items.append(v)
        elif c == '[':
            v, i = read_array(s, i)
            items.append(v)
        else:
            i += 1
    return items, i


def is_question(arr):
    return (
        len(arr) == 4
        and isinstance(arr[0], str) and len(arr[0]) > 10
        and isinstance(arr[1], list) and len(arr[1]) == 5
        and all(isinstance(o, str) for o in arr[1])
        and isinstance(arr[2], str) and arr[2] in LETTERS
        and isinstance(arr[3], str)
    )


# ── index builder ─────────────────────────────────────────────────────────────

BLOCK_RE   = re.compile(r'"(\d+)"\s*:\s*\{\s*\n?\s*name\s*:\s*"([^"]+)"')
LECTURE_RE = re.compile(r'\n\s+"([^"]{3,80})"\s*:\s*\[')

def build_index(text):
    """
    Returns list of dicts:
      {block, lecture, line, stem, options, correct, explanation}
    """
    lines = text.splitlines(keepends=True)
    # char offset → line number
    char_to_line = []
    for ln, line in enumerate(lines, 1):
        char_to_line.extend([ln] * len(line))

    def line_of(pos):
        return char_to_line[pos] if pos < len(char_to_line) else len(lines)

    # Map each char position to (block, lecture)
    # Build sorted list of (pos, block, lecture) events
    events = []  # (pos, block_name, lecture_name)

    current_block = None
    for m in BLOCK_RE.finditer(text):
        current_block = m.group(2)
        # Now find lectures in this block
        # Find the "lectures": { section
        lec_start = text.find('lectures:', m.end())
        if lec_start == -1:
            continue
        # Find the next block start to bound the search
        next_block = BLOCK_RE.search(text, m.end())
        bound = next_block.start() if next_block else len(text)
        for lm in LECTURE_RE.finditer(text, lec_start, bound):
            events.append((lm.start(), current_block, lm.group(1)))

    events.sort()

    def context_at(pos):
        blk = lec = '?'
        for epos, eb, el in events:
            if epos <= pos:
                blk, lec = eb, el
            else:
                break
        return blk, lec

    # Find all question arrays
    results = []
    q_pat = re.compile(r'\n\s+\[["\'"]')  # line starting a question array
    for m in q_pat.finditer(text):
        bracket_pos = text.index('[', m.start())
        try:
            arr, _ = read_array(text, bracket_pos)
        except Exception:
            continue
        if is_question(arr):
            blk, lec = context_at(bracket_pos)
            results.append({
                'block':       blk,
                'lecture':     lec,
                'line':        line_of(bracket_pos),
                'stem':        arr[0],
                'options':     arr[1],
                'correct':     arr[2],
                'explanation': arr[3],
            })

    return results


# ── display ───────────────────────────────────────────────────────────────────

def print_result(q, idx, total):
    sep = '─' * 72
    print(f'\n{sep}')
    print(f'  [{idx}/{total}]  {q["block"]}  ›  {q["lecture"]}  (line {q["line"]})')
    print(sep)
    print(f'\n  {q["stem"]}\n')
    for i, opt in enumerate(q['options']):
        letter = LETTERS[i]
        marker = ' ✓' if letter == q['correct'] else '  '
        print(f'  {letter}.{marker} {opt}')
    expl = q['explanation']
    print(f'\n  Explanation: {expl[:220]}{"…" if len(expl) > 220 else ""}')
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Search quiz questions in script.js')
    parser.add_argument('query', nargs='?', default='', help='Text to search in question stems')
    parser.add_argument('--block',   '-b', help='Filter by block number or name (e.g. 1, "Block 2")')
    parser.add_argument('--lecture', '-l', help='Filter by lecture name (partial match, case-insensitive)')
    parser.add_argument('--correct', '-c', help='Filter by correct answer letter (A–E)')
    args = parser.parse_args()

    if not any([args.query, args.block, args.lecture, args.correct]):
        parser.print_help()
        sys.exit(0)

    print('Indexing script.js…', end=' ', flush=True)
    text = SCRIPT_JS.read_text(encoding='utf-8')
    questions = build_index(text)
    print(f'{len(questions)} questions found.')

    results = []
    for q in questions:
        if args.query and args.query.lower() not in q['stem'].lower():
            continue
        if args.block:
            b = args.block.strip()
            if b not in q['block'] and f'Block {b}' != q['block']:
                continue
        if args.lecture and args.lecture.lower() not in q['lecture'].lower():
            continue
        if args.correct and q['correct'] != args.correct.upper():
            continue
        results.append(q)

    if not results:
        print(f'No matches.')
        sys.exit(0)

    print(f'\n{len(results)} match(es)', end='')
    if args.query:
        print(f' for "{args.query}"', end='')
    print()

    for idx, q in enumerate(results, 1):
        print_result(q, idx, len(results))


if __name__ == '__main__':
    main()
