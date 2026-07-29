"""Content normalisation and hashing.

`content_hash` is the mechanism that makes the FAISS index self-healing
(`DATABASE_DESIGN.md` §4.3): a memory whose stored hash differs from its
embedding's hash has been edited since it was indexed, and its vector is stale.

Everything therefore depends on the hash changing when — and only when — the
*meaning* of the content changed. Hashing the raw string makes a trailing
newline trigger a re-embed of an unchanged fact; hashing too aggressively makes
a real edit invisible and leaves a wrong vector in place forever. The second
failure is much worse, so normalisation here is deliberately conservative: it
removes only differences no embedding model would ever encode.
"""

from __future__ import annotations

import hashlib
import unicodedata

_HASH_PREFIX_LENGTH = 32


def normalise(text: str) -> str:
    """Reduce `text` to its hashable form.

    Applies, in order:

    - **NFC** Unicode composition, so "é" typed as one codepoint and as "e" plus
      a combining accent hash identically. They render identically and tokenise
      identically; treating them as different content would be a pure false
      positive.
    - Line-ending normalisation, so content pasted from Windows does not
      re-embed on every save.
    - Trailing whitespace stripped per line, and leading/trailing blank lines
      removed.

    Deliberately **not** applied: case folding, punctuation stripping, or
    internal whitespace collapsing. All three change meaning that the embedding
    model does encode — "the deploy failed" and "The deploy failed?" are not the
    same fact, and indentation is semantic inside a code block.
    """
    composed = unicodedata.normalize("NFC", text)
    lines = composed.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def content_hash(text: str) -> str:
    """Stable hash of normalised `text`.

    Truncated to 128 bits. This is a change detector, not a security primitive —
    there is no adversary choosing memory content to collide — and a shorter key
    keeps the `idx_memories_hash` index smaller. 128 bits still puts a chance
    collision far beyond any realistic corpus.
    """
    digest = hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()
    return digest[:_HASH_PREFIX_LENGTH]
