from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.schemas import UserCreate, UserUpdate, UserOut
from app.api.deps import get_current_active_user, check_role

router = APIRouter()


@router.get("/", response_model=list[UserOut])
async def list_users(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    """List all users in the current tenant."""
    stmt = select(User).where(User.tenant_id == current_user.tenant_id)
    result = await db.execute(stmt)
    users = result.scalars().all()
    return users


@router.get("/me", response_model=UserOut)
async def get_current_user(
    current_user: User = Depends(get_current_active_user),
):
    """Return the current authenticated user."""
    return current_user


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    """Get a user by ID (admin only)."""
    stmt = select(User).where(
        User.id == user_id,
        User.tenant_id == current_user.tenant_id,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    update_in: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    """Update a user (admin only)."""
    stmt = select(User).where(
        User.id == user_id,
        User.tenant_id == current_user.tenant_id,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = update_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    await db.flush()
    await db.refresh(user)
    return user


@router.delete("/{user_id}")
async def deactivate_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    """Deactivate a user (admin only)."""
    stmt = select(User).where(
        User.id == user_id,
        User.tenant_id == current_user.tenant_id,
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    await db.flush()
    return {"detail": "User deactivated"}
