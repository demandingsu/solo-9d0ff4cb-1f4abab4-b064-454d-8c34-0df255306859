"""Restore legacy batch membership from retained raw text and in-stock rules.

Batches stored before the ``submission_rules`` table existed only kept the
comma-joined rule codes in ``submissions.rule_codes`` (the "retained original
text").  That encoding is ambiguous: a single rule numbered ``A,B`` and two
rules numbered ``A`` and ``B`` both read as ``A,B``, and splitting on commas
can silently point a batch at rules that in fact belong to other batches.

For one item, the in-stock rules are exactly the union of the rules of all
accepted batches (rule codes are unique per item and rules are never
deleted).  A legacy batch is therefore restored by segmenting its retained
text on commas so that every segment is an in-stock rule code, under the
global constraint that each in-stock rule is assigned to exactly one batch.
A batch whose ordered code list is the same in every valid global assignment
is uniquely determinable; anything else is reported as undeterminable and the
retained original text is returned alongside.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set, Tuple

# Safety bounds for pathological data (many comma-heavy codes).  Exceeding a
# bound means uniqueness can no longer be proven, so the affected batches are
# conservatively reported as undeterminable.
MAX_SEGMENTATIONS_PER_BATCH = 256
MAX_PARTITIONS = 1000


class _CapExceeded(Exception):
    """Internal: a safety bound was exceeded while enumerating."""


def segmentations(text: str, codes: Set[str], cap: int = MAX_SEGMENTATIONS_PER_BATCH
                  ) -> Optional[List[List[str]]]:
    """All ways to split ``text`` into comma-joined segments from ``codes``.

    Each returned list contains rule codes in their original (submission)
    order and joins back to exactly ``text`` with commas.  Returns ``None``
    when there are more than ``cap`` segmentations.
    """
    if not text:
        return []
    by_first: Dict[str, List[str]] = {}
    for code in codes:
        if code:
            by_first.setdefault(code[0], []).append(code)
    memo: Dict[int, List[List[str]]] = {}
    end = len(text)

    def walk(pos: int) -> List[List[str]]:
        if pos == end:
            return [[]]
        if pos in memo:
            return memo[pos]
        out: List[List[str]] = []
        for code in by_first.get(text[pos], ()):
            if not text.startswith(code, pos):
                continue
            nxt = pos + len(code)
            if nxt == end:
                out.append([code])
            elif text[nxt] == ",":
                for rest in walk(nxt + 1):
                    out.append([code] + rest)
            if len(out) > cap:
                raise _CapExceeded
        memo[pos] = out
        return out

    try:
        return walk(0)
    except _CapExceeded:
        return None
    except RecursionError:  # absurdly long retained text
        return None


def reconstruct_batches(batches: Sequence[Tuple[int, str]],
                        legacy_codes: Set[str],
                        max_partitions: int = MAX_PARTITIONS
                        ) -> Dict[int, Optional[List[str]]]:
    """Assign each legacy batch its ordered rule codes, or ``None``.

    ``batches`` is ``(submission_id, retained_text)`` for the legacy accepted
    batches of one item; ``legacy_codes`` are the in-stock rule codes not
    already owned by new-format batches.  A batch maps to its code list when
    every valid global assignment (each rule in exactly one batch) agrees on
    it, and to ``None`` when the membership cannot be uniquely determined.
    """
    result: Dict[int, Optional[List[str]]] = {sub_id: None for sub_id, _ in batches}
    if not batches:
        return result

    candidates: Dict[int, List[List[str]]] = {}
    for sub_id, text in batches:
        segs = segmentations(text, legacy_codes)
        if not segs:  # no segmentation, or too many to prove uniqueness
            return result
        candidates[sub_id] = segs

    # Every in-stock rule must be assignable to at least one batch, otherwise
    # no global assignment exists (inconsistent data).
    coverable: Set[str] = set()
    for segs in candidates.values():
        for seg in segs:
            coverable.update(seg)
    if not legacy_codes <= coverable:
        return result

    order = sorted(candidates, key=lambda i: (len(candidates[i]), i))
    used: Set[str] = set()
    assignment: Dict[int, List[str]] = {}
    observed: Dict[int, set] = {sub_id: set() for sub_id, _ in batches}
    solutions = 0
    overflow = False

    def dfs(idx: int) -> bool:
        """Enumerate assignments; return False to abort (cap reached)."""
        nonlocal solutions, overflow
        if solutions >= max_partitions:
            overflow = True
            return False
        if idx == len(order):
            if used == legacy_codes:  # every rule assigned to exactly one batch
                solutions += 1
                for sub_id in order:
                    observed[sub_id].add(tuple(assignment[sub_id]))
            return True
        sub_id = order[idx]
        for seg in candidates[sub_id]:
            if any(code in used for code in seg):
                continue
            used.update(seg)
            assignment[sub_id] = seg
            if not dfs(idx + 1):
                return False
            for code in seg:
                used.discard(code)
            del assignment[sub_id]
        return True

    dfs(0)
    if overflow or solutions == 0:
        return result
    for sub_id, _ in batches:
        if len(observed[sub_id]) == 1:
            result[sub_id] = list(next(iter(observed[sub_id])))
    return result
