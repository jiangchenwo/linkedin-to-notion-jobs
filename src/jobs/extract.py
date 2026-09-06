"""Field extraction: one function per Notion field, plus canon()/sibling_key()
and build_job() that assembles a Job from a sibling group and its Detail.

The heading regexes, SKILL_PATTERNS, source_quality logic, work_mode, and h1b
phrase lists are ported from the old repo (analyze_linkedin_jobs.py and
notion_job_sync.py). The minimum-years parser is the reworked table from the
Phase 2 plan, not the old multi-signal version."""

import re

from .models import Card, Detail, Job

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


def canon(name: str) -> str:
    """Lowercase, drop everything except letters, digits, and spaces, and
    collapse whitespace. Used for company/title matching and sibling keys."""
    s = _NON_ALNUM.sub("", (name or "").lower())
    return _WS.sub(" ", s).strip()


def sibling_key(company: str, title: str) -> str:
    """Group key for postings of the same role across cities."""
    return f"{canon(company)}::{canon(title)}"


# Ported verbatim from analyze_linkedin_jobs.py:235-269.
SKILL_PATTERNS = {
    "Python": r"\bpython\b",
    "SQL": r"\bsql\b|postgres|mysql|database",
    "JavaScript/TypeScript": r"\btypescript\b|\bjavascript\b|\bnode\.?js\b|\breact\b",
    "Java/JVM": r"\bjava\b|\bspring\b",
    "Go": r"\bgolang\b|\bgo\b",
    "C++": r"\bc\+\+\b",
    "REST/API/backend": r"\bapi\b|\brest\b|\bbackend\b|microservice",
    "Cloud, any provider": r"\baws\b|\bazure\b|\bgcp\b|google cloud|cloud-native|cloud infrastructure",
    "AWS": r"\baws\b|bedrock|sagemaker|lambda|amazon s3|\bs3\b|ecs\b|eks\b",
    "Azure": r"\bazure\b|azure ai|azure openai",
    "GCP": r"\bgcp\b|google cloud|vertex ai",
    "Docker/containers": r"\bdocker\b|container",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b|\beks\b|\baks\b|\bgke\b",
    "CI/CD": r"\bci/cd\b|github actions|jenkins|deployment pipeline",
    "Terraform/IaC": r"\bterraform\b|infrastructure as code|\biac\b|cloudformation|cdk\b",
    "Data pipelines": r"data pipeline|etl\b|elt\b|airflow|spark|databricks|snowflake|warehouse",
    "Kafka/streaming": r"\bkafka\b|streaming|kinesis|eventbridge",
    "LLM/GenAI": r"\bllm\b|large language model|generative ai|genai|foundation model",
    "RAG/retrieval": r"\brag\b|retrieval|knowledge base|semantic search",
    "Embeddings/vector DB": r"embedding|vector database|vector db|pinecone|weaviate|milvus|faiss|chromadb",
    "Agents/workflows": r"\bagent\b|agentic|multi-agent|workflow orchestration",
    "Prompt engineering": r"prompt engineering|prompt design|prompt",
    "Evals/testing": r"\beval\b|evaluation|benchmark|test harness|quality measurement",
    "OpenAI/Anthropic": r"\bopenai\b|anthropic|claude",
    "LangChain/LangGraph": r"langchain|langgraph|llamaindex",
    "PyTorch": r"pytorch",
    "TensorFlow": r"tensorflow",
    "scikit-learn": r"scikit|sklearn|scikit-learn",
    "NLP": r"\bnlp\b|natural language",
    "Computer vision/OCR": r"computer vision|ocr\b|image|vision model",
    "Model serving/MLOps": r"mlops|model serving|model deployment|inference|feature store|monitoring",
    "Observability/reliability": r"observability|monitoring|logging|alerting|reliability|incident",
    "Security/responsible AI": r"cybersecurity|data security|privacy|responsible ai|ai safety|abuse|red team|governance|secure ai",
}


# --- qualifications sections (ported from analyze_linkedin_jobs.py:194-386) ---

MINIMUM_HEADING_RE = re.compile(
    r"\b(?:"
    r"minimum qualifications?\.{0,3}|basic qualifications?|required qualifications?|"
    r"additional required qualifications?|minimum requirements?|basic requirements?|"
    r"required skills?(?: and experience)?|required skill and experience|"
    r"required experience|required qualification|requirements added by the job poster|"
    r"what you must have|what you(?:'|’)ll need|what we(?:'|’)re looking for|"
    r"skills, experience, and qualifications|"
    r"skills and qualifications|qualifications required"
    r")\b",
    re.I,
)
PREFERRED_HEADING_RE = re.compile(
    r"\b(?:"
    r"preferred qualifications?\.{0,3}|preferred requirements?|preferred skills?|"
    r"preferred skill and experience|preferred experience|desired qualifications?|"
    r"desired skills?|nice to have|nice-to-have|bonus qualifications?|bonus points?|"
    r"what sets you apart|signal of excellence|strongly preferred"
    r")\b",
    re.I,
)
GENERIC_MINIMUM_LINE_RE = re.compile(
    r"^(?:"
    r"qualifications?|requirements?|required|basic|"
    r"you have|you bring|what you have|what you bring|"
    r"skills,? experience,? and qualifications"
    r")$",
    re.I,
)
OTHER_SECTION_LINE_RE = re.compile(
    r"^(?:"
    r"responsibilities|key responsibilities|what you(?:'|’)ll do|what you will do|"
    r"what you'll be doing|what you(?:'|’)ll get|benefits|summary of benefits|"
    r"compensation|salary|pay range|additional notes|physical requirements|"
    r"how can i join|interview process|travel requirements|equal opportunity|"
    r"about us|company description"
    r")$",
    re.I,
)


def _clean_heading_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip(" \t:-•*#.")


def _line_start_offsets(text: str) -> list[tuple[int, str]]:
    offsets: list[tuple[int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        offsets.append((cursor, line.rstrip("\r\n")))
        cursor += len(line)
    return offsets


def _section_markers(text: str) -> list[tuple[int, int, str]]:
    markers: list[tuple[int, int, str]] = []
    for pattern, kind in (
        (MINIMUM_HEADING_RE, "minimum"),
        (PREFERRED_HEADING_RE, "preferred"),
    ):
        for match in pattern.finditer(text):
            markers.append((match.start(), match.end(), kind))

    for offset, line in _line_start_offsets(text):
        clean = _clean_heading_line(line)
        if not clean or len(clean) > 120:
            continue
        leading = len(line) - len(line.lstrip())
        start = offset + leading
        end = start + len(line.strip())
        if GENERIC_MINIMUM_LINE_RE.fullmatch(clean):
            markers.append((start, end, "minimum"))
        elif OTHER_SECTION_LINE_RE.fullmatch(clean):
            markers.append((start, end, "other"))

    markers.sort(key=lambda item: (item[0], item[1] - item[0]))
    deduped: list[tuple[int, int, str]] = []
    for start, end, kind in markers:
        if deduped and start < deduped[-1][1]:
            prev_start, prev_end, prev_kind = deduped[-1]
            if kind != "other" and prev_kind == "other":
                deduped[-1] = (start, end, kind)
            elif end > prev_end and kind == prev_kind:
                deduped[-1] = (prev_start, end, kind)
            continue
        deduped.append((start, end, kind))
    return deduped


def _requirement_sections(text: str) -> dict[str, str]:
    markers = _section_markers(text)
    if not markers:
        return {"minimum": text.strip(), "preferred": ""}

    sections: dict[str, list[str]] = {"minimum": [], "preferred": []}
    for index, (start, _, kind) in enumerate(markers):
        if kind not in sections:
            continue
        end = markers[index + 1][0] if index + 1 < len(markers) else len(text)
        section = text[start:end].strip()
        if section:
            sections[kind].append(section)

    return {
        "minimum": "\n\n".join(sections["minimum"]).strip(),
        "preferred": "\n\n".join(sections["preferred"]).strip(),
    }


def _compact_requirement_text(text: str, *, limit: int = 1800) -> str:
    lines = []
    junk_re = re.compile(
        r"based on linkedin data|show more|apply|save|tailor my resume|"
        r"create cover letter|company insights",
        re.I,
    )
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip(" \t-•*")
        if not clean or junk_re.search(clean):
            continue
        lines.append(clean)
    compact = " ".join(lines)
    return compact[:limit].rstrip()


def qualifications(detail: Detail) -> tuple[str, str]:
    """Split the description into (minimum, preferred) qualification text, each
    compacted and capped at 1800 chars. Either is "" when the section is absent."""
    secs = _requirement_sections(detail.description_text)
    return (
        _compact_requirement_text(secs["minimum"]),
        _compact_requirement_text(secs["preferred"]),
    )


# --- minimum years (Phase 2 plan table, widened per plan step 18 to hit the
# 80% parse target: written numbers, "(N)" repeats, and "or more") ---

_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_NUM = r"(?:\d{1,2}|" + "|".join(_WORD_NUM) + r")"
_YEARS = r"(?:years?|yrs?)"
# "+" or "or more"/"or greater" both mean an open-ended lower bound.
_PLUS_WORDS = r"(?:\+|or\s+more|or\s+greater|or\s+above)"
# A parenthesised digit repeat: "Five (5) years".
_PAREN = r"(?:\s*\(\d{1,2}\))?"
_QUALIFIER = (
    r"(?:professional|industry|relevant|hands[- ]on|work|working|software|"
    r"engineering|programming|coding|development|developing|building|experience)"
)

_DEGREE = r"(?<![A-Za-z])(BS|BA|B\.S\.|Bachelor'?s?|MS|M\.S\.|Master'?s?|PhD|Ph\.D\.)"
_P_DEGREE_YEARS = re.compile(
    _DEGREE + r".{0,80}?(" + _NUM + r")" + _PAREN + r"\s*\+?\s*" + _YEARS,
    re.I | re.S,
)
_P_RANGE = re.compile(
    r"(" + _NUM + r")\s*(?:-|–|to)\s*(" + _NUM + r")" + _PAREN + r"\s*[)\s]*\+?\s*" + _YEARS,
    re.I,
)
_P_AT_LEAST = re.compile(
    r"(?:at least|minimum of|minimum|min\.?)\s*(" + _NUM + r")" + _PAREN
    + r"\s*" + _PLUS_WORDS + r"?\s*" + _YEARS,
    re.I,
)
_P_PLUS = re.compile(r"(" + _NUM + r")" + _PAREN + r"\s*" + _PLUS_WORDS + r"\s*" + _YEARS, re.I)
_P_QUALIFIED = re.compile(
    r"(" + _NUM + r")" + _PAREN + r"\s*" + _PLUS_WORDS + r"?\s*" + _YEARS
    + r"['’]?\s*(?:of\s+)?" + _QUALIFIER,
    re.I,
)


def _num(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else _WORD_NUM[token]


def _norm_degree(value: str) -> str:
    x = re.sub(r"[^a-z]", "", value.lower())
    if x.startswith("phd") or x.startswith("doctor"):
        return "PhD"
    if x.startswith("m"):
        return "MS"
    return "BS"


def _scan_years(text: str) -> tuple[str, int | None]:
    for m in _P_DEGREE_YEARS.finditer(text):
        n = _num(m.group(2))
        if n > 20:
            continue
        return f"{_norm_degree(m.group(1))}+{n}", n
    for m in _P_RANGE.finditer(text):
        n = _num(m.group(1))
        if n > 20:
            continue
        return f"{n}-{_num(m.group(2))}", n
    for m in _P_AT_LEAST.finditer(text):
        n = _num(m.group(1))
        if n > 20:
            continue
        return f"{n}+", n
    for m in _P_PLUS.finditer(text):
        n = _num(m.group(1))
        if n > 20:
            continue
        return f"{n}+", n
    for m in _P_QUALIFIED.finditer(text):
        n = _num(m.group(1))
        if n > 20:
            continue
        return f"{n}+", n
    return "Not explicit", None


def min_years(detail: Detail) -> tuple[str, int | None]:
    """Return (signal, lower_bound). Scan the minimum-qualifications section
    first, then the whole description. LinkedIn headings are noisy, so a real
    requirement often sits under a "preferred"-looking heading; scanning the
    whole text recovers it."""
    secs = _requirement_sections(detail.description_text)
    if secs["minimum"]:
        signal, lower = _scan_years(secs["minimum"])
        if signal != "Not explicit":
            return signal, lower
    return _scan_years(detail.description_text)


# --- location ---

_LOCATION_LINE_RE = re.compile(r"(?i)^\s*locations?\s*[:\-]\s*(.+)$")
_STATE_RE = re.compile(r"^[A-Z]{2}$")
_US_SUFFIX_RE = re.compile(r",?\s*United States$", re.I)
_METRO_SUFFIX_RE = re.compile(r"\s+Metropolitan Area$", re.I)


def _normalize_location(loc: str) -> str:
    """Drop LinkedIn's low-information suffixes so a real place survives:
    "New York, United States" -> "New York", "X Metropolitan Area" -> "X".
    A bare "United States" has nothing to keep, so it stays as is."""
    loc = loc.strip()
    stripped = _METRO_SUFFIX_RE.sub("", _US_SUFFIX_RE.sub("", loc)).strip()
    return stripped or loc


def _split_locations(capture: str) -> list[str]:
    rough = re.split(r"\s*;\s*|\s*\|\s*|\s+/\s+|\s+and\s+|\s+or\s+", capture)
    parts: list[str] = []
    for chunk in rough:
        merged: list[str] = []
        for piece in (p.strip() for p in chunk.split(",")):
            if _STATE_RE.match(piece) and merged:
                merged[-1] = f"{merged[-1]}, {piece}"
            elif piece:
                merged.append(piece)
        parts.extend(merged)
    return parts


def location(cards: list[Card], detail: Detail) -> list[str]:
    """Card locations (deduped, order preserved) plus any cities named on a
    "Locations:" line in the description. Never empty."""
    out: list[str] = []
    seen: set[str] = set()
    for card in cards:
        loc = _normalize_location(card.location or "")
        if loc and loc.lower() not in seen:
            seen.add(loc.lower())
            out.append(loc)
    for line in detail.description_text.splitlines()[:60]:
        m = _LOCATION_LINE_RE.match(line)
        if not m:
            continue
        for part in _split_locations(m.group(1)):
            if 3 <= len(part) <= 60 and part.lower() not in seen:
                seen.add(part.lower())
                out.append(part)
        break
    return out


# --- seniority and priority (overview Notion property tables) ---

_SEN_NEW_GRAD = re.compile(
    r"\b(new grad|new graduate|university grad|graduate engineer|early career)\b", re.I
)
_SEN_INTERN = re.compile(r"\bintern(ship)?\b", re.I)
_SEN_JUNIOR = re.compile(r"\b(junior|jr\.?|entry[- ]level)\b", re.I)


def seniority(card: Card, detail: Detail, min_years_lower: int | None) -> str:
    title = card.title or ""
    level = detail.seniority_level or ""
    if _SEN_NEW_GRAD.search(title):
        return "New Grad"
    if _SEN_INTERN.search(title):
        return "Internship"
    if _SEN_JUNIOR.search(title):
        return "Junior"
    if level == "Internship":
        return "Internship"
    if level == "Entry level":
        return "Entry Level"
    if level == "Associate":
        return "Associate"
    if level in ("Director", "Executive"):
        return "Senior"
    if min_years_lower is not None:
        if min_years_lower <= 2:
            return "Junior"
        if min_years_lower <= 4:
            return "Entry Level"
        if min_years_lower <= 6:
            return "Associate"
    if level == "Mid-Senior level":
        return "Associate" if min_years_lower is None else "Senior"
    return "Unknown"


def priority(seniority_value: str, min_years_lower: int | None) -> int:
    if seniority_value == "New Grad":
        return 1
    if seniority_value in ("Junior", "Entry Level", "Internship"):
        return 2
    if min_years_lower is None or min_years_lower <= 4:
        return 3
    if min_years_lower <= 6:
        return 4
    return 6


# --- work mode, h1b, skills, requirement signal (ported) ---


def work_mode(cards: list[Card], detail: Detail) -> str:
    text = " ".join(c.location or "" for c in cards)
    text += " " + (detail.location or "")
    text += " " + "\n".join(detail.description_text.splitlines()[:40])
    text = text.lower()
    if "remote" in text:
        return "Remote"
    if "hybrid" in text:
        return "Hybrid"
    if "on-site" in text or "onsite" in text or "on site" in text:
        return "On-site"
    return "Unknown"


_H1B_NEGATIVE = (
    "will not sponsor",
    "unable to sponsor",
    "cannot sponsor",
    "no sponsorship",
    "not sponsor",
    "do not sponsor",
    "without sponsorship",
    "must be authorized",
)
_H1B_POSITIVE = (
    "visa sponsorship is available",
    "sponsorship is available",
    "sponsor h-1b",
    "sponsor h1b",
    "h-1b sponsorship",
    "h1b sponsorship",
    "visa sponsorship",
)


def h1b(detail: Detail) -> str:
    text = f"{detail.description_text} {detail.location}".lower()
    if any(p in text for p in _H1B_NEGATIVE):
        return "No"
    if any(p in text for p in _H1B_POSITIVE):
        return "Yes"
    return "Unknown"


def skills(detail: Detail) -> str:
    hits = [
        name
        for name, pat in SKILL_PATTERNS.items()
        if re.search(pat, detail.description_text, re.I)
    ]
    return ", ".join(hits[:12])


_REQ_JUNK_RE = re.compile(
    r"based on linkedin data|chart has|data ranges|show more|apply|save|"
    r"use ai to assess|tailor my resume|create cover letter|company insights",
    re.I,
)
_REQ_KEEP_RE = re.compile(
    r"python|llm|ai|machine learning|cloud|aws|azure|rag|api|data|model|"
    r"production|kubernetes|docker|sql",
    re.I,
)


def requirement_signal(detail: Detail, skills_csv: str, limit: int = 3) -> str:
    """A few representative requirement lines; falls back to the first skills."""
    snippets: list[str] = []
    for line in detail.description_text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip(" -•\t")
        if _REQ_JUNK_RE.search(clean):
            continue
        if len(clean) < 35 or len(clean) > 180:
            continue
        if _REQ_KEEP_RE.search(clean):
            snippets.append(clean)
        if len(snippets) >= limit:
            break
    if snippets:
        return " / ".join(snippets)
    return ", ".join([s for s in skills_csv.split(", ") if s][:5])


# --- source type (ported from analyze_linkedin_jobs.py:521-538) ---

_STARTUP_SIGNAL_RE = re.compile(
    r"startup|venture-backed|seed|series [abc]|founding|equity", re.I
)
_DIRECT_COMPANY_RE = re.compile(
    r"inc\.|llc|ltd|corp|corporation|company|technologies|systems|health|ai", re.I
)


def source_type(company: str, description_text: str, companies: dict) -> str:
    """Classify the employer. Exclusion (`excluded_*`) fires only for a known
    aggregator or a confidential/blank name; an unrecognized company is always a
    direct employer (binding constraint in the overview)."""
    c = canon(company)
    if c in companies.get("aggregator", set()):
        return "excluded_intermediary"
    if "confidential" in c or c in ("", "none"):
        return "excluded_unknown"
    if c in companies.get("startup", set()):
        return "direct_startup"
    if c in companies.get("reliable", set()):
        return "direct_reliable_company"
    if _STARTUP_SIGNAL_RE.search(description_text):
        return "startup_or_growth_company"
    if _DIRECT_COMPANY_RE.search(company):
        return "direct_company"
    return "direct_company"


# --- unresolved fields ---

_GENERIC_LOCATION_RE = re.compile(
    r"^(United States|[A-Za-z .]+, United States|[A-Za-z .]+ Metropolitan Area)$"
)
_LOCATION_HINT_RE = re.compile(
    r"(?i)\b(locations?|offices?|based in|on[- ]?site in|hybrid in)\b"
)
_YEARS_HINT_RE = re.compile(r"(?i)\byears?\b")


def unresolved_fields(job: Job, detail: Detail) -> list[str]:
    """Fields the haiku fallback should try to fill: 'location' when every
    location is generic yet the text hints at real ones, and 'min_years' when
    the parser found nothing but the text mentions years."""
    out: list[str] = []
    locs = job.locations
    all_generic = bool(locs) and all(
        _GENERIC_LOCATION_RE.match(loc.strip()) for loc in locs
    )
    if all_generic and _LOCATION_HINT_RE.search(detail.description_text):
        out.append("location")
    if job.min_years_signal == "Not explicit" and _YEARS_HINT_RE.search(
        detail.description_text
    ):
        out.append("min_years")
    return out


def build_job(group: dict, detail: Detail, companies: dict, run_date) -> Job:
    """Assemble a Job from a sibling group (group['cards'] is a list of Card)
    and its parsed Detail. `run_date` is accepted for signature parity with the
    later sync step; the date shown is the newest card's date_posted."""
    cards = sorted(group["cards"], key=lambda c: int(c.job_id))
    job_ids = [c.job_id for c in cards]
    lowest = cards[0]
    signal, lower = min_years(detail)
    sen = seniority(lowest, detail, lower)
    minimum_q, preferred_q = qualifications(detail)
    parsed = skills(detail)
    job = Job(
        posting_key=f"linkedin:{job_ids[0]}",
        job_ids=job_ids,
        sibling_key=group["sibling_key"],
        title=lowest.title,
        company=lowest.company,
        locations=location(cards, detail),
        date_posted=max(c.date_posted for c in cards),
        url=lowest.url,
        work_mode=work_mode(cards, detail),
        seniority=sen,
        priority_score=priority(sen, lower),
        h1b_sponsorship=h1b(detail),
        source_type=source_type(lowest.company, detail.description_text, companies),
        min_years_signal=signal,
        min_years_lower=lower,
        minimum_qualifications=minimum_q,
        preferred_qualifications=preferred_q,
        parsed_skills=parsed,
        requirement_signal=requirement_signal(detail, parsed),
    )
    job.unresolved = unresolved_fields(job, detail)
    return job
