from typing import Annotated
from fastapi import APIRouter, Depends, Path, Query
from ...db.dependencies import get_db_connection
from ...schemas.employee import EmployeeCreate, EmployeeUpdate, EmployeeRead, EmployeePage
from ...services.employee import EmployeeService

router = APIRouter(prefix="/employees", tags=["employees"])
Id = Annotated[int, Path(gt=0, le=9999999999999999999)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Size = Annotated[int, Query(ge=1, le=100)]


def employee_service(connection=Depends(get_db_connection, scope="function")):
    return EmployeeService(connection)


@router.post("", response_model=EmployeeRead, status_code=201)
def create_employee(data: EmployeeCreate, service=Depends(employee_service)):
    return service.create(data)


@router.get("", response_model=EmployeePage)
def list_employees(page: Page = 1, size: Size = 20, service=Depends(employee_service)):
    return service.list(page, size)


@router.get("/{id}", response_model=EmployeeRead)
def get_employee(id: Id, service=Depends(employee_service)):
    return service.get(id)


@router.patch("/{id}", response_model=EmployeeRead)
def update_employee(id: Id, data: EmployeeUpdate, service=Depends(employee_service)):
    return service.update(id, data)


@router.post("/{id}/deactivate", response_model=EmployeeRead)
def deactivate_employee(id: Id, service=Depends(employee_service)):
    return service.deactivate(id)
