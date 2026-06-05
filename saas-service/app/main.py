from collections.abc import AsyncIterator

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_pagination import add_pagination
from pydantic import BaseModel

from app.config import settings
from app.api.routers import auth, pipelines, reports, signals, tenants, users, configs, experiments
from app.database import init_db


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

add_pagination(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": str(exc.status_code),
            "message": exc.detail,
            "details": None,
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "code": "500",
            "message": "Internal server error",
            "details": {"error": str(exc) if settings.app_debug else None},
        },
    )


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": settings.app_version}


app.include_router(auth, prefix="/api/v1/auth", tags=["auth"])
app.include_router(tenants, prefix="/api/v1/tenants", tags=["tenants"])
app.include_router(users, prefix="/api/v1/users", tags=["users"])
app.include_router(pipelines, prefix="/api/v1/pipelines", tags=["pipelines"])
app.include_router(signals, prefix="/api/v1/signals", tags=["signals"])
app.include_router(reports, prefix="/api/v1/reports", tags=["reports"])
app.include_router(configs, prefix="/api/v1/configs", tags=["configs"])
app.include_router(experiments, prefix="/api/v1/experiments", tags=["experiments"])