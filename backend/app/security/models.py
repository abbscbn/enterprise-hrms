from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from ..schemas.common import InputModel, RecordId

Role=Literal['ADMIN','HR_MANAGER','EMPLOYEE']

@dataclass(frozen=True)
class Principal:
    id:int
    username:str
    email:str
    role:Role
    employee_id:int|None=None
    session_id:str|None=None
    request_id:str='00000000-0000-0000-0000-000000000000'

class Login(InputModel):
    login:str=Field(min_length=1,max_length=254)
    password:str=Field(min_length=1,max_length=128)
    @field_validator('password',mode='before')
    @classmethod
    def preserve_password(cls,value):return value
    model_config={'extra':'forbid','str_strip_whitespace':False}

class UserCreate(InputModel):
    username:str=Field(pattern=r'^[a-z][a-z0-9_.-]{2,79}$')
    email:str=Field(min_length=3,max_length=254,pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    password:str=Field(min_length=12,max_length=128)
    role:Role='EMPLOYEE'
    employee_id:RecordId|None=None
    @field_validator('username','email',mode='before')
    @classmethod
    def lower(cls,value):return value.strip().lower() if isinstance(value,str) else value
    @model_validator(mode='after')
    def employee_required(self):
        if self.role=='EMPLOYEE' and self.employee_id is None:raise ValueError('EMPLOYEE için personel bağlantısı gerekir.')
        return self
    @field_validator('password')
    @classmethod
    def nonblank_password(cls,value):
        if not value.strip():raise ValueError('Geçersiz parola.')
        return value
    # Password is never trimmed. Other inputs are normalized explicitly.
    model_config={'extra':'forbid','str_strip_whitespace':False}

class UserUpdate(InputModel):
    role:Role|None=None
    is_active:bool|None=None
    employee_id:RecordId|None=None
    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self,k) is None for k in self.model_fields_set if k!='employee_id'):raise ValueError('Geçersiz kullanıcı değişikliği.')
        return self

class PasswordChange(BaseModel):
    @field_validator('new_password')
    @classmethod
    def nonblank_password(cls,value):
        if not value.strip():raise ValueError('Geçersiz parola.')
        return value
    current_password:str=Field(min_length=1,max_length=128)
    new_password:str=Field(min_length=12,max_length=128)
    model_config={'extra':'forbid'}

class UserRead(BaseModel):
    id:int
    username:str
    email:str
    role:Role
    employee_id:int|None
    is_active:bool=True
