"""/api/v1/skills: planning playbooks the assistant follows; toggle them or add your own."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from coffernest.deps import Owner, Reader, Session, Writer
from coffernest.models import SkillSetting
from coffernest.services import skills as svc

router = APIRouter(prefix="/skills", tags=["skills"])


class SkillOut(BaseModel):
    name: str
    title: str
    description: str
    tools: list[str]
    as_of: str
    builtin: bool
    shared: bool
    enabled: bool
    last_used_at: datetime | None


class SkillDetail(SkillOut):
    body: str
    references: list[str]


class TogglePatch(BaseModel):
    enabled: bool


class CustomIn(BaseModel):
    body: str = Field(min_length=40, max_length=40_000, description="A whole SKILL.md")


def _out(s: svc.Skill) -> SkillOut:
    return SkillOut(
        name=s.name,
        title=s.title,
        description=s.description,
        tools=list(s.tools),
        as_of=s.as_of,
        builtin=s.builtin,
        shared=s.shared,
        enabled=s.enabled,
        last_used_at=s.last_used_at,
    )


@router.get("", response_model=list[SkillOut])
async def list_skills(ctx: Reader, session: Session) -> list[SkillOut]:
    return [_out(s) for s in await svc.household_skills(session, ctx.household_id)]


@router.get("/{name}", response_model=SkillDetail)
async def get_skill(name: str, ctx: Reader, session: Session) -> SkillDetail:
    for s in await svc.household_skills(session, ctx.household_id):
        if s.name == name:
            return SkillDetail(**_out(s).model_dump(), body=s.body, references=sorted(s.references))
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No skill with that name")


async def _row(session: Session, household_id: object, name: str) -> SkillSetting | None:
    return await session.scalar(
        select(SkillSetting).where(
            SkillSetting.household_id == household_id, SkillSetting.name == name
        )
    )


@router.patch("/{name}", response_model=SkillOut)
async def toggle(name: str, body: TogglePatch, ctx: Writer, session: Session) -> SkillOut:
    known = {s.name: s for s in await svc.household_skills(session, ctx.household_id)}
    if name not in known:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No skill with that name")
    if known[name].shared:
        raise HTTPException(status.HTTP_409_CONFLICT, "Reference facts are always available")
    row = await _row(session, ctx.household_id, name)
    if row is None:
        row = SkillSetting(household_id=ctx.household_id, name=name)
        session.add(row)
    row.enabled = body.enabled
    await session.commit()
    for s in await svc.household_skills(session, ctx.household_id):
        if s.name == name:
            return _out(s)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No skill with that name")


@router.post("/custom", response_model=SkillOut, status_code=201)
async def add_custom(body: CustomIn, ctx: Owner, session: Session) -> SkillOut:
    try:
        skill = svc.parse(body.body)
    except svc.SkillError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if skill.name in svc.builtin_skills():
        raise HTTPException(status.HTTP_409_CONFLICT, "A built-in skill already uses that name")
    row = await _row(session, ctx.household_id, skill.name)
    if row is None:
        row = SkillSetting(household_id=ctx.household_id, name=skill.name)
        session.add(row)
    row.custom_body = body.body
    row.enabled = True
    await session.commit()
    return _out(svc.Skill(**{**skill.__dict__, "builtin": False}))


@router.delete("/custom/{name}", status_code=204)
async def delete_custom(name: str, ctx: Owner, session: Session) -> None:
    row = await _row(session, ctx.household_id, name)
    if row is None or row.custom_body is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No custom skill with that name")
    await session.delete(row)
    await session.commit()
