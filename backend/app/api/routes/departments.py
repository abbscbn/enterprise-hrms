from typing import Annotated
from fastapi import APIRouter, Depends, Path, Query
from ...db.dependencies import get_db_connection
from ...schemas.department import DepartmentCreate, DepartmentUpdate, DepartmentRead, DepartmentPage
from ...services.department import DepartmentService

router = APIRouter(prefix="/departments", tags=["departments"])
Id = Annotated[int, Path(gt=0, le=9999999999999999999)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Size = Annotated[int, Query(ge=1, le=100)]


def department_service(connection=Depends(get_db_connection, scope="function")):
    return DepartmentService(connection)


@router.post("", response_model=DepartmentRead, status_code=201)
def create_department(data: DepartmentCreate, service=Depends(department_service)):
    return service.create(data)


@router.get("", response_model=DepartmentPage)
def list_departments(page: Page = 1, size: Size = 20, service=Depends(department_service)):
    return service.list(page, size)


@router.get("/{id}", response_model=DepartmentRead)
def get_department(id: Id, service=Depends(department_service)):
    return service.get(id)


@router.patch("/{id}", response_model=DepartmentRead)
def update_department(id: Id, data: DepartmentUpdate, service=Depends(department_service)):
    return service.update(id, data)


@router.post("/{id}/deactivate", response_model=DepartmentRead)
def deactivate_department(id: Id, service=Depends(department_service)):
    return service.deactivate(id)
