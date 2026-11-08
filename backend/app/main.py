from fastapi import FastAPI

from .api.routes.health import router as health_router


app = FastAPI(title="Enterprise HRMS Backend")
app.include_router(health_router)
