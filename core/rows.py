"""Row-aligned scoring for repeated sections (line items, receipt tax lines).

doctypes declares which paths are repeated (`DocTypeSpec.list_paths`) and warns that "a naive
index-wise comparison is meaningless". This module is what makes that declaration usable.

WHY ALIGNMENT AND NOT INDEXING. If the model drops the second of six rows, an index-wise
comparison marks rows 2..6 wrong — five failures from one. The reported number then measures
row ORDER, not extraction. Worse, it is not monotone: a model that also drops row 1 can score
better. Alignment asks the two questions separately and reports both:

    did we find the row?     -> row precision / recall / F1, from the assignment
    did we get its cells?    -> per-field accuracy over MATCHED rows only

Neither is meaningful alone. Per-field accuracy over matched rows rewards a model that emits
one perfect row and drops nine; row F1 says nothing about whether the numbers are right. Any
report using this module must publish both.

WHY EXACT ASSIGNMENT AND NOT GREEDY. DocILE100 has six consecutive rows reading
'STEWART JENKINS AD 5' that differ only in quantity and price. Greedy matching assigns those
arbitrarily and then scores the wrong cells against each other. This module solves the
assignment exactly (Hungarian / Kuhn-Munkres, O(n^3) on a matrix bounded by the row count —
44 at the largest here) with numeric agreement as a deterministic tie-break, so interchangeable
rows are paired the way that reflects best on nothing in particular: identically.

No new dependency. requirements.txt is pinned to ai_backend's exactly, so scipy's
linear_sum_assignment is not available and the algorithm is implemented and tested here.
"""
from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.matching import MatchRule, compare, rule_for       # noqa: E402
from core.normalize import anls, numeric_from_field          # noqa: E402

#: A predicted row is the same row as a GT row when their identifying text agrees at least
#: this well. 0.8 is the map's declared ANLS threshold for line-item descriptions; it is the
#: identity test only — getting the row's NUMBERS wrong must not un-find the row, or row
#: recall and field accuracy stop being independent measurements.
MATCH_THRESHOLD = 0.8

#: DEFAULT prediction-side leaves that can carry a row's identifying text, for a line-item
#: shaped section. Both are consulted because datasets disagree about which column holds the
#: description (see the DocILE adapter), and so do models. Other repeated sections identify
#: their rows differently — a document-level charge by its printed label, `key` — so the
#: adapter declares the leaves per section and this is only the fallback.
PRED_TEXT_LEAVES: Tuple[str, ...] = ("description", "itemCode")

#: Row keys whose agreement breaks ties between otherwise identical candidates.
TIEBREAK_KEYS: Tuple[str, ...] = ("quantity", "unitPrice", "lineTotal")

_BIG = 1e6          # cost for a pair that fails the identity test


# --------------------------------------------------------------------------- results
@dataclass
class RowMatch:
    gt_index: int
    pred_index: int
    text_score: float
    matched_on: str            # which GT key carried the text that matched

    
@dataclass
class RowAlignment:
    matches: List[RowMatch] = field(default_factory=list)
    unmatched_gt: List[int] = field(default_factory=list)
    unmatched_pred: List[int] = field(default_factory=list)
    n_gt: int = 0
    n_pred: int = 0

    @property
    def recall(self) -> Optional[float]:
        """Of the rows the document states, how many did we find?"""
        return len(self.matches) / self.n_gt if self.n_gt else None

    @property
    def precision(self) -> Optional[float]:
        """Of the rows we emitted, how many are real? Invented rows land here and nowhere
        else — without this, a model that emits every plausible row scores perfect recall."""
        return len(self.matches) / self.n_pred if self.n_pred else None

    @property
    def f1(self) -> Optional[float]:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return 0.0 if (p is not None and r is not None) else None
        return 2 * p * r / (p + r)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_gt_rows": self.n_gt, "n_pred_rows": self.n_pred,
            "n_matched": len(self.matches),
            "row_precision": self.precision, "row_recall": self.recall, "row_f1": self.f1,
            "matched_on": sorted({m.matched_on for m in self.matches}),
        }


# --------------------------------------------------------------------------- similarity
def _text_of(row: Any, keys: Sequence[str]) -> List[Tuple[str, str]]:
    out = []
    if isinstance(row, dict):
        for k in keys:
            v = row.get(k)
            if isinstance(v, str) and v.strip():
                out.append((k, v))
    return out


def text_similarity(gt_row: Any, pred_row: Any, gt_keys: Sequence[str],
                    pred_keys: Sequence[str] = PRED_TEXT_LEAVES) -> Tuple[float, str]:
    """Best ANLS over every (GT text column) x (prediction text leaf) pair, and which GT
    column won.

    Cross-product rather than like-for-like because the annotation's choice of column drifts:
    DocILE100 puts the real description in itemName on most documents and in itemCode on the
    44-row radio-log family, where itemName is the generic label '60 Spot'. Scoring itemName
    against description alone would blame the model for the annotator's inconsistency.
    """
    gt_texts = _text_of(gt_row, gt_keys)
    pred_texts = _text_of(pred_row, pred_keys)
    if not gt_texts or not pred_texts:
        return 0.0, ""
    best, best_key = 0.0, ""
    for gk, gv in gt_texts:
        for _, pv in pred_texts:
            s = anls(pv, gv)
            if s > best:
                best, best_key = s, gk
    return best, best_key


def _numeric_agreement(gt_row: Any, pred_row: Any, row_targets: Dict[str, Tuple[str, ...]]) -> float:
    """Fraction of comparable numeric cells that agree at 2dp. Tie-break only."""
    if not isinstance(gt_row, dict) or not isinstance(pred_row, dict):
        return 0.0
    hits = total = 0
    for key in TIEBREAK_KEYS:
        gv = gt_row.get(key)
        if gv is None:
            continue
        t, _ = numeric_from_field(gv)
        if t is None:
            continue
        total += 1
        for leaf in row_targets.get(key, (key,)):
            p, _ = numeric_from_field(pred_row.get(leaf))
            if p is not None and p == t:
                hits += 1
                break
    return hits / total if total else 0.0


# --------------------------------------------------------------------------- assignment
def _hungarian(cost: List[List[float]]) -> List[Tuple[int, int]]:
    """Minimum-cost perfect matching on a rectangular matrix (Jonker-Volgenant style
    shortest-augmenting-path). Returns (row, col) pairs for the first min(n, m) rows.

    Implemented here rather than pulled from scipy because requirements.txt is pinned to
    ai_backend's exactly and must not gain a dependency the product does not have.
    """
    n = len(cost)
    m = len(cost[0]) if n else 0
    if n == 0 or m == 0:
        return []
    transposed = n > m
    if transposed:
        cost = [[cost[i][j] for i in range(n)] for j in range(m)]
        n, m = m, n

    INF = float("inf")
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)          # p[j] = row assigned to column j (1-based; 0 = none)
    way = [0] * (m + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], INF, -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    pairs = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j] != 0]
    return [(c, r) for r, c in pairs] if transposed else pairs


def align(gt_rows: Sequence[Any], pred_rows: Sequence[Any], *,
          gt_match_keys: Sequence[str],
          row_targets: Dict[str, Tuple[str, ...]],
          pred_text_leaves: Sequence[str] = PRED_TEXT_LEAVES,
          threshold: float = MATCH_THRESHOLD) -> RowAlignment:
    """Pair GT rows with predicted rows by identity, exactly and deterministically."""
    gt_rows = list(gt_rows or [])
    pred_rows = list(pred_rows or [])
    al = RowAlignment(n_gt=len(gt_rows), n_pred=len(pred_rows))
    if not gt_rows or not pred_rows:
        al.unmatched_gt = list(range(len(gt_rows)))
        al.unmatched_pred = list(range(len(pred_rows)))
        return al

    scores: Dict[Tuple[int, int], Tuple[float, str]] = {}
    cost: List[List[float]] = []
    for i, g in enumerate(gt_rows):
        row = []
        for j, pr in enumerate(pred_rows):
            s, key = text_similarity(g, pr, gt_match_keys, pred_text_leaves)
            scores[(i, j)] = (s, key)
            if s >= threshold:
                # Identity decides eligibility; numeric agreement only orders equal candidates.
                row.append(-(s + 0.001 * _numeric_agreement(g, pr, row_targets)))
            else:
                row.append(_BIG)
        cost.append(row)

    for i, j in _hungarian(cost):
        if cost[i][j] >= _BIG:
            continue                     # the assignment had to use an ineligible cell
        s, key = scores[(i, j)]
        al.matches.append(RowMatch(gt_index=i, pred_index=j, text_score=s, matched_on=key))

    al.matches.sort(key=lambda m: m.gt_index)
    taken_gt = {m.gt_index for m in al.matches}
    taken_pred = {m.pred_index for m in al.matches}
    al.unmatched_gt = [i for i in range(len(gt_rows)) if i not in taken_gt]
    al.unmatched_pred = [j for j in range(len(pred_rows)) if j not in taken_pred]
    return al


# --------------------------------------------------------------------------- scoring
def score_rows(gt_rows: Sequence[Any], pred_rows: Sequence[Any], *,
               list_path: str,
               gt_match_keys: Sequence[str],
               row_targets: Dict[str, Tuple[str, ...]],
               leaf_types: Dict[str, str],
               pred_text_leaves: Sequence[str] = PRED_TEXT_LEAVES,
               doc_id: str = "", cluster_id: str = "",
               spec=None,
               threshold: float = MATCH_THRESHOLD) -> Dict[str, Any]:
    """Align, then score the matched rows cell by cell.

    Cells are scored ONLY on matched rows and ONLY where the GT row states a value. Both
    restrictions matter: an unmatched row's cells have nothing to compare against, and a
    corpus-wide denominator per column would be wrong wherever the column is sparse — DocILE100
    states a quantity on 264 of 450 rows, so a 450-row quantity denominator overstates by 40%.

    Where row_targets gives several leaves for one GT key they are a UNION: the best-matching
    leaf wins, and the leaf that won is recorded. That is how one GT line total can be scored
    against a model that routed it to lineTotalIncludingTax rather than …ExcludingTax without
    crediting or penalising the routing.
    """
    al = align(gt_rows, pred_rows, gt_match_keys=gt_match_keys, row_targets=row_targets,
               pred_text_leaves=pred_text_leaves, threshold=threshold)

    cells: List[Dict[str, Any]] = []
    row_exact_flags: List[bool] = []

    for m in al.matches:
        g, pr = gt_rows[m.gt_index], pred_rows[m.pred_index]
        comparable = 0
        correct = 0
        for gt_key, leaves in row_targets.items():
            truth = g.get(gt_key) if isinstance(g, dict) else None
            if truth is None:
                continue
            comparable += 1
            leaf_path = f"{list_path}[].{leaves[0]}"
            rule = rule_for(leaf_path, leaf_types.get(leaves[0], "str"), spec)
            best = None
            best_leaf = None
            for leaf in leaves:
                pred = pr.get(leaf) if isinstance(pr, dict) else None
                res = compare(pred, truth, rule)
                if best is None or (res.matched, res.score) > (best.matched, best.score):
                    best, best_leaf = res, leaf
                if res.matched:
                    break
            ok = bool(best and best.matched)
            correct += ok
            cells.append({
                "doc_id": doc_id, "template": cluster_id,
                "path": f"{list_path}[].{gt_key}", "rule": rule.value,
                "state": "correct" if ok else "wrong",
                "ok": ok, "score": best.score if best else 0.0,
                "exact": bool(best.exact) if best else False,
                "gt_row": m.gt_index, "pred_row": m.pred_index,
                "matched_leaf": best_leaf, "matched_on": m.matched_on,
                "value_source": best.value_source if best else None,
            })
        row_exact_flags.append(comparable > 0 and correct == comparable)

    summary = al.to_dict()
    summary.update({
        "doc_id": doc_id, "cluster_id": cluster_id, "list_path": list_path,
        "n_rows_exact": sum(row_exact_flags),
        "row_exact_rate": (sum(row_exact_flags) / len(row_exact_flags)
                           if row_exact_flags else None),
        # The strictest reading, and the one a buyer actually cares about: every stated row
        # found, nothing invented, every cell right.
        "table_exact": bool(al.n_gt and al.n_gt == al.n_pred == len(al.matches)
                            and all(row_exact_flags)),
        "n_cells": len(cells),
        "n_cells_correct": sum(1 for c in cells if c["ok"]),
    })
    return {"summary": summary, "cells": cells, "alignment": al}
