"""Rank the missing facts that blocked or skipped jobs in a browser-batch ledger.

Read-only and offline: no network, browser, LLM or ledger writes. It separates what
the user can fix by confirming a fact (``experience``, ``fact``, ``consent``) from what
no answer fixes (``legal``, ``fit``, ``obstacle``). Only ``experience.<skill>_years``
is accepted by ``batch_apply.answer_cache`` today; every other kind needs a separate
policy decision before automation may answer it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from batch_apply.models import BatchJob

STUCK = {"blocked", "skipped"}
FIXABLE_KINDS = {"experience", "fact", "consent"}
_BLOCKER_KEYS = (
    "reason",
    "question",
    "required_question",
    "required_unknowns",
    "required_unknown_fields",
    "required_field",
    "label",
)
_MAX_TEXT = 600


@dataclass(frozen=True)
class Gap:
    key: str
    kind: str
    title: str
    pattern: re.Pattern[str]


def _gap(key: str, kind: str, title: str, pattern: str) -> Gap:
    return Gap(key, kind, title, re.compile(pattern, re.I))


GAPS: tuple[Gap, ...] = (
    _gap(
        "availability.notice_or_start",
        "fact",
        "Notice period or earliest start date",
        r"notice[- ]period|notice (of|time)|start[- ]date|earliest start|available to start"
        r"|availability (date|period)|when can you start|הודעה מוקדמת|תאריך התחלה",
    ),
    _gap(
        "address.postal_code",
        "fact",
        "Postal code or street address",
        r"postal code|zip code|street address|מיקוד",
    ),
    _gap(
        "workplace.hybrid_onsite_or_commute",
        "fact",
        "Hybrid, on-site, commute or relocation",
        r"hybrid|on-?site|in-?office|\boffice\b|commut|relocat|היברידי|משרד",
    ),
    _gap(
        "salary.expectation",
        "fact",
        "Salary expectation",
        r"salary|compensation|pay expectation|expected pay|שכר",
    ),
    _gap(
        "travel.commitment",
        "fact",
        "Travel commitment",
        r"\btravel|overseas|business trips?|נסיעות",
    ),
    _gap(
        "certifications.training",
        "fact",
        "Certifications, licences or training courses",
        r"certif|licen[cs]e|training course|official .{0,20}courses?|\bcourses?\b|istqb|הסמכה",
    ),
    _gap(
        "consent.terms_or_privacy",
        "consent",
        "Terms, privacy or marketing consent checkbox",
        r"consent|terms (of (use|service)|and conditions)|privacy (policy|notice)|marketing"
        r"|newsletter|gdpr|opt-?in|תנאי שימוש|הסכמה",
    ),
    _gap(
        "legal.id_clearance_or_status",
        "legal",
        "National ID, clearance, military service or work authorisation",
        r"national id|id number|identity number|teudat|clearance|security check|military"
        r"|\bidf\b|citizenship|work (permit|authori[sz]ation)|\bvisa\b|sponsor"
        r"|מספר זהות|סיווג|שירות צבאי",
    ),
    _gap(
        "fit.education_requirement",
        "fit",
        "Degree or student-status requirement",
        r"\bdegree\b|b\.?sc|bachelor|\bmaster|m\.?sc|\bstudent|\bgpa\b|תואר|סטודנט",
    ),
    _gap(
        "fit.role_or_seniority",
        "fit",
        "Role or seniority outside the profile",
        r"\bsenior\b|\blead\b|\bmanager\b|principal|not (a )?(good )?(fit|match)"
        r"|off[- ]profile|\bsales\b|soc analyst|security operations|narrowed scope"
        r"|deprioriti[sz]ed",
    ),
    _gap(
        "obstacle.captcha",
        "obstacle",
        "CAPTCHA or human check",
        r"captcha|human[- ](check|verification)|\brobot\b|cloudflare|turnstile"
        r"|bot (check|detection)|session[- ]verification"
        r"|verification (was )?(explicitly )?rejected",
    ),
    _gap(
        "obstacle.account_or_login",
        "obstacle",
        "Account, login or verification code required",
        r"create an? account|account (is )?required|sign[- ]?in|log[- ]?in|password"
        r"|registration|\bregister\b|verification code|\botp\b|credential|security[- ]code"
        r"|email verification|account limit|limit of ?\d+ applications",
    ),
    _gap(
        "obstacle.closed_or_duplicate",
        "obstacle",
        "Closed posting or duplicate application",
        r"\bclosed\b|no longer (available|accepting|lists?)|expired|position (was )?filled"
        r"|duplicate|already applied|prior application|\babsent\b|not (listed|open)"
        r"|redirects? to (the |a |an |current )?.{0,30}(board|openings|list)",
    ),
    _gap(
        "obstacle.page_or_upload_failure",
        "obstacle",
        "Page, form or file-upload failure in the browser",
        r"failed to load|\berr_[a-z_]+|file chooser|upload (control|failed|is unsupported)"
        r"|embedded (form|resume|application)|blank|empty (embedded )?(application )?area"
        r"|did not (open|load|enable)",
    ),
)
_BY_KEY = {gap.key: gap for gap in GAPS}
# Last resort for a text that names a requirement but nothing more specific.
_REQUIREMENT = re.compile(
    r"\brequires?\b|\brequirements?\b|proficiency|centers on|not represented|portfolio contains"
    r"|not established|exceed",
    re.I,
)

# (slug, display name, pattern). Slugs satisfy answer_cache's experience key format.
SKILLS: tuple[tuple[str, str, str], ...] = (
    ("python", "Python", r"\bpython\b"),
    ("java", "Java", r"\bjava\b(?!script)"),
    ("cpp", "C++", r"c\+\+"),
    ("csharp", "C#", r"c#|\bcsharp\b"),
    ("dotnet", ".NET", r"\.net\b|\bdotnet\b"),
    ("sql", "SQL", r"\bsql\b"),
    ("xml", "XML", r"\bxml\b"),
    ("ssis", "SSIS", r"\bssis\b"),
    ("etl", "ETL", r"\betl\b"),
    ("aws", "AWS", r"\baws\b"),
    ("azure", "Azure", r"\bazure\b"),
    ("gcp", "GCP", r"\bgcp\b|google cloud"),
    ("docker", "Docker", r"\bdocker\b"),
    ("kubernetes", "Kubernetes", r"kubernetes|\bk8s\b"),
    ("jenkins", "Jenkins", r"\bjenkins\b"),
    ("ci_cd", "CI/CD", r"ci\s*/\s*cd|\bci cd\b"),
    ("linux", "Linux", r"\blinux\b"),
    ("bash", "Bash", r"\bbash\b|shell script"),
    ("git", "Git", r"\bgit\b"),
    ("selenium", "Selenium", r"\bselenium\b"),
    ("playwright", "Playwright", r"\bplaywright\b"),
    ("cypress", "Cypress", r"\bcypress\b"),
    ("api_testing", "API testing", r"api test|rest api|postman"),
    ("manual_testing", "manual testing", r"manual (qa|test)"),
    ("test_automation", "test automation", r"(test|qa) automation|automation (test|qa)"),
    ("qa", "QA", r"\bqa\b|quality assurance"),
    ("javascript", "JavaScript", r"javascript|\bjs\b"),
    ("typescript", "TypeScript", r"typescript"),
    ("react", "React", r"\breact\b"),
    ("angular", "Angular", r"\bangular\b"),
    ("nodejs", "Node.js", r"node\.?js"),
    ("postgres", "PostgreSQL", r"postgres"),
    ("mysql", "MySQL", r"\bmysql\b"),
    ("mongodb", "MongoDB", r"mongo"),
    ("power_bi", "Power BI", r"power\s?bi"),
    ("tableau", "Tableau", r"\btableau\b"),
    ("excel", "Excel", r"\bexcel\b"),
    ("ml", "machine learning", r"machine learning|\bml\b"),
    ("deep_learning", "deep learning", r"deep learning"),
    ("computer_vision", "computer vision", r"computer vision"),
    ("nlp", "NLP", r"\bnlp\b"),
    ("genai", "generative AI", r"genai|generative ai|\bllm\b|lora"),
    ("pytorch", "PyTorch", r"pytorch"),
    ("tensorflow", "TensorFlow", r"tensorflow"),
    ("embedded", "embedded software", r"embedded"),
    ("networking", "networking", r"networking|\btcp\b|\bip\b"),
    ("agile", "Agile", r"\bagile\b|\bscrum\b|waterfall"),
)
_SKILL_RX = tuple((slug, name, re.compile(rx, re.I)) for slug, name, rx in SKILLS)
_DISPLAY = {slug: name for slug, name, _ in SKILLS}
# Wording that says the answer is simply not on file, as opposed to a requirement
# the user does not meet. Only the former can be fixed by confirming a number.
_EXPERIENCE_WORDS = re.compile(
    r"\byears?\b|experience (band|duration|level|length)|\bduration|\btenure|\bbands?\b", re.I
)
_NOT_ON_FILE = re.compile(
    r"not (verified|established|supported|documented|evidenced|confirmed|provided|available)"
    r"|unknown|unsupported|unverified|no verified|exact|required .{0,40}answer",
    re.I,
)


# A stated minimum means the requirement itself is unmet; confirming a number cannot fix it.
_MINIMUM = re.compile(
    r"\d\s*\+?\s*years|\b(one|two|three|four|five|six|seven|eight|ten)\+?\s+years"
    r"|at least|minimum|below|exceeds?|fewer than|short of|significant prior",
    re.I,
)


def experience_key(slug: str) -> str:
    return f"experience.{slug}_years"


def experience_scope(slug: str) -> str:
    """The exact scope string stored with, and later looked up for, an experience fact."""
    return f"years of professional experience with {_DISPLAY.get(slug, slug)}"


def skill_display(slug: str) -> str:
    return _DISPLAY.get(slug, slug)


@dataclass(frozen=True)
class GapJob:
    job_id: str
    company: str
    status: str
    host: str
    gaps: frozenset[str]


@dataclass(frozen=True)
class GapRow:
    key: str
    kind: str
    title: str
    jobs: int
    sole: int
    companies: tuple[str, ...]


def host_family(url: str) -> str:
    host = (urlsplit(url).hostname or "unknown").lower().removeprefix("www.")
    for suffix, family in (
        ("comeet.com", "comeet"),
        ("comeet.co", "comeet"),
        ("linkedin.com", "linkedin"),
        ("greenhouse.io", "greenhouse"),
        ("lever.co", "lever"),
        ("ashbyhq.com", "ashby"),
        ("smartrecruiters.com", "smartrecruiters"),
        ("myworkdayjobs.com", "workday"),
        ("indeed.com", "indeed"),
    ):
        if host == suffix or host.endswith("." + suffix):
            return family
    return host


def classify_text(text: str) -> set[str]:
    """Gap keys named by one blocker text; empty when nothing recognisable is said."""
    found = {gap.key for gap in GAPS if gap.pattern.search(text)}
    if _EXPERIENCE_WORDS.search(text):
        skills = [slug for slug, _, rx in _SKILL_RX if rx.search(text)]
        if _NOT_ON_FILE.search(text) and not _MINIMUM.search(text):
            found.update(experience_key(slug) for slug in skills or ["unspecified"])
        elif skills or _MINIMUM.search(text):
            found.add("fit.experience_requirement")
    if not found and _REQUIREMENT.search(text):
        found.add("fit.requirements_not_met")
    return found


def kind_of(key: str) -> str:
    if key == experience_key("unspecified"):
        return "fit"  # no technology named, so there is nothing the user can answer
    if key.startswith("experience."):
        return "experience"
    return _BY_KEY[key].kind if key in _BY_KEY else "fit"  # fit.* fallbacks


def title_of(key: str) -> str:
    if key in _BY_KEY:
        return _BY_KEY[key].title
    if key.startswith("experience."):
        slug = key.removeprefix("experience.").removesuffix("_years")
        return "Years of experience with " + (
            "an unspecified area (needs a manual look)"
            if slug == "unspecified"
            else skill_display(slug)
        )
    if key == "fit.experience_requirement":
        return "Stated experience minimum not met"
    return "Other stated requirement not met"


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def mine_ledger(path: str | Path) -> list[GapJob]:
    """One GapJob per ledger job; ``gaps`` is filled only for blocked or skipped jobs."""
    database = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        jobs = [
            BatchJob.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT payload FROM browser_batch_jobs ORDER BY canonical_url"
            )
        ]
        has_activity = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='browser_batch_activity'"
        ).fetchone()
        events = (
            [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload FROM browser_batch_activity "
                    "WHERE event_type='blocker' ORDER BY sequence"
                )
            ]
            if has_activity
            else []
        )
    texts: dict[str, list[str]] = defaultdict(list)
    for event in events:
        details = event.get("details") or {}
        key = event.get("job_id") or event.get("job_url") or ""
        for name in _BLOCKER_KEYS:
            texts[key].extend(s[:_MAX_TEXT] for s in _strings(details.get(name)))
    out: list[GapJob] = []
    for job in jobs:
        status = job.status.value
        gaps: set[str] = set()
        if status in STUCK:
            current = [job.reason, job.latest_evidence.text if job.latest_evidence else ""]
            gaps = classify_text(" ".join(p[:_MAX_TEXT] for p in current))
            if not gaps:  # blocker history only when the current reason says nothing usable
                history = texts.get(job.job_id, []) + texts.get(job.canonical_url, [])
                gaps = classify_text(" ".join(p[:_MAX_TEXT] for p in history))
        out.append(
            GapJob(job.job_id, job.company, status, host_family(job.canonical_url), frozenset(gaps))
        )
    return out


def rank(jobs: Iterable[GapJob]) -> list[GapRow]:
    """Gaps ordered by jobs affected; ``sole`` counts jobs this gap alone is holding back."""
    stuck = [job for job in jobs if job.status in STUCK and job.gaps]
    counts: Counter[str] = Counter()
    sole: Counter[str] = Counter()
    companies: dict[str, list[str]] = defaultdict(list)
    for job in stuck:
        for key in job.gaps:
            counts[key] += 1
            if job.company and job.company not in companies[key]:
                companies[key].append(job.company)
        if len(job.gaps) == 1:
            sole[next(iter(job.gaps))] += 1
    rows = [
        GapRow(
            key,
            kind_of(key),
            title_of(key),
            counts[key],
            sole[key],
            tuple(sorted(companies[key])[:5]),
        )
        for key in counts
    ]
    return sorted(rows, key=lambda row: (-row.jobs, row.key))


def unlock_estimate(jobs: Iterable[GapJob], answered: set[str]) -> int:
    """Stuck jobs whose every gap is user-fixable and answered; others stay stuck."""
    return sum(
        1
        for job in jobs
        if job.status in STUCK
        and job.gaps
        and all(kind_of(key) in FIXABLE_KINDS and key in answered for key in job.gaps)
    )


def unclassified(jobs: Iterable[GapJob]) -> int:
    return sum(1 for job in jobs if job.status in STUCK and not job.gaps)


def domain_outcomes(jobs: Iterable[GapJob]) -> list[dict[str, object]]:
    """Per host family: how jobs ended, so the queue can favour sources that complete."""
    table: dict[str, Counter[str]] = defaultdict(Counter)
    for job in jobs:
        row = table[job.host]
        row["total"] += 1
        if job.status == "submitted":
            row["submitted"] += 1
        elif "obstacle.captcha" in job.gaps:
            row["captcha"] += 1
        elif "obstacle.account_or_login" in job.gaps:
            row["account"] += 1
        elif (
            job.status in STUCK
            and job.gaps
            and all(kind_of(key) in FIXABLE_KINDS for key in job.gaps)
        ):
            row["fixable_by_facts"] += 1
        elif job.status in STUCK:
            row["other_stuck"] += 1
        else:
            row["other"] += 1
    return sorted(
        ({"host": host, **dict(row)} for host, row in table.items()),
        key=lambda item: (-int(item["total"]), str(item["host"])),  # type: ignore[call-overload]
    )
