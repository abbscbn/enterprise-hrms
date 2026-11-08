from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .enterprise.routes import router as enterprise_router
from .storage.routes import router as storage_router
from .security.routes import router as security_router
from .core.middleware import RequestSecurityMiddleware

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

app.add_middleware(RequestSecurityMiddleware)
app.include_router(security_router)

app.include_router(storage_router)

@app.exception_handler(RequestValidationError)
async def safe_validation_error(request,error):
    return JSONResponse(status_code=422,content={'detail':[{'loc':list(e['loc']),'msg':e['msg'],'type':e['type']} for e in error.errors()]})

app.include_router(enterprise_router)
