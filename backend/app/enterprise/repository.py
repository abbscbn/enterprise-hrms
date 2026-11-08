from ..security.repository import Repository

EMPLOYEE_FIELDS={'personal_email':'PERSONAL_EMAIL','phone_number':'PHONE_NUMBER','job_title':'JOB_TITLE','employment_type':'EMPLOYMENT_TYPE','employment_status':'EMPLOYMENT_STATUS','work_location':'WORK_LOCATION'}
DEPARTMENT_FIELDS={'description':'DESCRIPTION','manager_id':'MANAGER_ID'}
ALL_EMP=('personal_email','phone_number','job_title','manager_id','employment_type','employment_status','work_location','photo_id','created_by','updated_by','updated_at')
ALL_DEP=('description','manager_id','parent_id','created_by','updated_by','updated_at')

class ProfileRepository(Repository):
    def lock(self):
        with self.connection.cursor() as cursor:cursor.callproc('HR_CORE.HRMS_ENTERPRISE.LOCK_ORGANIZATION')
    def set_manager(self,identifier,manager):
        with self.connection.cursor() as cursor:cursor.callproc('HR_CORE.HRMS_ENTERPRISE.SET_EMPLOYEE_MANAGER',[identifier,manager])
    def set_parent(self,identifier,parent):
        with self.connection.cursor() as cursor:cursor.callproc('HR_CORE.HRMS_ENTERPRISE.SET_DEPARTMENT_PARENT',[identifier,parent])
    def assert_manager(self,manager):
        with self.connection.cursor() as cursor:cursor.callproc('HR_CORE.HRMS_ENTERPRISE.ASSERT_MANAGER',[manager])
    def load(self,kind,identifier):
        table,key,fields=('EMPLOYEE_PROFILE','EMPLOYEE_ID',ALL_EMP) if kind=='EMPLOYEE' else ('DEPARTMENT_PROFILE','DEPARTMENT_ID',ALL_DEP)
        projection=','.join("TO_CHAR(UPDATED_AT,'YYYY-MM-DD\"T\"HH24:MI:SS.FF6TZH:TZM') AS UPDATED_AT" if f=='updated_at' else f.upper() for f in fields)
        return self.one('SELECT '+projection+f' FROM HR_CORE.{table} WHERE {key}=:id',{'id':identifier}) or {}
    def merge(self,kind,identifier,values,actor_id,creating=False):
        table,key,allowed=('EMPLOYEE_PROFILE','EMPLOYEE_ID',EMPLOYEE_FIELDS) if kind=='EMPLOYEE' else ('DEPARTMENT_PROFILE','DEPARTMENT_ID',DEPARTMENT_FIELDS)
        selected={name:value for name,value in values.items() if name in allowed}
        assignments=[f'T.{allowed[name]}=:{name}' for name in selected]+['T.UPDATED_AT=SYSTIMESTAMP','T.UPDATED_BY=:actor']
        if creating:assignments.append('T.CREATED_BY=:creator')
        columns=[key]+[allowed[name] for name in selected]+['CREATED_BY','UPDATED_BY','UPDATED_AT']
        bind_values=[':id']+[':'+name for name in selected]+[':creator',':actor','SYSTIMESTAMP']
        self.execute(f'MERGE INTO HR_CORE.{table} T USING (SELECT :id AS K FROM DUAL) S ON (T.{key}=S.K) '
            'WHEN MATCHED THEN UPDATE SET '+','.join(assignments)+' WHEN NOT MATCHED THEN INSERT ('+','.join(columns)+') VALUES ('+','.join(bind_values)+')',
            {**selected,'id':identifier,'actor':actor_id,'creator':actor_id if creating else None})
