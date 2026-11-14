"""CV file catalog and transparent initial role matching, without rewriting CVs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from batch_apply.models import CVChoice, TabSnapshot

_GENERIC = {"software", "engineer", "engineering", "developer", "general", "and", "cv"}
_ALIASES = {
    "sdet": {"qa", "automation"},
    "quality": {"qa"},
    "test": {"qa"},
    "testing": {"qa"},
    "ml": {"machine", "learning"},
    "genai": {"ai"},
    "llm": {"ai"},
    "junior": {"entry", "level"},
    "graduate": {"entry", "level"},
    "sre": {"devops"},
}


def _tokens(text: str) -> set[str]:
    values = set(re.findall(r"[a-z0-9+#]+", text.lower()))
    for key, aliases in _ALIASES.items():
        if key in values:
            values.update(aliases)
    return values


def _weight(tokens: set[str]) -> float:
    return sum(0.2 if token in _GENERIC else 1.0 for token in tokens)


def cv_catalog(directory: str | Path) -> list[Path]:
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The CV directory must be a directory")
    # Top-level files only: a caller must choose the intended CV collection.
    return sorted(
        (
            path.resolve()
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf" and path.resolve().parent == root
        ),
        key=lambda path: path.name.lower(),
    )


def _role(path: Path) -> str:
    name = re.sub(r"_CV$", "", path.stem, flags=re.IGNORECASE)
    return name.replace("_", " ")


def choose_cv_file(
    directory: str | Path,
    filename: str,
    *,
    source: str,
    reason: str,
    confidence: float = 1.0,
) -> CVChoice:
    root = Path(directory).resolve(strict=True)
    path = (root / filename).resolve(strict=True)
    if path.parent != root or path.suffix.lower() != ".pdf" or not path.is_file():
        raise ValueError("The selected CV must be a PDF inside the supplied CV directory")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ValueError("The selected CV does not have a PDF header")
    if not reason.strip():
        raise ValueError("A CV selection needs a reason")
    return CVChoice(
        file=path.name,
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        role=_role(path),
        source=source,
        reason=reason,
        confidence=confidence,
    )


def recommend_cv(tab: TabSnapshot, directory: str | Path) -> tuple[CVChoice | None, str]:
    """Use filename role evidence; abstain on weak or ambiguous initial matches.

    An LLM browser runner can replace this suggestion with an explicit selection
    after reading the job and CV. This function does not claim to read CV content.
    """
    title = _tokens(tab.title)
    description = _tokens(tab.description)
    distinctive = title - _GENERIC
    ranked: list[tuple[float, str, Path, set[str]]] = []
    for path in cv_catalog(directory):
        role = _tokens(_role(path))
        hits = title & role
        role_coverage = _weight(hits) / max(_weight(role), 0.1)
        job_coverage = _weight(distinctive & role) / max(_weight(distinctive), 1)
        description_coverage = _weight(description & role) / max(_weight(role), 0.1)
        score = 0.7 * role_coverage + 0.3 * job_coverage + 0.1 * description_coverage
        ranked.append((score, path.name, path, hits))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked or ranked[0][0] < 0.6:
        return None, "cv_selection_needed: insufficient role evidence"
    best = ranked[0]
    if len(ranked) > 1 and best[0] - ranked[1][0] < 0.04:
        return None, "cv_selection_needed: ambiguous role match"
    reason = "Filename role match: " + ", ".join(sorted(best[3]))
    return choose_cv_file(
        directory,
        best[2].name,
        source="filename_role_match",
        reason=reason,
        confidence=min(best[0], 1),
    ), reason


def verify_cv(choice: CVChoice) -> bool:
    path = Path(choice.path)
    return (
        path.is_file()
        and path.stat().st_size == choice.size_bytes
        and hashlib.sha256(path.read_bytes()).hexdigest() == choice.sha256
    )
