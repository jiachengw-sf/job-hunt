"""A form spec: every field an application form asks for, as read from the ATS."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from jobhunt.discovery.text import html_to_text

FieldType = Literal[
    "text",
    "email",
    "phone",
    "textarea",
    "select",
    "multiselect",
    "radio",
    "checkbox",
    "boolean",
    "file",
    "location",
    "date",
    "number",
    "url",
]
Section = Literal["main", "eeo", "survey", "consent"]


class FormField(BaseModel):
    id: str = Field(description="The ATS's field name or id. Differs per job; never reuse.")
    label: str
    description: str | None = None
    type: FieldType
    required: bool = False
    options: list[str] = Field(default_factory=list)
    max_length: int | None = None
    section: Section = "main"


class FormSpec(BaseModel):
    ats: str
    url: str | None = None
    fields: list[FormField] = Field(default_factory=list)
    captcha_at_submit: bool = Field(False, description="A CAPTCHA guards the Submit button.")
    blocked: str | None = Field(
        None, description="Why the form can't be read or used without the human (login, ...)."
    )
    description: str | None = Field(None, description="Posting text read with the form.")
    pay: dict[str, object] | None = None


class FormError(Exception):
    """The form couldn't be read. ``status`` goes to form_specs.status."""

    def __init__(self, message: str, status: str = "error") -> None:
        super().__init__(message)
        self.status = status


def one_line(raw: Any) -> str:
    """A label as one line of plain text."""
    return " ".join((html_to_text(str(raw)) or "").split()) if raw else ""
