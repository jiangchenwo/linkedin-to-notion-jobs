from pathlib import Path

import pytest

from jobs import extract, profile

REPO = Path(__file__).parents[1]

CUSTOM_TOML = r"""
keywords = ["data engineer"]

[search]
location = "United States"
sortBy = "DD"
max_pages = 5
f_TPR_past_24h = "r86400"
f_TPR_past_week = "r604800"

[filters]
exclude_title_terms = ["senior", "staff"]
long_window_title_terms = ["new grad"]

[priority]
"New Grad" = 1
"Internship" = 2
"Junior" = 2
"Entry Level" = 2
"Associate" = 3
"Unknown" = 3
"Senior" = 6

[relevance]
terms = ["etl", "spark"]
core_skills = ["Data pipelines"]

[skills]
"Data pipelines" = 'etl|spark|airflow'

[areas]
default = "General Data"

[[areas.map]]
label = "Pipelines"
pattern = 'etl|spark|airflow'

[[areas.map]]
label = "Warehousing"
pattern = 'snowflake|redshift|warehouse'
"""


def _write(tmp_path, text=CUSTOM_TOML):
    p = tmp_path / "keywords.toml"
    p.write_text(text)
    return str(p)


def test_load_compiles_a_different_area_map(tmp_path):
    prof = profile.load(_write(tmp_path))
    assert prof.keywords == ("data engineer",)
    assert [label for label, _ in prof.areas] == ["Pipelines", "Warehousing"]
    assert prof.area_default == "General Data"
    # title hit alone is enough; the default fires when nothing matches
    assert prof.match_areas("ETL Engineer", "") == ["Pipelines"]
    assert prof.match_areas("Engineer", "we build accounting software") == ["General Data"]


def test_areas_honours_the_loaded_profile(tmp_path, monkeypatch):
    prof = profile.load(_write(tmp_path))
    monkeypatch.setattr(extract, "PROFILE", prof)
    # extract.areas() reads the swapped profile, not the shipped AI-engineer map
    assert extract.areas("Snowflake Warehouse Engineer", "") == ["Warehousing"]
    assert extract.areas("Backend Engineer", "no data terms here") == ["General Data"]


def test_empty_term_list_never_matches(tmp_path):
    prof = profile.load(
        _write(tmp_path, CUSTOM_TOML.replace('exclude_title_terms = ["senior", "staff"]', "exclude_title_terms = []"))
    )
    assert prof.exclude_title_re.search("Senior Staff Principal") is None


def test_new_default_thresholds(tmp_path):
    prof = profile.load(_write(tmp_path))
    # unset optional counts fall back to the shipped defaults
    assert prof.max_age_days == 2
    assert prof.long_window_max_age_days == 14
    assert prof.max_min_years is None  # omitted -> disabled
    assert prof.employment_types == frozenset()  # omitted -> accept every type


def test_missing_section_raises_profile_error(tmp_path):
    broken = CUSTOM_TOML.replace("[priority]", "[priorities]")
    with pytest.raises(profile.ProfileError, match=r"\[priority\]"):
        profile.load(_write(tmp_path, broken))


PROFILE_FILES = [REPO / "data" / "keywords.toml"] + sorted(
    (REPO / "data" / "examples").glob("*.toml")
)


@pytest.mark.parametrize("path", PROFILE_FILES, ids=lambda p: p.name)
def test_shipped_and_example_profiles_load(path):
    prof = profile.load(str(path))
    assert prof.keywords
    # an empty description matches nothing, so every profile returns its default
    assert prof.match_areas("", "") == [prof.area_default]
