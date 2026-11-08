"""Yalnız sentetik cursor/connection ve dependency override; canlı Oracle yasak."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch, call

import oracledb
from fastapi.testclient import TestClient
from pydantic import ValidationError
from backend.app.main import app
from backend.app.core import config
from backend.app.core.errors import (ApplicationError, NotFound, Conflict, UniqueConflict, ConnectionCleanupError,
    ReferenceConflict, LockConflict, CommitOutcomeUnknown, TransactionCleanupError, safe_error)
from backend.app.db.dependencies import get_db_connection
from backend.app.db.transactions import transaction
from backend.app.schemas.department import DepartmentCreate, DepartmentUpdate
from backend.app.schemas.employee import EmployeeCreate, EmployeeUpdate
from backend.app.repositories.department import DepartmentRepository
from backend.app.repositories.employee import EmployeeRepository
from backend.app.repositories.common import record, inserted_id
from backend.app.services.department import DepartmentService
from backend.app.services.employee import EmployeeService

STAMP = '2026-11-08T13:14:15.123456+03:00'
DEPT_ROW = (1, 'ENG', 'Engineering', 1, STAMP)
EMP_ROW = (10, 'E001', 'Ada', 'Lovelace', None, datetime(2026, 1, 2), 1, 1, STAMP)
DEPT = dict(id=1, department_code='ENG', department_name='Engineering', is_active=True,
            created_at=datetime.fromisoformat(STAMP))
EMP = dict(id=10, employee_no='E001', first_name='Ada', last_name='Lovelace', email=None,
           hire_date=date(2026, 1, 2), department_id=1, is_active=True, created_at=datetime.fromisoformat(STAMP))
CREATE_EMP = dict(employee_no='E001', first_name='Ada', last_name='Lovelace', email=None,
                  hire_date='2026-01-02', department_id=1)


def driver_error(code=1):
    return oracledb.DatabaseError(SimpleNamespace(code=code, message='RAW_PASSWORD_SQL_DSN'))


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.real_connect = self.enterContext(patch('oracledb.connect', side_effect=AssertionError('Gerçek Oracle yasak')))
        self.settings = self.enterContext(patch.object(config, 'load_dotenv', side_effect=AssertionError('.env okuma yasak')))
        self.connection = MagicMock()
        self.connection.autocommit = False
        self.cursor = MagicMock()
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.rowcount = 1
        self.cursor.var.return_value.getvalue.return_value = [10]
        self.connection.cursor.return_value = self.cursor

    def tearDown(self):
        self.real_connect.assert_not_called()
        self.settings.assert_not_called()


class SchemaTests(OfflineCase):
    def test_department_create_trims_strings(self):
        result = DepartmentCreate(department_code=' ENG ', department_name=' Engineering ')
        self.assertEqual(result.model_dump(), {'department_code': 'ENG', 'department_name': 'Engineering'})

    def test_employee_create_trims_and_keeps_nullable_email(self):
        result = EmployeeCreate(**{**CREATE_EMP, 'first_name': ' Ada ', 'email': None})
        self.assertEqual(result.first_name, 'Ada')
        self.assertIsNone(result.email)
        self.assertEqual(result.hire_date, date(2026, 1, 2))

    def test_required_strings_reject_empty_and_whitespace(self):
        for field in ('employee_no', 'first_name', 'last_name'):
            for value in ('', '  '):
                with self.subTest(field=field), self.assertRaises(ValidationError):
                    EmployeeCreate(**{**CREATE_EMP, field: value})
        for field in ('department_code', 'department_name'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                DepartmentCreate(**{**{'department_code': 'ENG', 'department_name': 'Engineering'}, field: ' '})

    def test_column_length_limits(self):
        for field, length in (('employee_no', 30), ('first_name', 100), ('last_name', 100), ('email', 254)):
            EmployeeCreate(**{**CREATE_EMP, field: 'x' * length})
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EmployeeCreate(**{**CREATE_EMP, field: 'x' * (length + 1)})
        for field, length in (('department_code', 30), ('department_name', 150)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                DepartmentCreate(**{**{'department_code': 'ENG', 'department_name': 'Engineering'}, field: 'x' * (length + 1)})

    def test_hire_date_rejects_time_datetime_and_epoch(self):
        for value in ('2026-01-02T00:00:00', datetime(2026, 1, 2), 1767312000, True, '2026-02-30'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EmployeeCreate(**{**CREATE_EMP, 'hire_date': value})

    def test_patch_omitted_email_and_explicit_null_differ(self):
        self.assertEqual(EmployeeUpdate(first_name='Ada').model_dump(exclude_unset=True), {'first_name': 'Ada'})
        self.assertEqual(EmployeeUpdate(email=None).model_dump(exclude_unset=True), {'email': None})

    def test_required_patch_fields_cannot_be_null(self):
        for field in ('first_name', 'last_name', 'hire_date', 'department_id'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EmployeeUpdate(**{field: None})
        with self.assertRaises(ValidationError):
            DepartmentUpdate(department_name=None)

    def test_empty_patches_rejected(self):
        for model in (DepartmentUpdate, EmployeeUpdate):
            with self.subTest(model=model), self.assertRaises(ValidationError):
                model()

    def test_create_rejects_identity_audit_and_active_fields(self):
        for field, value in (('id', 1), ('created_at', STAMP), ('is_active', False)):
            for model, payload in ((EmployeeCreate, CREATE_EMP),
                (DepartmentCreate, {'department_code': 'ENG', 'department_name': 'Engineering'})):
                with self.subTest(model=model, field=field), self.assertRaises(ValidationError):
                    model(**{**payload, field: value})

    def test_patch_rejects_immutable_fields(self):
        for field, value in (('employee_no', 'X'), ('is_active', False), ('id', 1), ('created_at', STAMP)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                EmployeeUpdate(**{field: value})
        with self.assertRaises(ValidationError):
            DepartmentUpdate(department_code='X')

    def test_department_id_is_strict_positive_number19(self):
        for value in (True, '1', 0, -1, 10 ** 19):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                EmployeeCreate(**{**CREATE_EMP, 'department_id': value})


class TransactionTests(OfflineCase):
    def test_commit_once_on_success(self):
        with transaction(self.connection):
            pass
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()

    def test_rollback_before_commit_on_application_error(self):
        with self.assertRaises(Conflict), transaction(self.connection):
            raise Conflict()
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_unique_and_fk_errors_are_sanitized_after_rollback(self):
        for code, kind in ((1, UniqueConflict), (2291, ReferenceConflict), (2292, ReferenceConflict),
                           (54, LockConflict), (60, LockConflict), (30006, LockConflict), (942, ApplicationError)):
            self.connection.reset_mock()
            with self.subTest(code=code), self.assertRaises(kind) as caught, transaction(self.connection):
                raise driver_error(code)
            self.assertNotIn('RAW_PASSWORD_SQL_DSN', str(caught.exception))
            self.connection.rollback.assert_called_once()
            self.connection.commit.assert_not_called()

    def test_commit_failure_is_unknown_without_rollback_or_retry(self):
        self.connection.commit.side_effect = driver_error(3113)
        work = Mock()
        with self.assertRaises(CommitOutcomeUnknown), transaction(self.connection):
            work()
        work.assert_called_once()
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()

    def test_commit_interrupts_also_produce_unknown(self):
        for error in (KeyboardInterrupt(), SystemExit(), BaseException()):
            self.connection.reset_mock()
            self.connection.commit.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(CommitOutcomeUnknown), transaction(self.connection):
                pass
            self.connection.commit.assert_called_once()
            self.connection.rollback.assert_not_called()

    def test_body_interruption_rolls_back_without_commit(self):
        for error in (KeyboardInterrupt(), SystemExit()):
            self.connection.reset_mock()
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)), transaction(self.connection):
                raise error
            self.connection.rollback.assert_called_once()
            self.connection.commit.assert_not_called()

    def test_rollback_failure_has_distinct_safe_error(self):
        self.connection.rollback.side_effect = driver_error(3113)
        with self.assertRaises(TransactionCleanupError), transaction(self.connection):
            raise Conflict()
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_autocommit_must_be_false(self):
        for value in (True, None, 0):
            self.connection.autocommit = value
            work = Mock()
            with self.subTest(value=value), self.assertRaises(ApplicationError), transaction(self.connection):
                work()
            work.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()


class RepositoryTests(OfflineCase):
    def test_department_insert_returning_and_bound_values(self):
        repo = DepartmentRepository(self.connection)
        self.assertEqual(repo.create({'department_code': "X' OR 1=1", 'department_name': 'Name'}), 10)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn('RETURNING ID INTO :new_id', sql)
        self.assertNotIn("X' OR", sql)
        self.assertNotIn('MAX(', sql)
        self.assertNotIn('ISEQ', sql)
        self.assertEqual(params['department_code'], "X' OR 1=1")
        self.assertIs(params['new_id'], self.cursor.var.return_value)
        self.cursor.var.assert_called_once_with(int)
        self.cursor.__exit__.assert_called_once_with(None, None, None)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()

    def test_employee_insert_omits_identity_and_audit_and_maps_email_null(self):
        values = EmployeeCreate(**CREATE_EMP).model_dump()
        self.assertEqual(EmployeeRepository(self.connection).create(values), 10)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn('RETURNING ID INTO :new_id', sql)
        self.assertNotIn('CREATED_AT', sql)
        self.assertNotIn('IS_ACTIVE', sql)
        self.assertIsNone(params['email'])
        self.assertIs(type(params['hire_date']), date)

    def test_department_row_mapping_preserves_timestamp_offset(self):
        self.cursor.fetchone.return_value = DEPT_ROW
        result = DepartmentRepository(self.connection).get(1)
        self.assertTrue(result['is_active'])
        self.assertEqual(result['created_at'].utcoffset(), timedelta(hours=3))
        sql, params = self.cursor.execute.call_args.args
        self.assertIn('TO_CHAR(CREATED_AT', sql)
        self.assertNotIn('SELECT *', sql)
        self.assertEqual(params, {'id': 1})

    def test_employee_date_and_optional_email_mapping(self):
        self.cursor.fetchone.return_value = EMP_ROW
        result = EmployeeRepository(self.connection).get(10)
        self.assertIs(type(result['hire_date']), date)
        self.assertIsNone(result['email'])
        self.assertEqual(result['department_id'], 1)

    def test_list_queries_are_bound_and_ordered(self):
        for cls, row in ((DepartmentRepository, DEPT_ROW), (EmployeeRepository, EMP_ROW)):
            self.cursor.fetchall.return_value = [row]
            self.assertEqual(len(cls(self.connection).list(3, 5)), 1)
            sql, params = self.cursor.execute.call_args.args
            self.assertIn('ORDER BY ID OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY', sql)
            self.assertEqual(params, {'offset': 10, 'limit': 5})
            self.assertNotIn('SELECT *', sql)
        self.connection.commit.assert_not_called()

    def test_empty_list_and_missing_get(self):
        for cls in (DepartmentRepository, EmployeeRepository):
            self.cursor.fetchall.return_value = []
            self.cursor.fetchone.return_value = None
            self.assertEqual(cls(self.connection).list(1, 20), [])
            self.assertIsNone(cls(self.connection).get(9))

    def test_row_locks_have_bounded_wait(self):
        self.cursor.fetchone.return_value = DEPT_ROW
        DepartmentRepository(self.connection).get(1, for_update=True)
        self.assertTrue(self.cursor.execute.call_args.args[0].endswith('FOR UPDATE WAIT 5'))
        self.cursor.fetchone.return_value = EMP_ROW
        EmployeeRepository(self.connection).get(10, for_update=True)
        self.assertTrue(self.cursor.execute.call_args.args[0].endswith('FOR UPDATE WAIT 5'))

    def test_employee_patch_uses_only_allowlisted_columns_and_explicit_null(self):
        EmployeeRepository(self.connection).update(10, {'email': None, 'first_name': "A'"})
        sql, params = self.cursor.execute.call_args.args
        self.assertIn('FIRST_NAME=:first_name, EMAIL=:email', sql)
        self.assertNotIn("A'", sql)
        self.assertEqual(params, {'email': None, 'first_name': "A'", 'id': 10})
        for values in ({'employee_no': 'X'}, {'is_active': 0}, {'x=1 --': 'x'}, {}):
            with self.subTest(values=values), self.assertRaises(ApplicationError):
                EmployeeRepository(self.connection).update(10, values)

    def test_department_update_allowlist_and_deactivate_sql(self):
        with self.assertRaises(ApplicationError):
            DepartmentRepository(self.connection).update(1, {'department_code': 'X'})
        for cls in (DepartmentRepository, EmployeeRepository):
            cls(self.connection).deactivate(1)
            sql, params = self.cursor.execute.call_args.args
            self.assertIn('SET IS_ACTIVE=0 WHERE ID=:id', sql)
            self.assertNotIn('DELETE', sql)
            self.assertEqual(params, {'id': 1})

    def test_active_employee_count(self):
        for value, expected in ((0, False), (2, True)):
            self.cursor.fetchone.return_value = (value,)
            self.assertIs(EmployeeRepository(self.connection).has_active(1), expected)
        self.assertEqual(self.cursor.execute.call_args.args[1], {'department_id': 1})
        self.assertIn('IS_ACTIVE=1', self.cursor.execute.call_args.args[0])

    def test_insert_and_update_require_exact_rowcount(self):
        for count in (0, 2, True):
            self.cursor.rowcount = count
            for action in (lambda: DepartmentRepository(self.connection).create({'department_code': 'X', 'department_name': 'Y'}),
                           lambda: EmployeeRepository(self.connection).deactivate(10)):
                with self.subTest(count=count), self.assertRaises(ApplicationError):
                    action()
        self.connection.commit.assert_not_called()

    def test_cursor_closed_when_execute_or_fetch_fails(self):
        for stage in ('execute', 'fetchone'):
            self.cursor.reset_mock()
            self.cursor.execute.side_effect = None
            self.cursor.fetchone.side_effect = None
            getattr(self.cursor, stage).side_effect = driver_error(942)
            with self.subTest(stage=stage), self.assertRaises(oracledb.Error):
                DepartmentRepository(self.connection).get(1)
            self.cursor.__exit__.assert_called_once()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()

    def test_bad_returning_values_are_rejected(self):
        variable = Mock()
        for value in ([], [None], [1, 2], [True], [0], [1.5], [Decimal('1.2')], '1'):
            variable.getvalue.return_value = value
            with self.subTest(value=value), self.assertRaises(ApplicationError):
                inserted_id(variable)
        variable.getvalue.return_value = [Decimal(19)]
        self.assertEqual(inserted_id(variable), 19)

    def test_bad_row_values_fail_closed(self):
        from backend.app.repositories.department import FIELDS
        for row in ((), DEPT_ROW[:-1], (*DEPT_ROW[:3], 2, STAMP),
                    (*DEPT_ROW[:4], datetime(2026, 1, 1)), (*DEPT_ROW[:4], 'not-a-date')):
            with self.subTest(row=row), self.assertRaises((ApplicationError, ValueError)):
                record(row, FIELDS)
        from backend.app.repositories.employee import FIELDS as EMP_FIELDS
        row = list(EMP_ROW)
        row[5] = datetime(2026, 1, 2, 12)
        with self.assertRaises(ApplicationError):
            record(tuple(row), EMP_FIELDS)


class ServiceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.service = EmployeeService(self.connection)
        self.service.departments = Mock()
        self.service.employees = Mock()
        self.service.departments.get.return_value = DEPT.copy()
        self.service.employees.get.return_value = EMP.copy()
        self.department = DepartmentService(self.connection)
        self.department.departments = Mock()
        self.department.employees = Mock()
        self.department.departments.get.return_value = DEPT.copy()
        self.department.employees.has_active.return_value = False

    def test_employee_create_locks_parent_before_insert_and_commits_after_read(self):
        events = []
        self.service.departments.get.side_effect = lambda *a, **k: events.append(('lock', a[0], k)) or DEPT.copy()
        self.service.employees.create.side_effect = lambda *a: events.append('insert') or 10
        self.service.employees.get.side_effect = lambda *a, **k: events.append('read') or EMP.copy()
        self.connection.commit.side_effect = lambda: events.append('commit')
        self.service.create(EmployeeCreate(**CREATE_EMP))
        self.assertEqual(events, [('lock', 1, {'for_update': True}), 'insert', 'read', 'commit'])
        self.connection.rollback.assert_not_called()

    def test_inactive_department_blocks_employee_create(self):
        self.service.departments.get.return_value = {**DEPT, 'is_active': False}
        with self.assertRaises(Conflict):
            self.service.create(EmployeeCreate(**CREATE_EMP))
        self.service.employees.create.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_missing_department_blocks_employee_create(self):
        self.service.departments.get.return_value = None
        with self.assertRaises(NotFound):
            self.service.create(EmployeeCreate(**CREATE_EMP))
        self.service.employees.create.assert_not_called()

    def test_employee_move_locks_employee_then_departments_in_sorted_order(self):
        events = []
        self.service.employees.get.side_effect = lambda *a, **k: events.append(('employee', k.get('for_update', False))) or {**EMP, 'department_id': 9}
        self.service.departments.get.side_effect = lambda identifier, **k: events.append(('department', identifier, k)) or {**DEPT, 'id': identifier}
        self.service.employees.update.side_effect = lambda *a: events.append('update')
        self.connection.commit.side_effect = lambda: events.append('commit')
        self.service.update(10, EmployeeUpdate(department_id=2))
        self.assertEqual(events, [('employee', True), ('department', 2, {'for_update': True}),
                                 ('department', 9, {'for_update': True}), 'update', ('employee', False), 'commit'])

    def test_same_department_is_locked_once(self):
        self.service.update(10, EmployeeUpdate(department_id=1))
        self.service.departments.get.assert_called_once_with(1, for_update=True)

    def test_move_to_inactive_department_rolls_back(self):
        self.service.departments.get.return_value = {**DEPT, 'is_active': False}
        with self.assertRaises(Conflict):
            self.service.update(10, EmployeeUpdate(department_id=2))
        self.service.employees.update.assert_not_called()
        self.connection.rollback.assert_called_once()

    def test_employee_email_null_is_forwarded_without_omitted_fields(self):
        self.service.update(10, EmployeeUpdate(email=None))
        self.service.employees.update.assert_called_once_with(10, {'email': None})

    def test_missing_employee_stops_before_department_locks(self):
        self.service.employees.get.return_value = None
        with self.assertRaises(NotFound):
            self.service.update(99, EmployeeUpdate(first_name='Other'))
        self.service.departments.get.assert_not_called()
        self.service.employees.update.assert_not_called()

    def test_department_deactivation_locks_before_active_count(self):
        events = []
        self.department.departments.get.side_effect = lambda *a, **k: events.append('lock' if k.get('for_update') else 'read') or DEPT.copy()
        self.department.employees.has_active.side_effect = lambda *a: events.append('count') or False
        self.department.departments.deactivate.side_effect = lambda *a: events.append('deactivate')
        self.connection.commit.side_effect = lambda: events.append('commit')
        self.department.deactivate(1)
        self.assertEqual(events, ['lock', 'count', 'deactivate', 'read', 'commit'])

    def test_active_employees_block_department_deactivation(self):
        self.department.employees.has_active.return_value = True
        with self.assertRaises(Conflict):
            self.department.deactivate(1)
        self.department.departments.deactivate.assert_not_called()
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_deactivate_is_idempotent_for_both_modules(self):
        self.department.departments.get.return_value = {**DEPT, 'is_active': False}
        self.department.deactivate(1)
        self.department.departments.deactivate.assert_not_called()
        self.service.employees.get.return_value = {**EMP, 'is_active': False}
        self.service.deactivate(10)
        self.service.employees.deactivate.assert_not_called()
        self.assertEqual(self.connection.commit.call_count, 2)

    def test_employee_deactivate_holds_parent_lock_until_commit(self):
        self.service.deactivate(10)
        self.service.employees.get.assert_any_call(10, for_update=True)
        self.service.departments.get.assert_called_once_with(1, for_update=True)
        self.service.employees.deactivate.assert_called_once_with(10)
        self.connection.commit.assert_called_once()

    def test_department_create_and_update_use_same_transaction(self):
        self.department.departments.create.return_value = 1
        self.department.create(DepartmentCreate(department_code='ENG', department_name='Engineering'))
        self.department.update(1, DepartmentUpdate(department_name='Other'))
        self.department.departments.update.assert_called_once_with(1, {'department_name': 'Other'})
        self.assertEqual(self.connection.commit.call_count, 2)
        self.connection.rollback.assert_not_called()

    def test_response_mapping_failure_rolls_back_before_commit(self):
        self.service.employees.create.return_value = 10
        self.service.employees.get.return_value = {**EMP, 'created_at': datetime(2026, 1, 2)}
        with self.assertRaises(ApplicationError):
            self.service.create(EmployeeCreate(**CREATE_EMP))
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_commit_failure_does_not_repeat_service_insert(self):
        self.connection.commit.side_effect = driver_error(3113)
        self.service.employees.create.return_value = 10
        with self.assertRaises(CommitOutcomeUnknown):
            self.service.create(EmployeeCreate(**CREATE_EMP))
        self.service.employees.create.assert_called_once()
        self.connection.rollback.assert_not_called()

    def test_lock_errors_rollback_without_retry(self):
        self.service.departments.get.side_effect = driver_error(30006)
        with self.assertRaises(LockConflict):
            self.service.create(EmployeeCreate(**CREATE_EMP))
        self.service.departments.get.assert_called_once()
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_read_missing_and_driver_error_are_safe_without_transaction(self):
        self.service.employees.get.return_value = None
        with self.assertRaises(NotFound):
            self.service.get(9)
        self.service.employees.get.side_effect = driver_error(942)
        with self.assertRaises(ApplicationError) as caught:
            self.service.get(9)
        self.assertNotIn('RAW_PASSWORD_SQL_DSN', str(caught.exception))
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()


class RouteTests(OfflineCase):
    def setUp(self):
        super().setUp()
        original = app.dependency_overrides.copy()
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(original)))
        app.dependency_overrides[get_db_connection] = lambda: self.connection
        self.client = self.enterContext(TestClient(app, raise_server_exceptions=False))

    def test_department_post_real_service_and_repository_chain(self):
        self.cursor.var.return_value.getvalue.return_value = [1]
        self.cursor.fetchone.return_value = DEPT_ROW
        response = self.client.post('/departments', json={'department_code': ' ENG ', 'department_name': 'Engineering'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['id'], 1)
        self.assertTrue(response.json()['is_active'])
        self.assertTrue(response.json()['created_at'].endswith('+03:00'))
        self.connection.commit.assert_called_once()
        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_employee_post_real_service_and_repository_chain(self):
        self.cursor.fetchone.side_effect = [DEPT_ROW, EMP_ROW]
        response = self.client.post('/employees', json=CREATE_EMP)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertIsNone(response.json()['email'])
        self.assertEqual(response.json()['hire_date'], '2026-01-02')
        self.assertIn('FOR UPDATE', self.cursor.execute.call_args_list[0].args[0])
        self.connection.commit.assert_called_once()

    def test_get_both_modules(self):
        for endpoint, row in (('/departments/1', DEPT_ROW), ('/employees/10', EMP_ROW)):
            self.cursor.fetchone.return_value = row
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200, response.text)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()

    def test_list_both_modules_with_pagination(self):
        for endpoint, row in (('/departments', DEPT_ROW), ('/employees', EMP_ROW)):
            self.cursor.fetchall.return_value = [row]
            response = self.client.get(endpoint, params={'page': 2, 'size': 3})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['page'], 2)
            self.assertEqual(response.json()['size'], 3)
            self.assertEqual(len(response.json()['items']), 1)
            self.assertEqual(self.cursor.execute.call_args.args[1], {'offset': 3, 'limit': 3})

    def test_empty_list_is_normal(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(self.client.get('/departments').json(), {'items': [], 'page': 1, 'size': 20})

    def test_department_patch(self):
        self.cursor.fetchone.side_effect = [DEPT_ROW, (1, 'ENG', 'Other', 1, STAMP)]
        response = self.client.patch('/departments/1', json={'department_name': ' Other '})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['department_name'], 'Other')
        self.connection.commit.assert_called_once()

    def test_employee_patch_explicit_email_null(self):
        self.cursor.fetchone.side_effect = [EMP_ROW, DEPT_ROW, EMP_ROW]
        response = self.client.patch('/employees/10', json={'email': None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['email'])
        self.assertEqual(self.cursor.execute.call_args_list[2].args[1], {'email': None, 'id': 10})
        self.connection.commit.assert_called_once()

    def test_department_deactivate(self):
        self.cursor.fetchone.side_effect = [DEPT_ROW, (0,), (1, 'ENG', 'Engineering', 0, STAMP)]
        response = self.client.post('/departments/1/deactivate')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['is_active'])
        self.connection.commit.assert_called_once()

    def test_employee_deactivate(self):
        self.cursor.fetchone.side_effect = [EMP_ROW, DEPT_ROW, (*EMP_ROW[:7], 0, STAMP)]
        response = self.client.post('/employees/10/deactivate')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['is_active'])
        self.connection.commit.assert_called_once()

    def test_missing_get_returns_404(self):
        self.cursor.fetchone.return_value = None
        for endpoint in ('/departments/9', '/employees/9'):
            self.assertEqual(self.client.get(endpoint).status_code, 404)

    def test_validation_errors_return_422_without_sql(self):
        for endpoint, body in (('/departments/1', {}), ('/employees/10', {}),
                               ('/departments/1', {'department_code': 'X'}),
                               ('/employees/10', {'is_active': False}),
                               ('/employees/10', {'hire_date': None})):
            response = self.client.patch(endpoint, json=body)
            self.assertEqual(response.status_code, 422, response.text)
        for query in ('?page=0', '?size=0', '?size=101', '?page=1000001'):
            self.assertEqual(self.client.get('/employees' + query).status_code, 422)
        self.cursor.execute.assert_not_called()
        self.connection.commit.assert_not_called()

    def test_unique_fk_lock_and_unknown_driver_errors_are_safe(self):
        for code, status, expected_code in ((1, 409, 'unique_conflict'), (2291, 409, 'reference_conflict'),
            (2292, 409, 'reference_conflict'), (30006, 409, 'lock_conflict'), (942, 500, 'internal_error')):
            self.cursor.execute.side_effect = driver_error(code)
            response = self.client.post('/departments', json={'department_code': 'ENG', 'department_name': 'Engineering'})
            with self.subTest(code=code):
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()['error']['code'], expected_code)
                self.assertNotIn('RAW_PASSWORD_SQL_DSN', response.text)
        self.assertEqual(self.connection.rollback.call_count, 5)
        self.connection.commit.assert_not_called()

    def test_commit_unknown_returns_safe_distinct_500(self):
        self.cursor.fetchone.return_value = DEPT_ROW
        self.connection.commit.side_effect = driver_error(3113)
        response = self.client.post('/departments', json={'department_code': 'ENG', 'department_name': 'Engineering'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['error']['code'], 'commit_outcome_unknown')
        self.assertNotIn('RAW_PASSWORD_SQL_DSN', response.text)
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()
        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_active_employee_blocks_deactivation_with_409(self):
        self.cursor.fetchone.side_effect = [DEPT_ROW, (1,)]
        response = self.client.post('/departments/1/deactivate')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()

    def test_inactive_department_blocks_create_with_409(self):
        self.cursor.fetchone.return_value = (1, 'ENG', 'Engineering', 0, STAMP)
        response = self.client.post('/employees', json=CREATE_EMP)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.rollback.assert_called_once()

    def test_connection_cleanup_failure_warns_about_possible_completed_write(self):
        from backend.app.db.oracle import OracleConnectionError
        self.assertIsInstance(safe_error(OracleConnectionError("güvenli hata", cleanup_failed=True)), ConnectionCleanupError)

    def test_response_after_request_connection_cleanup_failure_is_not_silent_success(self):
        from backend.app.db import dependencies
        app.dependency_overrides.clear()
        self.cursor.var.return_value.getvalue.return_value = [1]
        self.cursor.fetchone.return_value = DEPT_ROW
        self.connection.close.side_effect = oracledb.DatabaseError('RAW_PASSWORD_SQL_DSN')
        with patch.object(dependencies, 'create_oracle_connection', return_value=self.connection):
            response = self.client.post('/departments', json={'department_code': 'ENG', 'department_name': 'Engineering'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['error']['code'], 'connection_cleanup_failed')
        self.assertNotIn('RAW_PASSWORD_SQL_DSN', response.text)
        self.connection.commit.assert_called_once()
        self.connection.close.assert_called_once()
        self.connection.rollback.assert_not_called()

    def test_nullable_email_is_not_treated_as_unique(self):
        self.cursor.fetchone.side_effect = [DEPT_ROW, EMP_ROW, DEPT_ROW, EMP_ROW]
        for _ in range(2):
            response = self.client.post('/employees', json={**CREATE_EMP, 'email': 'same@example.test'})
            self.assertEqual(response.status_code, 201, response.text)
        queries = [c.args[0] for c in self.cursor.execute.call_args_list]
        self.assertFalse(any('WHERE EMAIL' in sql for sql in queries))

    def test_delete_endpoints_do_not_exist(self):
        for endpoint in ('/departments/1', '/employees/10'):
            self.assertEqual(self.client.delete(endpoint).status_code, 405)
        self.cursor.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
