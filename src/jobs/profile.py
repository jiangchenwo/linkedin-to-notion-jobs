"""The search profile: the target-specific config a fork edits in
data/keywords.toml, compiled once into regexes and lookup tables. Keeping it
here (not hardcoded in filters.py/extract.py) is what makes retargeting the
scraper at a different job title or field a config change, not a code change.

load() validates the file and raises ProfileError naming the offending section
and key; jobs.cli turns that into the JSON error contract, not a traceback."""

import re
import tomllib
from dataclasses import dataclass


class ProfileError(ValueError):
    """data/keywords.toml is missing a required section or key, or holds an
    invalid value. The message names the section and key so a fork can find its
    typo without a stack trace."""


# LinkedIn filter code sets (see the README code table). Comma-joined values are
# checked code by code.
_F_JT = set("FPCTIO")
_F_E = set("123456")
_F_WT = set("123")

# Every label seniority() can return; [priority] must score each one.
_SENIORITY_LABELS = frozenset(
    {"New Grad", "Internship", "Junior", "Entry Level", "Associate", "Senior", "Unknown"}
)


def _alternation(terms: list[str]) -> re.Pattern:
    """Case-insensitive, word-boundary-guarded alternation over literal terms.
    An empty list yields a pattern that never matches, so emptying a section in
    the TOML disables that rule instead of matching everything."""
    body = "|".join(re.escape(t.strip()) for t in terms if t.strip())
    if not body:
        return re.compile(r"(?!)")
    return re.compile(rf"\b(?:{body})\b", re.I)


def _compile(pattern: str, section: str, label: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.I)
    except re.error as e:
        raise ProfileError(f"[{section}] {label!r}: bad regex ({e})") from e


def _positive_int(value, section: str, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProfileError(f"[{section}] {key} must be a positive integer, got {value!r}")
    return value


def _require(data: dict, section: str) -> dict:
    if section not in data:
        raise ProfileError(f"[{section}] section is missing")
    return data[section]


def _req_key(d: dict, section: str, key: str):
    if key not in d:
        raise ProfileError(f"[{section}] {key} is required")
    return d[key]


def _check_codes(cfg: dict, key: str, allowed: set[str]) -> None:
    raw = cfg.get(key)
    if raw is None:
        return
    for code in str(raw).split(","):
        code = code.strip()
        if code and code not in allowed:
            raise ProfileError(
                f"[search] {key}={raw!r}: {code!r} is not one of {sorted(allowed)}"
            )


@dataclass(frozen=True)
class Profile:
    keywords: tuple[str, ...]
    search: dict
    exclude_title_re: re.Pattern
    long_window_title_re: re.Pattern
    max_age_days: int
    long_window_max_age_days: int
    employment_types: frozenset[str]
    exclude_description_re: re.Pattern
    max_min_years: int | None
    relevance_re: re.Pattern
    core_skills: frozenset[str]
    skills: tuple[tuple[str, re.Pattern], ...]
    areas: tuple[tuple[str, re.Pattern], ...]
    area_default: str
    priority: dict[str, int]

    def match_areas(self, title: str, description_text: str) -> list[str]:
        """Each area whose pattern hits the title or hits the description at
        least twice, in profile order, capped at 3; [area_default] when nothing
        matched."""
        title = title or ""
        description_text = description_text or ""
        out: list[str] = []
        for label, pat in self.areas:
            if pat.search(title) or len(pat.findall(description_text)) >= 2:
                out.append(label)
                if len(out) == 3:
                    break
        return out or [self.area_default]


def load(path: str = "data/keywords.toml") -> Profile:
    """Compile the profile at `path`. Raises ProfileError (naming the section
    and key) on a missing section, an empty keyword list, an uncompilable regex,
    a core skill not in [skills], a non-positive count, an out-of-set LinkedIn
    code, or a [priority] table that does not score every seniority label."""
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"{path}: invalid TOML ({e})") from e

    keywords = data.get("keywords")
    if not keywords:
        raise ProfileError("keywords must be a non-empty list")

    search = _require(data, "search")
    for key, allowed in (("f_JT", _F_JT), ("f_E", _F_E), ("f_WT", _F_WT)):
        _check_codes(search, key, allowed)
    if "max_pages" in search:
        _positive_int(search["max_pages"], "search", "max_pages")

    filt = _require(data, "filters")
    rel = _require(data, "relevance")
    sk = _require(data, "skills")
    ar = _require(data, "areas")
    pr = _require(data, "priority")

    skills = tuple((name, _compile(pat, "skills", name)) for name, pat in sk.items())
    skill_names = {name for name, _ in skills}
    for name in _req_key(rel, "relevance", "core_skills"):
        if name not in skill_names:
            raise ProfileError(f"[relevance] core_skills: {name!r} is not a key in [skills]")

    if "map" not in ar:
        raise ProfileError("[areas] needs at least one [[areas.map]] entry")
    areas = tuple(
        (m["label"], _compile(m["pattern"], "areas", m.get("label", "?"))) for m in ar["map"]
    )

    missing = _SENIORITY_LABELS - set(pr)
    if missing:
        raise ProfileError(f"[priority] is missing labels: {sorted(missing)}")
    for label, score in pr.items():
        if not isinstance(score, int) or isinstance(score, bool):
            raise ProfileError(f"[priority] {label!r} must be an integer, got {score!r}")

    max_min = filt.get("max_min_years")
    if max_min is not None:
        _positive_int(max_min, "filters", "max_min_years")

    return Profile(
        keywords=tuple(keywords),
        search=search,
        exclude_title_re=_alternation(_req_key(filt, "filters", "exclude_title_terms")),
        long_window_title_re=_alternation(filt.get("long_window_title_terms", [])),
        max_age_days=_positive_int(filt.get("max_age_days", 2), "filters", "max_age_days"),
        long_window_max_age_days=_positive_int(
            filt.get("long_window_max_age_days", 14), "filters", "long_window_max_age_days"
        ),
        employment_types=frozenset(filt.get("employment_types", [])),
        exclude_description_re=_alternation(filt.get("exclude_description_terms", [])),
        max_min_years=max_min,
        relevance_re=_alternation(_req_key(rel, "relevance", "terms")),
        core_skills=frozenset(rel["core_skills"]),
        skills=skills,
        areas=areas,
        area_default=_req_key(ar, "areas", "default"),
        priority=dict(pr),
    )
