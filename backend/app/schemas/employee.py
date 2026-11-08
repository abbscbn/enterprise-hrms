import re
from datetime import date
from typing import Annotated, Literal, ClassVar
from pydantic import AwareDatetime, BaseModel, Field, field_validator, AliasChoices, model_validator, computed_field
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


class EmployeeProfileFields(EmployeeFields):
    personal_email: Email | None = None
    phone_number: Annotated[str, Field(min_length=1,max_length=30)] | None = None
    job_title: Annotated[str, Field(min_length=1,max_length=150)] | None = None
    manager_id: RecordId | None = None
    work_location: Annotated[str, Field(min_length=1,max_length=150)] | None = None


class EmployeeCreate(EmployeeProfileFields):
    employee_no: Number
    first_name: PersonName
    last_name: PersonName
    email: Email | None = Field(None,validation_alias=AliasChoices("email","corporate_email"))
    hire_date: date
    department_id: RecordId


    employment_type: Literal['FULL_TIME','PART_TIME','CONTRACT','INTERN','UNSPECIFIED'] = 'FULL_TIME'
    employment_status: Literal['ACTIVE','ON_LEAVE'] = 'ACTIVE'


class EmployeeUpdate(PatchModel, EmployeeProfileFields):
    nullable_fields: ClassVar[set[str]] = {'email','personal_email','phone_number','job_title','manager_id','work_location'}
    employment_type: Literal['FULL_TIME','PART_TIME','CONTRACT','INTERN','UNSPECIFIED'] | None = None
    employment_status: Literal['ACTIVE','ON_LEAVE','TERMINATED'] | None = None
    first_name: PersonName | None = None
    last_name: PersonName | None = None
    email: Email | None = Field(None,validation_alias=AliasChoices("email","corporate_email"))
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
    personal_email: str | None = None
    phone_number: str | None = None
    job_title: str | None = None
    manager_id: int | None = None
    employment_type: str = 'UNSPECIFIED'
    employment_status: str = 'ACTIVE'
    work_location: str | None = None
    photo_id: int | None = None
    created_by: int | None = None
    updated_by: int | None = None
    updated_at: AwareDatetime | None = None

    @computed_field
    @property
    def corporate_email(self) -> str | None:
        return self.email

    @model_validator(mode='before')
    @classmethod
    def legacy_status(cls,value):
        if isinstance(value,dict) and 'employment_status' not in value:
            return {**value,'employment_status':'ACTIVE' if value.get('is_active') else 'TERMINATED'}
        return value


class EmployeePage(BaseModel):
    items: list[EmployeeRead]
    page: int
    size: int
