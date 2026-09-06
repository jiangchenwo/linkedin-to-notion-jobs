"""The search profile: the target-specific lists a fork edits in
data/keywords.toml, compiled once into regexes. Keeping them here (not hardcoded
in filters.py/extract.py) is what makes retargeting the scraper at a different
job title or field a config change rather than a code change."""

import re
import tomllib
from dataclasses import dataclass


def _alternation(terms: list[str]) -> re.Pattern:
    """Case-insensitive, word-boundary-guarded alternation over literal terms.
    An empty list yields a pattern that never matches, so emptying a section in
    the TOML disables that rule instead of matching everything."""
    body = "|".join(re.escape(t.strip()) for t in terms if t.strip())
    if not body:
        return re.compile(r"(?!)")
    return re.compile(rf"\b(?:{body})\b", re.I)


@dataclass(frozen=True)
class Profile:
    keywords: tuple[str, ...]
    search: dict
    senior_title_re: re.Pattern
    new_grad_title_re: re.Pattern
    relevance_re: re.Pattern
    core_skills: frozenset[str]
    areas: tuple[tuple[str, re.Pattern], ...]
    area_default: str

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
    with open(path, "rb") as f:
        data = tomllib.load(f)
    sen = data["seniority"]
    rel = data["relevance"]
    ar = data["areas"]
    return Profile(
        keywords=tuple(data["keywords"]),
        search=data["search"],
        senior_title_re=_alternation(sen["exclude_title_terms"]),
        new_grad_title_re=_alternation(sen["new_grad_title_terms"]),
        relevance_re=_alternation(rel["terms"]),
        core_skills=frozenset(rel["core_skills"]),
        areas=tuple((m["label"], re.compile(m["pattern"], re.I)) for m in ar["map"]),
        area_default=ar["default"],
    )
