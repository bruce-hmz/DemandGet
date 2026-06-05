from app.api.routers.auth import router as auth_router
from app.api.routers.pipelines import router as pipelines_router
from app.api.routers.reports import router as reports_router
from app.api.routers.signals import router as signals_router
from app.api.routers.tenants import router as tenants_router
from app.api.routers.users import router as users_router
from app.api.routers.configs import router as configs_router
from app.api.routers.experiments import router as experiments_router

auth = auth_router
pipelines = pipelines_router
reports = reports_router
signals = signals_router
tenants = tenants_router
users = users_router
configs = configs_router
experiments = experiments_router