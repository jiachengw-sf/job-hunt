"""Onboarding commands: resume add, profile confirm/set/show, setup form.

These run before onboarding is complete (they are how it gets completed).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import BaseModel, Field

from jobhunt.cli.framework import Result
from jobhunt.cli.framework import agent_command as _agent_command
from jobhunt.config import find_placeholders, read_yaml
from jobhunt.errors import ConfigError, NotFound, ValidationFailed
from jobhunt.locking import file_lock
from jobhunt.onboarding import profile as prof
from jobhunt.onboarding import resume as rs
from jobhunt.onboarding import server
from jobhunt.onboarding.form import write_yaml
from jobhunt.paths import Paths, get_paths, secure_file


class ResumeReport(BaseModel):
    dry_run: bool
    file: str = Field(description="Name inside ~/.jobhunt/resume/.")
    default: bool
    characters: int
    sections: list[str] = Field(description="Sections the draft found.")
    contact_found: dict[str, bool] = Field(
        description="Which contact details were found (values go to the form, never here)."
    )
    redacted_text: str = Field(description="Resume text without contact lines: read this.")
    draft: str | None = Field(description="Draft profile to tidy, then `profile confirm`.")
    draft_kept: bool = Field(
        False, description="An existing draft was kept; only its resume list changed."
    )
    resumes: list[str] = Field(default_factory=list, description="Resume files now listed.")
    placeholders: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list, description="Sections left out of the draft, and why."
    )


class ProfileReport(BaseModel):
    dry_run: bool
    path: str
    changed: dict[str, Any] = Field(default_factory=dict)
    profile: dict[str, Any] | None = None


class FormStatus(BaseModel):
    status: str = Field(description="waiting | saved | stopped | saved_and_stopped | not_started")
    url: str | None = Field(None, description="Open this in a browser (local only).")
    running: bool
    saved_at: str | None = None
    saves: int = 0
    files: dict[str, str] = Field(default_factory=dict, description="Onboarding file status.")


def register(cli: click.Group) -> None:
    def agent_command(name: str, **kw: Any):
        return _agent_command(cli, name, **kw)

    @agent_command("resume add", output=ResumeReport, writes=True)
    @click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option(
        "--default/--not-default",
        "make_default",
        default=None,
        help="Make it the default resume, or not. Unset: the first resume is the default and "
        "later ones aren't. With several resumes and no default, pick one per job "
        "(`prefill --resume`).",
    )
    @click.option("--label", default=None, help="A short name, e.g. 'data roles'.")
    @click.option(
        "--redraft", is_flag=True, help="Draft the profile from this resume even if one exists."
    )
    def resume_add(
        file: Path, make_default: bool | None, label: str | None, redraft: bool, dry_run: bool
    ) -> Result:
        """Copy a resume (PDF/DOCX/MD/TXT) into ~/.jobhunt/resume and draft profile.yaml from it.

        Contact details go only to the local form's pre-fill. The redacted text and the draft
        are what the AI tool reads and tidies; the user confirms with `jobhunt profile confirm`.
        Adding another resume keeps the draft (or profile) you have and adds the file to its
        resume list; `--redraft` drafts from the new one instead.
        """
        paths = get_paths()
        text = rs.extract_text(file)
        contact = rs.find_contact(text)
        draft = rs.draft_profile(text, contact, file.name)
        warnings = draft.pop("_warnings", [])
        redacted_path = paths.onboarding / f"{file.stem}.redacted.txt"
        has_profile = paths.profile.exists()
        old_draft = read_yaml(paths.profile_draft) if paths.profile_draft.exists() else None
        write_draft = redraft or not has_profile
        draft_kept = write_draft and not redraft and bool(old_draft)
        base = (read_yaml(paths.profile) or {}) if has_profile else (old_draft or {})
        resumes = merge_resumes(base.get("resumes") or [], file.name, label, make_default)
        if draft_kept:
            draft = {**(old_draft or {}), "resumes": resumes}
        else:
            draft["resumes"] = resumes
        entry = next(r for r in resumes if r["file"] == file.name)
        if not dry_run:
            paths.ensure()
            with file_lock(paths.lock):
                dest = paths.resume_dir / file.name
                if file.resolve() != dest.resolve():
                    shutil.copy2(file, dest)
                secure_file(dest)
                redacted_path.write_text(rs.redact(text, contact), encoding="utf-8")
                secure_file(redacted_path)
                cp = server.contact_path(paths)
                cp.write_text(
                    json.dumps(
                        {
                            "name": contact.name,
                            "email": contact.email,
                            "phone": contact.phone,
                            "links": contact.links,
                        }
                    ),
                    encoding="utf-8",
                )
                secure_file(cp)
                if write_draft:
                    write_yaml(
                        paths.profile_draft,
                        draft,
                        "Draft profile from your resume. Tidy it, then `jobhunt profile confirm`.",
                    )
                else:
                    data = read_yaml(paths.profile) or {}
                    data["resumes"] = resumes
                    prof.check(data, str(paths.profile))
                    write_yaml(paths.profile, data, "Your profile: resume content the AI may see.")
        sections = [k for k in ("education", "experience", "projects", "skills") if draft.get(k)]
        ph = find_placeholders(draft)
        nxt = []
        if draft_kept:
            nxt = [
                f"Kept your draft profile and added {file.name} to its resume list. To draft "
                f"from this resume instead, run `jobhunt resume add {file.name} --redraft`.",
            ]
        elif write_draft:
            nxt = [
                f"Read {redacted_path} and tidy {paths.profile_draft} (fill CHANGE_ME, fix "
                "titles/orgs, one-line project summaries). Show it to the user, then run "
                "`jobhunt profile confirm`.",
                "Then run `jobhunt setup form` for preferences and screening answers.",
            ]
            if warnings:
                nxt.insert(1, "Some sections were left out (see warnings): fill them in.")
        if len(resumes) > 1 and not any(r.get("default") for r in resumes):
            nxt.append(
                "No default resume: pick one per job with `jobhunt prefill <id> --resume <file>` "
                "(and `jobhunt applied ... --resume <file>`)."
            )
        return Result(
            data=ResumeReport(
                dry_run=dry_run,
                file=file.name,
                default=bool(entry.get("default")),
                characters=len(text),
                sections=sections,
                contact_found={
                    "name": bool(contact.name),
                    "email": bool(contact.email),
                    "phone": bool(contact.phone),
                    **{f"link_{k}": True for k in contact.links},
                },
                redacted_text=str(redacted_path),
                draft=str(paths.profile_draft) if write_draft else None,
                draft_kept=draft_kept,
                resumes=[r["file"] for r in resumes],
                placeholders=ph,
                warnings=warnings,
            ),
            next_actions=nxt,
        )

    @agent_command("profile confirm", output=ProfileReport, writes=True)
    @click.option(
        "--from", "src", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
        help="File to confirm (default: the draft from `resume add`).",
    )  # fmt: skip
    def profile_confirm(src: Path | None, dry_run: bool) -> Result:
        """Validate the draft profile and make it profile.yaml (after the user has reviewed it)."""
        paths = get_paths()
        src = src or paths.profile_draft
        if not src.exists():
            raise NotFound(
                f"No draft at {src}.", next_actions=["Run `jobhunt resume add <file>` first."]
            )
        data = read_yaml(src) or {}
        if ph := find_placeholders(data):
            raise ConfigError(
                f"The draft still has placeholders at: {', '.join(ph[:10])}",
                details={"placeholders": ph},
                next_actions=[
                    f"Fill or remove those keys in {src} (or `jobhunt profile set --draft`)."
                ],
            )
        model = prof.check(data, str(src))
        if not dry_run:
            paths.ensure()
            with file_lock(paths.lock):
                write_yaml(
                    paths.profile,
                    model.model_dump(mode="json", exclude_none=True),
                    "Your profile: resume content the AI may see. No contact details here.",
                )
        return Result(
            data=ProfileReport(dry_run=dry_run, path=str(paths.profile)),
            next_actions=["Run `jobhunt setup form` for preferences and screening answers."],
        )

    @agent_command("profile set", output=ProfileReport, writes=True)
    @click.argument("path")
    @click.argument("value")
    @click.option("--append", is_flag=True, help="Add VALUE to the list at PATH.")
    @click.option("--draft", is_flag=True, help="Edit the draft instead of profile.yaml.")
    def profile_set(path: str, value: str, append: bool, draft: bool, dry_run: bool) -> Result:
        """Set one profile value, e.g. `profile set experience[0].title "Data Analyst"`.

        Use it in the same turn the user confirms a fact. `[a, b]` sets a list; `null` clears.
        """
        paths = get_paths()
        target = paths.profile_draft if draft else paths.profile
        if not target.exists():
            raise NotFound(
                f"{target} doesn't exist.",
                next_actions=["Run `jobhunt resume add <file>` and `jobhunt profile confirm`."],
            )
        data = read_yaml(target) or {}
        before = _peek(data, path)
        new = prof.set_value(data, path, prof.parse_value(value), append=append)
        if not draft:
            prof.check(new, str(target))
        if not dry_run:
            with file_lock(paths.lock):
                header = (
                    "Draft profile" if draft else "Your profile: resume content the AI may see."
                )
                write_yaml(target, new, header)
        return Result(
            data=ProfileReport(
                dry_run=dry_run,
                path=str(target),
                changed={"path": path, "before": before, "after": _peek(new, path)},
            )
        )

    @agent_command("profile show", output=ProfileReport)
    @click.option("--draft", is_flag=True, help="Show the draft instead of profile.yaml.")
    def profile_show(draft: bool) -> Result:
        """Show profile.yaml (or the draft). It holds no contact details."""
        paths = get_paths()
        target = paths.profile_draft if draft else paths.profile
        if not target.exists():
            raise NotFound(
                f"{target} doesn't exist.", next_actions=["Run `jobhunt resume add <file>`."]
            )
        return Result(
            data=ProfileReport(dry_run=True, path=str(target), profile=read_yaml(target) or {})
        )

    @agent_command("setup form", output=FormStatus, writes=True)
    @click.option("--status", "status_only", is_flag=True, help="Report the form's status.")
    @click.option("--stop", is_flag=True, help="Stop the form server.")
    @click.option("--port", type=int, default=0, help="Port (default: any free port).")
    def setup_form(status_only: bool, stop: bool, port: int, dry_run: bool) -> Result:
        """Start the local form for preferences and screening answers; returns its URL at once.

        Sensitive answers (work authorization, EEO, compensation, ...) are entered only in this
        form, never in chat. Poll with `--status`; the server stops itself after an idle hour.
        """
        from jobhunt.config import onboarding_status

        paths = get_paths()
        if status_only or dry_run:
            st = server.read_state(paths) or {}
            return _form_result(paths, st, onboarding_status(paths), started=False)
        if stop:
            st = server.stop(paths) or {}
            return _form_result(paths, st, onboarding_status(paths), started=False)
        paths.ensure()
        try:
            st = server.launch(paths, port=port)
        except RuntimeError as e:
            raise ValidationFailed(str(e)) from e
        return _form_result(paths, st, onboarding_status(paths), started=True)


def _form_result(
    paths: Paths, st: dict[str, Any], files: dict[str, str], *, started: bool
) -> Result:
    running = server.pid_alive(st.get("pid")) and st.get("status") in ("waiting", "saved")
    data = FormStatus(
        status=st.get("status") or "not_started",
        url=st.get("url") if running else None,
        running=running,
        saved_at=st.get("saved_at"),
        saves=int(st.get("saves") or 0),
        files=files,
    )
    user, nxt = [], []
    if running and data.url:
        user.append(
            f"Open {data.url} in your browser and fill in your preferences and screening "
            "answers (every field is optional; sensitive answers stay on your computer). "
            "Click Save when done."
        )
        nxt.append("Poll `jobhunt setup form --status` until status is `saved`.")
    elif not running and started is False and data.status == "not_started":
        nxt.append("Run `jobhunt setup form` to start the form.")
    if data.status.startswith("saved"):
        nxt.append("Run `jobhunt doctor` to confirm onboarding, then `jobhunt run`.")
    return Result(data=data, user_actions=user, next_actions=nxt)


def _peek(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in prof.parse_path(path):
        try:
            cur = cur[part]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def merge_resumes(
    resumes: list[dict[str, Any]], name: str, label: str | None, make_default: bool | None
) -> list[dict[str, Any]]:
    """The resume list with ``name`` added or updated. ``make_default`` True makes it the only
    default, False makes it not one, None keeps its flag (a first resume becomes the default).
    Other entries keep their labels and flags."""
    old = next((r for r in resumes if r.get("file") == name), None)
    others = [dict(r) for r in resumes if r.get("file") != name]
    if make_default is None:
        default = bool(old.get("default")) if old else not others
    else:
        default = make_default
    if default:
        for r in others:
            r["default"] = False
    entry: dict[str, Any] = {"file": name, "default": default}
    if label or (old and old.get("label")):
        entry["label"] = label or old["label"]  # type: ignore[index]
    return [*others, entry]


def listed_resumes(paths: Paths) -> list[dict[str, Any]]:
    """The profile's resume entries whose files exist."""
    if not paths.profile.exists():
        return []
    data = yaml.safe_load(paths.profile.read_text(encoding="utf-8")) or {}
    return [r for r in data.get("resumes") or [] if (paths.resume_dir / r["file"]).exists()]


def resolve_resume(paths: Paths, given: str) -> Path:
    """A resume named on the command line: a path, or a file name inside ~/.jobhunt/resume."""
    p = Path(given).expanduser()
    if not p.is_absolute() and not p.exists():
        p = paths.resume_dir / given
    if not p.is_file():
        names = ", ".join(r["file"] for r in listed_resumes(paths)) or "none yet"
        raise ValidationFailed(
            f"No resume file {given!r}.",
            next_actions=[f"Pass a file path, or a file name inside {paths.resume_dir} ({names})."],
        )
    return p


def suggest_resume(paths: Paths, posting: str) -> str | None:
    """The listed resume whose text shares the most with the posting (BM25 over the redacted
    texts), when one clearly leads. A suggestion for the user, never applied on its own."""
    from jobhunt.fit.recall import BM25, tokenize

    names, docs = [], []
    for r in listed_resumes(paths):
        red = paths.onboarding / f"{Path(r['file']).stem}.redacted.txt"
        if red.exists():
            names.append(r["file"])
            docs.append(tokenize(red.read_text(encoding="utf-8")))
    if len(names) < 2:
        return None
    scores = BM25(docs).scores(tokenize(posting))
    ranked = sorted(zip(scores, names, strict=True), reverse=True)
    return ranked[0][1] if ranked[0][0] > 0 and ranked[0][0] > ranked[1][0] else None


def default_resume(paths: Paths) -> Path | None:
    """The default resume, or the only one. None when there are several and none is the
    default: the user picks one per job."""
    resumes = listed_resumes(paths)
    pick = next((r for r in resumes if r.get("default")), None)
    if pick is None and len(resumes) == 1:
        pick = resumes[0]
    return paths.resume_dir / pick["file"] if pick else None
