
import numpy as np


def analyze_thresholds(results):
    """
    results:
    [
        {
            "question": "...",
            "expected_behavior": "answer" veya "reject",
            "top_rerank_score": -2.5
        }
    ]
    """
    labeled = [
        item for item in results
        if item.get("expected_behavior") in {"answer", "reject"}
        and item.get("top_rerank_score") is not None
        and np.isfinite(float(item["top_rerank_score"]))
    ]

    if not labeled:
        return {"threshold": None, "reason": "no_labeled_scores"}

    positives = [
        float(item["top_rerank_score"])
        for item in labeled
        if item["expected_behavior"] == "answer"
    ]

    negatives = [
        float(item["top_rerank_score"])
        for item in labeled
        if item["expected_behavior"] == "reject"
    ]

    if not positives or not negatives:
        return {
            "threshold": None,
            "reason": "both_classes_required",
        }

    scores = sorted(set(positives + negatives))

    candidates = (
        [np.nextafter(scores[0], -np.inf)]
        + [(a + b) / 2 for a, b in zip(scores, scores[1:])]
        + [np.nextafter(scores[-1], np.inf)]
    )

    best = None

    for threshold in candidates:
        tp = sum(score >= threshold for score in positives)
        fn = len(positives) - tp
        tn = sum(score < threshold for score in negatives)
        fp = len(negatives) - tn

        sensitivity = tp / len(positives)
        specificity = tn / len(negatives)

        balanced_accuracy = (sensitivity + specificity) / 2

        # Eşit performansta yanlış kabulü daha az olanı seç.
        rank = (balanced_accuracy, -fp)

        if best is None or rank > best["rank"]:
            best = {
                "rank": rank,
                "threshold": float(threshold),
                "balanced_accuracy": round(balanced_accuracy, 4),
                "false_accepts": fp,
                "false_rejects": fn,
            }

    best.pop("rank")
    return best
