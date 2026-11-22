#!/usr/bin/env python3
"""Words Generator — draft words from Taylor's own notes.

Trains a word-level Markov chain (order 2) on the note files you
give it, then drafts new text in that material's own voice and
vocabulary: your words, recombined. The output is raw draft
material to edit — a drafting spur toward the word goal, not
finished prose.

Usage:
    python3 words_generator.py NOTES... [--words 500]
                               [--seed "a phrase from the notes"]
                               [--out draft.md] [--random-seed 42]
    python3 words_generator.py --selftest

Stdlib only. Built 2026-11-22 at Taylor's direction ("Create
python to create those words" — a generator that drafts words
from Taylor's notes, toward the 95,000-word Ulysses goal).
"""

import argparse
import random
import re
import sys
from collections import defaultdict
from datetime import datetime

MIN_CORPUS_WORDS = 200
SENTENCE_END = (".", "!", "?")
MAX_SENTENCE_WORDS = 40


def tokenize(text):
    """Split text into sentences of word tokens, punctuation kept
    attached to the word it follows."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # md links
    text = re.sub(r"[#>*_`]+", " ", text)                  # md marks
    sentences = []
    for chunk in re.split(r"(?<=[.!?])\s+", text):
        words = chunk.split()
        if len(words) >= 3:
            sentences.append(words)
    return sentences


def build_chain(sentences):
    chain = defaultdict(list)
    starts = []
    for words in sentences:
        if words[0][:1].isupper():
            starts.append((words[0], words[1]))
        for i in range(len(words) - 2):
            chain[(words[i], words[i + 1])].append(words[i + 2])
        chain[(words[-2], words[-1])].append(None)  # sentence end
    return chain, starts


def gen_sentence(chain, starts, rng, seed_words=None):
    if seed_words and len(seed_words) >= 2:
        pair = (seed_words[-2], seed_words[-1])
        if pair not in chain:
            pair = rng.choice(starts)
    else:
        pair = rng.choice(starts)
    out = [pair[0], pair[1]]
    while len(out) < MAX_SENTENCE_WORDS:
        nxt = rng.choice(chain.get(pair, [None]))
        if nxt is None:
            break
        out.append(nxt)
        pair = (pair[1], nxt)
        if nxt.endswith(SENTENCE_END):
            break
    text = " ".join(out)
    if not text.endswith(SENTENCE_END):
        text += "."
    return text


def generate(sentences, target_words, seed=None, rng=None):
    rng = rng or random.Random()
    chain, starts = build_chain(sentences)
    if not starts:
        raise ValueError("no sentence starts found in the notes")
    seed_words = seed.split() if seed else None
    paragraphs, total = [], 0
    while total < target_words:
        para = []
        for _ in range(rng.randint(3, 5)):
            s = gen_sentence(chain, starts, rng, seed_words)
            para.append(s)
            total += len(s.split())
            if total >= target_words:
                break
        paragraphs.append(" ".join(para))
    return "\n\n".join(paragraphs), total


def read_notes(paths):
    texts = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            texts.append(fh.read())
    return "\n".join(texts)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Draft words from your notes.")
    ap.add_argument("notes", nargs="*", help="note files to train on")
    ap.add_argument("--words", type=int, default=500,
                    help="target word count (default 500)")
    ap.add_argument("--seed", help="phrase from the notes to start from")
    ap.add_argument("--out", help="write the draft here (default: stdout)")
    ap.add_argument("--random-seed", type=int,
                    help="reproducible runs")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.notes:
        ap.error("give at least one notes file (or --selftest)")

    text = read_notes(args.notes)
    sentences = tokenize(text)
    corpus_words = sum(len(s) for s in sentences)
    if corpus_words < MIN_CORPUS_WORDS:
        print(f"Not enough notes to draft from: {corpus_words} words "
              f"(need at least {MIN_CORPUS_WORDS}). Give it more of "
              f"your notes.", file=sys.stderr)
        return 2

    rng = random.Random(args.random_seed)
    draft, count = generate(sentences, args.words, args.seed, rng)
    header = (f"<!-- Drafted by words_generator.py from "
              f"{', '.join(args.notes)} ({corpus_words} words of "
              f"notes) on "
              f"{datetime.now().strftime('%Y-%m-%d %H%M')} — raw "
              f"draft material, your words recombined. -->\n\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(header + draft + "\n")
        print(f"Wrote {count} draft words to {args.out} "
              f"(trained on {corpus_words} words of notes).")
    else:
        print(draft)
        print(f"\n[{count} draft words, trained on {corpus_words} "
              f"words of notes]", file=sys.stderr)
    return 0


def selftest():
    sample = (
        "The river runs cold in the morning. The crew checks the "
        "radios before the first call. A call comes in at dawn and "
        "the crew moves without a word. The bay doors open to the "
        "cold. Every shift writes its own story in the log book. "
        "The log book holds the names and the times. Training makes "
        "the hard calls smaller. The hard calls still come at night. "
        "At night the road is empty and the lights carry far. "
    ) * 12
    sentences = tokenize(sample)
    assert sum(len(s) for s in sentences) >= MIN_CORPUS_WORDS
    draft, count = generate(sentences, 120,
                            rng=random.Random(7))
    vocab = set(sample.replace(".", " ").split())
    out_words = [w.strip(".!?") for w in draft.split()]
    assert count >= 120, count
    assert all(w in vocab for w in out_words), "foreign word leaked in"
    seeded, _ = generate(sentences, 60, seed="The river runs",
                          rng=random.Random(3))
    assert seeded
    print("words_generator selftest: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
