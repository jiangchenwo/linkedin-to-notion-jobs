from jobs import extract, profile

CUSTOM_TOML = """
keywords = ["data engineer"]

[search]
location = "United States"
max_pages = 5

[seniority]
exclude_title_terms = ["senior", "staff"]
new_grad_title_terms = ["new grad"]

[relevance]
terms = ["etl", "spark"]
core_skills = ["Data pipelines"]

[areas]
default = "General Data"

[[areas.map]]
label = "Pipelines"
pattern = 'etl|spark|airflow'

[[areas.map]]
label = "Warehousing"
pattern = 'snowflake|redshift|warehouse'
"""


def _write(tmp_path):
    p = tmp_path / "keywords.toml"
    p.write_text(CUSTOM_TOML)
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
    toml = tmp_path / "keywords.toml"
    toml.write_text(
        CUSTOM_TOML.replace('exclude_title_terms = ["senior", "staff"]', "exclude_title_terms = []")
    )
    prof = profile.load(str(toml))
    assert prof.senior_title_re.search("Senior Staff Principal") is None
