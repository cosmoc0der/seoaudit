from __future__ import annotations

from seoaudit.core.models import CategoryResult, RuleResult, RuleScope, RuleStatus

_SCORED = (RuleStatus.PASSED, RuleStatus.FAILED)
_GRADES = ((90, "A"), (75, "B"), (60, "C"), (40, "D"), (0, "F"))


def rule_penalty(rule: RuleResult, page_count: int) -> float:
    """Failure costs at least 40% of the rule weight, the rest scales with prevalence."""
    if rule.status != RuleStatus.FAILED:
        return 0.0
    weight = rule.severity.weight
    if rule.scope == RuleScope.PAGE and page_count:
        ratio = min(1.0, rule.pages_affected / page_count)
        return weight * (0.4 + 0.6 * ratio)
    return float(weight)


def category_score(category: CategoryResult, page_count: int) -> int | None:
    scored = [r for r in category.rules if r.status in _SCORED and r.severity.weight > 0]
    total = sum(r.severity.weight for r in scored)
    if not total:
        return None
    penalty = sum(rule_penalty(r, page_count) for r in scored)
    return round(100 * max(0.0, 1 - penalty / total))


def overall_score(categories: list[CategoryResult]) -> int:
    rated = [(c.score, c.weight) for c in categories if c.score is not None]
    total_weight = sum(w for _, w in rated)
    return round(sum(s * w for s, w in rated) / total_weight) if total_weight else 0


def grade(score: int) -> str:
    return next(letter for threshold, letter in _GRADES if score >= threshold)
