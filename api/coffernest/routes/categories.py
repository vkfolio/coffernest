"""/api/v1/categories"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from coffernest.deps import Reader, Session, Writer, owned
from coffernest.enums import CategoryKind
from coffernest.models import Category, CategoryRule, RecurringRule, Transaction

router = APIRouter(prefix="/categories", tags=["categories"])


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: CategoryKind
    parent_id: uuid.UUID | None = None
    is_fixed: bool = False
    color_slot: int = Field(default=8, ge=1, le=8)


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    is_fixed: bool | None = None
    color_slot: int | None = Field(default=None, ge=1, le=8)
    archived: bool | None = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    kind: CategoryKind
    is_fixed: bool
    color_slot: int
    archived: bool


@router.get("", response_model=list[CategoryOut])
async def list_categories(ctx: Reader, session: Session) -> list[Category]:
    rows = await session.scalars(
        select(Category)
        .where(Category.household_id == ctx.household_id)
        .order_by(Category.parent_id.nulls_first(), Category.name)
    )
    return list(rows.all())


@router.post("", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryIn, ctx: Writer, session: Session) -> Category:
    if body.parent_id is not None:
        parent = await owned(session, Category, body.parent_id, ctx)
        if parent.parent_id is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Categories are two levels deep")
    category = Category(household_id=ctx.household_id, **body.model_dump())
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "That category already exists") from exc
    return category


class MergeIn(BaseModel):
    into_id: uuid.UUID


class MergeOut(BaseModel):
    moved: int


@router.post("/{category_id}/merge", response_model=MergeOut)
async def merge_category(
    category_id: uuid.UUID, body: MergeIn, ctx: Writer, session: Session
) -> MergeOut:
    """Move every entry, repeating entry and sorting rule into another category, then archive
    this one (and its sub-categories, whose entries move too)."""
    source = await owned(session, Category, category_id, ctx)
    target = await owned(session, Category, body.into_id, ctx)
    if source.id == target.id or target.parent_id == source.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick a different category to merge into")
    children = await session.scalars(select(Category.id).where(Category.parent_id == source.id))
    ids = [source.id, *children.all()]
    moved = await session.execute(
        update(Transaction).where(Transaction.category_id.in_(ids)).values(category_id=target.id)
    )
    for model in (RecurringRule, CategoryRule):
        await session.execute(
            update(model).where(model.category_id.in_(ids)).values(category_id=target.id)
        )
    await session.execute(update(Category).where(Category.id.in_(ids)).values(archived=True))
    await session.commit()
    return MergeOut(moved=getattr(moved, "rowcount", 0) or 0)


@router.patch("/{category_id}", response_model=CategoryOut)
async def patch_category(
    category_id: uuid.UUID, body: CategoryPatch, ctx: Writer, session: Session
) -> Category:
    category = await owned(session, Category, category_id, ctx)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(category, key, value)
    await session.commit()
    return category
