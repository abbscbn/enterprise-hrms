import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.app.core import config
from backend.app.db import dependencies, oracle
from backend.app.main import app


class OracleConfigTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "DB_HOST": "synthetic-host.invalid", "DB_PORT": "1521",
            "DB_SERVICE": "SYNTHETIC_SERVICE", "DB_USER": "SYNTHETIC_USER",
            "DB_PASSWORD": "synthetic-password-never-log",
        }
        self.enterContext(patch.dict(os.environ, self.values, clear=True))
        self.loader = self.enterContext(patch.object(config, "load_dotenv"))
        self.connect = self.enterContext(patch.object(oracle.oracledb, "connect",
                                                    side_effect=AssertionError("Gerçek bağlantı yasak")))

    def test_settings_are_loaded_on_explicit_call_and_keep_env_priority(self):
        self.loader.assert_not_called()
        settings = config.load_oracle_settings()
        self.assertEqual(settings.host, self.values["DB_HOST"])
        self.assertEqual(settings.port, 1521)
        self.assertEqual(settings.service, self.values["DB_SERVICE"])
        self.assertEqual(settings.user, self.values["DB_USER"])
        self.assertEqual(settings.password, self.values["DB_PASSWORD"])
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(config.PROJECT_ROOT, root)
        self.loader.assert_called_once_with(root / ".env", override=False)
        self.connect.assert_not_called()

    def test_existing_environment_beats_simulated_dotenv(self):
        def simulated_dotenv(path, override):
            for name in self.values:
                if override or name not in os.environ:
                    os.environ[name] = "different-value"
        self.loader.side_effect = simulated_dotenv
        self.assertEqual(config.load_oracle_settings().password, self.values["DB_PASSWORD"])
        self.assertEqual(dict(os.environ), self.values)

    def test_missing_variables_only_report_names_not_values(self):
        for name in self.values:
            with self.subTest(name=name), patch.dict(os.environ, self.values, clear=True):
                del os.environ[name]
                with self.assertRaises(config.OracleConfigurationError) as caught:
                    config.load_oracle_settings()
                self.assertIn(name, str(caught.exception))
                for secret in self.values.values():
                    self.assertNotIn(secret, str(caught.exception))
        self.connect.assert_not_called()

    def test_empty_and_whitespace_variables_are_rejected(self):
        for name in self.values:
            for value in ("", " \t "):
                with self.subTest(name=name, empty=not value), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(config.OracleConfigurationError):
                        config.load_oracle_settings()

    def test_invalid_ports_are_sanitized(self):
        for port in ("0", "-1", "65536", "1.5", "PORT_SECRET"):
            with self.subTest(port=port), patch.dict(os.environ, {"DB_PORT": port}):
                with self.assertRaises(config.OracleConfigurationError) as caught:
                    config.load_oracle_settings()
                self.assertEqual(str(caught.exception), "DB_PORT, 1 ile 65535 arasında tam sayı olmalıdır.")

    def test_port_boundaries_are_accepted(self):
        for port in ("1", "65535"):
            with self.subTest(port=port), patch.dict(os.environ, {"DB_PORT": port}):
                self.assertEqual(config.load_oracle_settings().port, int(port))

    def test_dotenv_errors_do_not_leak_contents(self):
        for error in (OSError("SECRET_ENV_CONTENT"), UnicodeError("SECRET_ENV_CONTENT"),
                      RuntimeError("SECRET_ENV_CONTENT")):
            with self.subTest(error_type=type(error).__name__):
                self.loader.side_effect = error
                with self.assertRaises(config.OracleConfigurationError) as caught:
                    config.load_oracle_settings()
                self.assertNotIn("SECRET_ENV_CONTENT", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)

    def test_settings_repr_is_secret_free_and_immutable(self):
        settings = config.load_oracle_settings()
        for value in self.values.values():
            self.assertNotIn(value, repr(settings))
        with self.assertRaises(AttributeError):
            settings.password = "replacement"


class OracleConnectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = config.OracleSettings("fake.invalid", 1521, "FAKE_SERVICE", "FAKE_USER", "FAKE_PASSWORD")
        self.loader = self.enterContext(patch.object(oracle, "load_oracle_settings", return_value=self.settings))
        self.dotenv = self.enterContext(patch.object(config, "load_dotenv",
                                                   side_effect=AssertionError("Gerçek .env okuma yasak")))
        self.thin = self.enterContext(patch.object(oracle.oracledb, "is_thin_mode", return_value=True))
        self.params = self.enterContext(patch.object(oracle.oracledb, "ConnectParams", return_value=object()))
        self.connection = MagicMock(spec=["autocommit", "close", "commit", "rollback", "cursor"])
        self.connect = self.enterContext(patch.object(oracle.oracledb, "connect", return_value=self.connection))

    def tearDown(self):
        self.dotenv.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.cursor.assert_not_called()
        super().tearDown()

    def test_thin_parameters_and_autocommit_are_correct(self):
        self.assertIs(oracle.create_oracle_connection(), self.connection)
        self.params.assert_called_once_with(host="fake.invalid", port=1521,
                                           service_name="FAKE_SERVICE", retry_count=0)
        self.connect.assert_called_once_with(user="FAKE_USER", password="FAKE_PASSWORD", params=self.params.return_value)
        self.assertIs(self.connection.autocommit, False)
        self.connection.close.assert_not_called()

    def test_thick_mode_is_rejected_without_connect(self):
        self.thin.return_value = False
        with self.assertRaisesRegex(oracle.OracleConnectionError, "Thin"):
            oracle.create_oracle_connection()
        self.connect.assert_not_called()
        self.params.assert_not_called()

    def test_config_error_prevents_connect(self):
        self.loader.side_effect = config.OracleConfigurationError("Eksik ortam değişkeni: DB_PASSWORD")
        with self.assertRaises(config.OracleConfigurationError):
            oracle.create_oracle_connection()
        self.connect.assert_not_called()

    def test_driver_and_parameter_errors_are_sanitized_without_retry(self):
        for target in (self.params, self.connect):
            with self.subTest(target="params" if target is self.params else "connect"):
                self.params.reset_mock(side_effect=True)
                self.connect.reset_mock(side_effect=True)
                target.side_effect = RuntimeError("FAKE_PASSWORD FAKE_USER fake.invalid RAW_SQL")
                with self.assertRaises(oracle.OracleConnectionError) as caught:
                    oracle.create_oracle_connection()
                self.assertEqual(str(caught.exception), "Oracle bağlantısı kurulamadı.")
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertLessEqual(self.connect.call_count, 1)
        self.connection.close.assert_not_called()

    def test_autocommit_failure_closes_connection_and_sanitizes_error(self):
        with patch.object(type(self.connection), "autocommit", PropertyMock(side_effect=RuntimeError("FAKE_PASSWORD")), create=True):
            with self.assertRaises(oracle.OracleConnectionError) as caught:
                oracle.create_oracle_connection()
        self.assertFalse(caught.exception.cleanup_failed)
        self.assertNotIn("FAKE_PASSWORD", str(caught.exception))
        self.connection.close.assert_called_once_with()
        self.connect.assert_called_once()

    def test_setup_and_close_failure_preserves_cleanup_information(self):
        self.connection.close.side_effect = RuntimeError("FAKE_PASSWORD")
        with patch.object(type(self.connection), "autocommit", PropertyMock(side_effect=RuntimeError("RAW_SQL")), create=True):
            with self.assertRaises(oracle.OracleConnectionError) as caught:
                oracle.create_oracle_connection()
        self.assertTrue(caught.exception.cleanup_failed)
        self.assertNotIn("RAW_SQL", str(caught.exception))
        self.assertNotIn("FAKE_PASSWORD", str(caught.exception))
        self.connection.close.assert_called_once_with()

    def test_setup_interrupt_still_closes_connection(self):
        with patch.object(type(self.connection), "autocommit", PropertyMock(side_effect=KeyboardInterrupt()), create=True):
            with self.assertRaises(KeyboardInterrupt):
                oracle.create_oracle_connection()
        self.connection.close.assert_called_once_with()


class OracleDependencyTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock(spec=["close", "commit", "rollback", "cursor"])
        self.factory = self.enterContext(patch.object(dependencies, "create_oracle_connection", return_value=self.connection))
        self.connect = self.enterContext(patch.object(oracle.oracledb, "connect",
                                                    side_effect=AssertionError("Gerçek bağlantı yasak")))

    def tearDown(self):
        self.connect.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.cursor.assert_not_called()
        super().tearDown()

    def test_successful_dependency_is_lazy_and_closes_once(self):
        dependency = dependencies.get_db_connection()
        self.factory.assert_not_called()
        self.assertIs(next(dependency), self.connection)
        self.connection.close.assert_not_called()
        with self.assertRaises(StopIteration):
            next(dependency)
        self.factory.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_request_error_closes_connection_without_hiding_original(self):
        dependency = dependencies.get_db_connection()
        next(dependency)
        error = ValueError("synthetic request failure")
        with self.assertRaises(ValueError) as caught:
            dependency.throw(error)
        self.assertIs(caught.exception, error)
        self.connection.close.assert_called_once_with()

    def test_generator_cancellation_closes_connection(self):
        dependency = dependencies.get_db_connection()
        next(dependency)
        dependency.close()
        self.connection.close.assert_called_once_with()

    def test_creation_error_does_not_retry_or_close_nonexistent_connection(self):
        self.factory.side_effect = oracle.OracleConnectionError("Oracle bağlantısı kurulamadı.")
        with self.assertRaises(oracle.OracleConnectionError):
            next(dependencies.get_db_connection())
        self.factory.assert_called_once()
        self.connection.close.assert_not_called()

    def test_cleanup_error_is_safe_after_success(self):
        self.connection.close.side_effect = RuntimeError("SECRET_DSN")
        dependency = dependencies.get_db_connection()
        next(dependency)
        with self.assertRaises(oracle.OracleConnectionError) as caught:
            next(dependency)
        self.assertTrue(caught.exception.cleanup_failed)
        self.assertNotIn("SECRET_DSN", str(caught.exception))
        self.connection.close.assert_called_once()

    def test_cleanup_failure_preserves_original_request_error(self):
        self.connection.close.side_effect = RuntimeError("SECRET_DSN")
        dependency = dependencies.get_db_connection()
        next(dependency)
        error = ValueError("synthetic request failure")
        with self.assertRaises(ValueError) as caught:
            dependency.throw(error)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.__notes__, ["Oracle bağlantısı kapatılamadı; bağlantı yaşam döngüsü incelenmelidir."])
        self.assertNotIn("SECRET_DSN", " ".join(error.__notes__))

    def test_fastapi_depends_closes_connection_after_request(self):
        test_app = FastAPI()

        @test_app.get("/probe")
        def probe(connection=Depends(dependencies.get_db_connection)):
            return {"provided": connection is self.connection}

        with TestClient(test_app) as client:
            self.assertEqual(client.get("/probe").json(), {"provided": True})
        self.factory.assert_called_once()
        self.connection.close.assert_called_once()

    def test_fastapi_depends_closes_connection_after_route_error(self):
        test_app = FastAPI()

        @test_app.get("/probe")
        def probe(connection=Depends(dependencies.get_db_connection)):
            raise ValueError("synthetic route failure")

        with TestClient(test_app, raise_server_exceptions=False) as client:
            self.assertEqual(client.get("/probe").status_code, 500)
        self.connection.close.assert_called_once()


class OracleApplicationIsolationTests(unittest.TestCase):
    def test_fresh_imports_do_not_read_config_or_connect(self):
        code = """
from unittest.mock import patch
with patch('dotenv.load_dotenv', side_effect=AssertionError('dotenv forbidden')) as dotenv, \\
     patch('oracledb.connect', side_effect=AssertionError('connect forbidden')) as connect:
    from backend.app.core import config
    from backend.app.db import oracle, dependencies
    from backend.app.main import app
    dotenv.assert_not_called()
    connect.assert_not_called()
    assert app.title == 'Enterprise HRMS Backend'
"""
        result = subprocess.run([sys.executable, "-B", "-c", code],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_health_never_reads_config_or_opens_connection(self):
        with (patch.object(config, "load_dotenv", side_effect=AssertionError("dotenv forbidden")) as dotenv,
              patch.object(oracle, "load_oracle_settings", side_effect=AssertionError("config forbidden")) as settings,
              patch.object(oracle.oracledb, "connect", side_effect=AssertionError("connect forbidden")) as connect,
              TestClient(app) as client):
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "service": "enterprise-hrms-backend"})
        dotenv.assert_not_called()
        settings.assert_not_called()
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
