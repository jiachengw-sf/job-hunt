"""Source plugins. Each source fetches one public, documented listing and normalizes it into
``Posting``s. Sources never touch the store; the pipeline does.

Adding a source: subclass ``Source``, set ``kind`` and ``priority``, implement ``url`` and
``parse``. Higher-priority sources win when several describe the same job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from jobhunt.discovery.resolver import dedupe_key, resolve
from jobhunt.discovery.text import html_to_text, parse_ts
from jobhunt.http import HttpClient, Response, TransportError


class Posting(BaseModel):
    """One job as one source describes it."""

    source_id: str
    url: str = Field(description="The listing or apply URL as the source gives it.")
    canonical_url: str
    ats: str
    board_token: str | None = None
    ats_job_id: str | None = None
    company: str
    title: str
    locations: list[str] = Field(default_factory=list)
    work_mode: str | None = None
    employment_type: str | None = None
    description: str | None = None
    pay: dict[str, Any] | None = None
    posted_at: str | None = Field(None, description="From the source only. Never inferred.")
    deadline: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.ats, self.board_token, self.ats_job_id, self.canonical_url)


class SourceError(Exception):
    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable  # the host could not be reached at all


@dataclass
class FetchResult:
    postings: list[Posting] = field(default_factory=list)
    not_modified: bool = False
    # True: postings are the source's complete listing, so listed jobs missing from it closed.
    complete: bool = True
    gone: list[str] = field(default_factory=list)  # dedupe keys confirmed closed (complete=False)
    etag: str | None = None
    last_modified: str | None = None
    errors: list[str] = field(default_factory=list)  # per-item errors that did not stop the fetch


class Source(ABC):
    kind: ClassVar[str]
    priority: ClassVar[int]

    def __init__(self, source_id: str, config: dict[str, Any]) -> None:
        self.id = source_id
        self.config = config

    @abstractmethod
    def url(self) -> str: ...

    @abstractmethod
    def parse(self, data: Any) -> list[Posting]: ...

    def fetch(
        self, client: HttpClient, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        resp = _get(client, self.url(), etag=etag, last_modified=last_modified)
        if resp.not_modified:
            return FetchResult(not_modified=True, etag=etag, last_modified=last_modified)
        if resp.status == 404:
            raise SourceError(f"{self.url()} returned 404: check the board name")
        if not resp.ok:
            raise SourceError(f"{self.url()} returned HTTP {resp.status}")
        try:
            data = resp.json()
        except ValueError as e:
            raise SourceError(f"{self.url()} did not return JSON") from e
        return FetchResult(
            postings=self.parse(data),
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
        )


def _get(client: HttpClient, url: str, **kw: Any) -> Response:
    try:
        return client.get(url, **kw)
    except TransportError as e:
        raise SourceError(str(e), unreachable=True) from e


# --- Simplify listing repos -----------------------------------------------------------------


class SimplifySource(Source):
    """A SimplifyJobs listing repo's ``.github/scripts/listings.json`` (on the ``dev`` branch).

    Uses ``active`` + ``is_visible``, and ``date_posted`` as the posting date. Ignores
    ``sponsorship`` (it says "Other" on nearly every row) and ``date_updated``.
    """

    kind = "simplify"
    priority = 1

    def url(self) -> str:
        repo, branch = self.config["repo"], self.config.get("branch", "dev")
        return (
            f"https://raw.githubusercontent.com/SimplifyJobs/{repo}/{branch}"
            "/.github/scripts/listings.json"
        )

    def parse(self, data: Any) -> list[Posting]:
        if not isinstance(data, list):
            raise SourceError("listings.json is not a list: the schema may have changed")
        out = []
        for row in data:
            if not (isinstance(row, dict) and row.get("active") and row.get("is_visible")):
                continue
            url, title, company = row.get("url"), row.get("title"), row.get("company_name")
            if not (url and title and company):
                continue
            r = resolve(url)
            out.append(
                Posting(
                    source_id=self.id,
                    url=url,
                    canonical_url=r.canonical_url,
                    ats=r.ats,
                    board_token=r.board_token,
                    ats_job_id=r.job_id,
                    company=company.strip(),
                    title=title.strip(),
                    locations=[str(x) for x in row.get("locations") or []],
                    posted_at=parse_ts(row.get("date_posted")),
                    extra={
                        "simplify_id": row.get("id"),
                        "terms": row.get("terms") or [],
                        "degrees": row.get("degrees") or [],
                        "category": row.get("category"),
                    },
                )
            )
        return out


# --- Greenhouse -----------------------------------------------------------------------------


class GreenhouseSource(Source):
    """``boards-api.greenhouse.io``. ``first_published`` is the posting date, never
    ``updated_at`` (it changes on every edit)."""

    kind = "greenhouse"
    priority = 3
    API = "https://boards-api.greenhouse.io/v1/boards"

    def url(self) -> str:
        return f"{self.API}/{self.config['board']}/jobs?content=true&pay_transparency=true"

    def parse(self, data: Any) -> list[Posting]:
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError("Greenhouse response has no jobs list")
        return [self._posting(j) for j in data["jobs"] if isinstance(j, dict) and j.get("id")]

    def _posting(self, j: dict[str, Any]) -> Posting:
        board = self.config["board"].lower()
        job_id = str(j["id"])
        loc = (j.get("location") or {}).get("name")
        return Posting(
            source_id=self.id,
            url=j.get("absolute_url") or f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
            canonical_url=f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}",
            ats="greenhouse",
            board_token=board,
            ats_job_id=job_id,
            company=self.config.get("company") or j.get("company_name") or board,
            title=(j.get("title") or "").strip(),
            locations=[loc.strip()] if loc else [],
            description=html_to_text(j.get("content")),
            pay=_greenhouse_pay(j.get("pay_input_ranges")),
            posted_at=parse_ts(j.get("first_published")),
            deadline=parse_ts(j.get("application_deadline")),
            extra={"departments": [d.get("name") for d in j.get("departments") or []]},
        )

    @classmethod
    def fetch_job(
        cls, client: HttpClient, board: str, job_id: str, source_id: str
    ) -> Posting | None:
        resp = _get(client, f"{cls.API}/{board}/jobs/{job_id}?pay_transparency=true")
        if resp.status == 404:
            return None
        if not resp.ok:
            raise SourceError(f"Greenhouse job {board}/{job_id}: HTTP {resp.status}")
        return cls(source_id, {"board": board})._posting(resp.json())


def _greenhouse_pay(ranges: Any) -> dict[str, Any] | None:
    if not ranges:
        return None
    r = ranges[0]
    try:
        return {
            "min": int(r["min_cents"]) / 100,
            "max": int(r["max_cents"]) / 100,
            "currency": r.get("currency_type"),
            "interval": None,
            "text": r.get("title"),
        }
    except (KeyError, TypeError, ValueError):
        return None


# --- Lever ----------------------------------------------------------------------------------


class LeverSource(Source):
    """``api.lever.co/v0/postings/{site}`` (EU: ``api.eu.lever.co``)."""

    kind = "lever"
    priority = 3

    @staticmethod
    def api(region: str | None) -> str:
        return (
            "https://api.eu.lever.co/v0/postings"
            if region == "eu"
            else "https://api.lever.co/v0/postings"
        )

    def url(self) -> str:
        return f"{self.api(self.config.get('region'))}/{self.config['board']}?mode=json"

    def parse(self, data: Any) -> list[Posting]:
        if not isinstance(data, list):
            raise SourceError("Lever response is not a list")
        return [self._posting(p) for p in data if isinstance(p, dict) and p.get("id")]

    def _posting(self, p: dict[str, Any]) -> Posting:
        site = self.config["board"].lower()
        job_id = str(p["id"]).lower()
        eu = self.config.get("region") == "eu"
        base = "https://jobs.eu.lever.co" if eu else "https://jobs.lever.co"
        cats = p.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        parts = [p.get("descriptionPlain") or html_to_text(p.get("description"))]
        for lst in p.get("lists") or []:
            parts.append(lst.get("text"))
            parts.append(html_to_text(lst.get("content")))
        parts.append(p.get("additionalPlain") or html_to_text(p.get("additional")))
        desc = "\n".join(x.strip() for x in parts if x and x.strip()) or None
        return Posting(
            source_id=self.id,
            url=p.get("hostedUrl") or f"{base}/{site}/{job_id}",
            canonical_url=f"{base}/{site}/{job_id}",
            ats="lever",
            board_token=site,
            ats_job_id=job_id,
            company=self.config.get("company") or site,
            title=(p.get("text") or "").strip(),
            locations=[str(x) for x in locations],
            work_mode=p.get("workplaceType") or None,
            employment_type=cats.get("commitment"),
            description=desc,
            pay=_lever_pay(p.get("salaryRange")),
            posted_at=parse_ts(p.get("createdAt")),
            extra={"team": cats.get("team"), "country": p.get("country")},
        )

    @classmethod
    def fetch_job(
        cls, client: HttpClient, board: str, job_id: str, source_id: str, region: str | None = None
    ) -> Posting | None:
        resp = _get(client, f"{cls.api(region)}/{board}/{job_id}?mode=json")
        if resp.status == 404:
            return None
        if not resp.ok:
            raise SourceError(f"Lever posting {board}/{job_id}: HTTP {resp.status}")
        return cls(source_id, {"board": board, "region": region})._posting(resp.json())


def _lever_pay(r: Any) -> dict[str, Any] | None:
    if not isinstance(r, dict) or r.get("min") is None:
        return None
    return {
        "min": r.get("min"),
        "max": r.get("max"),
        "currency": r.get("currency"),
        "interval": r.get("interval"),
        "text": None,
    }


# --- Ashby ----------------------------------------------------------------------------------


class AshbySource(Source):
    """``api.ashbyhq.com/posting-api/job-board/{board}``. ``publishedAt`` is the *last* publish
    date (republished jobs look new), so it is kept as metadata and not used as ``posted_at``;
    freshness relies on ``first_seen``."""

    kind = "ashby"
    priority = 3

    def url(self) -> str:
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{self.config['board']}"
            "?includeCompensation=true"
        )

    def parse(self, data: Any) -> list[Posting]:
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise SourceError("Ashby response has no jobs list")
        return [
            self._posting(j)
            for j in data["jobs"]
            if isinstance(j, dict) and j.get("id") and j.get("isListed", True) is not False
        ]

    def _posting(self, j: dict[str, Any]) -> Posting:
        board = self.config["board"]
        job_id = str(j["id"]).lower()
        locations = [j["location"]] if j.get("location") else []
        locations += [s["location"] for s in j.get("secondaryLocations") or [] if s.get("location")]
        return Posting(
            source_id=self.id,
            url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{job_id}",
            canonical_url=f"https://jobs.ashbyhq.com/{board}/{job_id}",
            ats="ashby",
            board_token=board,
            ats_job_id=job_id,
            company=self.config.get("company") or board,
            title=(j.get("title") or "").strip(),
            locations=locations,
            work_mode=(j.get("workplaceType") or ("Remote" if j.get("isRemote") else None)),
            employment_type=j.get("employmentType"),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            pay=_ashby_pay(j.get("compensation")),
            posted_at=None,
            extra={"published_at": parse_ts(j.get("publishedAt")), "team": j.get("team")},
        )

    @classmethod
    def fetch_job(
        cls, client: HttpClient, board: str, job_id: str, source_id: str
    ) -> Posting | None:
        src = cls(source_id, {"board": board})
        result = src.fetch(client)
        return next((p for p in result.postings if p.ats_job_id == job_id.lower()), None)


def _ashby_pay(c: Any) -> dict[str, Any] | None:
    if not isinstance(c, dict):
        return None
    for comp in c.get("summaryComponents") or []:
        if comp.get("compensationType") == "Salary" and comp.get("minValue") is not None:
            return {
                "min": comp.get("minValue"),
                "max": comp.get("maxValue"),
                "currency": comp.get("currencyCode"),
                "interval": comp.get("interval"),
                "text": c.get("compensationTierSummary"),
            }
    if c.get("compensationTierSummary"):
        return {"text": c["compensationTierSummary"]}
    return None


# --- Manual adds (re-polled one by one) -----------------------------------------------------


@dataclass(frozen=True)
class ManualTarget:
    key: str
    ats: str
    board: str
    job_id: str
    region: str | None = None


def fetch_single(client: HttpClient, t: ManualTarget, source_id: str) -> Posting | None:
    """One posting from a supported ATS; None when the ATS says it no longer exists."""
    if t.ats == "greenhouse":
        return GreenhouseSource.fetch_job(client, t.board, t.job_id, source_id)
    if t.ats == "lever":
        return LeverSource.fetch_job(client, t.board, t.job_id, source_id, t.region)
    if t.ats == "ashby":
        return AshbySource.fetch_job(client, t.board, t.job_id, source_id)
    raise SourceError(f"{t.ats} is not a supported ATS")


class ManualSource(Source):
    """Jobs added with ``jobhunt add``. Supported-ATS jobs are re-polled one by one so closures
    are detected; others can't be re-polled and stay as they are."""

    kind = "manual"
    priority = 2

    def __init__(self, source_id: str, config: dict[str, Any], targets: list[ManualTarget]) -> None:
        super().__init__(source_id, config)
        self.targets = targets

    def url(self) -> str:
        return "manual"

    def parse(self, data: Any) -> list[Posting]:  # pragma: no cover - not list-based
        return []

    def fetch(self, client: HttpClient, **_: Any) -> FetchResult:
        result = FetchResult(complete=False)
        for t in self.targets:
            try:
                p = fetch_single(client, t, self.id)
            except SourceError as e:
                result.errors.append(str(e))
                continue
            if p is None:
                result.gone.append(t.key)
            else:
                result.postings.append(p)
        return result


SOURCE_KINDS: dict[str, type[Source]] = {
    s.kind: s for s in (SimplifySource, GreenhouseSource, LeverSource, AshbySource)
}
PRIORITY: dict[str, int] = {**{k: v.priority for k, v in SOURCE_KINDS.items()}, "manual": 2}
