from fastapi import APIRouter, Depends, HTTPException
from fastapi_pagination import Params
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.signal import Signal
from app.models.user import User
from app.api.deps import get_current_active_user

router = APIRouter()


@router.get("/")
async def list_signals(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List all signals for current tenant."""
    stmt = select(Signal).where(Signal.tenant_id == current_user.tenant_id)
    result = await db.execute(stmt)
    signals = result.scalars().all()
    return signals