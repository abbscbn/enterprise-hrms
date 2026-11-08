"""Opt-in yerel HR_CORE; yeni etiketli fixture'lar, veri silme/DDL yok."""
import json
import os
import threading
import time
import uuid
import oracledb
from backend.tests.oracle_integration import checked_connection
from backend.app.repositories.department import DepartmentRepository
from backend.app.repositories.employee import EmployeeRepository
from backend.app.services.department import DepartmentService
from backend.app.services.employee import EmployeeService
from backend.app.schemas.department import DepartmentCreate
from backend.app.schemas.employee import EmployeeCreate
from backend.app.core.errors import Conflict


def run():
    if os.environ.get('HRMS_RUN_ORACLE_TESTS') != '1':raise RuntimeError()
    tag='MVPLOCK_'+uuid.uuid4().hex[:10].upper()
    print(json.dumps({'phase':'start','fixture_tag':tag}),flush=True)
    connection=checked_connection()
    try:
        departments=[DepartmentService(connection).create(DepartmentCreate(department_code=tag+s,department_name='Lock fixture '+s)) for s in ('A','B')]
    finally:connection.close()
    def employee(suffix, department):
        return EmployeeCreate(employee_no=tag+suffix,first_name='Integration',last_name='Concurrency',hire_date='2026-01-01',department_id=department)
    # Department wins: create must wait then observe inactive parent.
    connection=checked_connection();ready=threading.Event();result={}
    def waiting_create():
        other=checked_connection()
        try:
            ready.set()
            EmployeeService(other).create(employee('WAIT',departments[0].id))
            result['unexpected']=True
        except Conflict:result['blocked']=True
        except BaseException:result['error']=True
        finally:other.close()
    try:
        DepartmentRepository(connection).get(departments[0].id,for_update=True)
        thread=threading.Thread(target=waiting_create,daemon=True);thread.start()
        if not ready.wait(5):raise RuntimeError()
        time.sleep(.2)
        DepartmentService(connection).deactivate(departments[0].id)
        thread.join(10)
        if thread.is_alive() or result!={'blocked':True}:raise RuntimeError()
    finally:connection.close()
    # Employee wins: hold the commit point while department deactivation waits.
    ready=threading.Event();release=threading.Event();created={};deactivation={}
    class CommitGate:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def commit(self):
            ready.set()
            if not release.wait(5):raise RuntimeError('Commit gate timed out')
            self.conn.commit()
    def create_first():
        conn=checked_connection()
        try:created['record']=EmployeeService(CommitGate(conn)).create(employee('FIRST',departments[1].id))
        except BaseException:created['error']=True
        finally:conn.close()
    def deactivate_second():
        conn=checked_connection()
        try:
            DepartmentService(conn).deactivate(departments[1].id);deactivation['unexpected']=True
        except Conflict:deactivation['blocked']=True
        except BaseException:deactivation['error']=True
        finally:conn.close()
    creator=threading.Thread(target=create_first,daemon=True);creator.start()
    if not ready.wait(5):raise RuntimeError()
    deactivator=threading.Thread(target=deactivate_second,daemon=True);deactivator.start()
    time.sleep(.2);release.set();creator.join(10);deactivator.join(10)
    if creator.is_alive() or deactivator.is_alive() or 'record' not in created or deactivation!={'blocked':True}:raise RuntimeError()
    connection=checked_connection()
    try:
        # Direct SQL test of Oracle FK; definitely missing negative parent verified first.
        with connection.cursor() as cursor:
            cursor.execute('SELECT COUNT(*) FROM HR_CORE.DEPARTMENT WHERE ID=:id',{'id':-1})
            if cursor.fetchone()!=(0,):raise RuntimeError()
        try:
            EmployeeRepository(connection).create(employee('BADFK',1).model_dump() | {'department_id':-1})
        except oracledb.Error as error:
            if getattr(error.args[0],'code',None)!=2291:raise RuntimeError() from None
            connection.rollback()
        else:
            connection.rollback();raise RuntimeError()
        EmployeeService(connection).deactivate(created['record'].id)
        DepartmentService(connection).deactivate(departments[1].id)
    finally:connection.close()
    print(json.dumps({'passed':True,'fixture_tag':tag,'department_wins_assignment_blocked':True,
        'employee_wins_deactivation_blocked':True,'fk_2291_and_rollback_confirmed':True,'test_records_deactivated':True}))


if __name__=='__main__':
    try:run()
    except BaseException:
        print('{"passed":false,"action":"STOP; no retry; inspect tagged fixtures"}')
        raise SystemExit(1) from None
