import hmac
from fastapi import Depends,Request,Security
from fastapi.security import APIKeyCookie
from .models import Principal
from .passwords import digest
from .repository import Repository
from ..db.dependencies import get_db_connection
from ..core.errors import Unauthorized,Forbidden
from ..db.transactions import transaction
from ..db.session import validate_target_session
from ..repositories import audit


SESSION_COOKIE=APIKeyCookie(name='hrms_access',auto_error=False)

def cookie_token(token=Security(SESSION_COOKIE)):
    token=token or ''
    if not 32<=len(token)<=128:raise Unauthorized()
    return token


def get_current_user(request:Request,token=Depends(cookie_token),connection=Depends(get_db_connection,scope='function')):
    validate_target_session(connection)
    row=Repository(connection).principal(digest(token))
    if not row or row['role_code'] not in ('ADMIN','HR_MANAGER','EMPLOYEE'):raise Unauthorized()
    actor=Principal(int(row['id']),row['username'],row['email'],row['role_code'],int(row['employee_id']) if row['employee_id'] is not None else None,row['session_id'],request.state.request_id)
    request.state.actor=actor
    if request.method not in ('GET','HEAD','OPTIONS'):
        value=request.headers.get('x-csrf-token','')
        if not value or not hmac.compare_digest(digest(value),row['csrf_hash']):
            deny(connection,actor,'CSRF_REJECTED')
    return actor


def deny(connection,actor,action='ACCESS_DENIED',entity='API',identifier=None):
    with transaction(connection):audit.write(connection,actor,action,entity,identifier,'DENIED')
    raise Forbidden()


def require_roles(*roles):
    def dependency(actor=Depends(get_current_user),connection=Depends(get_db_connection,scope='function')):
        if actor.role not in roles:deny(connection,actor)
        return actor
    return dependency


def own_employee(connection,actor,identifier):
    if actor.role=='EMPLOYEE' and actor.employee_id!=identifier:deny(connection,actor,entity='EMPLOYEE',identifier=identifier)
