"""ATS resolver: any job or careers URL -> (ats, board token, job id, canonical URL).

Supported ATSs are the ones with public, documented posting APIs (Greenhouse, Lever, Ashby).
Others are recognized by name so coverage stats are accurate; their jobs dedupe on the canonical
URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SUPPORTED = frozenset({"greenhouse", "lever", "ashby"})

# host suffix -> ATS name, for ATSs jobhunt cannot read yet
UNSUPPORTED_HOSTS: dict[str, str] = {
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "workday.com": "workday",
    "icims.com": "icims",
    "oraclecloud.com": "oracle",
    "taleo.net": "taleo",
    "successfactors.com": "successfactors",
    "successfactors.eu": "successfactors",
    "sapsf.com": "successfactors",
    "sapsf.eu": "successfactors",
    "workable.com": "workable",
    "smartrecruiters.com": "smartrecruiters",
    "jobvite.com": "jobvite",
    "bamboohr.com": "bamboohr",
    "recruitee.com": "recruitee",
    "breezy.hr": "breezy",
    "applytojob.com": "jazzhr",
    "paylocity.com": "paylocity",
    "ultipro.com": "ukg",
    "ukg.net": "ukg",
    "adp.com": "adp",
    "dayforcehcm.com": "dayforce",
    "rippling.com": "rippling",
    "pinpointhq.com": "pinpoint",
    "teamtailor.com": "teamtailor",
    "personio.de": "personio",
    "personio.com": "personio",
    "eightfold.ai": "eightfold",
    "avature.net": "avature",
    "wellfound.com": "wellfound",
}

# Query parameters that only track where a click came from.
TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gh_src|source|src|ref|referrer|refid|lever-source.*|lever-origin|iis|iisn|"
    r"mode|codes|trk|trackingid|_ga|gclid|fbclid|mc_cid|mc_eid)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Resolved:
    ats: str  # greenhouse | lever | ashby | workday | ... | unknown
    board_token: str | None
    job_id: str | None
    canonical_url: str
    region: str | None = None  # "eu" for Lever's EU instance

    @property
    def supported(self) -> bool:
        return self.ats in SUPPORTED

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.ats, self.board_token, self.job_id, self.canonical_url)


def dedupe_key(ats: str | None, board: str | None, job_id: str | None, url: str) -> str:
    """``ats:board:job_id`` when all three are known, else ``url:<canonical url>``. Never on
    company + title: one title in several locations is one role, not duplicates."""
    if ats and ats in SUPPORTED and board and job_id:
        return f"{ats}:{board.lower()}:{job_id.lower()}"
    return f"url:{url}"


def canonical_url(url: str) -> str:
    """https, lower-case host, no fragment, no tracking params, sorted query, no trailing /."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.endswith(":443"):
        host = host[:-4]
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not TRACKING_PARAMS.match(k)
    )
    path = parts.path.rstrip("/") or ""
    return urlunsplit(("https", host, path, urlencode(query), ""))


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def resolve(url: str) -> Resolved:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().split(":")[0]
    segs = _segments(parts.path)
    query = dict(parse_qsl(parts.query))

    if host.endswith("greenhouse.io"):
        r = _greenhouse(host, segs, query)
        if r:
            return r
    if host in ("jobs.lever.co", "jobs.eu.lever.co", "api.lever.co", "api.eu.lever.co"):
        r = _lever(host, segs)
        if r:
            return r
    if host in ("jobs.ashbyhq.com", "api.ashbyhq.com"):
        r = _ashby(host, segs)
        if r:
            return r
    # Employer career sites that embed an ATS board pass the job id in the query string.
    if query.get("gh_jid", "").isdigit():
        return Resolved("greenhouse", query.get("for"), query["gh_jid"], canonical_url(url))
    if query.get("ashby_jid"):
        return Resolved("ashby", None, query["ashby_jid"], canonical_url(url))
    for suffix, name in UNSUPPORTED_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return Resolved(name, None, None, canonical_url(url))
    return Resolved("unknown", None, None, canonical_url(url))


def _greenhouse(host: str, segs: list[str], query: dict[str, str]) -> Resolved | None:
    def gh(token: str, job_id: str | None) -> Resolved:
        token = token.lower()
        url = f"https://job-boards.greenhouse.io/{token}" + (f"/jobs/{job_id}" if job_id else "")
        return Resolved("greenhouse", token, job_id, url)

    if host.startswith("boards-api."):
        # /v1/boards/{token}[/jobs[/{id}]]
        if len(segs) >= 3 and segs[0] == "v1" and segs[1] == "boards":
            job_id = segs[4] if len(segs) >= 5 and segs[3] == "jobs" else None
            return gh(segs[2], job_id)
        return None
    if segs[:1] == ["embed"]:
        token = query.get("for")
        if token:
            job_id = query.get("token") if segs[1:2] == ["job_app"] else None
            return gh(token, job_id if job_id and job_id.isdigit() else None)
        return None
    if not segs:
        return None
    job_id = None
    if len(segs) >= 3 and segs[1] == "jobs" and segs[2].isdigit():
        job_id = segs[2]
    elif query.get("gh_jid", "").isdigit():
        job_id = query["gh_jid"]
    return gh(segs[0], job_id)


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _lever(host: str, segs: list[str]) -> Resolved | None:
    eu = ".eu." in host
    if host.startswith("api."):
        # /v0/postings/{site}[/{id}]
        if len(segs) >= 3 and segs[:2] == ["v0", "postings"]:
            segs = segs[2:]
        else:
            return None
    if not segs:
        return None
    site = segs[0].lower()
    job_id = segs[1].lower() if len(segs) >= 2 and _UUID.match(segs[1]) else None
    base = "https://jobs.eu.lever.co" if eu else "https://jobs.lever.co"
    url = f"{base}/{site}" + (f"/{job_id}" if job_id else "")
    return Resolved("lever", site, job_id, url, region="eu" if eu else None)


def _ashby(host: str, segs: list[str]) -> Resolved | None:
    if host.startswith("api."):
        # /posting-api/job-board/{board}
        if len(segs) >= 3 and segs[:2] == ["posting-api", "job-board"]:
            board = segs[2]
            return Resolved("ashby", board, None, f"https://jobs.ashbyhq.com/{board}")
        return None
    if not segs:
        return None
    board = segs[0]
    job_id = segs[1].lower() if len(segs) >= 2 and _UUID.match(segs[1]) else None
    url = f"https://jobs.ashbyhq.com/{board}" + (f"/{job_id}" if job_id else "")
    return Resolved("ashby", board, job_id, url)
