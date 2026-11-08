from ...security.dependencies import get_current_user,require_roles
from typing import Annotated
from fastapi import APIRouter, Depends, Path, Query
from ...db.dependencies import get_db_connection
from ...schemas.department import DepartmentCreate, DepartmentUpdate, DepartmentRead, DepartmentPage
from ...services.department import DepartmentService

router = APIRouter(prefix="/departments", tags=["departments"])
Id = Annotated[int, Path(gt=0, le=9999999999999999999)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Size = Annotated[int, Query(ge=1, le=100)]


def department_service(actor=Depends(get_current_user),connection=Depends(get_db_connection, scope="function")):
    return DepartmentService(connection,actor)


@router.post("", response_model=DepartmentRead, status_code=201, dependencies=[Depends(require_roles("ADMIN","HR_MANAGER"))])
def create_department(data: DepartmentCreate, service=Depends(department_service)):
    return service.create(data)


@router.get("", response_model=DepartmentPage)
def list_departments(page: Page = 1, size: Size = 20, q:str|None=Query(None,max_length=100), is_active:bool|None=None, service=Depends(department_service)):
    return service.list(page, size, **{k:v for k,v in {'search':q,'is_active':is_active}.items() if v is not None})


@router.get("/{id}", response_model=DepartmentRead)
def get_department(id: Id, service=Depends(department_service)):
    return service.get(id)


@router.patch("/{id}", response_model=DepartmentRead, dependencies=[Depends(require_roles("ADMIN","HR_MANAGER"))])
def update_department(id: Id, data: DepartmentUpdate, service=Depends(department_service)):
    return service.update(id, data)


@router.post("/{id}/deactivate", response_model=DepartmentRead, dependencies=[Depends(require_roles("ADMIN","HR_MANAGER"))])
def deactivate_department(id: Id, service=Depends(department_service)):
    return service.deactivate(id)
