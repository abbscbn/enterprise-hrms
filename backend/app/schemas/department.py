from typing import Annotated
from pydantic import AwareDatetime, BaseModel, Field
from .common import InputModel, PatchModel, RecordId

Code = Annotated[str, Field(min_length=1, max_length=30)]
Name = Annotated[str, Field(min_length=1, max_length=150)]


class DepartmentCreate(InputModel):
    department_code: Code
    department_name: Name


class DepartmentUpdate(PatchModel):
    department_name: Name | None = None


class DepartmentRead(BaseModel):
    id: RecordId
    department_code: Code
    department_name: Name
    is_active: bool
    created_at: AwareDatetime


class DepartmentPage(BaseModel):
    items: list[DepartmentRead]
    page: int
    size: int
