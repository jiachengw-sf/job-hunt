"""ATS resolver: detect the ATS, board token and job id from any URL."""

from __future__ import annotations

import pytest

from jobhunt.discovery.resolver import canonical_url, resolve

UUID = "4d1c2e7a-0b6f-4c1e-9a7d-2f3b5c6d7e02"


@pytest.mark.parametrize(
    ("url", "ats", "board", "job_id", "key"),
    [
        (
            "https://boards.greenhouse.io/northwind/jobs/1001",
            "greenhouse",
            "northwind",
            "1001",
            "greenhouse:northwind:1001",
        ),
        (
            "https://job-boards.greenhouse.io/Northwind/jobs/1001?gh_src=simplify",
            "greenhouse",
            "northwind",
            "1001",
            "greenhouse:northwind:1001",
        ),
        (
            "https://boards.greenhouse.io/embed/job_app?for=northwind&token=1001",
            "greenhouse",
            "northwind",
            "1001",
            "greenhouse:northwind:1001",
        ),
        (
            "https://boards-api.greenhouse.io/v1/boards/northwind/jobs/1001",
            "greenhouse",
            "northwind",
            "1001",
            "greenhouse:northwind:1001",
        ),
        ("https://job-boards.greenhouse.io/northwind", "greenhouse", "northwind", None, None),
        (
            "https://www.northwind.example/careers/listing?gh_jid=1001",
            "greenhouse",
            None,
            "1001",
            None,
        ),
        (
            f"https://jobs.lever.co/bluefin/{UUID}/apply?lever-source=Simplify",
            "lever",
            "bluefin",
            UUID,
            f"lever:bluefin:{UUID}",
        ),
        (
            f"https://jobs.eu.lever.co/bluefin/{UUID}",
            "lever",
            "bluefin",
            UUID,
            f"lever:bluefin:{UUID}",
        ),
        ("https://jobs.lever.co/bluefin", "lever", "bluefin", None, None),
        (
            f"https://jobs.ashbyhq.com/Quillstack/{UUID}/application",
            "ashby",
            "Quillstack",
            UUID,
            f"ashby:quillstack:{UUID}",
        ),
        (
            "https://api.ashbyhq.com/posting-api/job-board/quillstack",
            "ashby",
            "quillstack",
            None,
            None,
        ),
        (f"https://quillstack.example/careers?ashby_jid={UUID}", "ashby", None, UUID, None),
        (
            "https://harborlight.wd5.myworkdayjobs.com/en-US/careers/job/X_R-1",
            "workday",
            None,
            None,
            None,
        ),
        (
            "https://careers-cobalt.icims.com/jobs/5521/x/job?iis=Simplify",
            "icims",
            None,
            None,
            None,
        ),
        (
            "https://hcog.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1",
            "oracle",
            None,
            None,
            None,
        ),
        (
            "https://career5.successfactors.eu/sfcareer/jobreqcareer?jobId=1",
            "successfactors",
            None,
            None,
            None,
        ),
        ("https://apply.workable.com/acme/j/ABC123/", "workable", None, None, None),
        ("https://careers.mapleleaf-energy.example/jobs/1", "unknown", None, None, None),
    ],
)
def test_resolve(url, ats, board, job_id, key):
    r = resolve(url)
    assert (r.ats, r.board_token, r.job_id) == (ats, board, job_id)
    assert r.supported == (ats in {"greenhouse", "lever", "ashby"})
    if key:
        assert r.dedupe_key == key
    else:
        assert r.dedupe_key == f"url:{r.canonical_url}"


def test_lever_eu_region():
    assert resolve(f"https://jobs.eu.lever.co/bluefin/{UUID}").region == "eu"
    assert resolve(f"https://jobs.lever.co/bluefin/{UUID}").region is None


def test_simplify_link_and_direct_link_share_a_key():
    a = resolve("https://boards.greenhouse.io/northwind/jobs/1001")
    b = resolve("https://job-boards.greenhouse.io/northwind/jobs/1001?gh_src=simplify&utm_source=x")
    assert a.dedupe_key == b.dedupe_key
    assert a.canonical_url == b.canonical_url


def test_canonical_url_strips_tracking_only():
    url = "HTTP://Careers.Example.com/jobs/42/?utm_source=x&b=2&a=1&ref=feed#apply"
    assert canonical_url(url) == "https://careers.example.com/jobs/42?a=1&b=2"
