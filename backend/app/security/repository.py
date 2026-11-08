from ..core.errors import ApplicationError

USER_FIELDS='ID,USERNAME,EMAIL,ROLE_CODE,EMPLOYEE_ID,IS_ACTIVE'

class Repository:
    def __init__(self,connection):self.connection=connection
    def all(self,sql,params=None):
        with self.connection.cursor() as cursor:
            cursor.execute(sql,params or {})
            names=[(x.name if hasattr(x,'name') else x[0]).lower() for x in cursor.description]
            return [dict(zip(names,row)) for row in cursor.fetchall()]
    def one(self,sql,params=None):
        rows=self.all(sql,params)
        if len(rows)>1:raise ApplicationError()
        return rows[0] if rows else None
    def execute(self,sql,params=None):
        with self.connection.cursor() as cursor:
            cursor.execute(sql,params or {})
            return cursor.rowcount
    def insert_id(self,sql,params):
        with self.connection.cursor() as cursor:
            output=cursor.var(int);cursor.execute(sql+' RETURNING ID INTO :new_id',{**params,'new_id':output})
            from ..repositories.common import inserted_id,updated_one
            updated_one(cursor)
            return inserted_id(output)
    def guard(self):
        with self.connection.cursor() as cursor:cursor.callproc('HR_CORE.HRMS_ENTERPRISE.LOCK_ORGANIZATION')
    def user(self,identifier):return self.one('SELECT '+USER_FIELDS+' FROM HR_CORE.HRMS_USER WHERE ID=:id',{'id':identifier})
    def login_user(self,login):return self.one('SELECT '+USER_FIELDS+',PASSWORD_HASH FROM HR_CORE.HRMS_USER WHERE USERNAME=:login OR EMAIL=:login',{'login':login})
    def principal(self,access_hash):
        return self.one('SELECT U.ID,U.USERNAME,U.EMAIL,U.ROLE_CODE,U.EMPLOYEE_ID,S.ID AS SESSION_ID,S.CSRF_HASH '
            'FROM HR_CORE.HRMS_USER U JOIN HR_CORE.HRMS_SESSION S ON S.USER_ID=U.ID '
            'WHERE S.ACCESS_HASH=:hash AND S.REVOKED_AT IS NULL AND S.EXPIRES_AT>SYSTIMESTAMP AND U.IS_ACTIVE=1',{'hash':access_hash})
