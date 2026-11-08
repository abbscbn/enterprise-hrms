"""Sabit v2 şeması: SQL kullanıcı girdisinden üretilmez. V1 parser'ı değişmez."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    nullable: bool = True
    default: str | None = None
    identity: bool = False


@dataclass(frozen=True)
class Table:
    version: int
    name: str
    columns: tuple[Column, ...]
    primary: tuple[str, ...]
    unique: tuple[tuple[str, str], ...] = ()
    foreign: tuple[tuple[str, str, str, str], ...] = ()
    checks: tuple[tuple[str, str], ...] = ()

    @property
    def filename(self):
        return f'V{self.version:04d}__create_{self.name.lower()}.sql'

    def sql(self):
        fields=[]
        for c in self.columns:
            text=f'    {c.name} {c.kind}'
            if c.identity:text+=' GENERATED ALWAYS AS IDENTITY'
            if c.default is not None:text+=' DEFAULT '+c.default
            if not c.nullable and not c.identity:text+=' NOT NULL'
            fields.append(text)
        fields.append(f'    CONSTRAINT PK_{self.name} PRIMARY KEY ({", ".join(self.primary)})')
        fields += [f'    CONSTRAINT {name} UNIQUE ({col})' for name,col in self.unique]
        fields += [f'    CONSTRAINT {name} FOREIGN KEY ({col}) REFERENCES {ref}({target})' for name,col,ref,target in self.foreign]
        fields += [f'    CONSTRAINT {name} CHECK ({condition})' for name,condition in self.checks]
        return 'CREATE TABLE '+self.name+' (\n'+',\n'.join(fields)+'\n);\n'


def C(name,kind,nullable=True,default=None,identity=False):return Column(name,kind,nullable,default,identity)
ID=C('ID','NUMBER(19,0)',False,identity=True)
CREATED=C('CREATED_AT','TIMESTAMP(6) WITH TIME ZONE',False,'SYSTIMESTAMP')
TABLES=(
 Table(5,'HRMS_USER',(ID,C('USERNAME','VARCHAR2(80 CHAR)',False),C('EMAIL','VARCHAR2(254 CHAR)',False),C('PASSWORD_HASH','VARCHAR2(512 CHAR)',False),C('ROLE_CODE','VARCHAR2(20 CHAR)',False),C('EMPLOYEE_ID','NUMBER(19,0)'),C('IS_ACTIVE','NUMBER(1,0)',False,'1'),CREATED),('ID',),
       (('UQ_HRMS_USER_USERNAME','USERNAME'),('UQ_HRMS_USER_EMAIL','EMAIL'),('UQ_HRMS_USER_EMPLOYEE','EMPLOYEE_ID')),
       (('FK_HRMS_USER_EMPLOYEE','EMPLOYEE_ID','EMPLOYEE','ID'),),
       (('CK_HRMS_USER_ROLE',"ROLE_CODE IN ('ADMIN','HR_MANAGER','EMPLOYEE')"),('CK_HRMS_USER_ACTIVE','IS_ACTIVE IN (0,1)'))),
 Table(6,'HRMS_SESSION',(C('ID','VARCHAR2(36 CHAR)',False),C('USER_ID','NUMBER(19,0)',False),C('ACCESS_HASH','VARCHAR2(64 CHAR)',False),C('REFRESH_HASH','VARCHAR2(64 CHAR)',False),C('CSRF_HASH','VARCHAR2(64 CHAR)',False),C('EXPIRES_AT','TIMESTAMP(6) WITH TIME ZONE',False),C('REFRESH_EXPIRES_AT','TIMESTAMP(6) WITH TIME ZONE',False),C('REVOKED_AT','TIMESTAMP(6) WITH TIME ZONE'),CREATED),('ID',),
       (('UQ_HRMS_SESSION_ACCESS','ACCESS_HASH'),('UQ_HRMS_SESSION_REFRESH','REFRESH_HASH')),
       (('FK_HRMS_SESSION_USER','USER_ID','HRMS_USER','ID'),)),
 Table(7,'HRMS_REFRESH_USED',(C('TOKEN_HASH','VARCHAR2(64 CHAR)',False),C('SESSION_ID','VARCHAR2(36 CHAR)',False),CREATED),('TOKEN_HASH',),(),(('FK_HRMS_REFRESH_SESSION','SESSION_ID','HRMS_SESSION','ID'),)),
 Table(8,'HRMS_LOGIN_GUARD',(C('GUARD_KEY','VARCHAR2(64 CHAR)',False),C('ATTEMPTS','NUMBER(10,0)',False),C('WINDOW_AT','TIMESTAMP(6) WITH TIME ZONE',False)),('GUARD_KEY',)),
 Table(9,'HRMS_AUDIT',(ID,C('ACTOR_ID','NUMBER(19,0)'),C('ACTION','VARCHAR2(60 CHAR)',False),C('ENTITY_TYPE','VARCHAR2(30 CHAR)',False),C('ENTITY_ID','NUMBER(19,0)'),C('OUTCOME','VARCHAR2(20 CHAR)',False),C('REQUEST_ID','VARCHAR2(36 CHAR)',False),CREATED),('ID',),(),(('FK_HRMS_AUDIT_ACTOR','ACTOR_ID','HRMS_USER','ID'),)),
 Table(10,'HRMS_FILE',(ID,C('EMPLOYEE_ID','NUMBER(19,0)',False),C('CATEGORY','VARCHAR2(20 CHAR)',False),C('DOCUMENT_TYPE','VARCHAR2(30 CHAR)',False),C('ORIGINAL_NAME','VARCHAR2(255 CHAR)',False),C('STORAGE_KEY','VARCHAR2(80 CHAR)',False),C('MIME_TYPE','VARCHAR2(100 CHAR)',False),C('SIZE_BYTES','NUMBER(10,0)',False),C('CHECKSUM','VARCHAR2(64 CHAR)',False),C('UPLOADED_BY','NUMBER(19,0)',False),C('DESCRIPTION','VARCHAR2(1000 CHAR)'),C('VISIBILITY','VARCHAR2(20 CHAR)',False),C('ARCHIVED_AT','TIMESTAMP(6) WITH TIME ZONE'),CREATED),('ID',),(('UQ_HRMS_FILE_STORAGE','STORAGE_KEY'),),
       (('FK_HRMS_FILE_EMPLOYEE','EMPLOYEE_ID','EMPLOYEE','ID'),('FK_HRMS_FILE_UPLOADER','UPLOADED_BY','HRMS_USER','ID')),
       (('CK_HRMS_FILE_CATEGORY',"CATEGORY IN ('PHOTO','DOCUMENT')"),('CK_HRMS_FILE_VISIBILITY',"VISIBILITY IN ('SELF','HR_ONLY','ADMIN_ONLY')"),('CK_HRMS_FILE_SIZE','SIZE_BYTES > 0'))),
 Table(11,'EMPLOYEE_PROFILE',(C('EMPLOYEE_ID','NUMBER(19,0)',False),C('PERSONAL_EMAIL','VARCHAR2(254 CHAR)'),C('PHONE_NUMBER','VARCHAR2(30 CHAR)'),C('JOB_TITLE','VARCHAR2(150 CHAR)'),C('MANAGER_ID','NUMBER(19,0)'),C('EMPLOYMENT_TYPE','VARCHAR2(20 CHAR)'),C('EMPLOYMENT_STATUS','VARCHAR2(20 CHAR)'),C('WORK_LOCATION','VARCHAR2(150 CHAR)'),C('PHOTO_ID','NUMBER(19,0)'),C('CREATED_BY','NUMBER(19,0)'),C('UPDATED_BY','NUMBER(19,0)'),C('UPDATED_AT','TIMESTAMP(6) WITH TIME ZONE')),('EMPLOYEE_ID',),(),
       (('FK_EMP_PROF_EMPLOYEE','EMPLOYEE_ID','EMPLOYEE','ID'),('FK_EMP_PROF_MANAGER','MANAGER_ID','EMPLOYEE','ID'),('FK_EMP_PROF_PHOTO','PHOTO_ID','HRMS_FILE','ID'),('FK_EMP_PROF_CREATOR','CREATED_BY','HRMS_USER','ID'),('FK_EMP_PROF_UPDATER','UPDATED_BY','HRMS_USER','ID')),
       (('CK_EMP_PROF_SELF','MANAGER_ID <> EMPLOYEE_ID'),('CK_EMP_PROF_TYPE',"EMPLOYMENT_TYPE IN ('FULL_TIME','PART_TIME','CONTRACT','INTERN','UNSPECIFIED')"),('CK_EMP_PROF_STATUS',"EMPLOYMENT_STATUS IN ('ACTIVE','ON_LEAVE','TERMINATED')"))),
 Table(12,'DEPARTMENT_PROFILE',(C('DEPARTMENT_ID','NUMBER(19,0)',False),C('DESCRIPTION','VARCHAR2(1000 CHAR)'),C('MANAGER_ID','NUMBER(19,0)'),C('PARENT_ID','NUMBER(19,0)'),C('CREATED_BY','NUMBER(19,0)'),C('UPDATED_BY','NUMBER(19,0)'),C('UPDATED_AT','TIMESTAMP(6) WITH TIME ZONE')),('DEPARTMENT_ID',),(),
       (('FK_DEP_PROF_DEPARTMENT','DEPARTMENT_ID','DEPARTMENT','ID'),('FK_DEP_PROF_PARENT','PARENT_ID','DEPARTMENT','ID'),('FK_DEP_PROF_MANAGER','MANAGER_ID','EMPLOYEE','ID'),('FK_DEP_PROF_CREATOR','CREATED_BY','HRMS_USER','ID'),('FK_DEP_PROF_UPDATER','UPDATED_BY','HRMS_USER','ID')),
       (('CK_DEP_PROF_SELF','PARENT_ID <> DEPARTMENT_ID'),)),
 Table(13,'HRMS_HIERARCHY_GUARD',(C('ID','NUMBER(1,0)',False),),('ID',),(),(),(('CK_HRMS_GUARD_ID','ID = 1'),)),
)
