from typing import Annotated, ClassVar
from pydantic import AwareDatetime, BaseModel, Field
from .common import InputModel, PatchModel, RecordId

Code = Annotated[str, Field(min_length=1, max_length=30)]
Name = Annotated[str, Field(min_length=1, max_length=150)]


class DepartmentProfileFields(InputModel):
    description: Annotated[str,Field(max_length=1000)] | None = None
    manager_id: RecordId | None = None
    parent_id: RecordId | None = None


class DepartmentCreate(DepartmentProfileFields):
    department_code: Code
    department_name: Name


class DepartmentUpdate(PatchModel,DepartmentProfileFields):
    nullable_fields: ClassVar[set[str]] = {'description','manager_id','parent_id'}
    department_name: Name | None = None


class DepartmentRead(BaseModel):
    id: RecordId
    department_code: Code
    department_name: Name
    is_active: bool
    created_at: AwareDatetime
    description: str | None = None
    manager_id: int | None = None
    parent_id: int | None = None
    created_by: int | None = None
    updated_by: int | None = None
    updated_at: AwareDatetime | None = None


class DepartmentPage(BaseModel):
    items: list[DepartmentRead]
    page: int
    size: int
