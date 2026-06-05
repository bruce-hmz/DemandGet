from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi_pagination import Params
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Tenant
from app.models.user import User
from app.schemas import TenantCreate, TenantUpdate, TenantOut
from app.api.deps import get_current_active_user, check_role

router = APIRouter()


@router.get("/", response_model=list[TenantOut])
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    result = await db.execute(stmt)
    tenants = result.scalars().all()
    return tenants


@router.patch("/", response_model=TenantOut)
async def update_tenant(
    update_in: TenantUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(check_role("admin")),
):
    stmt = select(Tenant).where(Tenant.id == current_user.tenant_id)
    result = await db.execute(stmt)
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    update_data = update_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(tenant, field, value)

    await db.flush()
    await db.refresh(tenant)
    return tenant
