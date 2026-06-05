from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi_pagination import Params
from fastapi_pagination.limit_offset import LimitOffsetPage
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.api.deps import get_current_active_user, check_role

router = APIRouter()


class TemplateOut(BaseModel):
    id: str
    industry: str
    name: str
    description: str
    config_template: dict[str, Any]
    is_system: bool = True
    created_at: datetime

    class Config:
        from_attributes = True


class TemplateCreate(BaseModel):
    industry: str
    name: str
    description: str
    config_template: dict[str, Any]


PREDEFINED_TEMPLATES = [
    {
        "id": "saas",
        "industry": "software",
        "name": "SaaS B2B",
        "description": "SaaS business model signals for B2B products",
        "config_template": {
            "vertical": "saas",
            "keywords": ["SaaS", "B2B", "enterprise software", "cloud", "subscription"],
            "channels": ["twitter", "reddit", "stackoverflow", "indiehackers"],
            "target_audience": "business",
        },
    },
    {
        "id": "ecommerce",
        "industry": "ecommerce",
        "name": "E-commerce",
        "description": "E-commerce product and market signals",
        "config_template": {
            "vertical": "ecommerce",
            "keywords": ["ecommerce", "online store", "shopping cart", "payment", "checkout"],
            "channels": ["twitter", "reddit", "producthunt", "hackernews"],
            "target_audience": "consumer",
        },
    },
    {
        "id": "fintech",
        "industry": "fintech",
        "name": "Fintech",
        "description": "Financial technology and payment solutions",
        "config_template": {
            "vertical": "fintech",
            "keywords": ["fintech", "payment", "banking", "crypto", "defi"],
            "channels": ["twitter", "reddit", "hackernews"],
            "target_audience": "both",
        },
    },
    {
        "id": "healthtech",
        "industry": "healthtech",
        "name": "HealthTech",
        "description": "Healthcare technology and digital health",
        "config_template": {
            "vertical": "healthtech",
            "keywords": ["healthtech", "digital health", "telemedicine", "ehr", "medical"],
            "channels": ["twitter", "reddit", "hackernews"],
            "target_audience": "business",
        },
    },
    {
        "id": "edtech",
        "industry": "edtech",
        "name": "EdTech",
        "description": "Educational technology and learning platforms",
        "config_template": {
            "vertical": "edtech",
            "keywords": ["edtech", "online learning", "education", "lms", "e-learning"],
            "channels": ["twitter", "reddit", "hackernews", "indiehackers"],
            "target_audience": "consumer",
        },
    },
]


@router.get("/templates", response_model=list[TemplateOut])
async def get_templates(
    industry: Optional[str] = None,
):
    templates = [t for t in PREDEFINED_TEMPLATES if industry is None or t["industry"] == industry]
    return [
        TemplateOut(
            id=t["id"],
            industry=t["industry"],
            name=t["name"],
            description=t["description"],
            config_template=t["config_template"],
            is_system=True,
            created_at=datetime.utcnow(),
        )
        for t in templates
    ]


@router.post("/templates", response_model=TemplateOut, status_code=201)
async def create_template(
    template_in: TemplateCreate,
    current_user: User = Depends(check_role("admin")),
):
    for existing in PREDEFINED_TEMPLATES:
        if existing["id"] == template_in.name.lower().replace(" ", "_"):
            raise HTTPException(status_code=400, detail="Template with this name already exists")
    
    return TemplateOut(
        id=template_in.name.lower().replace(" ", "_"),
        industry=template_in.industry,
        name=template_in.name,
        description=template_in.description,
        config_template=template_in.config_template,
        is_system=False,
        created_at=datetime.utcnow(),
    )


@router.get("/presets/{industry}")
async def get_preset(industry: str):
    preset = next((t for t in PREDEFINED_TEMPLATES if t["industry"] == industry), None)
    if not preset:
        raise HTTPException(status_code=404, detail="Industry preset not found")
    return {
        "industry": industry,
        "preset": preset,
    }