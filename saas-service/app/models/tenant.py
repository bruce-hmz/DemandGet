from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    plan = Column(String(50), nullable=False, default="free")
    monthly_budget = Column(Numeric(10, 2), nullable=False, default=0)
    token_limit = Column(Integer, nullable=False, default=100000)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    users = relationship("User", back_populates="tenant")
    members = relationship("Member", back_populates="tenant")
    pipelines = relationship("Pipeline", back_populates="tenant", cascade="all, delete-orphan")
    signals = relationship("Signal", back_populates="tenant")
    clusters = relationship("Cluster", back_populates="tenant")
    experiments = relationship("Experiment", back_populates="tenant")
