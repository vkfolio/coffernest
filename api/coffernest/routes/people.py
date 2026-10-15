"""/api/v1/people — who an entry is for: you, your spouse, each child. Kids need no login."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.deps import Reader, Session, Writer, owned
from coffernest.enums import Relation
from coffernest.models import Member, Person
from coffernest.services import live

router = APIRouter(prefix="/people", tags=["people"])


class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    relation: Relation = Relation.child
    color_slot: int = Field(default=8, ge=1, le=8)


class PersonPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    relation: Relation | None = None
    color_slot: int | None = Field(default=None, ge=1, le=8)
    archived: bool | None = None


class PersonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    relation: Relation
    member_id: uuid.UUID | None
    color_slot: int
    archived: bool


async def person_for_member(
    session: AsyncSession, member: Member, display_name: str, relation: Relation
) -> Person:
    """The person a member signs in as. Does not commit."""
    name = (display_name.strip().split() or ["Me"])[0][:40]
    taken = await session.scalar(
        select(Person.id).where(Person.household_id == member.household_id, Person.name == name)
    )
    person = Person(
        household_id=member.household_id,
        name=name if taken is None else f"{name} {str(member.id)[:4]}",
        relation=relation,
        member_id=member.id,
    )
    session.add(person)
    await session.flush()
    return person


@router.get("", response_model=list[PersonOut])
async def list_people(ctx: Reader, session: Session) -> list[Person]:
    rows = await session.scalars(
        select(Person).where(Person.household_id == ctx.household_id).order_by(Person.created_at)
    )
    return list(rows.all())


@router.post("", response_model=PersonOut, status_code=201)
async def create_person(body: PersonIn, ctx: Writer, session: Session) -> Person:
    person = Person(household_id=ctx.household_id, **body.model_dump())
    person.name = person.name.strip()
    session.add(person)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "There's already someone with that name"
        ) from exc
    live.publish(ctx.household_id)
    return person


@router.patch("/{person_id}", response_model=PersonOut)
async def patch_person(
    person_id: uuid.UUID, body: PersonPatch, ctx: Writer, session: Session
) -> Person:
    person = await owned(session, Person, person_id, ctx)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(person, key, value.strip() if isinstance(value, str) else value)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "There's already someone with that name"
        ) from exc
    live.publish(ctx.household_id)
    return person
