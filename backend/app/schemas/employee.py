import re
from datetime import date
from typing import Annotated
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from .common import InputModel, PatchModel, RecordId

Number = Annotated[str, Field(min_length=1, max_length=30)]
PersonName = Annotated[str, Field(min_length=1, max_length=100)]
Email = Annotated[str, Field(min_length=1, max_length=254)]


class EmployeeFields(InputModel):
    @field_validator("hire_date", mode="before", check_fields=False)
    @classmethod
    def date_only(cls, value):
        if type(value) is date or (type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)):
            return value
        raise ValueError("HIRE_DATE yalnız YYYY-MM-DD tarihi olabilir.")


class EmployeeCreate(EmployeeFields):
    employee_no: Number
    first_name: PersonName
    last_name: PersonName
    email: Email | None = None
    hire_date: date
    department_id: RecordId


class EmployeeUpdate(PatchModel, EmployeeFields):
    first_name: PersonName | None = None
    last_name: PersonName | None = None
    email: Email | None = None
    hire_date: date | None = None
    department_id: RecordId | None = None


class EmployeeRead(BaseModel):
    id: RecordId
    employee_no: Number
    first_name: PersonName
    last_name: PersonName
    email: Email | None
    hire_date: date
    department_id: RecordId
    is_active: bool
    created_at: AwareDatetime


class EmployeePage(BaseModel):
    items: list[EmployeeRead]
    page: int
    size: int
