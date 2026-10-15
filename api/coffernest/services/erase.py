"""Deleting an account or a whole household, for good.

The database does the heavy lifting: every household table cascades from ``household`` and every
session/token table from ``user_account``. Rows are removed with Core ``DELETE`` statements
because the ORM relationships have no ``passive_deletes`` and would try to null the foreign keys
first. Files in object storage don't cascade, so callers remove each household's prefix after the
transaction commits (``storage_prefixes``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from coffernest.enums import Role
from coffernest.models import Household, LoginThrottle, Member, User


class EraseError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def storage_prefix(household_id: uuid.UUID) -> str:
    return f"{household_id}/"


async def erase_household(session: AsyncSession, household_id: uuid.UUID) -> None:
    """Remove the household and everything in it. Members who belonged nowhere else lose their
    account too (it could no longer sign in); the rest are signed out everywhere."""
    member_users = (
        await session.scalars(select(Member.user_id).where(Member.household_id == household_id))
    ).all()
    elsewhere = set(
        (
            await session.scalars(
                select(Member.user_id).where(
                    Member.user_id.in_(member_users), Member.household_id != household_id
                )
            )
        ).all()
    )
    orphans = [uid for uid in member_users if uid not in elsewhere]
    await session.execute(delete(Household).where(Household.id == household_id))
    if orphans:
        await _erase_users(session, orphans)
    if elsewhere:
        await session.execute(
            update(User).where(User.id.in_(elsewhere)).values(token_version=User.token_version + 1)
        )


async def erase_account(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Delete one person's account. A household they're the only member of goes with it.
    Returns the households removed (their files still need deleting)."""
    memberships = (await session.scalars(select(Member).where(Member.user_id == user_id))).all()
    doomed: list[uuid.UUID] = []
    for member in memberships:
        others = await session.scalar(
            select(func.count())
            .select_from(Member)
            .where(Member.household_id == member.household_id, Member.user_id != user_id)
        )
        if not others:
            doomed.append(member.household_id)
            continue
        if member.role == Role.owner:
            other_owners = await session.scalar(
                select(func.count())
                .select_from(Member)
                .where(
                    Member.household_id == member.household_id,
                    Member.user_id != user_id,
                    Member.role == Role.owner,
                )
            )
            if not other_owners:
                raise EraseError(
                    "You're the only owner of a household others still use. Make someone else "
                    "an owner first, or delete the household."
                )
    for household_id in doomed:
        await session.execute(delete(Household).where(Household.id == household_id))
    await _erase_users(session, [user_id])
    return doomed


async def _erase_users(session: AsyncSession, user_ids: list[uuid.UUID]) -> None:
    emails = (await session.scalars(select(User.email).where(User.id.in_(user_ids)))).all()
    if emails:
        await session.execute(
            delete(LoginThrottle).where(LoginThrottle.key.in_([f"acct:{e}" for e in emails]))
        )
    await session.execute(delete(User).where(User.id.in_(user_ids)))
