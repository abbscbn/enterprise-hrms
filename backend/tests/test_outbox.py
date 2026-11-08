import json
import os
import unittest
from unittest.mock import MagicMock, Mock, patch
from backend.app.events.outbox import enqueue_employee_created
from backend.app.events.relay import relay_once
from backend.app.events.broker import topology
from backend.app.services.employee import EmployeeService
from backend.app.schemas.employee import EmployeeCreate
from backend.app.core.errors import CommitOutcomeUnknown
from backend.app.db.session import validate_target_session
from backend.tests.test_crud import EMP,DEPT,CREATE_EMP


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.connect=self.enterContext(patch('oracledb.connect',side_effect=AssertionError('No real DB')))
        self.connection=MagicMock();self.connection.autocommit=False
        self.cursor=self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount=1
        self.enterContext(patch('backend.app.db.transactions.validate_target_session'))

    def test_enqueue_uses_same_connection_and_no_commit(self):
        event_id=enqueue_employee_created(self.connection,12)
        sql,params=self.cursor.execute.call_args.args
        self.assertIn('HR_CORE.HRMS_OUTBOX',sql)
        self.assertNotIn(event_id,sql)
        event=json.loads(params['payload'])
        self.assertEqual(event['employee_id'],12)
        self.assertEqual(set(event),{'event_id','event_type','version','employee_id','occurred_at'})
        self.connection.commit.assert_not_called();self.connection.rollback.assert_not_called()

    def test_employee_and_event_share_transaction(self):
        service=EmployeeService(self.connection);service.departments=Mock();service.employees=Mock()
        service.departments.get.return_value=DEPT;service.employees.create.return_value=10;service.employees.get.return_value=EMP
        events=[]
        with patch.dict(os.environ,{'HRMS_OUTBOX_ENABLED':'1'}),patch('backend.app.services.employee.enqueue_employee_created',side_effect=lambda *args:events.append('outbox')) as enqueue:
            self.connection.commit.side_effect=lambda:events.append('commit')
            service.create(EmployeeCreate(**CREATE_EMP))
        self.assertEqual(events,['outbox','commit']);enqueue.assert_called_once_with(self.connection,10)

    def test_outbox_failure_rolls_back_employee(self):
        service=EmployeeService(self.connection);service.departments=Mock();service.employees=Mock()
        service.departments.get.return_value=DEPT;service.employees.get.return_value=EMP;service.employees.create.return_value=10
        with patch.dict(os.environ,{'HRMS_OUTBOX_ENABLED':'1'}),patch('backend.app.services.employee.enqueue_employee_created',side_effect=RuntimeError()):
            with self.assertRaises(Exception):service.create(EmployeeCreate(**CREATE_EMP))
        self.connection.rollback.assert_called_once();self.connection.commit.assert_not_called()

    def test_target_mismatch_is_rejected(self):
        self.cursor.fetchone.return_value=('OTHER','FREEPDB1','HR_CORE')
        with self.assertRaises(Exception):validate_target_session(self.connection)

    def test_relay_confirmed_publish_precedes_marker_and_commit(self):
        cursor=self.connection.cursor.return_value
        cursor.fetchone.return_value=(1,'event',json.dumps({'event_id':'event'}));cursor.rowcount=1
        channel=Mock();events=[]
        channel.basic_publish.side_effect=lambda **kw:events.append('publish')
        self.connection.commit.side_effect=lambda:events.append('commit')
        with patch('backend.app.events.relay.validate_target_session'):
            self.assertTrue(relay_once(self.connection,channel))
        self.assertEqual(events,['publish','commit'])
        self.assertIn('PUBLISHED_AT=SYSTIMESTAMP',cursor.execute.call_args.args[0])
        self.assertTrue(channel.basic_publish.call_args.kwargs['mandatory'])
        self.assertEqual(channel.basic_publish.call_args.kwargs['properties'].delivery_mode,2)
        cursor.close.assert_called_once()

    def test_relay_commit_failure_never_republishes(self):
        cursor=self.connection.cursor.return_value;cursor.fetchone.return_value=(1,'event','{"event_id":"event"}');cursor.rowcount=1
        channel=Mock();self.connection.commit.side_effect=RuntimeError('unknown')
        with patch('backend.app.events.relay.validate_target_session'),self.assertRaises(RuntimeError):relay_once(self.connection,channel)
        channel.basic_publish.assert_called_once();self.connection.commit.assert_called_once();self.connection.rollback.assert_not_called()

    def test_topology_has_durable_queue_and_dead_letter(self):
        channel=Mock();topology(channel)
        self.assertEqual(channel.queue_declare.call_count,2)
        self.assertTrue(channel.queue_declare.call_args.kwargs['durable'])
        self.assertEqual(channel.queue_declare.call_args.kwargs['arguments']['x-dead-letter-exchange'],'hrms.dead')


class PackageRuleTests(unittest.TestCase):
    def test_package_assignment_is_authoritative_without_duplicate_python_rule(self):
        connection=MagicMock();connection.autocommit=False
        service=EmployeeService(connection);service.departments=Mock();service.employees=Mock()
        service.employees.create.return_value=10;service.employees.get.return_value=EMP
        with patch.dict(os.environ,{'HRMS_PLSQL_ENABLED':'1','HRMS_OUTBOX_ENABLED':'0'}),patch('backend.app.db.transactions.validate_target_session'):
            service.create(EmployeeCreate(**CREATE_EMP))
        service.departments.lock_for_assignment.assert_called_once_with(1,1)
        service.departments.get.assert_not_called();connection.commit.assert_called_once()

    def test_package_deactivation_does_not_run_second_python_count(self):
        from backend.app.services.department import DepartmentService
        connection=MagicMock();connection.autocommit=False
        service=DepartmentService(connection);service.departments=Mock(spec=['get','assert_no_active_employees','deactivate']);service.employees=Mock()
        service.departments.get.return_value=DEPT
        with patch.dict(os.environ,{'HRMS_PLSQL_ENABLED':'1'}),patch('backend.app.db.transactions.validate_target_session'):
            service.deactivate(1)
        service.departments.assert_no_active_employees.assert_called_once_with(1)
        service.employees.has_active.assert_not_called()

    def test_package_custom_error_mapping_is_safe(self):
        import oracledb
        from types import SimpleNamespace
        from backend.app.core.errors import safe_error
        for code,status in ((20001,409),(20002,409),(20003,404)):
            result=safe_error(oracledb.DatabaseError(SimpleNamespace(code=code,message='RAW_SECRET')))
            self.assertEqual(result.status_code,status);self.assertNotIn('RAW_SECRET',str(result))


class RelayHeartbeatTests(unittest.TestCase):
    def test_idle_relay_pumps_heartbeats_instead_of_sleeping(self):
        from backend.app.events import relay
        broker=MagicMock();database=MagicMock()
        with patch.object(relay.pika,'BlockingConnection',return_value=broker),patch.object(relay,'broker_parameters'),patch.object(relay,'create_oracle_connection',return_value=database),patch.object(relay,'relay_once',side_effect=[False,KeyboardInterrupt()]):
            self.assertEqual(relay.main(),1)
        broker.process_data_events.assert_any_call(time_limit=2)
        database.close.assert_called_once()


if __name__=='__main__':unittest.main()
