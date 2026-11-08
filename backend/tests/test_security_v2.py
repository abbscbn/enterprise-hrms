import hashlib
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock,patch
import unittest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.security.models import Principal,Login,UserCreate,UserUpdate
from backend.app.security.passwords import hash_password,verify_password,digest
from backend.app.security.service import AuthService
from backend.app.security.dependencies import get_current_user
from backend.app.db.dependencies import get_db_connection
from backend.app.core.errors import Unauthorized,Forbidden,Conflict,TooManyRequests
from backend.tests.test_crud import EMP_ROW,DEPT_ROW

PASSWORD='Synthetic-test-password-42!'
HASH=hash_password(PASSWORD)
USER={'id':1,'username':'fixture','email':'fixture@example.invalid','role_code':'ADMIN','employee_id':None,'is_active':1,'password_hash':HASH}

class AuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('oracledb.connect',side_effect=AssertionError('Live Oracle forbidden')))
        self.enterContext(patch('backend.app.db.transactions.validate_target_session'))
        self.enterContext(patch('backend.app.security.service.audit.write'))
        self.connection=MagicMock();self.connection.autocommit=False
        self.service=AuthService(self.connection);self.service.repo=Mock()
        self.service.repo.execute.return_value=1

    def test_argon2_hash_is_salted_and_verifies(self):
        self.assertTrue(HASH.startswith('$argon2id$'));self.assertTrue(verify_password(HASH,PASSWORD))
        self.assertFalse(verify_password(HASH,'incorrect'));self.assertNotEqual(hash_password(PASSWORD),HASH)

    def test_login_hashes_tokens_and_commits_once(self):
        self.service.repo.one.return_value=None;self.service.repo.login_user.return_value=USER
        user,tokens=self.service.login(Login(login='fixture',password=PASSWORD),'00000000-0000-0000-0000-000000000000','test')
        sql,params=self.service.repo.execute.call_args.args
        self.assertIn('HRMS_SESSION',sql);self.assertNotIn(':access,',sql)
        self.assertEqual(params['access_hash'],digest(tokens[0]));self.assertEqual(params['refresh'],digest(tokens[1]))
        self.assertNotIn(tokens[0],str(params));self.assertNotIn('password_hash',user.model_dump())
        self.connection.commit.assert_called_once();self.connection.rollback.assert_not_called()

    def test_bad_password_attempt_is_committed_but_no_session_created(self):
        self.service.repo.one.return_value=None;self.service.repo.login_user.return_value=USER
        with self.assertRaises(Unauthorized):self.service.login(Login(login='fixture',password='wrong'),'0'*36,'test')
        self.connection.commit.assert_called_once();self.connection.rollback.assert_not_called()
        self.assertFalse(any('INSERT INTO HR_CORE.HRMS_SESSION' in c.args[0] for c in self.service.repo.execute.call_args_list))

    def test_inactive_account_rejected_even_correct_password(self):
        self.service.repo.one.return_value=None;self.service.repo.login_user.return_value={**USER,'is_active':0}
        with self.assertRaises(Unauthorized):self.service.login(Login(login='fixture',password=PASSWORD),'0'*36,'test')

    def test_missing_user_does_not_expose_identity(self):
        self.service.repo.one.return_value=None;self.service.repo.login_user.return_value=None
        with self.assertRaises(Unauthorized):self.service.login(Login(login='missing',password=PASSWORD),'0'*36,'test')
        self.connection.commit.assert_called_once()

    def test_persistent_attempt_limit_is_enforced(self):
        self.service.repo.one.return_value={'attempts':5,'recent':1}
        with self.assertRaises(TooManyRequests):self.service.login(Login(login='fixture',password=PASSWORD),'0'*36,'test')
        self.service.repo.login_user.assert_not_called();self.connection.commit.assert_called_once()

    def test_refresh_rotates_and_records_old_hash(self):
        old='old-refresh';csrf='csrf-token'
        self.service.repo.one.return_value={'id':'session','user_id':1,'csrf_hash':digest(csrf)}
        self.service.repo.user.return_value=USER
        user,tokens=self.service.refresh(old,csrf,'0'*36)
        calls=self.service.repo.execute.call_args_list
        self.assertIn('HRMS_REFRESH_USED',calls[0].args[0]);self.assertEqual(calls[0].args[1]['hash'],digest(old))
        self.assertEqual(calls[1].args[1]['refresh'],digest(tokens[1]));self.connection.commit.assert_called_once()

    def test_refresh_replay_revokes_family_and_commits_rejection(self):
        self.service.repo.one.side_effect=[None,{'session_id':'session'}]
        with self.assertRaises(Unauthorized):self.service.refresh('used-refresh','csrf','0'*36)
        self.assertIn('REVOKED_AT=SYSTIMESTAMP',self.service.repo.execute.call_args.args[0])
        self.connection.commit.assert_called_once()

    def test_refresh_csrf_mismatch_does_not_rotate(self):
        self.service.repo.one.return_value={'id':'session','user_id':1,'csrf_hash':digest('expected')}
        with self.assertRaises(Forbidden):self.service.refresh('refresh','bad','0'*36)
        self.service.repo.execute.assert_not_called()

    def test_password_change_revokes_all_sessions(self):
        self.service.repo.one.return_value={'password_hash':HASH}
        self.service.change_password(Principal(1,'fixture','fixture@example.invalid','ADMIN'),SimpleNamespace(current_password=PASSWORD,new_password='New-synthetic-password-43!'))
        self.assertEqual(self.service.repo.execute.call_count,2)
        self.assertIn('REVOKED_AT=SYSTIMESTAMP',self.service.repo.execute.call_args.args[0])

    def test_last_active_admin_cannot_be_demoted(self):
        self.service.repo.user.return_value=USER;self.service.repo.one.return_value={'n':1}
        with self.assertRaises(Conflict):self.service.update_user(1,UserUpdate(role='HR_MANAGER'),Principal(1,'fixture','x','ADMIN'))
        self.service.repo.execute.assert_not_called();self.connection.rollback.assert_called_once()

    def test_bootstrap_never_overwrites_existing_account(self):
        self.service.repo.one.return_value={'n':1}
        with self.assertRaises(Conflict):self.service.create_user(UserCreate(username='fixture',email='fixture@example.invalid',password=PASSWORD,role='ADMIN'),bootstrap=True)
        self.service.repo.insert_id.assert_not_called()


class AuthorizationRouteTests(unittest.TestCase):
    def setUp(self):
        self.connect=self.enterContext(patch('oracledb.connect',side_effect=AssertionError('Live Oracle forbidden')))
        self.enterContext(patch('backend.app.core.config.load_dotenv',side_effect=AssertionError('.env forbidden')))
        self.enterContext(patch('backend.app.db.transactions.validate_target_session'))
        self.target=self.enterContext(patch('backend.app.security.dependencies.validate_target_session'))
        self.audit=self.enterContext(patch('backend.app.security.dependencies.audit.write'))
        self.enterContext(patch.dict('os.environ',{'HRMS_ENTERPRISE_ENABLED':'0'}))
        self.old=app.dependency_overrides.copy();self.addCleanup(lambda:(app.dependency_overrides.clear(),app.dependency_overrides.update(self.old)))
        app.dependency_overrides.clear();self.connection=MagicMock();self.connection.autocommit=False
        self.cursor=self.connection.cursor.return_value.__enter__.return_value;self.cursor.fetchone.return_value=EMP_ROW
        app.dependency_overrides[get_db_connection]=lambda:self.connection
        self.row={'id':1,'username':'fixture','email':'fixture@example.invalid','role_code':'EMPLOYEE','employee_id':10,'session_id':'session','csrf_hash':digest('csrf')}
        self.lookup=self.enterContext(patch('backend.app.security.dependencies.Repository.principal',side_effect=lambda _:self.row))
        self.client=self.enterContext(TestClient(app,raise_server_exceptions=False))
        self.client.cookies.set('hrms_access','a'*64);self.client.headers['x-csrf-token']='csrf'

    def test_no_cookie_is_401_and_does_not_query_session(self):
        self.client.cookies.clear();self.assertEqual(self.client.get('/employees/10').status_code,401);self.lookup.assert_not_called()

    def test_invalid_expired_or_revoked_session_is_401(self):
        self.row=None;self.assertEqual(self.client.get('/employees/10').status_code,401)

    def test_employee_can_read_only_own_profile(self):
        self.assertEqual(self.client.get('/employees/10').status_code,200)
        self.cursor.execute.reset_mock()
        self.assertEqual(self.client.get('/employees/11').status_code,403)
        self.cursor.execute.assert_not_called();self.audit.assert_called()

    def test_employee_cannot_list_others_or_mutate_roles_or_create(self):
        for method,url,body in (('GET','/employees',None),('GET','/users',None),('GET','/audit',None),('POST','/employees',{}),('PATCH','/users/1',{'role':'ADMIN'})):
            response=self.client.request(method,url,json=body) if body is not None else self.client.request(method,url)
            with self.subTest(url=url):self.assertEqual(response.status_code,403)

    def test_employee_cannot_change_own_hr_fields(self):
        self.assertEqual(self.client.patch('/employees/10',json={'first_name':'Other'}).status_code,403)
        self.cursor.execute.assert_not_called()

    def test_csrf_required_on_cookie_writes(self):
        self.row={**self.row,'role_code':'ADMIN'};del self.client.headers['x-csrf-token']
        self.assertEqual(self.client.post('/departments',json={}).status_code,403)
        self.cursor.execute.assert_not_called()

    def test_cross_origin_login_is_rejected_before_service(self):
        with patch('backend.app.security.routes.AuthService.login') as login:
            response=self.client.post('/auth/login',headers={'Origin':'https://attacker.invalid'},json={'login':'fixture','password':PASSWORD})
        self.assertEqual(response.status_code,403);login.assert_not_called()

    def test_hr_manager_cannot_mutate_users_or_view_audit(self):
        self.row={**self.row,'role_code':'HR_MANAGER'}
        self.assertEqual(self.client.patch('/users/1',json={'role':'ADMIN'}).status_code,403)
        self.assertEqual(self.client.get('/audit').status_code,403)

    def test_validation_never_echoes_password(self):
        response=self.client.post('/auth/login',json={'login':'fixture','password':'SENSITIVE'*100})
        self.assertEqual(response.status_code,422);self.assertNotIn('SENSITIVE',response.text)

    def test_login_cookie_flags_and_no_token_body(self):
        from backend.app.security.models import UserRead
        self.client.cookies.clear()
        with patch('backend.app.security.routes.AuthService.login',return_value=(UserRead(id=1,username='fixture',email='x',role='ADMIN',employee_id=None),('access-secret','refresh-secret','csrf-secret'))):
            response=self.client.post('/auth/login',json={'login':'fixture','password':PASSWORD})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('secret',response.text)
        cookies=response.headers.get_list('set-cookie')
        self.assertTrue(all('SameSite=strict' in c for c in cookies));self.assertTrue(all('HttpOnly' in c for c in cookies if not c.startswith('hrms_csrf=')))

    def test_multipart_body_limit_before_database(self):
        response=self.client.post('/employees/10/documents',content=b'x',headers={'Content-Length':str(13*1024*1024)})
        self.assertEqual(response.status_code,413)
        self.cursor.execute.assert_not_called()

    def test_wrong_target_stops_before_principal_query(self):
        from backend.app.core.errors import ApplicationError
        self.target.side_effect=ApplicationError()
        self.assertEqual(self.client.get('/auth/me').status_code,500)
        self.lookup.assert_not_called()

    def test_production_insecure_cookie_config_prevents_login_writes(self):
        with patch.dict('os.environ',{'HRMS_ENV':'production','HRMS_COOKIE_SECURE':'0'}),patch('backend.app.security.routes.AuthService.login') as login:
            self.assertEqual(self.client.post('/auth/login',json={'login':'fixture','password':PASSWORD}).status_code,403)
            login.assert_not_called()
