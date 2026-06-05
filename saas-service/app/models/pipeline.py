from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    config_json = Column(JSON, nullable=False, default=dict)
    schedule_cron = Column(String(100))
    last_run_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="pipelines")
    runs = relationship("PipelineRun", back_populates="pipeline", cascade="all, delete-orphan")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True, index=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=False, index=True)
    status = Column(String(50), nullable=False, default="pending")
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    docs_fetched = Column(Integer, nullable=False, default=0)
    signals_extracted = Column(Integer, nullable=False, default=0)
    llm_tokens = Column(Integer, nullable=False, default=0)
    cost = Column(Integer, nullable=False, default=0)
    error = Column(Text)
    task_id = Column(String(100), index=True)

    pipeline = relationship("Pipeline", back_populates="runs")
    signals = relationship("Signal", back_populates="pipeline_run", cascade="all, delete-orphan")
    clusters = relationship("Cluster", back_populates="pipeline_run", cascade="all, delete-orphan")