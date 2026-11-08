import hmac
from uuid import uuid4
from .repository import Repository,USER_FIELDS
from .passwords import hash_password,verify_password,digest,new_token,DUMMY_HASH
from .models import Principal,UserRead
from ..core.errors import ApplicationError,Conflict,NotFound,Unauthorized,Forbidden,TooManyRequests
from ..db.transactions import transaction
from ..repositories import audit


def user_read(row):
    return UserRead(id=int(row['id']),username=row['username'],email=row['email'],role=row['role_code'],
                    employee_id=int(row['employee_id']) if row['employee_id'] is not None else None,is_active=bool(row['is_active']))

class AuthService:
    def __init__(self,connection):self.connection=connection;self.repo=Repository(connection)

    def _tokens(self,user_id,session_id=None,old_hash=None):
        access,refresh,csrf=new_token(),new_token(),new_token()
        params={'id':session_id or str(uuid4()),'user_id':user_id,'access_hash':digest(access),'refresh':digest(refresh),'csrf':digest(csrf)}
        if session_id:
            self.repo.execute('INSERT INTO HR_CORE.HRMS_REFRESH_USED (TOKEN_HASH,SESSION_ID) VALUES (:hash,:id)',{'hash':old_hash,'id':session_id})
            count=self.repo.execute('UPDATE HR_CORE.HRMS_SESSION SET ACCESS_HASH=:access_hash,REFRESH_HASH=:refresh,CSRF_HASH=:csrf,'
                "EXPIRES_AT=SYSTIMESTAMP+NUMTODSINTERVAL(900,'SECOND') WHERE ID=:id AND USER_ID=:user_id AND REVOKED_AT IS NULL",params)
            if count!=1:raise Unauthorized()
        else:
            self.repo.execute('INSERT INTO HR_CORE.HRMS_SESSION (ID,USER_ID,ACCESS_HASH,REFRESH_HASH,CSRF_HASH,EXPIRES_AT,REFRESH_EXPIRES_AT) '
                "VALUES (:id,:user_id,:access_hash,:refresh,:csrf,SYSTIMESTAMP+NUMTODSINTERVAL(900,'SECOND'),SYSTIMESTAMP+NUMTODSINTERVAL(604800,'SECOND'))",params)
        return access,refresh,csrf

    def login(self,data,request_id,client_key):
        failure=None;result=None
        with transaction(self.connection):
            self.repo.guard()
            # One persistent identity+client bucket; no raw username/IP audit trail.
            key=digest(data.login.strip().lower()+'|'+client_key)
            guard=self.repo.one('SELECT ATTEMPTS,CASE WHEN WINDOW_AT>SYSTIMESTAMP-NUMTODSINTERVAL(900,\'SECOND\') THEN 1 ELSE 0 END AS RECENT '
                'FROM HR_CORE.HRMS_LOGIN_GUARD WHERE GUARD_KEY=:key',{'key':key})
            attempts=int(guard['attempts']) if guard and guard['recent']==1 else 0
            if attempts>=5:
                audit.write(self.connection,None,'LOGIN','USER',outcome='RATE_LIMITED',request_id=request_id);failure=TooManyRequests()
            else:
                row=self.repo.login_user(data.login.strip().lower())
                encoded=row['password_hash'] if row else DUMMY_HASH
                valid=verify_password(encoded,data.password)
                if not row or not valid or row['is_active']!=1:
                    self.repo.execute('MERGE INTO HR_CORE.HRMS_LOGIN_GUARD T USING (SELECT :key AS K FROM DUAL) S ON (T.GUARD_KEY=S.K) '
                        'WHEN MATCHED THEN UPDATE SET T.ATTEMPTS=:attempts,T.WINDOW_AT=CASE WHEN :reset=1 THEN SYSTIMESTAMP ELSE T.WINDOW_AT END '
                        'WHEN NOT MATCHED THEN INSERT (GUARD_KEY,ATTEMPTS,WINDOW_AT) VALUES (:key,:attempts,SYSTIMESTAMP)',
                        {'key':key,'attempts':attempts+1,'reset':int(attempts==0)})
                    audit.write(self.connection,None,'LOGIN','USER',outcome='DENIED',request_id=request_id);failure=Unauthorized()
                else:
                    self.repo.execute('UPDATE HR_CORE.HRMS_LOGIN_GUARD SET ATTEMPTS=0 WHERE GUARD_KEY=:key',{'key':key})
                    tokens=self._tokens(int(row['id']))
                    actor=Principal(int(row['id']),row['username'],row['email'],row['role_code'],row['employee_id'],request_id=request_id)
                    audit.write(self.connection,actor,'LOGIN','USER',actor.id)
                    result=(user_read(row),tokens)
        if failure:raise failure
        return result

    def refresh(self,refresh,csrf,request_id):
        failure=None;result=None
        with transaction(self.connection):
            self.repo.guard()
            hashed=digest(refresh)
            row=self.repo.one('SELECT S.ID,S.USER_ID,S.CSRF_HASH FROM HR_CORE.HRMS_SESSION S JOIN HR_CORE.HRMS_USER U ON U.ID=S.USER_ID '
                'WHERE S.REFRESH_HASH=:hash AND S.REVOKED_AT IS NULL AND S.REFRESH_EXPIRES_AT>SYSTIMESTAMP AND U.IS_ACTIVE=1',{'hash':hashed})
            if row is None:
                used=self.repo.one('SELECT SESSION_ID FROM HR_CORE.HRMS_REFRESH_USED WHERE TOKEN_HASH=:hash',{'hash':hashed})
                if used:self.repo.execute('UPDATE HR_CORE.HRMS_SESSION SET REVOKED_AT=SYSTIMESTAMP WHERE ID=:id',{'id':used['session_id']})
                audit.write(self.connection,None,'REFRESH_REJECTED','SESSION',outcome='DENIED',request_id=request_id);failure=Unauthorized()
            elif not hmac.compare_digest(digest(csrf),row['csrf_hash']):
                failure=Forbidden()
            else:
                user=self.repo.user(row['user_id']);result=(user_read(user),self._tokens(row['user_id'],row['id'],hashed))
        if failure:raise failure
        return result

    def logout(self,actor):
        with transaction(self.connection):
            self.repo.execute('UPDATE HR_CORE.HRMS_SESSION SET REVOKED_AT=SYSTIMESTAMP WHERE ID=:id AND USER_ID=:user_id',{'id':actor.session_id,'user_id':actor.id})
            audit.write(self.connection,actor,'LOGOUT','USER',actor.id)

    def change_password(self,actor,data):
        with transaction(self.connection):
            self.repo.guard()
            row=self.repo.one('SELECT PASSWORD_HASH FROM HR_CORE.HRMS_USER WHERE ID=:id',{'id':actor.id})
            if not row or not verify_password(row['password_hash'],data.current_password):raise Unauthorized()
            self.repo.execute('UPDATE HR_CORE.HRMS_USER SET PASSWORD_HASH=:hash WHERE ID=:id',{'hash':hash_password(data.new_password),'id':actor.id})
            self.repo.execute('UPDATE HR_CORE.HRMS_SESSION SET REVOKED_AT=SYSTIMESTAMP WHERE USER_ID=:id',{'id':actor.id})
            audit.write(self.connection,actor,'PASSWORD_CHANGED','USER',actor.id)

    def create_user(self,data,actor=None,bootstrap=False):
        with transaction(self.connection):
            self.repo.guard()
            if bootstrap:
                count=self.repo.one('SELECT COUNT(*) AS N FROM HR_CORE.HRMS_USER')
                if count['n']!=0 or data.role!='ADMIN':raise Conflict()
            elif actor is None or actor.role!='ADMIN':raise Forbidden()
            identifier=self.repo.insert_id('INSERT INTO HR_CORE.HRMS_USER (USERNAME,EMAIL,PASSWORD_HASH,ROLE_CODE,EMPLOYEE_ID) '
                'VALUES (:username,:email,:hash,:role,:employee_id)',{'username':data.username,'email':data.email,
                'hash':hash_password(data.password),'role':data.role,'employee_id':data.employee_id})
            new_actor=actor or Principal(identifier,data.username,data.email,'ADMIN')
            audit.write(self.connection,new_actor,'USER_CREATED','USER',identifier)
            return user_read(self.repo.user(identifier))

    def update_user(self,identifier,data,actor):
        if actor.role!='ADMIN':raise Forbidden()
        with transaction(self.connection):
            self.repo.guard();current=self.repo.user(identifier)
            if current is None:raise NotFound()
            changes=data.model_dump(exclude_unset=True)
            role=changes.get('role',current['role_code']);active=changes.get('is_active',bool(current['is_active']))
            employee_id=changes.get('employee_id',current['employee_id'])
            if role=='EMPLOYEE' and employee_id is None:raise Conflict()
            if current['role_code']=='ADMIN' and current['is_active']==1 and (role!='ADMIN' or not active):
                count=self.repo.one("SELECT COUNT(*) AS N FROM HR_CORE.HRMS_USER WHERE ROLE_CODE='ADMIN' AND IS_ACTIVE=1")
                if count['n']<=1:raise Conflict()
            self.repo.execute('UPDATE HR_CORE.HRMS_USER SET ROLE_CODE=:role,IS_ACTIVE=:active,EMPLOYEE_ID=:employee_id WHERE ID=:id',
                {'role':role,'active':int(active),'employee_id':employee_id,'id':identifier})
            self.repo.execute('UPDATE HR_CORE.HRMS_SESSION SET REVOKED_AT=SYSTIMESTAMP WHERE USER_ID=:id',{'id':identifier})
            audit.write(self.connection,actor,'USER_UPDATED','USER',identifier)
            return user_read(self.repo.user(identifier))
