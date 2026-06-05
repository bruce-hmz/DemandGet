from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Tenant, Member, UserRole
from app.schemas import UserOut, TenantCreate
from app.api.deps import (
    get_password_hash,
    verify_password,
    create_access_token,
    create_refresh_token,
    get_current_active_user,
    get_current_user,
    oauth2_scheme,
)
from app.config import settings

router = APIRouter()


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: UserOut
    tenant_slug: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None
    tenant_name: str
    tenant_slug: str


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
async def register(
    register_in: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.email == register_in.email)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    stmt = select(Tenant).where(Tenant.slug == register_in.tenant_slug)
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Tenant slug already exists")

    hashed_password = get_password_hash(register_in.password)
    user = User(
        email=register_in.email,
        hashed_password=hashed_password,
        full_name=register_in.full_name,
        role=UserRole.ADMIN,
    )
    db.add(user)
    await db.flush()

    tenant = Tenant(
        name=register_in.tenant_name,
        slug=register_in.tenant_slug,
        plan="free",
        monthly_budget=0,
        token_limit=100000,
    )
    db.add(tenant)
    await db.flush()

    user.tenant_id = tenant.id
    member = Member(tenant_id=tenant.id, user_id=user.id)
    db.add(member)

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": str(user.id), "tenant_id": tenant.id},
        expires_delta=access_token_expires,
    )
    refresh_token = create_refresh_token(
        data={"sub": str(user.id), "tenant_id": tenant.id},
    )

    await db.flush()
    await db.refresh(user)

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user=UserOut.model_validate(user),
        tenant_slug=tenant.slug,
    )


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).where(User.email == form_data.username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": str(user.id), "tenant_id": user.tenant_id},
        expires_delta=access_token_expires,
    )
    refresh_token = create_refresh_token(
        data={"sub": str(user.id), "tenant_id": user.tenant_id},
    )

    tenant_stmt = select(Tenant).where(Tenant.id == user.tenant_id)
    tenant_result = await db.execute(tenant_stmt)
    tenant = tenant_result.scalar_one_or_none()

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        user=UserOut.model_validate(user),
        tenant_slug=tenant.slug if tenant else None,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    refresh_token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(refresh_token, db)

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": str(user.id), "tenant_id": user.tenant_id},
        expires_delta=access_token_expires,
    )
    new_refresh_token = create_refresh_token(
        data={"sub": str(user.id), "tenant_id": user.tenant_id},
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
    )


@router.get("/me", response_model=UserOut)
async def get_me(current_user: User = Depends(get_current_active_user)):
    return current_user
