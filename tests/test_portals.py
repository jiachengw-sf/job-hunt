"""The status-portal registry: where a candidate checks an application, per ATS."""

from __future__ import annotations

from jobhunt.tracker.portals import status_check_defaults, workday_user_home

WORKDAY = "https://harborlight.wd5.myworkdayjobs.com/en-US/careers/job/Charlotte-NC/Technology-Intern_R-10023"


def test_status_portal_registry():
    gh = status_check_defaults("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1")
    assert gh.method == "portal" and gh.url == "https://my.greenhouse.io/"
    wd = status_check_defaults("workday", WORKDAY)
    assert wd.method == "portal"
    assert wd.url == "https://harborlight.wd5.myworkdayjobs.com/careers/userHome"
    assert "Candidate Home" in wd.note
    assert workday_user_home("https://acme.wd1.myworkdayjobs.com/External/job/X/Y_R1") == (
        "https://acme.wd1.myworkdayjobs.com/External/userHome"
    )
    # a URL the rule can't read: email, and the note says why
    odd = status_check_defaults("workday", "https://careers.example.com/jobs/1")
    assert odd.method == "email" and odd.url is None and "couldn't" in odd.note
    for ats in ("lever", "ashby", "icims", "smartrecruiters"):
        assert status_check_defaults(ats, None).method == "email"
    unknown = status_check_defaults("some-new-ats", "https://x.example/jobs/1")
    assert unknown.method == "email" and unknown.url is None and "doesn't know" in unknown.note
    assert status_check_defaults(None, None).method == "email"
