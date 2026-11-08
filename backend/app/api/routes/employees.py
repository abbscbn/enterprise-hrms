from ...security.dependencies import get_current_user,require_roles
from typing import Annotated,Literal
from fastapi import APIRouter, Depends, Path, Query
from ...db.dependencies import get_db_connection
from ...schemas.employee import EmployeeCreate, EmployeeUpdate, EmployeeRead, EmployeePage
from ...services.employee import EmployeeService

router = APIRouter(prefix="/employees", tags=["employees"])
Id = Annotated[int, Path(gt=0, le=9999999999999999999)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Size = Annotated[int, Query(ge=1, le=100)]


def employee_service(actor=Depends(get_current_user),connection=Depends(get_db_connection, scope="function")):
    return EmployeeService(connection,actor)


@router.post("", response_model=EmployeeRead, status_code=201, dependencies=[Depends(require_roles("ADMIN","HR_MANAGER"))])
def create_employee(data: EmployeeCreate, service=Depends(employee_service)):
    return service.create(data)


@router.get("", response_model=EmployeePage, dependencies=[Depends(require_roles("ADMIN","HR_MANAGER"))])
def list_employees(page: Page = 1, size: Size = 20, q:str|None=Query(None,max_length=100), department_id:int|None=Query(None,gt=0), status:Literal['ACTIVE','ON_LEAVE','TERMINATED']|None=None, service=Depends(employee_service)):
    return service.list(page, size, **{k:v for k,v in {'search':q,'department_id':department_id,'status':status}.items() if v is not None})


@router.get("/{id}", response_model=EmployeeRead)
def get_employee(id: Id, service=Depends(employee_service)):
    return service.get(id)


@router.patch("/{id}", response_model=EmployeeRead)
def update_employee(id: Id, data: EmployeeUpdate, service=Depends(employee_service)):
    return service.update(id, data)


@router.post("/{id}/deactivate", response_model=EmployeeRead, dependencies=[Depends(require_roles("ADMIN","HR_MANAGER"))])
def deactivate_employee(id: Id, service=Depends(employee_service)):
    return service.deactivate(id)
