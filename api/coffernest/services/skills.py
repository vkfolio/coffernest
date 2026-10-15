"""Financial-planning skills: Markdown playbooks the assistant loads on demand.

Built-in skills live in ``coffernest/skills/<name>/SKILL.md`` (YAML-ish frontmatter + body).
A household can switch any of them off, or paste its own (stored in ``skill_setting``).
The model only sees the index (name + description) up front and calls ``load_skill`` for the
body, so twenty skills cost a few hundred tokens until one is needed.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.models import SkillSetting

ROOT = Path(__file__).resolve().parent.parent / "skills"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,78}$")


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str
    body: str
    tools: tuple[str, ...] = ()
    as_of: str = ""
    builtin: bool = True
    shared: bool = False  # reference material (india-facts), always available, not toggled
    enabled: bool = True
    last_used_at: datetime | None = None
    references: dict[str, str] = field(default_factory=dict)


def parse(text: str, *, builtin: bool = False) -> Skill:
    """Parse a SKILL.md. Raises SkillError with a message the owner can act on."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise SkillError("A skill starts with a --- frontmatter block (name, description)")
    end = text.find("\n---", 4)
    if end < 0:
        raise SkillError("The frontmatter block isn't closed with ---")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    body = text[end + 4 :].lstrip("-").strip()
    name = meta.get("name", "")
    if not _NAME.match(name):
        raise SkillError("name must be lowercase letters, digits and dashes")
    description = meta.get("description", "")
    if len(description) < 10:
        raise SkillError("Add a description that says when to use the skill")
    if len(body) < 20:
        raise SkillError("The skill needs instructions below the frontmatter")
    tools = tuple(
        t.strip().strip('"').strip("'")
        for t in meta.get("tools", "").strip("[]").split(",")
        if t.strip()
    )
    return Skill(
        name=name,
        title=meta.get("title") or name.replace("-", " ").capitalize(),
        description=description[:600],
        body=body[:40_000],
        tools=tools,
        as_of=meta.get("as_of", ""),
        builtin=builtin,
    )


@lru_cache
def builtin_skills() -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    if not ROOT.exists():
        return out
    for path in sorted(ROOT.glob("*/SKILL.md")):
        skill = parse(path.read_text(encoding="utf-8"), builtin=True)
        refs = {
            p.stem: p.read_text(encoding="utf-8")
            for p in sorted((path.parent / "references").glob("*.md"))
        }
        out[skill.name] = replace(skill, references=refs)
    for path in sorted((ROOT / "_shared").glob("*.md")):
        skill = parse(path.read_text(encoding="utf-8"), builtin=True)
        out[skill.name] = replace(skill, shared=True)
    return out


async def household_skills(session: AsyncSession, household_id: uuid.UUID) -> list[Skill]:
    rows = {
        r.name: r
        for r in (
            await session.scalars(
                select(SkillSetting).where(SkillSetting.household_id == household_id)
            )
        ).all()
    }
    out: list[Skill] = []
    for name, skill in builtin_skills().items():
        row = rows.get(name)
        out.append(
            replace(
                skill,
                enabled=row.enabled if row is not None else True,
                last_used_at=row.last_used_at if row is not None else None,
            )
        )
    for name, row in rows.items():
        if row.custom_body is None or name in builtin_skills():
            continue
        try:
            skill = parse(row.custom_body)
        except SkillError:
            continue
        out.append(replace(skill, enabled=row.enabled, last_used_at=row.last_used_at))
    return out


async def enabled_skills(session: AsyncSession, household_id: uuid.UUID) -> list[Skill]:
    return [s for s in await household_skills(session, household_id) if s.enabled or s.shared]


def index_text(skills: list[Skill]) -> str:
    """The short list the model sees in its system prompt."""
    lines = [f"- {s.name}: {s.description}" for s in skills if not s.shared]
    shared = [s.name for s in skills if s.shared]
    if shared:
        lines.append(f"- {', '.join(shared)}: current Indian tax, rate and rule facts with sources")
    return "\n".join(lines)


async def load(session: AsyncSession, household_id: uuid.UUID, name: str) -> Skill | None:
    wanted = name.strip().lower()
    for skill in await enabled_skills(session, household_id):
        if skill.name == wanted:
            return skill
    return None


async def mark_used(session: AsyncSession, household_id: uuid.UUID, names: list[str]) -> None:
    if not names:
        return
    now = datetime.now(UTC)
    rows = {
        r.name: r
        for r in (
            await session.scalars(
                select(SkillSetting).where(
                    SkillSetting.household_id == household_id, SkillSetting.name.in_(names)
                )
            )
        ).all()
    }
    for name in names:
        row = rows.get(name)
        if row is None:
            session.add(SkillSetting(household_id=household_id, name=name, last_used_at=now))
        else:
            row.last_used_at = now
    await session.commit()
