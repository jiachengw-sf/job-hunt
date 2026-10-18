"""Highlights, screenshots and the ready_for_review report."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from jobhunt.prefill.review import highlight, review, screenshot
from tests.test_apply_cli import secrets_of
from tests.test_prefill_adapters import RESUMES, make_sheet, run_fill


def test_review_highlights_and_never_carries_values(browser_context, fx_pages, tmp_path):
    persona = "intl-sponsorship"
    sheet = make_sheet(persona, "lv_A", RESUMES / f"{persona}.pdf")
    page, adapter, _, _, outcomes = run_fill(browser_context, fx_pages, "lv_A", sheet)
    marked = highlight(page, adapter, sheet, outcomes)
    notes = page.locator("[data-jobhunt-note]")
    assert marked == notes.count() >= 2  # the two Lever radios at least
    texts = notes.all_inner_texts()
    assert any("recommended: Yes" in t for t in texts)  # the value shows on the user's page
    assert "never submits" in page.locator("[data-jobhunt-banner]").inner_text()
    assert page.locator("[data-jobhunt-note] input, [data-jobhunt-banner] input").count() == 0

    shot = screenshot(page, tmp_path / "screens", 7, "2026-10-18T12:00:00Z")
    assert shot
    png = Path(shot)
    assert png.parent == tmp_path / "screens" and png.stat().st_size > 1000
    assert os.name == "nt" or stat.S_IMODE(png.stat().st_mode) == 0o600

    rep = review(7, sheet, outcomes, company="Bluefin", title="Intern", blocked=2, shot=shot)
    assert rep.status == "ready_for_review"
    assert rep.filled == rep.to_fill == len(sheet.to_fill())
    assert {i.status for i in rep.needs_you} >= {"recommend"}
    assert rep.needs_you[0].required
    blob = json.dumps(rep.model_dump(mode="json"))
    assert "recommended:" not in blob and "expected:" not in blob
    for s in secrets_of(persona):
        assert s not in blob


def test_required_problem_is_urgent(browser_context, fx_pages):
    sheet = make_sheet("intl-sponsorship", "gh_A", RESUMES / "intl-sponsorship.pdf")
    ff = next(f for f in sheet.fields if f.id == "question_100104")  # required select
    ff.value = "Carrier pigeon"  # not an option on the page
    page, adapter, _, _, outcomes = run_fill(browser_context, fx_pages, "gh_A", sheet)
    o = next(o for o in outcomes if o.id == "question_100104")
    assert o.status == "no_option"
    highlight(page, adapter, sheet, outcomes)
    box = page.locator(".select__container:has(#question_100104)")
    assert "220, 38, 38" in box.evaluate("e => getComputedStyle(e).outlineColor")
    rep = review(1, sheet, outcomes)
    assert rep.needs_you[0].id == "question_100104"
