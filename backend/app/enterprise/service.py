import os
from ..core.errors import Forbidden,Conflict
from ..security.dependencies import own_employee,deny
from ..repositories import audit
from .repository import ProfileRepository,ALL_EMP,ALL_DEP

EMP_BASE={'employee_no','first_name','last_name','email','hire_date','department_id'}
DEP_BASE={'department_code','department_name'}

def enabled():return os.environ.get('HRMS_ENTERPRISE_ENABLED','0')=='1'

def split_values(kind,values):
    base=EMP_BASE if kind=='EMPLOYEE' else DEP_BASE
    return {k:v for k,v in values.items() if k in base},{k:v for k,v in values.items() if k not in base}

def authorize(connection,actor,identifier,changes=None):
    if actor is None:return
    own_employee(connection,actor,identifier)
    if actor.role=='EMPLOYEE' and changes is not None and not set(changes).issubset({'personal_email','phone_number'}):deny(connection,actor,entity='EMPLOYEE',identifier=identifier)

def enrich(connection,kind,row):
    if not enabled() or row is None:return row
    profile=ProfileRepository(connection).load(kind,row['id'])
    if kind=='EMPLOYEE':
        profile['employment_type']=profile.get('employment_type') or 'UNSPECIFIED'
        profile['employment_status']=profile.get('employment_status') or ('ACTIVE' if row['is_active'] else 'TERMINATED')
    return {**row,**profile}

def save(connection,kind,identifier,values,actor,creating=False):
    if not enabled():return
    repo=ProfileRepository(connection)
    if kind=='EMPLOYEE' and 'manager_id' in values:repo.set_manager(identifier,values['manager_id'])
    if kind=='DEPARTMENT':
        if 'manager_id' in values:repo.assert_manager(values['manager_id'])
        if 'parent_id' in values:repo.set_parent(identifier,values['parent_id'])
    repo.merge(kind,identifier,values,actor.id if actor else None,creating)

def lock(connection):
    if enabled():ProfileRepository(connection).lock()

def record(connection,actor,action,kind,identifier):
    if enabled():audit.write(connection,actor,action,kind,identifier)
