from datetime import datetime
from typing import Any, Optional, Generic, TypeVar

from pydantic import BaseModel, EmailStr

ModelType = TypeVar("ModelType")


class BaseSchema(BaseModel):
    model_config = {"from_attributes": True}


class TenantBase(BaseSchema):
    name: str
    slug: str
    plan: str = "free"


class TenantCreate(TenantBase):
    monthly_budget: float = 0
    token_limit: int = 100000


class TenantUpdate(BaseSchema):
    name: Optional[str] = None
    plan: Optional[str] = None
    monthly_budget: Optional[float] = None
    token_limit: Optional[int] = None


class TenantOut(TenantBase):
    id: int
    monthly_budget: float
    token_limit: int
    created_at: datetime

    class Config:
        from_attributes = True


class UserBase(BaseSchema):
    email: EmailStr
    full_name: Optional[str] = None
    role: str = "viewer"


class UserCreate(UserBase):
    password: str


class UserUpdate(BaseSchema):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class UserOut(UserBase):
    id: int
    tenant_id: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class MemberOut(BaseSchema):
    user: UserOut
    join_date: datetime


class PipelineBase(BaseSchema):
    name: str
    description: Optional[str] = None
    config_json: dict[str, Any] = {}
    schedule_cron: Optional[str] = None


class PipelineCreate(PipelineBase):
    pass


class PipelineUpdate(BaseSchema):
    name: Optional[str] = None
    description: Optional[str] = None
    config_json: Optional[dict[str, Any]] = None
    schedule_cron: Optional[str] = None


class PipelineOut(PipelineBase):
    id: int
    tenant_id: int
    last_run_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class PipelineRunBase(BaseSchema):
    status: str = "pending"
    docs_fetched: int = 0
    signals_extracted: int = 0
    llm_tokens: int = 0
    cost: float = 0.0
    error: Optional[str] = None


class PipelineRunOut(PipelineRunBase):
    id: int
    pipeline_id: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    task_id: Optional[str]

    class Config:
        from_attributes = True


class SignalBase(BaseSchema):
    source_channel: str
    source_url: str
    raw_quote: str
    user_role: Optional[str] = None
    pain_category: Optional[str] = None
    pain_intensity: Optional[int] = None
    implied_task: Optional[str] = None
    ai_solvable: Optional[bool] = None
    monetization_signal: Optional[bool] = None


class SignalCreate(SignalBase):
    pass


class SignalOut(SignalBase):
    id: int
    tenant_id: int
    pipeline_run_id: int
    fetched_at: datetime
    cluster_id: Optional[int]

    class Config:
        from_attributes = True


class ClusterBase(BaseSchema):
    summary: str
    signal_count: int = 0
    avg_pain_intensity: Optional[float] = None
    pay_signal_ratio: Optional[float] = None
    ai_fit_ratio: Optional[float] = None
    competitor_count: int = 0
    score: Optional[float] = None
    verdict: Optional[str] = None


class ClusterCreate(ClusterBase):
    pass


class ClusterOut(ClusterBase):
    id: int
    tenant_id: int
    pipeline_run_id: int
    first_seen: datetime
    last_seen: datetime

    class Config:
        from_attributes = True


class ExperimentBase(BaseSchema):
    status: str = "proposed"
    landing_url: Optional[str] = None
    spend_usd: float = 0.0
    ctr: Optional[float] = None
    conversion_rate: Optional[float] = None
    notes: Optional[str] = None


class ExperimentCreate(ExperimentBase):
    pass


class ExperimentUpdate(BaseSchema):
    status: Optional[str] = None
    landing_url: Optional[str] = None
    spend_usd: Optional[float] = None
    ctr: Optional[float] = None
    conversion_rate: Optional[float] = None
    notes: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None


class ExperimentOut(ExperimentBase):
    id: int
    tenant_id: int
    cluster_id: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]

    class Config:
        from_attributes = True