"""Build `app/discovery/wordlists/english_words.txt`, the English vocabulary the
foreign-title filter checks titles against (ADR-0029, G233).

The words come from the English blurbs (`books.description` that reads as
English to `goodreads_filters.snippet_language`) of the library databases
given, lowercased, a possessive "'s" dropped. Built once from the
reference install's libraries and shipped as a fixed file: built per
install it would be nearly empty on a fresh one, and an empty vocabulary
would make every title look foreign.

    python tools/build_english_words.py <seshat_<slug>.db> [<db> ...]
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.discovery.goodreads_filters import snippet_language  # noqa: E402

OUT = ROOT / "app" / "discovery" / "wordlists" / "english_words.txt"
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def words_of(text: str) -> set[str]:
    return {re.sub(r"'s$", "", w.lower()) for w in _WORD.findall(text or "")}


def main(paths: list[str]) -> None:
    vocab: set[str] = set()
    blurbs = 0
    for path in paths:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for (desc,) in db.execute("SELECT description FROM books WHERE description IS NOT NULL"):
            if snippet_language(desc) == "en":
                blurbs += 1
                vocab |= words_of(desc)
        db.close()
    vocab = {w for w in vocab if len(w) >= 2 and w.isascii()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "# English words from the reference install's English blurbs (tools/build_english_words.py)\n"
        + "\n".join(sorted(vocab)) + "\n",
        encoding="utf-8",
    )
    print(f"{len(vocab)} words from {blurbs} English blurbs -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1:])
