from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str = "dev-secret-key-change-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    database_url: str = "postgresql+asyncpg://chuhai_user:chuhai_pass@postgres:5432/chuhai"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"
    cors_origins_list: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    app_name: str = "web-chuhai-agent-saas"
    app_version: str = "0.1.0"
    app_debug: bool = True
    llm_price_in: float = 0.0
    llm_price_out: float = 0.0
    llm_monthly_budget: float = 0.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
