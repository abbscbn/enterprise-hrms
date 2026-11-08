import os
import unittest
from unittest.mock import MagicMock,Mock,patch
from backend.app.enterprise import service
from backend.app.enterprise.repository import ProfileRepository
from backend.app.security.models import Principal
from backend.app.schemas.employee import EmployeeUpdate
from backend.app.schemas.department import DepartmentUpdate
from backend.app.core.errors import Forbidden

class EnterpriseModelTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('oracledb.connect',side_effect=AssertionError('No live Oracle')))
        self.enterContext(patch.dict(os.environ,{'HRMS_ENTERPRISE_ENABLED':'1'}))
        self.connection=MagicMock()

    def test_nullable_profile_clears_and_audit_fields_forbidden(self):
        model=EmployeeUpdate(manager_id=None,personal_email=None)
        self.assertEqual(model.model_dump(exclude_unset=True),{'manager_id':None,'personal_email':None})
        for data in ({'created_by':1},{'updated_at':'2026-01-01'},{'is_active':True}):
            with self.assertRaises(ValueError):EmployeeUpdate(**data)

    def test_department_parent_and_manager_are_explicit_nullable(self):
        self.assertEqual(DepartmentUpdate(parent_id=None,manager_id=None).model_dump(exclude_unset=True),{'parent_id':None,'manager_id':None})

    def test_employee_cannot_write_hr_fields(self):
        actor=Principal(1,'own','own@example.invalid','EMPLOYEE',10)
        with patch.object(service,'deny',side_effect=Forbidden()) as denied:
            with self.assertRaises(Forbidden):service.authorize(self.connection,actor,10,{'job_title':'Manager'})
            denied.assert_called_once()

    def test_employee_can_write_only_contact_fields(self):
        actor=Principal(1,'own','own@example.invalid','EMPLOYEE',10)
        with patch.object(service,'deny') as denied:
            service.authorize(self.connection,actor,10,{'personal_email':None,'phone_number':'123'});denied.assert_not_called()

    def test_employee_manager_package_precedes_profile_merge(self):
        with patch.object(service,'ProfileRepository') as factory:
            repo=factory.return_value;service.save(self.connection,'EMPLOYEE',10,{'manager_id':20,'job_title':'Engineer'},None)
            self.assertEqual([c[0] for c in repo.mock_calls],['set_manager','merge'])
            repo.set_manager.assert_called_once_with(10,20)

    def test_department_manager_parent_precede_profile_merge(self):
        with patch.object(service,'ProfileRepository') as factory:
            repo=factory.return_value;repo.attach_mock(Mock(),'assert_manager');service.save(self.connection,'DEPARTMENT',10,{'manager_id':20,'parent_id':30},None)
            self.assertEqual([c[0] for c in repo.mock_calls],['assert_manager','set_parent','merge'])

    def test_package_failure_prevents_merge(self):
        with patch.object(service,'ProfileRepository') as factory:
            repo=factory.return_value;repo.set_manager.side_effect=RuntimeError()
            with self.assertRaises(RuntimeError):service.save(self.connection,'EMPLOYEE',10,{'manager_id':10},None)
            repo.merge.assert_not_called()

    def test_existing_employee_fallback_preserves_inactive(self):
        with patch.object(service,'ProfileRepository') as factory:
            factory.return_value.load.return_value={}
            row=service.enrich(self.connection,'EMPLOYEE',{'id':10,'is_active':False})
            self.assertEqual(row['employment_status'],'TERMINATED');self.assertEqual(row['employment_type'],'UNSPECIFIED')

    def test_created_by_set_even_package_precreated_profile(self):
        repo=ProfileRepository(self.connection);repo.execute=Mock()
        repo.merge('EMPLOYEE',10,{'job_title':'Engineer'},1,True)
        sql,params=repo.execute.call_args.args;self.assertIn('T.CREATED_BY=:creator',sql);self.assertEqual(params['creator'],1)

    def test_global_lock_uses_existing_connection(self):
        service.lock(self.connection)
        self.connection.cursor.return_value.__enter__.return_value.callproc.assert_called_once_with('HR_CORE.HRMS_ENTERPRISE.LOCK_ORGANIZATION')
        for name in ('commit','rollback','close'):getattr(self.connection,name).assert_not_called()
