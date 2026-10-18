"""profile.yaml: resume content the AI may see. No contact details, address or sensitive answers
here; those live in answers.yaml and are filled by code only."""

from __future__ import annotations

from pydantic import Field

from jobhunt.models.base import Strict


class ResumeFile(Strict):
    file: str = Field(description="File name inside ~/.jobhunt/resume/.")
    label: str | None = None
    default: bool = False


class Education(Strict):
    school: str
    degree: str | None = Field(None, description="e.g. BS, BA, MS, PhD")
    majors: list[str] = Field(default_factory=list)
    minors: list[str] = Field(default_factory=list)
    start: str | None = Field(None, description="YYYY-MM")
    graduation: str | None = Field(None, description="Expected or actual, YYYY-MM")
    coursework: list[str] = Field(default_factory=list)
    honors: list[str] = Field(default_factory=list)


class Experience(Strict):
    org: str
    title: str
    start: str | None = None
    end: str | None = Field(None, description="YYYY-MM, or null if current")
    location: str | None = None
    bullets: list[str] = Field(default_factory=list)


class Project(Strict):
    name: str
    summary: str = Field(description="One line.")
    tech: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


class Link(Strict):
    label: str
    url: str


class Profile(Strict):
    version: int = 1
    headline: str | None = None
    resumes: list[ResumeFile] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(
        default_factory=dict, description="Category -> skills, e.g. languages: [Python, SQL]."
    )
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
