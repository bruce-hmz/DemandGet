from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False, index=True)
    source_channel = Column(String(100), nullable=False)
    source_url = Column(String(2048), nullable=False)
    raw_quote = Column(Text, nullable=False)
    user_role = Column(String(255))
    pain_category = Column(String(255))
    pain_intensity = Column(Integer)
    implied_task = Column(Text)
    ai_solvable = Column(Boolean)
    monetization_signal = Column(Boolean)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    cluster_id = Column(Integer, ForeignKey("clusters.id"), index=True)

    tenant = relationship("Tenant", back_populates="signals")
    pipeline_run = relationship("PipelineRun", back_populates="signals")

    __table_args__ = (
        UniqueConstraint("tenant_id", "source_url", "raw_quote", name="uq_signal_tenant_url_quote"),
    )


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False, index=True)
    pipeline_run_id = Column(Integer, ForeignKey("pipeline_runs.id"), nullable=False, index=True)
    summary = Column(Text, nullable=False)
    signal_count = Column(Integer, nullable=False, default=0)
    avg_pain_intensity = Column(Numeric(3, 2))
    pay_signal_ratio = Column(Numeric(5, 4))
    ai_fit_ratio = Column(Numeric(5, 4))
    competitor_count = Column(Integer, nullable=False, default=0)
    score = Column(Numeric(5, 4))
    verdict = Column(String(10))
    first_seen = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen = Column(DateTime, nullable=False, default=datetime.utcnow)

    tenant = relationship("Tenant", back_populates="clusters")
    pipeline_run = relationship("PipelineRun", back_populates="clusters")
    experiments = relationship("Experiment", back_populates="cluster")