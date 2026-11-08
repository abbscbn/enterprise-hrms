from fastapi import FastAPI

from .api.routes.health import router as health_router
from .api.routes.departments import router as departments_router
from .api.routes.employees import router as employees_router
from .core.errors import ApplicationError, application_error_handler, internal_error_handler


app = FastAPI(title="Enterprise HRMS Backend")
app.include_router(health_router)
app.include_router(departments_router)
app.include_router(employees_router)
app.add_exception_handler(ApplicationError, application_error_handler)
app.add_exception_handler(Exception, internal_error_handler)
