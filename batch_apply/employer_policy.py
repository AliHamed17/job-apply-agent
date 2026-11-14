"""Persistent employer exclusions evaluated only against employer identities and URLs."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

DEFAULT_POLICY_PATH = Path("runtime-data/employer-policy.json")
_TENANT_HOSTS = {
    "jobs.lever.co",
    "jobs.eu.lever.co",
    "apply.workable.com",
    "boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "careers.smartrecruiters.com",
}


class EmployerPolicyError(ValueError):
    """The configured employer policy disallows the requested application."""


def _name(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def _domain(value: str) -> str:
    domain = value.strip().strip(".").casefold().encode("idna").decode("ascii")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain) or "." not in domain:
        raise ValueError("Policy domains must be hostnames without a scheme, path or port")
    if any(
        not label or label.startswith("-") or label.endswith("-") for label in domain.split(".")
    ):
        raise ValueError("Invalid policy domain")
    return domain


def _strings(rule: dict, field: str) -> list[str]:
    values = rule.get(field, [])
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError(f"Policy {field} must be a list of nonempty strings")
    return values


def _read_policy(path: Path) -> list[dict] | None:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    policy = json.loads(raw)
    if not isinstance(policy, dict) or set(policy) != {"schema_version", "exclusions"}:
        raise ValueError("Policy requires schema_version and exclusions")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("Unsupported employer policy version")
    if not isinstance(policy["exclusions"], list):
        raise ValueError("Policy exclusions must be a list")
    ids = set()
    rules = []
    for rule in policy["exclusions"]:
        if not isinstance(rule, dict) or set(rule) - {
            "id",
            "employer",
            "aliases",
            "domains",
            "reason",
            "source",
        }:
            raise ValueError("Invalid employer exclusion rule")
        for field in ("id", "employer", "reason"):
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise ValueError(f"Policy rule requires {field}")
        if rule["id"] in ids or not _name(rule["employer"]):
            raise ValueError("Policy rule identity must be nonempty and unique")
        source = rule.get("source")
        if not isinstance(source, dict) or not all(
            isinstance(source.get(field), str) and source[field].strip()
            for field in ("type", "text")
        ):
            raise ValueError("Policy rules require explicit source type and text")
        aliases = [rule["employer"], *_strings(rule, "aliases")]
        if any(not _name(alias) for alias in aliases):
            raise ValueError("Policy aliases must contain name characters")
        rules.append(
            {
                **rule,
                "names": {_name(v) for v in aliases},
                "domains": [_domain(v) for v in _strings(rule, "domains")],
            }
        )
        ids.add(rule["id"])
    return rules


def evaluate_employer(
    employer: str = "",
    urls: Iterable[str] = (),
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> dict:
    """Return a fresh decision; absent configuration allows, invalid configuration denies.

    Inputs must be actual employer labels or job/application URLs. Descriptions,
    CV content, filenames and filesystem paths are deliberately never inspected.
    Shared ATS hosts are matched only on their employer tenant segment.
    """
    path = Path(policy_path)
    result = {
        "allowed": True,
        "reason": "No employer exclusion matched.",
        "matched_rule": None,
        "policy_path": str(path.resolve()),
        "policy_present": True,
    }
    try:
        rules = _read_policy(path)
    except (OSError, ValueError, UnicodeError, TypeError):
        return {
            **result,
            "allowed": False,
            "reason": "employer_policy_invalid: existing policy cannot be validated",
        }
    if rules is None:
        return {**result, "policy_present": False, "reason": "No employer policy configured."}
    name = _name(employer)
    identities = []
    for url in [urls] if isinstance(urls, str) else urls:
        try:
            parsed = urlsplit(url)
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                continue
            host = parsed.hostname.rstrip(".").casefold().encode("idna").decode("ascii")
            parts = [unquote(p) for p in parsed.path.split("/") if p]
            tenant = parts[0] if host in _TENANT_HOSTS and parts else ""
            if host in {"comeet.com", "www.comeet.com", "comeet.co", "www.comeet.co"}:
                tenant = parts[1] if len(parts) > 1 and parts[0] == "jobs" else ""
            identities.append((host, _name(tenant).replace(" ", "")))
        except (ValueError, UnicodeError):
            continue
    for rule in rules:
        matched = bool(name and name in rule["names"])
        for host, tenant in identities:
            matched = (
                matched
                or any(host == domain or host.endswith("." + domain) for domain in rule["domains"])
                or bool(tenant and tenant in {n.replace(" ", "") for n in rule["names"]})
            )
        if matched:
            return {
                **result,
                "allowed": False,
                "matched_rule": rule["id"],
                "reason": f"employer_excluded: {rule['employer']}: {rule['reason']}",
            }
    return result


def require_employer_allowed(
    employer: str = "",
    urls: Iterable[str] = (),
    *,
    policy_path: str | Path = DEFAULT_POLICY_PATH,
) -> dict:
    decision = evaluate_employer(employer, urls, policy_path=policy_path)
    if not decision["allowed"]:
        raise EmployerPolicyError(decision["reason"])
    return decision
