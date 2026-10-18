"""Answer-bank commands: answers show | add | edit | confirm | questions.

Sensitive values never appear in output, and sensitive answers can't be set from here: those go
through the local form (`jobhunt setup form`), so they never pass through the AI chat.
"""

from __future__ import annotations

import re
from typing import Any

import click
import yaml
from pydantic import BaseModel, Field, ValidationError

from jobhunt.bank.keys import BUILTIN, AnswerContext, is_sensitive_label
from jobhunt.bank.match import Matcher, normalize
from jobhunt.cli.framework import Result, agent_command
from jobhunt.config import read_yaml
from jobhunt.errors import NeedsUser, NotFound, OnboardingIncomplete, ValidationFailed
from jobhunt.locking import file_lock
from jobhunt.models import Answers, Profile
from jobhunt.onboarding.form import write_yaml
from jobhunt.paths import Paths, get_paths

FORM_ACTION = "Answer it in the local form: run `jobhunt setup form` and open the URL it prints."


class KeyStatus(BaseModel):
    key: str
    question: str
    answered: bool
    value: str | None = Field(None, description="Shown only when not sensitive.")
    sensitive: bool
    wordings: list[str] = Field(default_factory=list, description="Wordings you confirmed.")
    options: list[str] = Field(default_factory=list)


class AnswersReport(BaseModel):
    builtin: list[KeyStatus]
    custom: list[KeyStatus]
    answered: int
    unanswered: int
    new_questions: int


class BankChange(BaseModel):
    dry_run: bool
    key: str
    action: str
    entry: KeyStatus


class QuestionsReport(BaseModel):
    questions: list[dict[str, Any]]
    total: int


def _load(paths: Paths) -> tuple[dict[str, Any], Answers]:
    if not paths.answers.exists():
        raise OnboardingIncomplete(
            f"{paths.answers} is missing.", next_actions=["Run `jobhunt setup form` first."]
        )
    raw = read_yaml(paths.answers) or {}
    try:
        return raw, Answers.model_validate(raw)
    except ValidationError as e:
        raise ValidationFailed(f"{paths.answers} is invalid: {e.errors()[0]['msg']}") from e


def _save(paths: Paths, raw: dict[str, Any]) -> None:
    Answers.model_validate(raw)
    write_yaml(paths.answers, raw, "Your screening answers. Sensitive: never share.")


def _status(paths: Paths, ans: Answers) -> tuple[list[KeyStatus], list[KeyStatus]]:
    profile = None
    if paths.profile.exists():
        try:
            profile = Profile.model_validate(read_yaml(paths.profile) or {})
        except ValidationError:
            profile = None
    ctx = AnswerContext(profile or Profile(), ans)
    entries = {e.key: e for e in ans.bank}
    builtin = []
    for k, spec in BUILTIN.items():
        if spec.kind == "essay" or k.startswith("files."):
            continue
        v = spec.resolve(ctx)
        e = entries.get(k)
        shown = None
        if v not in (None, "", []) and not spec.sensitive:
            shown = ", ".join(v) if isinstance(v, list) else str(v)
        builtin.append(
            KeyStatus(
                key=k,
                question=spec.question,
                answered=v not in (None, "", []),
                value=shown,
                sensitive=spec.sensitive,
                wordings=e.wordings if e else [],
                options=e.options if e else [],
            )
        )
    custom = [
        KeyStatus(
            key=e.key,
            question=e.question,
            answered=bool(e.answer),
            value=None if e.sensitive else e.answer,
            sensitive=e.sensitive,
            wordings=e.wordings,
            options=e.options,
        )
        for e in ans.bank
        if e.key not in BUILTIN
    ]
    return builtin, custom


def _entry_status(ans: Answers, key: str) -> KeyStatus:
    e = next(x for x in ans.bank if x.key == key)
    spec = BUILTIN.get(key)
    sens = e.sensitive or bool(spec and spec.sensitive)
    return KeyStatus(
        key=key,
        question=e.question,
        answered=bool(e.answer) or bool(spec),
        value=None if sens else e.answer,
        sensitive=sens,
        wordings=e.wordings,
        options=e.options,
    )


def _new_questions(paths: Paths) -> list[dict[str, Any]]:
    if not paths.new_questions.exists():
        return []
    data = yaml.safe_load(paths.new_questions.read_text(encoding="utf-8")) or {}
    return list(data.get("questions") or [])


def register(cli: click.Group) -> None:
    @agent_command(cli, "answers show", output=AnswersReport)
    @click.option("--unanswered", is_flag=True, help="Only questions without an answer.")
    def answers_show(unanswered: bool) -> Result:
        """List the answer bank: which questions are answered (sensitive values hidden)."""
        paths = get_paths()
        _, ans = _load(paths)
        builtin, custom = _status(paths, ans)
        if unanswered:
            builtin = [b for b in builtin if not b.answered]
            custom = [c for c in custom if not c.answered]
        allk = builtin + custom
        return Result(
            data=AnswersReport(
                builtin=builtin,
                custom=custom,
                answered=sum(k.answered for k in allk),
                unanswered=sum(not k.answered for k in allk),
                new_questions=len(_new_questions(paths)),
            )
        )

    @agent_command(cli, "answers add", output=BankChange, writes=True)
    @click.option("--question", required=True, help="The question as the form words it.")
    @click.option("--answer", required=True, help="The user's answer, in their words.")
    @click.option(
        "--option",
        "options",
        multiple=True,
        help="An acceptable dropdown option text (repeatable).",
    )
    @click.option("--key", default=None, help="Stable key (default: derived from the question).")
    def answers_add(
        question: str, answer: str, options: tuple[str, ...], key: str | None, dry_run: bool
    ) -> Result:
        """Save a user-approved answer to a new, non-sensitive question.

        Sensitive questions (work authorization, EEO, salary, ...) are refused: they go
        through the local form so the answer never passes through chat.
        """
        if is_sensitive_label(question):
            raise NeedsUser(
                "That question is sensitive; its answer goes in the local form, not chat.",
                user_actions=[FORM_ACTION],
            )
        paths = get_paths()
        raw, _ = _load(paths)
        key = key or "custom." + (re.sub(r"[^a-z0-9]+", "_", normalize(question))[:48].strip("_"))
        if key in BUILTIN:
            raise ValidationFailed(
                f"{key} is a built-in question answered in the form; use "
                "`jobhunt answers confirm` to map a wording to it."
            )
        bank = raw.setdefault("bank", [])
        if any(e.get("key") == key for e in bank):
            raise ValidationFailed(f"{key} exists; use `jobhunt answers edit {key}`.")
        entry: dict[str, Any] = {
            "key": key,
            "question": question,
            "answer": answer,
            "wordings": [question],
        }
        if options:
            entry["options"] = list(options)
        bank.append(entry)
        if not dry_run:
            with file_lock(paths.lock):
                _save(paths, raw)
        new = Answers.model_validate(raw)
        return Result(
            data=BankChange(dry_run=dry_run, key=key, action="added", entry=_entry_status(new, key))
        )

    @agent_command(cli, "answers edit", output=BankChange, writes=True)
    @click.argument("key")
    @click.option(
        "--answer", default=None, help="New answer (non-sensitive custom questions only)."
    )
    @click.option("--option", "options", multiple=True, help="Replace the acceptable option texts.")
    @click.option("--add-wording", multiple=True, help="Add a confirmed wording.")
    @click.option("--remove-wording", multiple=True, help="Remove a wording.")
    def answers_edit(
        key: str,
        answer: str | None,
        options: tuple[str, ...],
        add_wording: tuple[str, ...],
        remove_wording: tuple[str, ...],
        dry_run: bool,
    ) -> Result:
        """Change a bank entry's answer, option texts or wordings."""
        paths = get_paths()
        raw, _ = _load(paths)
        bank = raw.setdefault("bank", [])
        e = next((x for x in bank if x.get("key") == key), None)
        spec = BUILTIN.get(key)
        if e is None and spec is None:
            raise NotFound(f"No bank entry {key}.", next_actions=["Run `jobhunt answers show`."])
        if e is None:
            e = {"key": key, "question": spec.question, "wordings": []}  # type: ignore[union-attr]
            bank.append(e)
        sensitive = bool(e.get("sensitive")) or bool(spec and spec.sensitive)
        if answer is not None:
            if spec is not None:
                raise ValidationFailed(f"{key} is answered in the form (`jobhunt setup form`).")
            if sensitive:
                raise NeedsUser("That answer is sensitive.", user_actions=[FORM_ACTION])
            e["answer"] = answer
        if options:
            e["options"] = list(options)
        for w in add_wording:
            if w not in e.setdefault("wordings", []):
                e["wordings"].append(w)
        e["wordings"] = [w for w in e.get("wordings") or [] if w not in remove_wording]
        if not dry_run:
            with file_lock(paths.lock):
                _save(paths, raw)
        new = Answers.model_validate(raw)
        return Result(
            data=BankChange(
                dry_run=dry_run, key=key, action="edited", entry=_entry_status(new, key)
            )
        )

    @agent_command(cli, "answers confirm", output=BankChange, writes=True)
    @click.argument("label")
    @click.argument("key")
    def answers_confirm(label: str, key: str, dry_run: bool) -> Result:
        """Record that the user confirmed LABEL is the same question as KEY (reuse its answer).

        Only after the user says so: a new wording never reuses an answer unconfirmed.
        """
        paths = get_paths()
        raw, _ = _load(paths)
        spec = BUILTIN.get(key)
        bank = raw.setdefault("bank", [])
        e = next((x for x in bank if x.get("key") == key), None)
        if e is None and spec is None:
            raise NotFound(f"No question {key}.", next_actions=["Run `jobhunt answers show`."])
        if e is None:
            e = {"key": key, "question": spec.question, "wordings": []}  # type: ignore[union-attr]
            bank.append(e)
        if label not in e.setdefault("wordings", []):
            e["wordings"].append(label)
        if not dry_run:
            with file_lock(paths.lock):
                _save(paths, raw)
        new = Answers.model_validate(raw)
        m = Matcher(new).match(label)
        nxt = ["Run `jobhunt inspect` to re-classify jobs with the new mapping."]
        if m.key != key:
            nxt.insert(
                0, f"Note: the label still resolves to {m.key} by field name or curated wording."
            )
        return Result(
            data=BankChange(
                dry_run=dry_run, key=key, action="confirmed", entry=_entry_status(new, key)
            ),
            next_actions=nxt,
        )

    @agent_command(cli, "answers questions", output=QuestionsReport)
    @click.option("--limit", type=int, default=20, help="How many to list.")
    def answers_questions(limit: int) -> Result:
        """Unanswered questions found on application forms, most frequent first."""
        paths = get_paths()
        qs = _new_questions(paths)
        out = [
            {
                "question": q["label"],
                "jobs": q.get("jobs"),
                "required_in": q.get("required_in"),
                "type": q.get("type"),
                "options": (q.get("options") or [])[:10],
                "suggested_key": q.get("candidate_key"),
                "sensitive": q.get("sensitive"),
            }
            for q in qs[:limit]
        ]
        user = []
        if any(q["sensitive"] for q in out):
            user.append("Some of these are sensitive. " + FORM_ACTION)
        nxt = []
        if any(q["suggested_key"] for q in out):
            nxt.append(
                "For a suggested match, ask the user whether it is the same question; if yes, "
                'run `jobhunt answers confirm "<question>" <key>`.'
            )
        if any(not q["sensitive"] for q in out):
            nxt.append("Ask the user the non-sensitive ones and save with `jobhunt answers add`.")
        return Result(
            data=QuestionsReport(questions=out, total=len(qs)), user_actions=user, next_actions=nxt
        )
