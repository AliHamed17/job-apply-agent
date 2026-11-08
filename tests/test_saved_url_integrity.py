from __future__ import annotations

import json

import pytest

from discovery.fast_discovery import read_saved_urls
from ingestion.url_utils import extract_urls

_ATS_SHAPES = [
    "https://careers.example.test/City-_-District-Hardware-Tester/61360000",
    "https://jobs.example.test/team_rnd/software-engineer",
    "https://example.wd3.myworkdayjobs.com/External/job/Engineer_JR2025000",
    "https://careers.example.test/hcmUI/CandidateExperience/en/sites/CX_1/job/123",
    "https://jobs.example.test/role?job_id=abc_def&source_id=group_entry",
    "https://jobs.example.test/_role_/ending_",
    "https://jobs.example.test/~candidate/role*variant",
]


@pytest.mark.parametrize("url", _ATS_SHAPES)
def test_explicit_saved_urls_preserve_ats_identifiers(url):
    assert read_saved_urls(json.dumps({"leads": [{"url": url}]})) == [url]


@pytest.mark.parametrize("url", _ATS_SHAPES)
def test_message_urls_preserve_ats_identifiers(url):
    assert extract_urls("Apply here: " + url + " for this role") == [url]
    assert read_saved_urls("Apply here: " + url + " for this role") == [url]


@pytest.mark.parametrize("marker", ["*", "_", "~", "`", "```", "**", "__", "*_"])
def test_only_matching_outer_markup_is_trimmed(marker):
    url = "https://jobs.example.test/team_rnd/role_?job_id=abc_def"
    assert extract_urls("Apply " + marker + url + marker[::-1] + ".") == [url]


def test_bare_trailing_markup_characters_are_url_content():
    urls = [
        "https://jobs.example.test/ends_",
        "https://jobs.example.test/ends~",
        "https://jobs.example.test/ends*",
    ]
    assert extract_urls(" ".join(urls)) == urls
