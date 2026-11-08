import os
from fastapi import APIRouter,Depends,Request,Response,Query
from .models import Login,PasswordChange,UserCreate,UserUpdate,UserRead
from .service import AuthService,user_read
from .repository import Repository,USER_FIELDS
from .dependencies import get_current_user,require_roles
from ..db.dependencies import get_db_connection
from ..core.errors import Unauthorized,Forbidden

router=APIRouter(tags=['authentication'])


def validate_cookie_configuration():
    if os.environ.get('HRMS_ENV','development')!='development' and os.environ.get('HRMS_COOKIE_SECURE','0')!='1':raise Forbidden()


def set_cookies(response,tokens):
    secure=os.environ.get('HRMS_COOKIE_SECURE','0')=='1'
    if os.environ.get('HRMS_ENV','development')!='development' and not secure:raise Forbidden()
    for name,value,age,path in (('hrms_access',tokens[0],900,'/'),('hrms_refresh',tokens[1],604800,'/'),('hrms_csrf',tokens[2],604800,'/')):
        response.set_cookie(name,value,max_age=age,httponly=name!='hrms_csrf',secure=secure,samesite='strict',path=path)


def clear_cookies(response):
    for name,path in (('hrms_access','/'),('hrms_refresh','/'),('hrms_csrf','/')):response.delete_cookie(name,path=path,samesite='strict')


@router.post('/auth/login',response_model=UserRead)
def login(data:Login,request:Request,response:Response,connection=Depends(get_db_connection,scope='function')):
    validate_cookie_configuration()
    user,tokens=AuthService(connection).login(data,request.state.request_id,request.client.host if request.client else 'unknown')
    set_cookies(response,tokens);return user

@router.post('/auth/refresh',response_model=UserRead)
def refresh(request:Request,response:Response,connection=Depends(get_db_connection,scope='function')):
    validate_cookie_configuration()
    token=request.cookies.get('hrms_refresh','');csrf=request.headers.get('x-csrf-token','')
    if not token or not csrf:raise Unauthorized()
    user,tokens=AuthService(connection).refresh(token,csrf,request.state.request_id)
    set_cookies(response,tokens);return user

@router.get('/auth/me',response_model=UserRead)
def me(actor=Depends(get_current_user)):
    return UserRead(id=actor.id,username=actor.username,email=actor.email,role=actor.role,employee_id=actor.employee_id)

@router.post('/auth/logout')
def logout(response:Response,actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):
    AuthService(connection).logout(actor);clear_cookies(response);return {'status':'ok'}

@router.post('/auth/password')
def password(data:PasswordChange,response:Response,actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):
    AuthService(connection).change_password(actor,data);clear_cookies(response);return {'status':'ok'}

@router.get('/users',response_model=list[UserRead],tags=['users'])
def users(page:int=Query(1,ge=1,le=1000000),size:int=Query(20,ge=1,le=100),actor=Depends(require_roles('ADMIN','HR_MANAGER')),connection=Depends(get_db_connection,scope='function')):
    rows=Repository(connection).all('SELECT '+USER_FIELDS+' FROM HR_CORE.HRMS_USER ORDER BY ID OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY',{'offset':(page-1)*size,'limit':size})
    return [user_read(r) for r in rows]

@router.post('/users',response_model=UserRead,status_code=201,tags=['users'])
def create_user(data:UserCreate,actor=Depends(require_roles('ADMIN')),connection=Depends(get_db_connection,scope='function')):
    return AuthService(connection).create_user(data,actor)

@router.patch('/users/{id}',response_model=UserRead,tags=['users'])
def update_user(id:int,data:UserUpdate,actor=Depends(require_roles('ADMIN')),connection=Depends(get_db_connection,scope='function')):
    return AuthService(connection).update_user(id,data,actor)

@router.get('/audit',tags=['audit'])
def audit_records(page:int=Query(1,ge=1,le=1000000),size:int=Query(20,ge=1,le=100),actor=Depends(require_roles('ADMIN')),connection=Depends(get_db_connection,scope='function')):
    return Repository(connection).all("SELECT ID,ACTOR_ID,ACTION,ENTITY_TYPE,ENTITY_ID,OUTCOME,REQUEST_ID,TO_CHAR(CREATED_AT,'YYYY-MM-DD\"T\"HH24:MI:SS.FF6TZH:TZM') AS CREATED_AT FROM HR_CORE.HRMS_AUDIT ORDER BY ID DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",{'offset':(page-1)*size,'limit':size})
