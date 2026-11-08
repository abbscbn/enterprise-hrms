from fastapi import APIRouter,Depends,UploadFile,File,Form,Query
from fastapi.responses import Response
from urllib.parse import quote
from ..security.dependencies import get_current_user
from ..db.dependencies import get_db_connection
from ..security.repository import Repository
from ..core.errors import NotFound,UploadRejected
from .service import FileService
from .schemas import DocumentType,Visibility,FileRead
from .validation import validate_file,DOCUMENT_LIMIT,PHOTO_LIMIT

router=APIRouter(tags=['private files'])

def content(file,limit):
    data=bytearray()
    try:
        while chunk:=file.file.read(65536):
            data.extend(chunk)
            if len(data)>limit:raise UploadRejected()
        return bytes(data)
    finally:file.file.close()


def service(actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):return FileService(connection,actor)

@router.post('/employees/{id}/photo',response_model=FileRead,status_code=201)
def upload_photo(id:int,file:UploadFile=File(...),files=Depends(service)):
    files.authorize(id,write=True,photo=True)
    validated=validate_file(file.filename,file.content_type,content(file,PHOTO_LIMIT),True)
    return files.upload(id,validated,'OTHER','SELF',None,True)

@router.get('/employees/{id}/photo')
def photo(id:int,files=Depends(service)):
    files.authorize(id)
    row=files.repo.one('SELECT PHOTO_ID FROM HR_CORE.EMPLOYEE_PROFILE WHERE EMPLOYEE_ID=:id',{'id':id})
    if not row or row['photo_id'] is None:raise NotFound()
    metadata,data=files.download(id,row['photo_id'],True)
    return Response(data,media_type=metadata['mime_type'],headers={'Cache-Control':'private, no-store','Content-Security-Policy':"sandbox; default-src 'none'"})

@router.post('/employees/{id}/documents',response_model=FileRead,status_code=201)
def upload_document(id:int,file:UploadFile=File(...),document_type:DocumentType=Form('OTHER'),visibility:Visibility=Form('HR_ONLY'),description:str|None=Form(None,max_length=1000),files=Depends(service)):
    files.authorize(id,write=True)
    validated=validate_file(file.filename,file.content_type,content(file,DOCUMENT_LIMIT))
    return files.upload(id,validated,document_type,visibility,description)

@router.get('/employees/{id}/documents')
def documents(id:int,page:int=Query(1,ge=1,le=1000000),size:int=Query(20,ge=1,le=100),document_type:DocumentType|None=None,files=Depends(service)):
    return files.list(id,page,size,document_type)

@router.get('/employees/{id}/documents/{document_id}',response_model=FileRead)
def metadata(id:int,document_id:int,files=Depends(service)):
    return files.metadata(id,document_id)

@router.get('/employees/{id}/documents/{document_id}/download')
def download(id:int,document_id:int,files=Depends(service)):
    metadata,data=files.download(id,document_id)
    return Response(data,media_type=metadata['mime_type'],headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(metadata['original_name'],safe=''),'Cache-Control':'private, no-store','Content-Security-Policy':"sandbox; default-src 'none'",'X-Frame-Options':'DENY'})

@router.post('/employees/{id}/documents/{document_id}/archive')
def archive(id:int,document_id:int,files=Depends(service)):
    return files.archive(id,document_id)
