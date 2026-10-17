"""Agreement metrics for offline LLM judges (Cohen's κ / weighted κ).

Raw agreement rates inflate under class imbalance. These helpers implement the
metrology called for in docs/JUDGE_METROLOGY_REFINEMENT.md / LLM_JUDGE_GUIDE.md.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Hashable, Iterable, Sequence


def _pairs(
    a: Sequence[Hashable | None],
    b: Sequence[Hashable | None],
) -> list[tuple[Hashable, Hashable]]:
    if len(a) != len(b):
        raise ValueError(f"rater sequences length mismatch: {len(a)} vs {len(b)}")
    pairs: list[tuple[Hashable, Hashable]] = []
    for left, right in zip(a, b):
        if left is None or right is None:
            continue
        pairs.append((left, right))
    return pairs


def observed_agreement(a: Sequence[Hashable | None], b: Sequence[Hashable | None]) -> float | None:
    pairs = _pairs(a, b)
    if not pairs:
        return None
    return sum(1 for left, right in pairs if left == right) / len(pairs)


def cohen_kappa(a: Sequence[Hashable | None], b: Sequence[Hashable | None]) -> float | None:
    """Unweighted Cohen's κ for categorical labels (exact score or band)."""
    pairs = _pairs(a, b)
    if not pairs:
        return None
    n = len(pairs)
    categories = sorted({label for pair in pairs for label in pair}, key=str)
    if len(categories) < 2:
        # Degenerate: both raters only ever used one label.
        return 1.0 if pairs[0][0] == pairs[0][1] else 0.0

    index = {label: i for i, label in enumerate(categories)}
    matrix = [[0 for _ in categories] for _ in categories]
    for left, right in pairs:
        matrix[index[left]][index[right]] += 1

    po = sum(matrix[i][i] for i in range(len(categories))) / n
    row_marginal = [sum(row) / n for row in matrix]
    col_marginal = [
        sum(matrix[row][col] for row in range(len(categories))) / n
        for col in range(len(categories))
    ]
    pe = sum(r * c for r, c in zip(row_marginal, col_marginal))
    if abs(1.0 - pe) < 1e-12:
        return 1.0 if abs(po - 1.0) < 1e-12 else 0.0
    return (po - pe) / (1.0 - pe)


def quadratic_weighted_kappa(
    a: Sequence[Any | None],
    b: Sequence[Any | None],
    *,
    min_score: float = 1.0,
    max_score: float = 10.0,
) -> float | None:
    """Quadratic weighted κ for ordinal numeric scores on [min_score, max_score]."""
    pairs: list[tuple[float, float]] = []
    for left, right in zip(a, b):
        if left is None or right is None:
            continue
        try:
            left_f = float(left)
            right_f = float(right)
        except (TypeError, ValueError):
            continue
        pairs.append((left_f, right_f))
    if not pairs:
        return None

    # Discretize to integer bins so the confusion matrix is well-defined.
    lo = int(round(min_score))
    hi = int(round(max_score))
    if hi <= lo:
        raise ValueError("max_score must be greater than min_score")
    categories = list(range(lo, hi + 1))
    index = {value: i for i, value in enumerate(categories)}
    k = len(categories)
    matrix = [[0.0 for _ in categories] for _ in categories]
    for left_f, right_f in pairs:
        left_i = min(hi, max(lo, int(round(left_f))))
        right_i = min(hi, max(lo, int(round(right_f))))
        matrix[index[left_i]][index[right_i]] += 1.0

    n = sum(sum(row) for row in matrix)
    if n <= 0:
        return None
    hist_a = [sum(matrix[i][j] for j in range(k)) for i in range(k)]
    hist_b = [sum(matrix[i][j] for i in range(k)) for j in range(k)]
    denom = float((k - 1) ** 2)
    observed = 0.0
    expected = 0.0
    for i in range(k):
        for j in range(k):
            weight = ((i - j) ** 2) / denom
            observed += weight * matrix[i][j]
            expected += weight * hist_a[i] * hist_b[j] / n
    if abs(expected) < 1e-12:
        return 1.0 if abs(observed) < 1e-12 else 0.0
    return 1.0 - (observed / expected)


def agreement_block(
    scores_a: Iterable[Any | None],
    scores_b: Iterable[Any | None],
    bands_a: Iterable[Hashable | None] | None = None,
    bands_b: Iterable[Hashable | None] | None = None,
    *,
    reviewer_panel_kind: str = "same_model_replicate",
) -> dict[str, Any]:
    """Bundle raw rates + κ for a pilot/validation report section."""
    score_a = list(scores_a)
    score_b = list(scores_b)
    block: dict[str, Any] = {
        "reviewer_panel_kind": reviewer_panel_kind,
        "n_paired_scores": len(_pairs(score_a, score_b)),
        "exact_score_agreement_rate": observed_agreement(score_a, score_b),
        "cohen_kappa_exact_score": cohen_kappa(score_a, score_b),
        "quadratic_weighted_kappa": quadratic_weighted_kappa(score_a, score_b),
    }
    if bands_a is not None and bands_b is not None:
        band_a = list(bands_a)
        band_b = list(bands_b)
        block["band_agreement_rate"] = observed_agreement(band_a, band_b)
        block["cohen_kappa_band"] = cohen_kappa(band_a, band_b)
    return block


def imbalance_warning(labels: Sequence[Hashable | None]) -> dict[str, Any] | None:
    """Flag κ-paradox risk when one category dominates."""
    counts = Counter(label for label in labels if label is not None)
    total = sum(counts.values())
    if total == 0:
        return None
    top_label, top_n = counts.most_common(1)[0]
    share = top_n / total
    if share < 0.85:
        return None
    return {
        "code": "class_imbalance_kappa_paradox_risk",
        "dominant_label": top_label,
        "dominant_share": share,
        "message": (
            "One label dominates the sample; raw agreement may look high while "
            "κ is unstable. Report per-class precision/recall alongside κ."
        ),
    }
