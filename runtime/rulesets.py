#!/usr/bin/env python3
"""Strict parser for the one supported GitHub protection contract: rulesets."""
from __future__ import annotations


class RulesetError(RuntimeError):
    pass


def fetch_all_rulesets(fetch_page):
    """Fetch all repository rulesets so pagination cannot hide a match."""
    result = []
    page = 1
    while True:
        value = fetch_page(page)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise RulesetError("GitHub repository ruleset metadata is malformed")
        result.extend(value)
        if len(value) < 100:
            return result
        page += 1
        if page > 100:
            raise RulesetError("GitHub repository rulesets exceeded pagination bound")


def fetch_ruleset_details(summaries: object, fetch_detail):
    """Expand every paginated ruleset summary before contract validation."""
    if not isinstance(summaries, list) or any(not isinstance(item, dict) for item in summaries):
        raise RulesetError("GitHub repository ruleset summaries are malformed")
    details = []
    for summary in summaries:
        ruleset_id = summary.get("id")
        if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id < 1:
            raise RulesetError("GitHub repository ruleset identity is malformed")
        detail = fetch_detail(ruleset_id)
        if not isinstance(detail, dict):
            raise RulesetError("GitHub repository ruleset detail is malformed")
        details.append(detail)
    return details


def ruleset_applies(ruleset: dict[str, object], default_branch: str) -> bool:
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        return False
    conditions = ruleset.get("conditions")
    refs = conditions.get("ref_name") if isinstance(conditions, dict) else None
    include = refs.get("include") if isinstance(refs, dict) else None
    if not isinstance(include, list):
        return False
    return "~DEFAULT_BRANCH" in include or f"refs/heads/{default_branch}" in include


def require_default_branch_ruleset(rulesets: object, default_branch: str) -> dict[str, object]:
    if not isinstance(rulesets, list) or any(not isinstance(item, dict) for item in rulesets):
        raise RulesetError("GitHub repository ruleset metadata is malformed")
    applicable = [item for item in rulesets if ruleset_applies(item, default_branch)]
    if len(applicable) != 1:
        raise RulesetError("exactly one active ruleset must target the default branch")
    selected = applicable[0]
    bypass = selected.get("bypass_actors")
    if bypass != []:
        raise RulesetError("default-branch ruleset has bypass actors; automation bypass is not accepted")
    rules = selected.get("rules")
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise RulesetError("default-branch ruleset rules are malformed")
    pull_requests = [rule for rule in rules if rule.get("type") == "pull_request"]
    if len(pull_requests) != 1:
        raise RulesetError("default-branch ruleset must require pull requests")
    return selected
