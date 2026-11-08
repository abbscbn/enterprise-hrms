from typing import Literal
from pydantic import BaseModel

DocumentType=Literal['CV','DIPLOMA','CERTIFICATE','CONTRACT','IDENTITY','OTHER']
Visibility=Literal['SELF','HR_ONLY','ADMIN_ONLY']
class FileRead(BaseModel):
    id:int
    employee_id:int
    category:str
    document_type:str
    original_name:str
    mime_type:str
    size_bytes:int
    checksum:str
    uploaded_by:int
    description:str|None
    visibility:str
    created_at:str
