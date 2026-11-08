"""Migration mantığını sahte veriler ve geçici dosyalarla Oracle olmadan test et."""

import hashlib
import importlib.util
import io
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, PropertyMock, call, patch


MODULE_PATH = Path(__file__).resolve().with_name("migrate.py")
SPEC = importlib.util.spec_from_file_location("hrms_migrate_under_test", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError("Yerel migrate.py modülü yüklenemedi.")
migrate = importlib.util.module_from_spec(SPEC)
# dataclass dekoratörleri yükleme sırasında modül kaydına ihtiyaç duyabilir.
sys.modules[SPEC.name] = migrate
SPEC.loader.exec_module(migrate)


CHECKSUM = "a" * 64
OTHER_CHECKSUM = "b" * 64


def local_migration(version=1, checksum=CHECKSUM):
    # Path yalnızca veri olarak kullanılır; dosya oluşturulmaz veya okunmaz.
    return migrate.Migration(version, Path(f"V{version:04d}__example.sql"), checksum)


def history_row(version=1, status="SUCCESS", checksum=CHECKSUM, script_name=None):
    # read_database_history dönüşündeki altı alanın sırası korunur.
    return (version, script_name or f"V{version:04d}__example.sql", checksum, status, None, None)


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        # Hiçbir test gerçek bağlantı/.env okuma yoluna giremez.
        self.history = self.enterContext(patch.object(
            migrate, "read_database_history",
            side_effect=AssertionError("Test gerçek Oracle okuma yolunu çağıramaz."),
        ))

    def render_plan(self, migrations, history):
        self.history.side_effect = None
        self.history.return_value = history
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = migrate.show_database_plan(migrations)
        return code, output.getvalue()


class CompareMigrationsTests(OfflineTestCase):
    def test_local_only_is_pending(self):
        entries = migrate.compare_migrations([local_migration()], [])
        self.assertEqual(entries, [migrate.DatabasePlanEntry(1, "V0001__example.sql", "PENDING")])

    def test_success_with_matching_checksum_is_applied(self):
        entry, = migrate.compare_migrations([local_migration()], [history_row()])
        self.assertEqual(entry.status, "APPLIED")
        self.assertEqual(entry.database_status, "SUCCESS")
        self.assertFalse(entry.script_name_mismatch)

    def test_success_with_different_checksum_is_mismatch(self):
        entry, = migrate.compare_migrations(
            [local_migration()], [history_row(checksum=OTHER_CHECKSUM)]
        )
        self.assertEqual(entry.status, "CHECKSUM MISMATCH")

    def test_failed_record_is_failed(self):
        entry, = migrate.compare_migrations([local_migration()], [history_row(status="FAILED")])
        self.assertEqual(entry.status, "FAILED")
        self.assertEqual(entry.database_status, "FAILED")

    def test_running_record_is_running(self):
        entry, = migrate.compare_migrations([local_migration()], [history_row(status="RUNNING")])
        self.assertEqual(entry.status, "RUNNING")
        self.assertEqual(entry.database_status, "RUNNING")

    def test_database_only_is_orphaned(self):
        entry, = migrate.compare_migrations([], [history_row()])
        self.assertEqual(entry.status, "ORPHANED")
        self.assertEqual(entry.script_name, "V0001__example.sql")
        self.assertEqual(entry.database_status, "SUCCESS")

    def test_success_with_different_script_name_is_inconsistent(self):
        for checksum, expected_status in ((CHECKSUM, "APPLIED"), (OTHER_CHECKSUM, "CHECKSUM MISMATCH")):
            with self.subTest(checksum=checksum):
                entry, = migrate.compare_migrations(
                    [local_migration()],
                    [history_row(checksum=checksum, script_name="V0001__renamed.sql")],
                )
                self.assertEqual(entry.status, expected_status)
                self.assertTrue(entry.script_name_mismatch)
                self.assertEqual(entry.script_name, "V0001__example.sql")
                self.assertEqual(entry.database_script_name, "V0001__renamed.sql")

    def test_empty_inputs_have_no_entries(self):
        self.assertEqual(migrate.compare_migrations([], []), [])

    def test_multiple_migrations_are_sorted_by_numeric_version(self):
        entries = migrate.compare_migrations(
            [local_migration(10), local_migration(2), local_migration(1)],
            [history_row(20), history_row(2)],
        )
        self.assertEqual([entry.version for entry in entries], [1, 2, 10, 20])
        self.assertEqual([entry.status for entry in entries], ["PENDING", "APPLIED", "PENDING", "ORPHANED"])


class DatabasePlanOutputTests(OfflineTestCase):
    def test_problem_states_return_failure(self):
        cases = (
            ("FAILED", [local_migration()], [history_row(status="FAILED")]),
            ("RUNNING", [local_migration()], [history_row(status="RUNNING")]),
            ("CHECKSUM MISMATCH", [local_migration()], [history_row(checksum=OTHER_CHECKSUM)]),
            ("ORPHANED", [], [history_row()]),
            ("SCRIPT_NAME MISMATCH", [local_migration()], [history_row(script_name="V0001__renamed.sql")]),
        )
        for label, migrations, history in cases:
            with self.subTest(label=label):
                code, output = self.render_plan(migrations, history)
                self.assertEqual(code, 1)
                self.assertIn(label, output)

    def test_pending_and_applied_return_success(self):
        cases = (
            ([local_migration()], []),
            ([local_migration()], [history_row()]),
            ([local_migration(2), local_migration(1)], [history_row(1)]),
        )
        for migrations, history in cases:
            with self.subTest(migrations=migrations, history=history):
                code, _ = self.render_plan(migrations, history)
                self.assertEqual(code, 0)

    def test_empty_plan_returns_success(self):
        code, output = self.render_plan([], [])
        self.assertEqual(code, 0)
        self.assertIn("planlanacak migration bulunmuyor", output)

    def test_output_order_and_summary_counts(self):
        code, output = self.render_plan(
            [local_migration(10), local_migration(2), local_migration(1)],
            [history_row(20), history_row(2)],
        )
        self.assertEqual(code, 1)
        lines = [line for line in output.splitlines() if line.startswith("V")]
        self.assertEqual([line.split(" | ")[0] for line in lines], ["V0001", "V0002", "V0010", "V0020"])
        self.assertIn("PENDING: 2 | APPLIED: 1 | FAILED: 0 | RUNNING: 0 | CHECKSUM MISMATCH: 0 | ORPHANED: 1", output)
        self.assertIn("SUCCESS kayıtlarında dosya adı tutarsızlığı: 0", output)


class ExistingCommandRegressionTests(OfflineTestCase):
    def test_validate_and_plan_remain_local(self):
        for command, expected in (
            ("validate", "Yerel doğrulama başarılı: 1 migration dosyası."),
            ("plan", "V0001 | V0001__example.sql | SHA-256: " + CHECKSUM),
        ):
            with self.subTest(command=command):
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", ["migrate.py", command]),
                    patch.object(migrate, "collect_migrations", return_value=[local_migration()]) as collect,
                    patch("sys.stdout", output),
                ):
                    self.assertEqual(migrate.main(), 0)
                collect.assert_called_once_with(migrate.MIGRATIONS_DIRECTORY)
                self.history.assert_not_called()
                self.assertIn(expected, output.getvalue())

    def test_status_preserves_history_output_and_skips_local_files(self):
        self.history.side_effect = None
        self.history.return_value = [history_row()]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["migrate.py", "status"]),
            patch.object(migrate, "collect_migrations") as collect,
            patch("sys.stdout", output),
        ):
            self.assertEqual(migrate.main(), 0)
        collect.assert_not_called()
        self.history.assert_called_once_with()
        self.assertIn("checksum karşılaştırması yapmaz", output.getvalue())
        self.assertIn(f"V0001 | V0001__example.sql | {CHECKSUM} | SUCCESS | - | -", output.getvalue())

    def test_status_empty_history_remains_successful(self):
        self.history.side_effect = None
        self.history.return_value = []
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(migrate.show_status(), 0)
        self.assertIn("Migration geçmişi boş; bu normal bir durumdur.", output.getvalue())


class ApplyCommandTests(OfflineTestCase):
    """CLI dispatch/çıktı testi: executor her zaman mock, Oracle/.env sınırları kapalı."""

    def setUp(self):
        super().setUp()
        self.completed = migrate.MigrationExecutionResult(
            phase=migrate.ExecutionPhase.COMPLETE,
            ddl_outcome=migrate.DDLOutcome.SUCCEEDED,
            history_state=migrate.HistoryState.SUCCESS_CONFIRMED,
            classification=migrate.ExecutionClassification(migrate.ExecutionOutcome.COMPLETE, False),
            success=True)
        self.executor = self.enterContext(patch.object(migrate, "execute_one_migration", return_value=self.completed))
        self.open_connection = self.enterContext(patch.object(
            migrate, "open_oracle_connection", side_effect=AssertionError("CLI testi gerçek bağlantı açamaz")))
        self.settings = self.enterContext(patch.object(
            migrate, "load_connection_settings", side_effect=AssertionError("CLI testi .env okuyamaz")))
        self.collect = self.enterContext(patch.object(
            migrate, "collect_migrations", side_effect=AssertionError("apply discovery işini executor'a bırakmalı")))

    def tearDown(self):
        self.open_connection.assert_not_called()
        self.settings.assert_not_called()
        super().tearDown()

    def run_cli(self, command="apply"):
        with (patch.object(sys, "argv", ["migrate.py", command]),
              patch("sys.stdout", new_callable=io.StringIO) as output,
              patch("sys.stderr", new_callable=io.StringIO) as errors):
            code = migrate.main()
        return code, output.getvalue(), errors.getvalue()

    def no_pending(self):
        return migrate.MigrationExecutionResult(
            phase=migrate.ExecutionPhase.INITIAL_PLAN,
            ddl_outcome=migrate.DDLOutcome.NOT_STARTED,
            history_state=migrate.HistoryState.NOT_RECORDED,
            classification=migrate.ExecutionClassification(migrate.ExecutionOutcome.NOT_EXECUTED, False),
            no_pending=True)

    def test_apply_is_recognized_without_executing(self):
        self.assertEqual(migrate.build_parser().parse_args(["apply"]).command, "apply")
        self.executor.assert_not_called()

    def test_apply_dispatches_once_and_preserves_success(self):
        code, output, errors = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Uygulama başarılı", output)
        self.assertIn("SUCCESS", output)
        self.assertEqual(errors, "")
        self.executor.assert_called_once_with()
        self.collect.assert_not_called()
        self.history.assert_not_called()

    def test_no_pending_is_normal_successful_noop(self):
        self.executor.return_value = self.no_pending()
        code, output, errors = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("PENDING migration yok", output)
        self.assertIn("hiçbir migration uygulanmadı", output)
        self.assertNotIn("Uygulama başarılı", output)
        self.assertEqual(errors, "")
        self.executor.assert_called_once_with()

    def test_unknown_ddl_preserves_failure_and_requires_review(self):
        self.executor.return_value = replace(self.completed,
            phase=migrate.ExecutionPhase.DDL, ddl_outcome=migrate.DDLOutcome.UNKNOWN,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED, success=False,
            classification=migrate.ExecutionClassification(migrate.ExecutionOutcome.DDL_UNKNOWN, True))
        code, output, errors = self.run_cli()
        self.assertEqual(code, self.executor.return_value.exit_code)
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Manuel inceleme gerekiyor", errors)
        self.assertIn("UNKNOWN", errors)
        self.assertIn("RUNNING_CONFIRMED", errors)
        self.executor.assert_called_once_with()

    def test_complete_classification_does_not_hide_cleanup_failure(self):
        self.executor.return_value = replace(self.completed, success=False, cleanup_failed=True)
        code, output, errors = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("temizliği başarısız", errors)
        self.assertIn("Manuel inceleme gerekiyor", errors)
        self.assertIn("SUCCESS_CONFIRMED", errors)
        self.assertNotIn("Uygulama başarılı", errors)
        self.executor.assert_called_once_with()

    def test_no_pending_does_not_override_cleanup_failure(self):
        self.executor.return_value = replace(self.no_pending(), cleanup_failed=True)
        code, output, errors = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("temizliği başarısız", errors)
        self.assertIn("Manuel inceleme gerekiyor", errors)

    def test_no_pending_does_not_override_interruption(self):
        self.executor.return_value = replace(self.no_pending(), interrupted=True)
        code, output, errors = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("İşlem kesildi", errors)

    def test_commit_uncertainty_is_visible_and_not_retried(self):
        self.executor.return_value = replace(self.completed,
            phase=migrate.ExecutionPhase.SUCCESS_COMMIT, success=False,
            history_state=migrate.HistoryState.COMMIT_UNKNOWN,
            classification=migrate.ExecutionClassification(migrate.ExecutionOutcome.HISTORY_COMMIT_UNKNOWN, True))
        code, _, errors = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("SUCCESS_COMMIT", errors)
        self.assertIn("COMMIT_UNKNOWN", errors)
        self.assertIn("otomatik tekrar denemeyin", errors)
        self.executor.assert_called_once_with()

    def test_confirmed_failed_history_warns_that_table_can_remain(self):
        self.executor.return_value = replace(self.completed, success=False,
            phase=migrate.ExecutionPhase.FAILED_COMMIT,
            history_state=migrate.HistoryState.FAILED_CONFIRMED,
            classification=migrate.ExecutionClassification(migrate.ExecutionOutcome.DDL_METADATA_MISMATCH, True),
            failed_history_confirmed=True)
        code, _, errors = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("FAILED kaydı kesinleşti", errors)
        self.assertIn("tablo kalıcı olabilir", errors)
        self.assertIn("Manuel inceleme gerekiyor", errors)

    def test_result_exit_code_is_not_recomputed_by_cli(self):
        with patch.object(migrate.MigrationExecutionResult, "exit_code", new_callable=PropertyMock, return_value=7):
            code, output, errors = self.run_cli()
        self.assertEqual(code, 7)
        self.assertEqual(output, "")
        self.assertNotIn("Uygulama başarılı", errors)
        self.executor.assert_called_once_with()

    def test_unexpected_executor_error_is_sanitized_without_retry(self):
        secret = "PASSWORD_DSN_AND_RAW_SQL_SECRET"
        self.executor.side_effect = RuntimeError(secret)
        code, output, errors = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertNotIn(secret, errors)
        self.assertNotIn("Traceback", errors)
        self.assertIn("manuel inceleme gerekiyor", errors)
        self.executor.assert_called_once_with()

    def test_unexpected_base_exceptions_stop_safely(self):
        class StopNow(BaseException):
            pass
        for error in (KeyboardInterrupt(), SystemExit(9), StopNow("SECRET")):
            with self.subTest(error_type=type(error).__name__):
                self.executor.reset_mock()
                self.executor.side_effect = error
                code, output, errors = self.run_cli()
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertNotIn("SECRET", errors)
                self.assertNotIn("Traceback", errors)
                self.executor.assert_called_once_with()

    def test_apply_help_never_calls_executor(self):
        with (patch.object(sys, "argv", ["migrate.py", "apply", "--help"]),
              patch("sys.stdout", new_callable=io.StringIO) as output):
            with self.assertRaises(SystemExit) as stopped:
                migrate.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("apply", output.getvalue())
        self.executor.assert_not_called()
        self.collect.assert_not_called()

    def test_apply_cli_does_not_read_or_supply_terminal_approval(self):
        with (patch("builtins.input", side_effect=AssertionError("Onay executor'a aittir")) as user_input,
              patch.object(migrate, "request_apply_confirmation", side_effect=AssertionError("CLI onayı atlayamaz")) as confirm):
            self.run_cli()
        self.executor.assert_called_once_with()
        user_input.assert_not_called()
        confirm.assert_not_called()

    def test_existing_commands_never_dispatch_to_executor(self):
        self.collect.side_effect = None
        self.collect.return_value = []
        self.history.side_effect = None
        self.history.return_value = []
        for command in ("validate", "plan", "status", "db-plan"):
            with self.subTest(command=command):
                self.assertEqual(self.run_cli(command)[0], 0)
        self.executor.assert_not_called()


class ApplyEligibilityTests(OfflineTestCase):
    def test_smallest_pending_version_is_selected(self):
        entries = migrate.compare_migrations(
            [local_migration(10), local_migration(2), local_migration(1)], []
        )
        result = migrate.assess_apply_eligibility(list(reversed(entries)))
        self.assertTrue(result.eligible)
        self.assertEqual(result.next_migration, entries[0])
        self.assertEqual(result.next_migration.version, 1)
        self.assertEqual(result.blockers, ())

    def test_no_pending_means_no_candidate(self):
        for entries in (
            [], migrate.compare_migrations([local_migration()], [history_row()]),
        ):
            with self.subTest(entries=entries):
                result = migrate.assess_apply_eligibility(entries)
                self.assertFalse(result.eligible)
                self.assertIsNone(result.next_migration)
                self.assertEqual(result.blockers, ())

    def test_problem_states_block_even_an_otherwise_valid_candidate(self):
        for status in ("FAILED", "RUNNING", "ORPHANED", "CHECKSUM MISMATCH"):
            with self.subTest(status=status):
                entries = [
                    migrate.DatabasePlanEntry(1, "V0001__example.sql", status),
                    migrate.DatabasePlanEntry(2, "V0002__example.sql", "PENDING"),
                ]
                result = migrate.assess_apply_eligibility(entries)
                self.assertFalse(result.eligible)
                self.assertIsNone(result.next_migration)
                self.assertTrue(any(status in blocker for blocker in result.blockers))

    def test_success_script_name_mismatch_blocks(self):
        entries = migrate.compare_migrations(
            [local_migration(1), local_migration(2)],
            [history_row(script_name="V0001__renamed.sql")],
        )
        self.assertEqual(entries[0].status, "APPLIED")
        result = migrate.assess_apply_eligibility(entries)
        self.assertFalse(result.eligible)
        self.assertIsNone(result.next_migration)
        self.assertTrue(any("SCRIPT_NAME" in blocker for blocker in result.blockers))

    def test_pending_below_highest_applied_blocks_entire_plan(self):
        entries = migrate.compare_migrations(
            [local_migration(2), local_migration(10), local_migration(20)], [history_row(10)]
        )
        result = migrate.assess_apply_eligibility(entries)
        self.assertFalse(result.eligible)
        self.assertIsNone(result.next_migration)
        self.assertTrue(any("V0002" in blocker and "V0010" in blocker for blocker in result.blockers))

    def test_version_gaps_alone_are_allowed(self):
        entries = migrate.compare_migrations(
            [local_migration(1), local_migration(10), local_migration(30)], [history_row(1)]
        )
        result = migrate.assess_apply_eligibility(entries)
        self.assertTrue(result.eligible)
        self.assertEqual(result.next_migration.version, 10)
        self.assertEqual(result.blockers, ())

    def test_duplicate_plan_versions_are_blocked(self):
        entry = migrate.DatabasePlanEntry(1, "V0001__example.sql", "PENDING")
        result = migrate.assess_apply_eligibility([entry, entry])
        self.assertFalse(result.eligible)
        self.assertIsNone(result.next_migration)
        self.assertTrue(any("tekrarlanan" in blocker for blocker in result.blockers))

    def test_unknown_plan_status_is_blocked(self):
        result = migrate.assess_apply_eligibility([
            migrate.DatabasePlanEntry(1, "V0001__example.sql", "UNKNOWN")
        ])
        self.assertFalse(result.eligible)
        self.assertIsNone(result.next_migration)
        self.assertTrue(result.blockers)


class CreateTableValidationTests(OfflineTestCase):
    def assert_sql_rejected(self, statements):
        for statement in statements:
            with self.subTest(sql=statement):
                with self.assertRaises(ValueError):
                    migrate.validate_create_table(statement)

    def test_supported_create_table_forms(self):
        statements = (
            "CREATE TABLE employees (id NUMBER)",
            "create table hr_core.employees (id number not null);",
            "CREATE TABLE employees (id NUMBER, name VARCHAR2(255 CHAR), "
            "hired_at DATE, notes CLOB, created_at TIMESTAMP, "
            "updated_at TIMESTAMP(6) WITH TIME ZONE)",
            "CREATE TABLE employees (id NUMBER, PRIMARY KEY (id))",
            "CREATE TABLE employees (id NUMBER NOT NULL, code NUMBER, "
            "CONSTRAINT pk_employees PRIMARY KEY (id, code))",
        )
        for statement in statements:
            with self.subTest(sql=statement):
                definition = migrate.validate_create_table(statement)
                self.assertIsInstance(definition, migrate.CreateTableDefinition)
                self.assertEqual(definition.table_name, "EMPLOYEES")
                self.assertEqual(definition.columns[0], "ID")
                self.assertTrue(definition.statement.startswith("CREATE TABLE "))

    def test_comments_are_removed_without_merging_tokens(self):
        definition = migrate.validate_create_table(
            "-- ön açıklama; 'literal'\nCREATE/* açıklama ; */TABLE employees "
            "(id NUMBER -- kolon açıklaması;\r\n); /* son yorum */"
        )
        self.assertEqual(definition.statement, "CREATE TABLE EMPLOYEES ( ID NUMBER )")
        self.assert_sql_rejected(["CRE/* yorum */ATE TABLE employees (id NUMBER)"])

    def test_supported_identifier_boundaries(self):
        for name in ("a", "a" * 30, "employee_2"):
            with self.subTest(name=name):
                definition = migrate.validate_create_table(f"CREATE TABLE {name} ({name} NUMBER)")
                self.assertEqual(definition.table_name, name.upper())
                self.assertEqual(definition.columns, (name.upper(),))

    def test_invalid_identifiers_are_rejected(self):
        for name in ("a" * 31, "_employee", "1employee", "emp$", "emp#", "çalışan",
                     "emp-name", "SYS_EMP", "ORA_EMP", "SELECT"):
            self.assert_sql_rejected([
                f"CREATE TABLE {name} (id NUMBER)",
                f"CREATE TABLE employees ({name} NUMBER)",
            ])

    def test_number_precision_and_scale_boundaries_are_accepted(self):
        for kind in ("NUMBER", "NUMBER(1)", "NUMBER(38)", "NUMBER(1,0)",
                     "NUMBER(1,1)", "NUMBER(38,0)", "NUMBER(38,38)"):
            with self.subTest(kind=kind):
                definition = migrate.validate_create_table(f"CREATE TABLE employees (id {kind})")
                self.assertEqual(definition.columns, ("ID",))

    def test_number_outside_mvp_bounds_is_rejected(self):
        # Bazıları Oracle'da geçerli olsa da mevcut MVP alt kümesinin dışındadır.
        self.assert_sql_rejected([
            f"CREATE TABLE employees (id {kind})" for kind in (
                "NUMBER(0)", "NUMBER(39)", "NUMBER(4,5)", "NUMBER(4,-1)",
                "NUMBER(*)", "NUMBER(4.0)", "NUMBER()", "NUMBER(10000)",
            )
        ])

    def test_varchar2_char_length_boundaries_are_accepted(self):
        for length in (1, 1000):
            with self.subTest(length=length):
                definition = migrate.validate_create_table(
                    f"CREATE TABLE employees (name VARCHAR2({length} CHAR))"
                )
                self.assertEqual(definition.columns, ("NAME",))

    def test_varchar2_invalid_lengths_and_semantics_are_rejected(self):
        self.assert_sql_rejected([
            f"CREATE TABLE employees (name {kind})" for kind in (
                "VARCHAR2(0 CHAR)", "VARCHAR2(1001 CHAR)", "VARCHAR2(-1 CHAR)",
                "VARCHAR2(10 BYTE)", "VARCHAR2(10)", "VARCHAR2(1.5 CHAR)",
            )
        ])

    def test_multiple_statements_are_rejected(self):
        base = "CREATE TABLE employees (id NUMBER)"
        self.assert_sql_rejected([
            base + "; CREATE TABLE departments (id NUMBER);",
            base + "; DROP TABLE employees;",
            base + " CREATE TABLE departments (id NUMBER)",
            base + ";;",
        ])

    def test_literals_constraints_and_unsupported_sql_are_rejected(self):
        self.assert_sql_rejected([
            "CREATE TABLE employees (id NUMBER 'value')",
            'CREATE TABLE "employees" (id NUMBER)',
            'CREATE TABLE employees ("id" NUMBER)',
            "CREATE TABLE employees (id NUMBER DEFAULT 1)",
            "CREATE TABLE employees (name VARCHAR2(10 CHAR) DEFAULT 'text')",
            "CREATE TABLE employees (name VARCHAR2(10 CHAR) DEFAULT q'[a;b]')",
            "CREATE TABLE employees (id NUMBER CHECK (id > 0))",
            "CREATE TABLE employees (id NUMBER, CHECK (id IS NOT NULL))",
            "CREATE TABLE employees (id NUMBER, FOREIGN KEY (id) REFERENCES departments(id))",
            "CREATE TABLE employees (id NUMBER REFERENCES departments(id))",
            "CREATE TABLE employees (id NUMBER UNIQUE)",
            "CREATE TABLE employees (id NUMBER, UNIQUE (id))",
            "CREATE TABLE employees (id NUMBER PRIMARY KEY)",
            "CREATE TABLE employees AS SELECT id FROM departments",
            "CREATE GLOBAL TEMPORARY TABLE employees (id NUMBER)",
            "CREATE TABLE employees (id BLOB)",
            "ALTER TABLE employees ADD (id NUMBER)",
            "BEGIN NULL; END;",
        ])

    def test_unclosed_nested_comments_and_hints_are_rejected(self):
        self.assert_sql_rejected([
            "/* kapanmamış CREATE TABLE employees (id NUMBER)",
            "CREATE TABLE employees (id NUMBER) /* kapanmamış",
            "/* dış /* iç */ yorum */ CREATE TABLE employees (id NUMBER)",
            "CREATE /*+ hint */ TABLE employees (id NUMBER)",
        ])

    def test_empty_or_comment_only_sql_is_rejected(self):
        self.assert_sql_rejected(["", " \r\n\t", "-- yorum", "/* yorum */",
                                  "-- yorum\n/* açıklama */\n", ";"])

    def test_comment_and_semicolon_statement_hiding_is_rejected(self):
        base = "CREATE TABLE employees (id NUMBER)"
        self.assert_sql_rejected([
            base + "; /* yorum */ DROP TABLE employees;",
            base + "; -- yorum\nCREATE TABLE departments (id NUMBER);",
            base + " /* ; */ DROP TABLE employees",
            base + ";/* -- ; */;",
            base + " -- ; DROP TABLE görünmez\n; DROP TABLE employees",
            base + ";\n/\nCREATE TABLE departments (id NUMBER)",
        ])
        # Yorumun içindeki metin yürütülebilir SQL değildir; güvenle atılır.
        definition = migrate.validate_create_table(base + "; -- DROP TABLE employees;\n")
        self.assertEqual(definition.table_name, "EMPLOYEES")

    def test_trailing_clauses_are_rejected(self):
        base = "CREATE TABLE employees (id NUMBER)"
        self.assert_sql_rejected([
            base + suffix for suffix in (
                " TABLESPACE users", " AS SELECT id FROM departments",
                " ORGANIZATION EXTERNAL", " ON COMMIT DELETE ROWS", " ENABLE ROW MOVEMENT",
            )
        ])

    def test_single_terminal_semicolon_is_optional_and_removed(self):
        base = "CREATE TABLE employees (id NUMBER)"
        expected = migrate.validate_create_table(base)
        for suffix in (";", "; \n", "; /* son yorum; */", " /* yorum */ ; -- son\n"):
            with self.subTest(suffix=suffix):
                definition = migrate.validate_create_table(base + suffix)
                self.assertEqual(definition, expected)
                self.assertNotIn(";", definition.statement)

    def test_schema_migrations_and_other_schemas_are_rejected(self):
        self.assert_sql_rejected([
            f"CREATE TABLE {name} (id NUMBER)" for name in (
                "SCHEMA_MIGRATIONS", "hr_core.schema_migrations", "OTHER.employees",
                "HR_CORE.HR_CORE.employees",
            )
        ])

    def test_invalid_columns_and_primary_keys_are_rejected(self):
        self.assert_sql_rejected([
            "CREATE TABLE employees ()",
            "CREATE TABLE employees (id NUMBER, ID NUMBER)",
            "CREATE TABLE employees (id NUMBER,)",
            "CREATE TABLE employees (id NUMBER, PRIMARY KEY (missing))",
            "CREATE TABLE employees (id NUMBER, PRIMARY KEY (id,id))",
            "CREATE TABLE employees (id NUMBER, PRIMARY KEY ())",
            "CREATE TABLE employees (id NUMBER, PRIMARY KEY (id), name DATE)",
        ])

    def test_timestamp_mvp_precision_and_timezone_rules(self):
        for kind in ("TIMESTAMP", "TIMESTAMP(6)", "TIMESTAMP WITH TIME ZONE",
                     "TIMESTAMP(6) WITH TIME ZONE"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    migrate.validate_create_table(f"CREATE TABLE employees (created_at {kind})").columns,
                    ("CREATED_AT",),
                )
        self.assert_sql_rejected([
            "CREATE TABLE employees (created_at TIMESTAMP(3))",
            "CREATE TABLE employees (created_at TIMESTAMP WITH LOCAL TIME ZONE)",
        ])


class PreparedMigrationTests(OfflineTestCase):
    def make_migration(self, content, path=Path("V0001__example.sql"), version=1):
        return migrate.Migration(version, path, hashlib.sha256(content).hexdigest())

    def test_checksum_and_text_use_exact_same_raw_bytes(self):
        content = "-- Türkçe açıklama\r\nCREATE TABLE employees (id NUMBER);\r\n".encode("utf-8")
        migration = self.make_migration(content)
        prepared = migrate.prepare_migration_content(migration, content)
        self.assertIsInstance(prepared, migrate.PreparedMigration)
        self.assertIs(prepared.content, content)
        self.assertEqual(prepared.text.encode("utf-8"), content)
        self.assertEqual(prepared.migration.checksum, hashlib.sha256(content).hexdigest())
        self.assertNotEqual(prepared.migration.checksum, hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest())
        self.assertNotEqual(prepared.migration.checksum, hashlib.sha256(prepared.definition.statement.encode()).hexdigest())

    def test_changed_checksum_is_rejected(self):
        original = b"CREATE TABLE employees (id NUMBER);"
        migration = self.make_migration(original)
        with self.assertRaisesRegex(ValueError, "değişmiş"):
            migrate.prepare_migration_content(migration, original + b"\n")

    def test_invalid_utf8_is_rejected_with_matching_checksum(self):
        content = b"CREATE TABLE employees (id NUMBER);\xff"
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            migrate.prepare_migration_content(self.make_migration(content), content)

    def test_empty_and_comment_only_content_is_rejected(self):
        for content in (b"", b" \n\t", b"-- comment\n", b"/* comment */"):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    migrate.prepare_migration_content(self.make_migration(content), content)

    def test_filename_and_version_mismatch_is_rejected(self):
        content = b"CREATE TABLE employees (id NUMBER)"
        for path, version in ((Path("invalid.sql"), 1), (Path("V0002__example.sql"), 1)):
            with self.subTest(path=path, version=version):
                with self.assertRaisesRegex(ValueError, "dosya adı"):
                    migrate.prepare_migration_content(self.make_migration(content, path, version), content)


class MigrationPathSafetyTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        # Geçici dizinler yalnızca test çalıştırıldığında oluşturulur ve temizlenir.
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.base / "migrations"
        self.directory.mkdir()
        self.content = b"CREATE TABLE employees (id NUMBER);"
        self.path = self.directory / "V0001__example.sql"

    def migration(self, path=None):
        return migrate.Migration(1, path or self.path, hashlib.sha256(self.content).hexdigest())

    def test_valid_path_is_accepted_with_one_content_read(self):
        self.path.write_bytes(self.content)
        with patch.object(Path, "read_bytes", autospec=True, return_value=self.content) as read:
            prepared = migrate.read_migration_for_apply(self.migration(), self.directory)
        read.assert_called_once_with(self.path.resolve())
        self.assertEqual(prepared.content, self.content)
        self.assertEqual(prepared.definition.table_name, "EMPLOYEES")

    def test_symlink_outside_migration_directory_is_rejected_before_read(self):
        outside = self.base / "outside.sql"
        outside.write_bytes(self.content)
        self.path.symlink_to(outside)
        with patch.object(Path, "read_bytes", autospec=True) as read:
            with self.assertRaises(ValueError):
                migrate.read_migration_for_apply(self.migration(), self.directory)
        read.assert_not_called()

    def test_symlink_resolving_inside_directory_is_accepted(self):
        target = self.directory / "target.sql"
        target.write_bytes(self.content)
        self.path.symlink_to(target)
        prepared = migrate.read_migration_for_apply(self.migration(), self.directory)
        self.assertEqual(prepared.content, self.content)
        self.assertEqual(prepared.migration.path, self.path)

    def test_missing_nonfile_and_outside_paths_are_rejected(self):
        outside = self.base / "V0001__example.sql"
        outside.write_bytes(self.content)
        nonfile = self.directory / "folder"
        nonfile.mkdir()
        for path in (self.path, nonfile, outside):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    migrate.read_migration_for_apply(self.migration(path), self.directory)

    def test_real_file_changed_since_discovery_is_rejected(self):
        self.path.write_bytes(self.content + b"\n")
        with self.assertRaisesRegex(ValueError, "değişmiş"):
            migrate.read_migration_for_apply(self.migration(), self.directory)


class DatabasePlanCommandRegressionTests(OfflineTestCase):
    def test_db_plan_cli_stays_read_only_and_does_not_use_apply_helpers(self):
        self.history.side_effect = None
        self.history.return_value = []
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["migrate.py", "db-plan"]),
            patch.object(migrate, "collect_migrations", return_value=[local_migration()]) as collect,
            patch.object(migrate, "assess_apply_eligibility") as assess,
            patch.object(migrate, "read_migration_for_apply") as prepare,
            patch("sys.stdout", output),
        ):
            self.assertEqual(migrate.main(), 0)
        collect.assert_called_once_with(migrate.MIGRATIONS_DIRECTORY)
        self.history.assert_called_once_with("Db-plan")
        assess.assert_not_called()
        prepare.assert_not_called()
        self.assertIn("V0001 | V0001__example.sql | PENDING", output.getvalue())
        self.assertIn("PENDING: 1", output.getvalue())


class MockOracleError(Exception):
    """Gerçek Oracle sürücüsü gerektirmeyen hata türü."""


class ConnectionHelperTestCase(unittest.TestCase):
    def setUp(self):
        self.password = "synthetic-test-password-never-print"
        self.settings = {
            "DB_HOST": "test-host.invalid", "DB_PORT": "1521",
            "DB_SERVICE": "FREEPDB1", "DB_USER": "HR_CORE", "DB_PASSWORD": self.password,
        }
        self.enterContext(patch.dict(migrate.os.environ, self.settings, clear=True))
        self.connection = MagicMock(spec=[
            "__enter__", "__exit__", "cursor", "close", "commit", "rollback", "autocommit",
        ])
        self.connection.__enter__.return_value = self.connection
        self.connection.__exit__.return_value = False
        self.cursor = MagicMock(spec=["__enter__", "__exit__", "execute", "fetchone", "fetchall"])
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.__exit__.return_value = False
        self.cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", "HR_CORE")
        self.cursor.fetchall.return_value = [history_row()]
        self.connection.cursor.return_value = self.cursor
        self.driver = ModuleType("oracledb")
        self.driver.Error = MockOracleError
        self.driver.is_thin_mode = Mock(return_value=True)
        self.driver.ConnectParams = Mock(return_value=object())
        self.driver.connect = Mock(return_value=self.connection)
        self.loader = Mock()
        dotenv = ModuleType("dotenv")
        dotenv.load_dotenv = self.loader
        # read_database_history içindeki import'lar gerçek paketleri yükleyemez.
        self.enterContext(patch.dict(sys.modules, {"oracledb": self.driver, "dotenv": dotenv}))
        self.stdout = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))
        self.stderr = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def oracle_error(self, code="DPY-6005"):
        return MockOracleError(SimpleNamespace(full_code=code, message=self.password))

    def assert_no_secret_output(self):
        self.assertNotIn(self.password, self.stdout.getvalue() + self.stderr.getvalue())
        self.assertNotIn(self.settings["DB_HOST"], self.stdout.getvalue() + self.stderr.getvalue())


class LoadConnectionSettingsTests(ConnectionHelperTestCase):
    def test_valid_settings_and_override_false(self):
        settings, port = migrate.load_connection_settings(self.loader)
        self.loader.assert_called_once_with(migrate.PROJECT_ROOT / ".env", override=False)
        self.assertEqual(settings, self.settings)
        self.assertEqual(port, 1521)
        self.assertIsInstance(port, int)
        self.driver.connect.assert_not_called()
        self.assert_no_secret_output()

    def test_environment_values_take_priority_over_simulated_dotenv(self):
        fake_file_values = {name: "file-value" for name in self.settings}

        def simulate_dotenv(path, *, override):
            for name, value in fake_file_values.items():
                if override or name not in migrate.os.environ:
                    migrate.os.environ[name] = value

        self.loader.side_effect = simulate_dotenv
        # Dosya okunmaz; mock loader yalnızca bellekteki verilerle çalışır.
        migrate.os.environ.pop("DB_SERVICE")
        fake_file_values["DB_SERVICE"] = "FREEPDB1"
        settings, port = migrate.load_connection_settings(self.loader)
        self.assertEqual(settings, self.settings)
        self.assertEqual(port, 1521)
        self.loader.assert_called_once_with(migrate.PROJECT_ROOT / ".env", override=False)

    def test_missing_settings_are_rejected_without_secret_values(self):
        for name in self.settings:
            with self.subTest(name=name), patch.dict(migrate.os.environ, self.settings, clear=True):
                del migrate.os.environ[name]
                with self.assertRaises(ValueError) as caught:
                    migrate.load_connection_settings(self.loader)
                self.assertIn(name, str(caught.exception))
                self.assertNotIn(self.password, str(caught.exception))

    def test_empty_and_whitespace_settings_are_rejected(self):
        for name in self.settings:
            for value in ("", " \t\n"):
                with self.subTest(name=name, value=value), patch.dict(migrate.os.environ, {name: value}):
                    with self.assertRaises(ValueError) as caught:
                        migrate.load_connection_settings(self.loader)
                    self.assertIn(name, str(caught.exception))
                    self.assertNotIn(self.password, str(caught.exception))

    def test_invalid_ports_are_rejected_without_echoing_values(self):
        for value in ("0", "65536", "-1", "1521.5", "not-a-port", self.password):
            with self.subTest(value=value), patch.dict(migrate.os.environ, {"DB_PORT": value}):
                with self.assertRaisesRegex(ValueError, "DB_PORT") as caught:
                    migrate.load_connection_settings(self.loader)
                self.assertNotIn(self.password, str(caught.exception))

    def test_port_boundaries_are_accepted(self):
        for value in ("1", "65535"):
            with self.subTest(value=value), patch.dict(migrate.os.environ, {"DB_PORT": value}):
                _, port = migrate.load_connection_settings(self.loader)
                self.assertEqual(port, int(value))

    def test_dotenv_read_errors_are_sanitized(self):
        for error in (OSError(self.password), UnicodeError(self.password)):
            with self.subTest(error_type=type(error).__name__):
                self.loader.side_effect = error
                with self.assertRaisesRegex(ValueError, "okunamadı") as caught:
                    migrate.load_connection_settings(self.loader)
                self.assertNotIn(self.password, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)


class OpenOracleConnectionTests(ConnectionHelperTestCase):
    def test_thin_connection_is_returned_with_autocommit_disabled(self):
        self.connection.autocommit = True
        result = migrate.open_oracle_connection(self.settings, 1521, self.driver)
        self.assertIs(result, self.connection)
        self.assertIs(self.connection.autocommit, False)
        self.driver.is_thin_mode.assert_called_once_with()
        self.driver.ConnectParams.assert_called_once_with(
            host=self.settings["DB_HOST"], port=1521, service_name="FREEPDB1",
        )
        self.driver.connect.assert_called_once_with(
            user="HR_CORE", password=self.password, params=self.driver.ConnectParams.return_value,
        )
        self.connection.cursor.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.assert_no_secret_output()

    def test_thick_mode_is_rejected_before_connect(self):
        self.driver.is_thin_mode.return_value = False
        with self.assertRaisesRegex(ValueError, "Thin"):
            migrate.open_oracle_connection(self.settings, 1521, self.driver)
        self.driver.ConnectParams.assert_not_called()
        self.driver.connect.assert_not_called()
        self.assert_no_secret_output()

    def test_connection_error_is_propagated_without_printing_secrets(self):
        # Alt yardımcı hatayı çağırana taşır; kullanıcıya güvenli raporlamayı
        # read_database_history yapar. Ham exception'ın temizlendiği varsayılmaz.
        error = self.oracle_error()
        self.driver.connect.side_effect = error
        with self.assertRaises(MockOracleError) as caught:
            migrate.open_oracle_connection(self.settings, 1521, self.driver)
        self.assertIs(caught.exception, error)
        self.connection.close.assert_not_called()
        self.assert_no_secret_output()

    def test_autocommit_setup_failure_closes_connection(self):
        error = self.oracle_error()
        with patch.object(type(self.connection), "autocommit", PropertyMock(side_effect=error), create=True):
            with self.assertRaises(migrate.ConnectionSetupError) as caught:
                migrate.open_oracle_connection(self.settings, 1521, self.driver)
        self.assertNotIn(self.password, str(caught.exception))
        self.assertFalse(caught.exception.setup_interrupted)
        self.assertFalse(caught.exception.cleanup_failed)
        self.connection.close.assert_called_once_with()
        self.assert_no_secret_output()

    def test_cleanup_error_does_not_hide_autocommit_setup_error(self):
        error = self.oracle_error()
        self.connection.close.side_effect = RuntimeError("synthetic cleanup failure")
        with patch.object(type(self.connection), "autocommit", PropertyMock(side_effect=error), create=True):
            with self.assertRaises(migrate.ConnectionSetupError) as caught:
                migrate.open_oracle_connection(self.settings, 1521, self.driver)
        self.assertNotIn(self.password, str(caught.exception))
        self.assertFalse(caught.exception.setup_interrupted)
        self.assertTrue(caught.exception.cleanup_failed)
        self.assertFalse(caught.exception.cleanup_interrupted)
        self.connection.close.assert_called_once_with()
        self.assert_no_secret_output()


class ValidateTargetSessionTests(ConnectionHelperTestCase):
    def test_only_expected_target_is_accepted(self):
        self.assertIsNone(migrate.validate_target_session(self.connection))
        self.cursor.execute.assert_called_once_with(
            "SELECT SYS_CONTEXT('USERENV', 'SESSION_USER'), "
            "SYS_CONTEXT('USERENV', 'CON_NAME'), "
            "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL"
        )
        self.cursor.fetchone.assert_called_once_with()
        self.cursor.__exit__.assert_called_once_with(None, None, None)
        self.connection.close.assert_not_called()
        self.driver.connect.assert_not_called()

    def test_wrong_user_pdb_and_empty_result_are_rejected(self):
        for target in (("OTHER", "FREEPDB1", "HR_CORE"), ("HR_CORE", "OTHER", "HR_CORE"),
                       ("hr_core", "FREEPDB1", "HR_CORE"), None):
            with self.subTest(target=target):
                self.cursor.reset_mock()
                self.cursor.fetchone.return_value = target
                with self.assertRaisesRegex(ValueError, "HR_CORE / FREEPDB1"):
                    migrate.validate_target_session(self.connection)
                self.cursor.execute.assert_called_once()
                self.assertNotIn("SCHEMA_MIGRATIONS", self.cursor.execute.call_args.args[0])
                self.cursor.__exit__.assert_called_once()

    def test_wrong_current_schema_is_rejected(self):
        for schema in ("OTHER", "hr_core", "", None):
            with self.subTest(schema=schema):
                self.cursor.reset_mock()
                self.cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", schema)
                with self.assertRaisesRegex(ValueError, "CURRENT_SCHEMA=HR_CORE"):
                    migrate.validate_target_session(self.connection)
                self.cursor.execute.assert_called_once()
                self.cursor.__exit__.assert_called_once()
        self.connection.close.assert_not_called()

    def test_missing_or_unexpected_session_result_is_rejected(self):
        for target in (None, (), ("HR_CORE",), ("HR_CORE", "FREEPDB1"),
                       ("HR_CORE", "FREEPDB1", "HR_CORE", "EXTRA"), "HR_CORE",
                       (None, "FREEPDB1", "HR_CORE"), ("HR_CORE", None, "HR_CORE")):
            with self.subTest(target=target):
                self.cursor.reset_mock()
                self.cursor.fetchone.return_value = target
                with self.assertRaises(ValueError):
                    migrate.validate_target_session(self.connection)
                self.cursor.__exit__.assert_called_once()
        self.driver.connect.assert_not_called()

    def test_target_query_error_exits_cursor_without_closing_connection(self):
        error = self.oracle_error()
        self.cursor.execute.side_effect = error
        with self.assertRaises(MockOracleError):
            migrate.validate_target_session(self.connection)
        self.cursor.__exit__.assert_called_once()
        self.assertIs(self.cursor.__exit__.call_args.args[1], error)
        self.connection.close.assert_not_called()


class ReadHistoryFromConnectionTests(ConnectionHelperTestCase):
    def test_existing_connection_query_and_transaction_ownership(self):
        rows = [history_row(1), history_row(10)]
        self.cursor.fetchall.return_value = rows
        result = migrate.read_history_from_connection(self.connection)
        self.assertIs(result, rows)
        self.connection.cursor.assert_called_once_with()
        self.cursor.execute.assert_called_once_with(
            "SELECT VERSION, SCRIPT_NAME, CHECKSUM, STATUS, STARTED_AT, FINISHED_AT "
            "FROM HR_CORE.SCHEMA_MIGRATIONS ORDER BY VERSION"
        )
        self.cursor.fetchall.assert_called_once_with()
        self.driver.connect.assert_not_called()
        self.driver.ConnectParams.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.__enter__.assert_not_called()
        self.connection.__exit__.assert_not_called()
        self.cursor.__exit__.assert_called_once_with(None, None, None)

    def test_empty_history_is_returned_unchanged(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(migrate.read_history_from_connection(self.connection), [])
        self.connection.close.assert_not_called()

    def test_query_and_fetch_errors_release_cursor_not_connection(self):
        for method in (self.cursor.execute, self.cursor.fetchall):
            with self.subTest(method=method._mock_name):
                self.cursor.reset_mock(side_effect=True)
                error = self.oracle_error("ORA-00942")
                method.side_effect = error
                with self.assertRaises(MockOracleError) as caught:
                    migrate.read_history_from_connection(self.connection)
                self.assertIs(caught.exception, error)
                self.cursor.__exit__.assert_called_once()
                self.connection.close.assert_not_called()
                self.connection.commit.assert_not_called()
                self.connection.rollback.assert_not_called()


class ReadDatabaseHistoryTests(ConnectionHelperTestCase):
    def test_helpers_are_called_in_order_on_same_connection(self):
        rows = [history_row()]
        manager = Mock()
        with (
            patch.object(migrate, "load_connection_settings", return_value=(self.settings, 1521)) as load,
            patch.object(migrate, "open_oracle_connection", return_value=self.connection) as open_connection,
            patch.object(migrate, "validate_target_session") as validate,
            patch.object(migrate, "read_history_from_connection", return_value=rows) as history,
        ):
            for name, child in (
                ("load", load), ("open", open_connection),
                ("enter", self.connection.__enter__), ("validate", validate),
                ("history", history), ("exit", self.connection.__exit__),
            ):
                manager.attach_mock(child, name)
            self.assertIs(migrate.read_database_history(), rows)
        self.assertEqual(manager.mock_calls, [
            call.load(self.loader), call.open(self.settings, 1521, self.driver),
            call.enter(), call.validate(self.connection), call.history(self.connection),
            call.exit(None, None, None),
        ])
        self.assertEqual(self.stdout.getvalue(), "Hedef: HR_CORE / FREEPDB1\n")
        self.assert_no_secret_output()

    def test_settings_failure_never_opens_connection(self):
        with patch.dict(migrate.os.environ, {"DB_PORT": "invalid"}):
            self.assertIsNone(migrate.read_database_history())
        self.driver.connect.assert_not_called()
        self.assertIn("DB_PORT", self.stderr.getvalue())
        self.assert_no_secret_output()

    def test_connection_failure_reports_only_safe_code_and_message(self):
        self.driver.connect.side_effect = self.oracle_error()
        self.assertIsNone(migrate.read_database_history())
        self.assertIn("DPY-6005", self.stderr.getvalue())
        self.assertIn("Oracle sunucusuna erişilemiyor", self.stderr.getvalue())
        self.connection.__enter__.assert_not_called()
        self.assert_no_secret_output()

    def test_wrong_target_skips_history_and_exits_connection(self):
        for target in (("OTHER", "FREEPDB1", "HR_CORE"),
                       ("HR_CORE", "OTHER", "HR_CORE"), ("HR_CORE", "FREEPDB1", "OTHER")):
            with self.subTest(target=target):
                self.connection.reset_mock()
                self.cursor.reset_mock()
                self.cursor.fetchone.return_value = target
                with patch.object(migrate, "read_history_from_connection") as history:
                    self.assertIsNone(migrate.read_database_history())
                history.assert_not_called()
                self.cursor.execute.assert_called_once()
                self.connection.__exit__.assert_called_once()
                self.assertIs(self.connection.__exit__.call_args.args[0], ValueError)
        self.assert_no_secret_output()

    def test_history_error_cleans_contexts_and_sanitizes_message(self):
        self.cursor.fetchall.side_effect = self.oracle_error("ORA-00942")
        self.assertIsNone(migrate.read_database_history())
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.assertIn("CURRENT_SCHEMA", self.cursor.execute.call_args_list[0].args[0])
        self.assertEqual(
            self.cursor.execute.call_args_list[1].args[0],
            "SELECT VERSION, SCRIPT_NAME, CHECKSUM, STATUS, STARTED_AT, FINISHED_AT "
            "FROM HR_CORE.SCHEMA_MIGRATIONS ORDER BY VERSION",
        )
        self.cursor.fetchall.assert_called_once_with()
        self.assertEqual(self.cursor.__exit__.call_count, 2)
        self.connection.__exit__.assert_called_once()
        self.assertIs(self.connection.__exit__.call_args.args[0], MockOracleError)
        self.assertIn("ORA-00942", self.stderr.getvalue())
        self.assertNotIn("Hedef:", self.stdout.getvalue())
        self.assert_no_secret_output()

    def test_invalid_error_code_is_not_echoed(self):
        self.driver.connect.side_effect = self.oracle_error(self.password)
        self.assertIsNone(migrate.read_database_history())
        self.assertIn("ORACLE_ERROR", self.stderr.getvalue())
        self.assert_no_secret_output()

    def test_status_and_db_plan_success_through_real_wrapper_with_mock_driver(self):
        for command in ("status", "db-plan"):
            with self.subTest(command=command):
                self.stdout.truncate(0)
                self.stdout.seek(0)
                self.cursor.fetchall.return_value = [history_row()]
                with (
                    patch.object(sys, "argv", ["migrate.py", command]),
                    patch.object(migrate, "collect_migrations", return_value=[local_migration()]) as collect,
                ):
                    self.assertEqual(migrate.main(), 0)
                if command == "status":
                    collect.assert_not_called()
                    self.assertIn("checksum karşılaştırması yapmaz", self.stdout.getvalue())
                    self.assertIn("SUCCESS", self.stdout.getvalue())
                else:
                    collect.assert_called_once_with(migrate.MIGRATIONS_DIRECTORY)
                    self.assertIn("APPLIED: 1", self.stdout.getvalue())
                self.assert_no_secret_output()

    def test_status_and_db_plan_connection_failure_keep_failure_exit(self):
        self.driver.connect.side_effect = self.oracle_error()
        for command in ("status", "db-plan"):
            with self.subTest(command=command), patch.object(sys, "argv", ["migrate.py", command]):
                with patch.object(migrate, "collect_migrations", return_value=[local_migration()]):
                    self.assertEqual(migrate.main(), 1)
                self.assert_no_secret_output()


class ApplyPlanTestCase(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.content = b"-- reviewed source\nCREATE TABLE employees (id NUMBER);\n"
        self.checksum = hashlib.sha256(self.content).hexdigest()
        # Dosya sınırı mock; checksum, SQL doğrulaması ve plan gerçek fonksiyonlardır.
        self.reader = self.enterContext(patch.object(
            migrate, "read_migration_for_apply",
            side_effect=lambda migration, directory: migrate.prepare_migration_content(migration, self.content),
        ))

    def local(self, version=1):
        return local_migration(version, self.checksum)

    def make_plan(self, version=1):
        return migrate.build_apply_plan([self.local(version)], [])


class BuildApplyPlanTests(ApplyPlanTestCase):
    def test_smallest_eligible_pending_is_selected(self):
        migrations = [self.local(10), self.local(2), self.local(1)]
        plan = migrate.build_apply_plan(
            migrations, [history_row(1, checksum=self.checksum)]
        )
        self.assertEqual(plan.version, 2)
        self.reader.assert_called_once_with(migrations[1], migrate.MIGRATIONS_DIRECTORY)

    def test_problem_states_reject_plan_before_source_read(self):
        for status in ("FAILED", "RUNNING", "ORPHANED", "CHECKSUM MISMATCH"):
            with self.subTest(status=status):
                self.reader.reset_mock()
                migrations = [self.local(2)] if status == "ORPHANED" else [self.local(1), self.local(2)]
                record_status = status if status in {"FAILED", "RUNNING"} else "SUCCESS"
                checksum = OTHER_CHECKSUM if status == "CHECKSUM MISMATCH" else self.checksum
                with self.assertRaisesRegex(ValueError, status):
                    migrate.build_apply_plan(migrations, [history_row(1, record_status, checksum)])
                self.reader.assert_not_called()

    def test_no_eligible_candidate_returns_none(self):
        for migrations, history in (
            ([], []), ([self.local()], [history_row(checksum=self.checksum)]),
        ):
            with self.subTest(migrations=migrations):
                self.assertIsNone(migrate.build_apply_plan(migrations, history))
        self.reader.assert_not_called()

    def test_plan_is_immutable(self):
        plan = self.make_plan()
        with self.assertRaises(FrozenInstanceError):
            plan.version = 2
        with self.assertRaises(FrozenInstanceError):
            plan.comparison_snapshot[0].status = "APPLIED"
        self.assertIsInstance(plan.source_bytes, bytes)
        self.assertIsInstance(plan.comparison_snapshot, tuple)

    def test_plan_matches_validated_source_checksum_and_sql(self):
        plan = self.make_plan()
        definition = migrate.validate_create_table(self.content.decode("utf-8"))
        self.assertEqual((plan.target_user, plan.target_pdb), ("HR_CORE", "FREEPDB1"))
        self.assertEqual(plan.script_name, "V0001__example.sql")
        self.assertEqual(plan.source_bytes, self.content)
        self.assertEqual(plan.checksum, hashlib.sha256(plan.source_bytes).hexdigest())
        self.assertEqual(plan.statement, definition.statement)
        self.assertEqual(plan.target_table, definition.table_name)
        self.driver.connect.assert_not_called()
        self.loader.assert_not_called()

    def test_success_script_name_mismatch_blocks_plan(self):
        with self.assertRaisesRegex(ValueError, "SCRIPT_NAME"):
            migrate.build_apply_plan(
                [self.local(), self.local(2)],
                [history_row(checksum=self.checksum, script_name="V0001__renamed.sql")],
            )
        self.reader.assert_not_called()

    def test_wrong_target_is_rejected(self):
        for kwargs in ({"target_user": "OTHER"}, {"target_pdb": "OTHER"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                migrate.build_apply_plan([self.local()], [], **kwargs)
        self.reader.assert_not_called()


class ApplyPlanComparisonTests(ApplyPlanTestCase):
    def test_equal_plans_are_accepted(self):
        approved = self.make_plan()
        self.assertIsNone(migrate.validate_apply_plan_unchanged(approved, replace(approved)))

    def test_changed_plan_fields_are_rejected(self):
        approved = self.make_plan()
        changes = {
            "version": 2, "script_name": "V0001__renamed.sql", "checksum": OTHER_CHECKSUM,
            "target_table": "DEPARTMENTS", "statement": "CREATE TABLE DEPARTMENTS ( ID NUMBER )",
            "target_user": "OTHER", "target_pdb": "OTHER", "source_bytes": self.content + b"\n",
        }
        for field_name, value in changes.items():
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                migrate.validate_apply_plan_unchanged(approved, replace(approved, **{field_name: value}))

    def test_disappearing_candidate_is_rejected(self):
        with self.assertRaises(ValueError):
            migrate.validate_apply_plan_unchanged(self.make_plan(), None)

    def test_different_migration_cannot_replace_approved_candidate(self):
        approved = self.make_plan(1)
        current = self.make_plan(2)
        with self.assertRaises(ValueError):
            migrate.validate_apply_plan_unchanged(approved, current)

    def test_changed_comparison_snapshot_is_rejected(self):
        approved = self.make_plan()
        changed = replace(approved, comparison_snapshot=())
        with self.assertRaises(ValueError):
            migrate.validate_apply_plan_unchanged(approved, changed)


class ApplyConfirmationTests(ApplyPlanTestCase):
    def confirmation_text(self, plan):
        return f"APPLY {plan.target_user}/{plan.target_pdb} V{plan.version:04d} {plan.checksum}"

    def test_exact_confirmation_is_accepted(self):
        plan = self.make_plan()
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value=self.confirmation_text(plan)) as user_input,
        ):
            self.assertIsNone(migrate.request_apply_confirmation(plan))
        self.assertIn(self.confirmation_text(plan), user_input.call_args.args[0])
        self.driver.connect.assert_not_called()

    def test_wrong_empty_or_modified_confirmation_is_rejected(self):
        plan = self.make_plan()
        expected = self.confirmation_text(plan)
        for answer in ("", "yes", expected.lower(), " " + expected, expected + " ", expected[:-1]):
            with self.subTest(answer=answer), patch.object(sys.stdin, "isatty", return_value=True):
                with patch("builtins.input", return_value=answer):
                    with self.assertRaises(migrate.ApplyApprovalError):
                        migrate.request_apply_confirmation(plan)

    def test_eof_interrupt_and_input_error_are_rejected(self):
        plan = self.make_plan()
        for error in (EOFError(), KeyboardInterrupt(), OSError(self.password)):
            with self.subTest(error_type=type(error).__name__), patch.object(sys.stdin, "isatty", return_value=True):
                with patch("builtins.input", side_effect=error):
                    with self.assertRaises(migrate.ApplyApprovalError) as caught:
                        migrate.request_apply_confirmation(plan)
                self.assertNotIn(self.password, str(caught.exception))

    def test_noninteractive_terminal_does_not_prompt(self):
        plan = self.make_plan()
        with patch.object(sys.stdin, "isatty", return_value=False), patch("builtins.input") as user_input:
            with self.assertRaises(migrate.ApplyApprovalError):
                migrate.request_apply_confirmation(plan)
        user_input.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")

    def test_confirmation_screen_has_plan_warning_but_no_secrets(self):
        plan = self.make_plan()
        with patch.object(sys.stdin, "isatty", return_value=True):
            with patch("builtins.input", return_value=self.confirmation_text(plan)) as user_input:
                migrate.request_apply_confirmation(plan)
        output = self.stdout.getvalue()
        for expected in ("HR_CORE / FREEPDB1", "V0001", plan.script_name, plan.checksum,
                         "HR_CORE.EMPLOYEES", "DDL", "implicit commit", "rollback ile geri alınamaz"):
            self.assertIn(expected, output)
        self.assert_no_secret_output()
        self.assertNotIn(self.password, user_input.call_args.args[0])
        self.assertNotIn(plan.statement, output)
        self.loader.assert_not_called()


class MigrationLockHelperTests(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.handle = "synthetic-lock-handle"
        self.cursor = MagicMock(spec=["__enter__", "__exit__", "var", "callproc", "callfunc"])
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.__exit__.return_value = False
        self.cursor.var.return_value.getvalue.return_value = self.handle
        self.cursor.callfunc.return_value = 0
        self.connection.cursor.return_value = self.cursor

    def assert_connection_ownership_preserved(self):
        self.driver.connect.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.loader.assert_not_called()

    def test_allocate_uses_fixed_schema_lock_name_and_output_bind(self):
        self.assertEqual(migrate.MIGRATION_LOCK_NAME, "enterprise-hrms:FREEPDB1:HR_CORE:migrations")
        self.assertEqual(migrate.allocate_migration_lock(self.connection), self.handle)
        self.cursor.var.assert_called_once_with(str, size=128)
        self.cursor.callproc.assert_called_once_with(
            "SYS.DBMS_LOCK.ALLOCATE_UNIQUE",
            [migrate.MIGRATION_LOCK_NAME, self.cursor.var.return_value],
        )
        self.cursor.callfunc.assert_not_called()
        self.assert_connection_ownership_preserved()

    def test_allocate_invalid_handle_is_rejected(self):
        for value in (None, "", "a" * 129, 123):
            with self.subTest(value=value):
                self.cursor.var.return_value.getvalue.return_value = value
                with self.assertRaises(migrate.MigrationLockError):
                    migrate.allocate_migration_lock(self.connection)
        self.assert_connection_ownership_preserved()

    def test_allocate_driver_error_is_sanitized(self):
        self.cursor.callproc.side_effect = self.oracle_error()
        with self.assertRaises(migrate.MigrationLockError) as caught:
            migrate.allocate_migration_lock(self.connection)
        self.assertNotIn(self.password, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assert_connection_ownership_preserved()

    def test_request_uses_exclusive_nowait_and_false_release_on_commit(self):
        self.assertIsNone(migrate.request_migration_lock(self.connection, self.handle))
        self.cursor.callfunc.assert_called_once_with(
            "SYS.DBMS_LOCK.REQUEST", int, [self.handle, 6, 0, False]
        )
        self.assertIs(self.cursor.callfunc.call_args.args[2][3], False)
        self.assert_connection_ownership_preserved()

    def test_request_accepts_only_integer_zero(self):
        for result in (1, 2, 3, 4, 5, 99, -1, None, "0", 0.0, False, True):
            with self.subTest(result=repr(result)):
                self.cursor.callfunc.return_value = result
                with self.assertRaises(migrate.MigrationLockError):
                    migrate.request_migration_lock(self.connection, self.handle)
        self.assert_connection_ownership_preserved()

    def test_request_driver_error_is_sanitized(self):
        self.cursor.callfunc.side_effect = self.oracle_error()
        with self.assertRaises(migrate.MigrationLockError) as caught:
            migrate.request_migration_lock(self.connection, self.handle)
        self.assertNotIn(self.password, str(caught.exception))
        self.assert_connection_ownership_preserved()

    def test_release_uses_same_handle(self):
        self.assertIsNone(migrate.release_migration_lock(self.connection, self.handle))
        self.cursor.callfunc.assert_called_once_with("SYS.DBMS_LOCK.RELEASE", int, [self.handle])
        self.assert_connection_ownership_preserved()

    def test_release_nonzero_and_unexpected_results_are_rejected(self):
        for result in (1, 3, 4, 5, 99, None, "0", False, 0.0):
            with self.subTest(result=repr(result)):
                self.cursor.callfunc.return_value = result
                with self.assertRaises(migrate.MigrationLockError):
                    migrate.release_migration_lock(self.connection, self.handle)
        self.assert_connection_ownership_preserved()

    def test_release_driver_error_is_sanitized(self):
        self.cursor.callfunc.side_effect = self.oracle_error()
        with self.assertRaises(migrate.MigrationLockError) as caught:
            migrate.release_migration_lock(self.connection, self.handle)
        self.assertNotIn(self.password, str(caught.exception))
        self.assert_connection_ownership_preserved()


class MigrationLockContextTests(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.handle = "synthetic-lock-handle"
        self.allocate = self.enterContext(patch.object(migrate, "allocate_migration_lock", return_value=self.handle))
        self.request = self.enterContext(patch.object(migrate, "request_migration_lock"))
        self.release = self.enterContext(patch.object(migrate, "release_migration_lock"))

    def test_request_failure_does_not_enter_body_or_release(self):
        error = migrate.MigrationLockError("synthetic request failure")
        self.request.side_effect = error
        body = Mock()
        with self.assertRaises(migrate.MigrationLockError) as caught:
            with migrate.migration_lock(self.connection):
                body()
        self.assertIs(caught.exception, error)
        body.assert_not_called()
        self.release.assert_not_called()

    def test_allocation_failure_does_not_request_or_release(self):
        self.allocate.side_effect = migrate.MigrationLockError("synthetic allocation failure")
        body = Mock()
        with self.assertRaises(migrate.MigrationLockError):
            with migrate.migration_lock(self.connection):
                body()
        body.assert_not_called()
        self.request.assert_not_called()
        self.release.assert_not_called()

    def test_success_releases_after_body_in_correct_order(self):
        manager = Mock()
        body = Mock()
        for name, child in (("allocate", self.allocate), ("request", self.request),
                            ("body", body), ("release", self.release)):
            manager.attach_mock(child, name)
        with migrate.migration_lock(self.connection) as handle:
            self.assertEqual(handle, self.handle)
            body()
            self.release.assert_not_called()
        self.assertEqual(manager.mock_calls, [
            call.allocate(self.connection), call.request(self.connection, self.handle),
            call.body(), call.release(self.connection, self.handle),
        ])
        self.driver.connect.assert_not_called()
        self.connection.close.assert_not_called()

    def test_body_error_still_releases_and_preserves_error(self):
        error = RuntimeError("synthetic operation failure")
        with self.assertRaises(RuntimeError) as caught:
            with migrate.migration_lock(self.connection):
                raise error
        self.assertIs(caught.exception, error)
        self.release.assert_called_once_with(self.connection, self.handle)

    def test_release_error_does_not_hide_original_operation_error(self):
        error = RuntimeError("synthetic operation failure")
        cleanup = migrate.MigrationLockError("synthetic release failure")
        self.release.side_effect = cleanup
        with self.assertRaises(RuntimeError) as caught:
            with migrate.migration_lock(self.connection):
                raise error
        self.assertIs(caught.exception, error)
        self.assertIn(str(cleanup), error.__notes__)
        self.assertIn(str(cleanup), self.stderr.getvalue())
        self.release.assert_called_once_with(self.connection, self.handle)

    def test_release_error_alone_is_reported(self):
        cleanup = migrate.MigrationLockError("synthetic release failure")
        self.release.side_effect = cleanup
        with self.assertRaises(migrate.MigrationLockError) as caught:
            with migrate.migration_lock(self.connection):
                pass
        self.assertIs(caught.exception, cleanup)
        self.release.assert_called_once_with(self.connection, self.handle)

    def test_warning_write_failure_does_not_hide_original_error(self):
        error = RuntimeError("synthetic operation failure")
        self.release.side_effect = migrate.MigrationLockError("synthetic release failure")
        with patch("builtins.print", side_effect=OSError("synthetic terminal failure")):
            with self.assertRaises(RuntimeError) as caught:
                with migrate.migration_lock(self.connection):
                    raise error
        self.assertIs(caught.exception, error)
        self.assertTrue(error.__notes__)


class HistoryWriteTestCase(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.connection.autocommit = False
        self.cursor = MagicMock(spec=["__enter__", "__exit__", "execute", "rowcount"])
        self.cursor.__enter__.return_value = self.cursor
        self.cursor.__exit__.return_value = False
        self.cursor.rowcount = 1
        self.connection.cursor.return_value = self.cursor
        content = b"CREATE TABLE employees (id NUMBER)"
        self.plan = migrate.ApplyPlan(
            target_user="HR_CORE", target_pdb="FREEPDB1", version=1,
            script_name="V0001__example.sql", checksum=hashlib.sha256(content).hexdigest(),
            target_table="EMPLOYEES", statement=content.decode(), source_bytes=content,
            comparison_snapshot=(),
        )

    def operations(self):
        return (
            ("RUNNING", lambda: migrate.insert_running_migration(self.connection, self.plan)),
            ("SUCCESS", lambda: migrate.mark_migration_success(self.connection, self.plan)),
            ("FAILED", lambda: migrate.mark_migration_failed(
                self.connection, self.plan, failure_reason="DDL_REJECTED", outcome_confirmed=True,
            )),
        )

    def assert_transaction_untouched(self):
        self.driver.connect.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.__enter__.assert_not_called()
        self.connection.__exit__.assert_not_called()
        self.loader.assert_not_called()
        self.assertIs(self.connection.autocommit, False)

    def assert_safe_history_error(self, error):
        text = str(error)
        for secret in (self.password, self.settings["DB_HOST"], self.plan.statement):
            self.assertNotIn(secret, text)
        self.assert_no_secret_output()

    def assert_update_identity(self, status):
        sql, binds = self.cursor.execute.call_args.args
        self.assertIn("UPDATE HR_CORE.SCHEMA_MIGRATIONS", sql)
        self.assertIn("SET STATUS = :status", sql)
        self.assertIn("FINISHED_AT = SYSTIMESTAMP", sql)
        self.assertIn("ERROR_MESSAGE = :error_message", sql)
        self.assertIn("WHERE VERSION = :version AND SCRIPT_NAME = :script_name", sql)
        self.assertIn("AND CHECKSUM = :checksum AND STATUS = 'RUNNING'", sql)
        self.assertEqual(binds["version"], self.plan.version)
        self.assertEqual(binds["script_name"], self.plan.script_name)
        self.assertEqual(binds["checksum"], self.plan.checksum)
        self.assertEqual(binds["status"], status)
        self.assertEqual(set(binds), {"version", "script_name", "checksum", "status", "error_message"})
        self.assertNotIn(self.plan.checksum, sql)
        self.assertNotIn(self.plan.script_name, sql)
        return binds


class RunningHistoryInsertTests(HistoryWriteTestCase):
    def test_insert_uses_identity_binds_and_oracle_timestamp(self):
        self.assertIsNone(migrate.insert_running_migration(self.connection, self.plan))
        self.cursor.execute.assert_called_once_with(
            "INSERT INTO HR_CORE.SCHEMA_MIGRATIONS "
            "(VERSION, SCRIPT_NAME, CHECKSUM, STATUS, STARTED_AT, FINISHED_AT, ERROR_MESSAGE) "
            "VALUES (:version, :script_name, :checksum, :status, SYSTIMESTAMP, NULL, NULL)",
            {"version": 1, "script_name": self.plan.script_name,
             "checksum": self.plan.checksum, "status": "RUNNING"},
        )
        self.connection.cursor.assert_called_once_with()
        self.cursor.__exit__.assert_called_once_with(None, None, None)
        self.assert_transaction_untouched()

    def test_insert_requires_exactly_one_affected_row(self):
        for count in (0, 2, -1, None, True, 1.0):
            with self.subTest(rowcount=count):
                self.cursor.reset_mock()
                self.cursor.rowcount = count
                with self.assertRaises(migrate.MigrationHistoryError):
                    migrate.insert_running_migration(self.connection, self.plan)
                self.cursor.execute.assert_called_once()
                self.cursor.__exit__.assert_called_once()
        self.assert_transaction_untouched()

    def test_unique_constraint_collision_is_safely_reported_without_retry(self):
        for detail in (
            SimpleNamespace(full_code="ORA-00001", message=self.password),
            SimpleNamespace(code=1, message=self.password),
        ):
            with self.subTest(detail_type=tuple(vars(detail))):
                self.cursor.reset_mock()
                self.cursor.execute.side_effect = MockOracleError(detail)
                with self.assertRaisesRegex(migrate.MigrationHistoryError, "çakışması") as caught:
                    migrate.insert_running_migration(self.connection, self.plan)
                self.assert_safe_history_error(caught.exception)
                self.cursor.execute.assert_called_once()
                self.cursor.__exit__.assert_called_once()
        self.assert_transaction_untouched()

    def test_driver_failure_does_not_retry_or_update_existing_history(self):
        self.cursor.execute.side_effect = self.oracle_error()
        with self.assertRaises(migrate.MigrationHistoryError) as caught:
            migrate.insert_running_migration(self.connection, self.plan)
        self.cursor.execute.assert_called_once()
        self.assertTrue(self.cursor.execute.call_args.args[0].startswith("INSERT "))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assert_safe_history_error(caught.exception)
        self.assert_transaction_untouched()


class SuccessHistoryUpdateTests(HistoryWriteTestCase):
    def test_success_updates_only_matching_running_row_and_clears_error(self):
        self.assertIsNone(migrate.mark_migration_success(self.connection, self.plan))
        self.cursor.execute.assert_called_once()
        binds = self.assert_update_identity("SUCCESS")
        self.assertIsNone(binds["error_message"])
        self.assert_transaction_untouched()

    def test_success_requires_exactly_one_affected_row(self):
        for count in (0, 2, 10, -1, None, True, 1.0):
            with self.subTest(rowcount=count):
                self.cursor.reset_mock()
                self.cursor.rowcount = count
                with self.assertRaises(migrate.MigrationHistoryError):
                    migrate.mark_migration_success(self.connection, self.plan)
                self.cursor.execute.assert_called_once()
                self.assert_update_identity("SUCCESS")
                self.cursor.__exit__.assert_called_once()
        self.assert_transaction_untouched()

    def test_success_driver_failure_never_retries_or_marks_failed(self):
        self.cursor.execute.side_effect = self.oracle_error()
        with patch.object(migrate, "mark_migration_failed") as mark_failed:
            with self.assertRaises(migrate.MigrationHistoryError) as caught:
                migrate.mark_migration_success(self.connection, self.plan)
        self.cursor.execute.assert_called_once()
        mark_failed.assert_not_called()
        self.assert_update_identity("SUCCESS")
        self.assert_safe_history_error(caught.exception)
        self.assert_transaction_untouched()


class FailedHistoryUpdateTests(HistoryWriteTestCase):
    def test_outcome_must_be_explicitly_confirmed(self):
        for confirmed in (False, None, 0, 1, "True"):
            with self.subTest(confirmed=confirmed):
                with self.assertRaises(migrate.MigrationHistoryError):
                    migrate.mark_migration_failed(
                        self.connection, self.plan, failure_reason="DDL_REJECTED",
                        outcome_confirmed=confirmed,
                    )
        # Varsayılan parametre de güvenli şekilde reddetmelidir.
        with self.assertRaises(migrate.MigrationHistoryError):
            migrate.mark_migration_failed(self.connection, self.plan, failure_reason="DDL_REJECTED")
        self.connection.cursor.assert_not_called()
        self.assert_transaction_untouched()

    def test_supported_reasons_write_fixed_safe_messages_and_finished_at(self):
        expected_messages = {
            "DDL_REJECTED": "Migration DDL işlemi Oracle tarafından kesin olarak reddedildi.",
            "POST_VALIDATION_FAILED": (
                "DDL tamamlandı ancak son yapı doğrulaması başarısız oldu; "
                "kalıcı nesne etkileri manuel incelenmelidir."
            ),
        }
        for reason, expected in expected_messages.items():
            with self.subTest(reason=reason):
                self.cursor.reset_mock()
                self.assertIsNone(migrate.mark_migration_failed(
                    self.connection, self.plan, failure_reason=reason, outcome_confirmed=True,
                ))
                self.cursor.execute.assert_called_once()
                binds = self.assert_update_identity("FAILED")
                self.assertEqual(binds["error_message"], expected)
                self.assertNotIn(self.password, binds["error_message"])
                self.assertNotIn(self.plan.statement, binds["error_message"])
        self.assert_transaction_untouched()

    def test_free_text_secrets_sql_and_ambiguous_reasons_are_rejected(self):
        for reason in ("", "ddl_rejected", "arbitrary error", self.password,
                       self.plan.statement, "NETWORK_ERROR", "TIMEOUT", "COMMIT_UNKNOWN"):
            with self.subTest(reason=reason):
                with self.assertRaises(migrate.MigrationHistoryError) as caught:
                    migrate.mark_migration_failed(
                        self.connection, self.plan, failure_reason=reason, outcome_confirmed=True,
                    )
                self.assert_safe_history_error(caught.exception)
        self.connection.cursor.assert_not_called()
        self.assert_transaction_untouched()

    def test_failed_requires_exactly_one_affected_row(self):
        for count in (0, 2, -1, None, True, 1.0):
            with self.subTest(rowcount=count):
                self.cursor.reset_mock()
                self.cursor.rowcount = count
                with self.assertRaises(migrate.MigrationHistoryError):
                    migrate.mark_migration_failed(
                        self.connection, self.plan, failure_reason="DDL_REJECTED", outcome_confirmed=True,
                    )
                self.cursor.execute.assert_called_once()
                self.assert_update_identity("FAILED")
        self.assert_transaction_untouched()

    def test_failed_driver_error_is_sanitized_and_not_retried(self):
        self.cursor.execute.side_effect = self.oracle_error()
        with self.assertRaises(migrate.MigrationHistoryError) as caught:
            migrate.mark_migration_failed(
                self.connection, self.plan, failure_reason="DDL_REJECTED", outcome_confirmed=True,
            )
        self.cursor.execute.assert_called_once()
        self.assert_safe_history_error(caught.exception)
        self.assert_transaction_untouched()


class HistoryWriteSafetyTests(HistoryWriteTestCase):
    def test_autocommit_must_be_exactly_false_for_every_writer(self):
        for label, operation in self.operations():
            for value in (True, None, 0, "False"):
                with self.subTest(operation=label, autocommit=value):
                    self.connection.autocommit = value
                    with self.assertRaisesRegex(migrate.MigrationHistoryError, "autocommit=False"):
                        operation()
        self.connection.cursor.assert_not_called()
        self.connection.autocommit = False
        self.assert_transaction_untouched()

    def test_invalid_plan_identity_is_rejected_before_cursor_creation(self):
        original = self.plan
        changes = (
            {"target_user": "OTHER"}, {"target_pdb": "OTHER"}, {"version": -1},
            {"version": 10000}, {"version": True}, {"script_name": "invalid.sql"},
            {"script_name": "V0002__example.sql"}, {"checksum": "not-a-checksum"},
            {"checksum": "A" * 64}, {"script_name": "V0001__" + "a" * 250 + ".sql"},
        )
        for change in changes:
            self.plan = replace(original, **change)
            for label, operation in self.operations():
                with self.subTest(change=change, operation=label):
                    with self.assertRaises(migrate.MigrationHistoryError) as caught:
                        operation()
                    self.assert_safe_history_error(caught.exception)
        self.connection.cursor.assert_not_called()
        self.plan = original
        self.assert_transaction_untouched()

    def test_all_writers_use_existing_connection_and_release_cursor(self):
        for label, operation in self.operations():
            with self.subTest(operation=label):
                self.connection.cursor.reset_mock()
                self.cursor.reset_mock()
                self.assertIsNone(operation())
                self.connection.cursor.assert_called_once_with()
                self.cursor.__enter__.assert_called_once_with()
                self.cursor.__exit__.assert_called_once_with(None, None, None)
                self.assert_transaction_untouched()

    def test_driver_errors_sanitize_password_host_and_sql_for_all_writers(self):
        details = (
            SimpleNamespace(full_code="DPY-6005", message=self.password + self.plan.statement),
            SimpleNamespace(full_code=self.password, message=self.settings["DB_HOST"]),
            SimpleNamespace(full_code=None, message=self.password),
        )
        for detail in details:
            for label, operation in self.operations():
                with self.subTest(operation=label, code_type=type(detail.full_code).__name__):
                    self.cursor.reset_mock()
                    self.cursor.execute.side_effect = MockOracleError(detail)
                    with self.assertRaises(migrate.MigrationHistoryError) as caught:
                        operation()
                    self.assert_safe_history_error(caught.exception)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.cursor.execute.assert_called_once()
                    self.cursor.__exit__.assert_called_once()
                    self.assert_transaction_untouched()

    def test_cursor_is_exited_on_execute_errors_for_all_writers(self):
        for label, operation in self.operations():
            with self.subTest(operation=label):
                self.cursor.reset_mock()
                error = self.oracle_error()
                self.cursor.execute.side_effect = error
                with self.assertRaises(migrate.MigrationHistoryError):
                    operation()
                self.cursor.__exit__.assert_called_once()
                self.assertIs(self.cursor.__exit__.call_args.args[0], MockOracleError)
                self.assertIs(self.cursor.__exit__.call_args.args[1], error)
                self.assert_transaction_untouched()


class MetadataPrecheckTestCase(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.definition = migrate.validate_create_table(
            "CREATE TABLE HR_CORE.employees (id NUMBER, CONSTRAINT pk_employees PRIMARY KEY (id))"
        )
        self.session_cursor = self.cursor
        self.metadata_cursor = MagicMock(spec=["__enter__", "__exit__", "execute", "fetchone"])
        self.metadata_cursor.__enter__.return_value = self.metadata_cursor
        self.metadata_cursor.__exit__.return_value = False
        self.set_metadata_results((0,))

    def set_metadata_results(self, *rows):
        self.connection.cursor.reset_mock()
        self.session_cursor.reset_mock(side_effect=True)
        self.session_cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", "HR_CORE")
        self.metadata_cursor.reset_mock(side_effect=True)
        self.metadata_cursor.fetchone.side_effect = list(rows)
        self.connection.cursor.side_effect = [
            cursor for _ in rows for cursor in (self.session_cursor, self.metadata_cursor)
        ]

    def assert_no_connection_or_transaction_changes(self):
        self.driver.connect.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.__enter__.assert_not_called()
        self.connection.__exit__.assert_not_called()
        self.loader.assert_not_called()

    def assert_safe_metadata_error(self, error):
        for secret in (self.password, self.settings["DB_HOST"], self.definition.statement):
            self.assertNotIn(secret, str(error))
        self.assert_no_secret_output()


class TargetObjectPrecheckTests(MetadataPrecheckTestCase):
    def test_available_object_uses_hr_core_and_name_bind(self):
        self.assertIsNone(migrate.check_target_object_available(self.connection, self.definition))
        self.metadata_cursor.execute.assert_called_once()
        sql, binds = self.metadata_cursor.execute.call_args.args
        self.assertIn("SELECT COUNT(*) FROM ALL_OBJECTS", sql)
        self.assertIn("OWNER = :owner AND OBJECT_NAME = :object_name", sql)
        self.assertEqual(binds, {"owner": "HR_CORE", "object_name": "EMPLOYEES"})
        self.assertNotIn("EMPLOYEES", sql)
        self.session_cursor.fetchone.assert_called_once_with()
        self.metadata_cursor.__exit__.assert_called_once_with(None, None, None)
        self.assert_no_connection_or_transaction_changes()

    def test_existing_objects_block_and_query_covers_shared_namespace(self):
        for count in (1, 3):
            with self.subTest(count=count):
                self.set_metadata_results((count,))
                with self.assertRaisesRegex(migrate.MetadataPrecheckError, "çakışan"):
                    migrate.check_target_object_available(self.connection, self.definition)
                sql = self.metadata_cursor.execute.call_args.args[0]
                for kind in ("TABLE", "VIEW", "SEQUENCE", "SYNONYM", "PACKAGE", "PROCEDURE",
                             "FUNCTION", "OPERATOR", "TYPE", "MATERIALIZED VIEW"):
                    self.assertIn(f"'{kind}'", sql)
                # Index ve trigger farklı isim alanındadır; salt isim eşitliği yeterli değildir.
                self.assertNotIn("'INDEX'", sql)
                self.assertNotIn("'TRIGGER'", sql)

    def test_unexpected_object_counts_are_not_treated_as_absence(self):
        for row in (None, (), (None,), (-1,), (True,), (0.0,), ("0",), (0, 1), "0"):
            with self.subTest(row=row):
                self.set_metadata_results(row)
                with self.assertRaises(migrate.MetadataPrecheckError):
                    migrate.check_target_object_available(self.connection, self.definition)

    def test_object_query_error_is_sanitized_and_exits_cursor(self):
        error = MockOracleError(SimpleNamespace(message=self.password + self.definition.statement))
        self.metadata_cursor.execute.side_effect = error
        with self.assertRaises(migrate.MetadataPrecheckError) as caught:
            migrate.check_target_object_available(self.connection, self.definition)
        self.assert_safe_metadata_error(caught.exception)
        self.assertTrue(caught.exception.__suppress_context__)
        self.metadata_cursor.__exit__.assert_called_once()
        self.assertIs(self.metadata_cursor.__exit__.call_args.args[1], error)
        self.metadata_cursor.fetchone.assert_not_called()
        self.assert_no_connection_or_transaction_changes()

    def test_unvalidated_or_inconsistent_model_is_rejected_before_query(self):
        for definition in (
            replace(self.definition, table_name="OTHER"),
            replace(
                self.definition,
                column_definitions=(replace(self.definition.column_definitions[0], name="OTHER"),),
            ),
            replace(
                self.definition,
                primary_key=replace(self.definition.primary_key, constraint_name="OTHER"),
            ),
            replace(self.definition, statement="CREATE TABLE OTHER.employees (id NUMBER)"),
        ):
            with self.subTest(definition=definition):
                with self.assertRaises(migrate.MetadataPrecheckError):
                    migrate.check_target_object_available(self.connection, definition)
        self.connection.cursor.assert_not_called()


class PrimaryKeyNamePrecheckTests(MetadataPrecheckTestCase):
    def test_no_explicit_constraint_name_skips_all_queries(self):
        for sql in (
            "CREATE TABLE employees (id NUMBER)",
            "CREATE TABLE employees (id NUMBER, PRIMARY KEY (id))",
        ):
            with self.subTest(sql=sql):
                definition = migrate.validate_create_table(sql)
                self.assertIsNone(definition.primary_key_constraint_name)
                self.assertIsNone(migrate.check_primary_key_name_available(self.connection, definition))
        self.connection.cursor.assert_not_called()
        self.assert_no_connection_or_transaction_changes()

    def test_available_explicit_name_uses_owner_and_constraint_binds(self):
        self.assertEqual(self.definition.primary_key_constraint_name, "PK_EMPLOYEES")
        self.assertIsNone(migrate.check_primary_key_name_available(self.connection, self.definition))
        self.metadata_cursor.execute.assert_called_once_with(
            "SELECT COUNT(*) FROM ALL_CONSTRAINTS "
            "WHERE OWNER = :owner AND CONSTRAINT_NAME = :constraint_name",
            {"owner": "HR_CORE", "constraint_name": "PK_EMPLOYEES"},
        )
        self.assertNotIn("PK_EMPLOYEES", self.metadata_cursor.execute.call_args.args[0])
        self.assert_no_connection_or_transaction_changes()

    def test_existing_constraint_name_is_rejected(self):
        self.set_metadata_results((1,))
        with self.assertRaisesRegex(migrate.MetadataPrecheckError, "zaten kullanılıyor"):
            migrate.check_primary_key_name_available(self.connection, self.definition)
        self.metadata_cursor.execute.assert_called_once()

    def test_unexpected_constraint_results_are_rejected(self):
        for row in (None, (), (None,), (-1,), (False,), (0.0,), ("0",), (0, 0)):
            with self.subTest(row=row):
                self.set_metadata_results(row)
                with self.assertRaises(migrate.MetadataPrecheckError):
                    migrate.check_primary_key_name_available(self.connection, self.definition)

    def test_constraint_query_failure_is_not_interpreted_as_available(self):
        self.metadata_cursor.execute.side_effect = self.oracle_error()
        with self.assertRaises(migrate.MetadataPrecheckError) as caught:
            migrate.check_primary_key_name_available(self.connection, self.definition)
        self.assert_safe_metadata_error(caught.exception)
        self.metadata_cursor.fetchone.assert_not_called()
        self.metadata_cursor.__exit__.assert_called_once()
        self.assert_no_connection_or_transaction_changes()


class CombinedMetadataPrecheckTests(MetadataPrecheckTestCase):
    def test_helpers_are_called_in_order_with_same_connection_and_model(self):
        manager = Mock()
        with (
            patch.object(migrate, "check_target_object_available") as objects,
            patch.object(migrate, "check_primary_key_name_available") as constraints,
        ):
            manager.attach_mock(objects, "objects")
            manager.attach_mock(constraints, "constraints")
            self.assertIsNone(migrate.validate_create_table_preconditions(self.connection, self.definition))
        self.assertEqual(manager.mock_calls, [
            call.objects(self.connection, self.definition),
            call.constraints(self.connection, self.definition),
        ])

    def test_first_check_failure_skips_second_and_protected_body(self):
        body = Mock()
        error = migrate.MetadataPrecheckError("synthetic object conflict")
        with (
            patch.object(migrate, "check_target_object_available", side_effect=error),
            patch.object(migrate, "check_primary_key_name_available") as constraints,
        ):
            with self.assertRaises(migrate.MetadataPrecheckError) as caught:
                migrate.validate_create_table_preconditions(self.connection, self.definition)
                body()  # DDL yerine yalnızca erişilmemesi gereken sahte işlem.
        self.assertIs(caught.exception, error)
        constraints.assert_not_called()
        body.assert_not_called()

    def test_second_check_conflict_stops_protected_body(self):
        body = Mock()
        self.set_metadata_results((0,), (1,))
        with self.assertRaises(migrate.MetadataPrecheckError):
            migrate.validate_create_table_preconditions(self.connection, self.definition)
            body()
        body.assert_not_called()
        self.assertEqual(self.metadata_cursor.execute.call_count, 2)
        self.assert_no_connection_or_transaction_changes()

    def test_real_helpers_accept_both_available_names(self):
        self.set_metadata_results((0,), (0,))
        self.assertIsNone(migrate.validate_create_table_preconditions(self.connection, self.definition))
        queries = [item.args[0] for item in self.metadata_cursor.execute.call_args_list]
        self.assertIn("ALL_OBJECTS", queries[0])
        self.assertIn("ALL_CONSTRAINTS", queries[1])
        self.assertEqual(self.session_cursor.execute.call_count, 2)
        self.assert_no_connection_or_transaction_changes()


class MetadataPrecheckSafetyTests(MetadataPrecheckTestCase):
    def test_every_helper_leaves_connection_and_transactions_to_caller(self):
        for helper, rows in (
            (migrate.check_target_object_available, ((0,),)),
            (migrate.check_primary_key_name_available, ((0,),)),
            (migrate.validate_create_table_preconditions, ((0,), (0,))),
        ):
            with self.subTest(helper=helper.__name__):
                self.set_metadata_results(*rows)
                self.assertIsNone(helper(self.connection, self.definition))
                self.assert_no_connection_or_transaction_changes()
                for invocation in self.metadata_cursor.execute.call_args_list:
                    self.assertTrue(invocation.args[0].startswith("SELECT "))

    def test_wrong_session_blocks_metadata_query(self):
        for helper in (migrate.check_target_object_available, migrate.check_primary_key_name_available):
            with self.subTest(helper=helper.__name__):
                self.set_metadata_results((0,))
                self.session_cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", "OTHER")
                with self.assertRaises(migrate.MetadataPrecheckError):
                    helper(self.connection, self.definition)
                self.metadata_cursor.execute.assert_not_called()
                self.assert_no_connection_or_transaction_changes()

    def test_fetch_failure_exits_cursor_and_does_not_mean_absence(self):
        for helper in (migrate.check_target_object_available, migrate.check_primary_key_name_available):
            with self.subTest(helper=helper.__name__):
                self.set_metadata_results((0,))
                error = self.oracle_error()
                self.metadata_cursor.fetchone.side_effect = error
                with self.assertRaises(migrate.MetadataPrecheckError) as caught:
                    helper(self.connection, self.definition)
                self.metadata_cursor.__exit__.assert_called_once()
                self.assertIs(self.metadata_cursor.__exit__.call_args.args[1], error)
                self.assert_safe_metadata_error(caught.exception)
                self.assert_no_connection_or_transaction_changes()


class DetailedCreateTableModelTests(OfflineTestCase):
    def column(self, kind, suffix=""):
        return migrate.validate_create_table(
            f"CREATE TABLE employees (value_col {kind}{suffix})"
        ).column_definitions[0]

    def test_unparameterized_number_is_distinct(self):
        bare = self.column("NUMBER")
        bounded = self.column("NUMBER(10)")
        self.assertEqual(bare, migrate.ColumnDefinition("VALUE_COL", "NUMBER"))
        self.assertIsNone(bare.number_precision)
        self.assertIsNone(bare.number_scale)
        self.assertNotEqual(bare, bounded)

    def test_number_precision_implies_zero_scale(self):
        for precision in (1, 10, 38):
            with self.subTest(precision=precision):
                implicit = self.column(f"NUMBER({precision})")
                explicit = self.column(f"NUMBER({precision},0)")
                self.assertEqual(implicit, explicit)
                self.assertEqual((implicit.number_precision, implicit.number_scale), (precision, 0))

    def test_number_precision_and_scale_are_retained(self):
        for precision, scale in ((1, 1), (12, 4), (38, 0), (38, 38)):
            with self.subTest(precision=precision, scale=scale):
                self.assertEqual(
                    self.column(f"NUMBER({precision},{scale})"),
                    migrate.ColumnDefinition("VALUE_COL", "NUMBER", number_precision=precision, number_scale=scale),
                )

    def test_varchar2_retains_character_length_and_semantics(self):
        for length in (1, 255, 1000):
            with self.subTest(length=length):
                self.assertEqual(
                    self.column(f"VARCHAR2({length} CHAR)"),
                    migrate.ColumnDefinition("VALUE_COL", "VARCHAR2", char_length=length, char_semantics="CHAR"),
                )

    def test_implicit_and_explicit_timestamp_precision_are_equal(self):
        self.assertEqual(self.column("TIMESTAMP"), self.column("TIMESTAMP(6)"))
        self.assertEqual(
            self.column("TIMESTAMP"),
            migrate.ColumnDefinition("VALUE_COL", "TIMESTAMP", timestamp_precision=6),
        )

    def test_timestamp_timezone_is_retained_and_distinct(self):
        implicit = self.column("TIMESTAMP WITH TIME ZONE")
        explicit = self.column("TIMESTAMP(6) WITH TIME ZONE")
        self.assertEqual(implicit, explicit)
        self.assertEqual(explicit.timestamp_precision, 6)
        self.assertIs(explicit.with_time_zone, True)
        self.assertEqual(explicit.data_type, "TIMESTAMP")
        self.assertNotEqual(explicit, self.column("TIMESTAMP(6)"))

    def test_only_currently_supported_timestamp_precision_is_accepted(self):
        # Mevcut gramer sadece 6'yı destekler; model testi grameri genişletmez.
        for precision in (0, 1, 3, 5, 7, 9):
            for timezone in ("", " WITH TIME ZONE"):
                with self.subTest(precision=precision, timezone=timezone):
                    with self.assertRaises(ValueError):
                        self.column(f"TIMESTAMP({precision}){timezone}")

    def test_explicit_not_null_and_nullable_are_distinct(self):
        for kind in ("NUMBER", "VARCHAR2(10 CHAR)", "DATE", "CLOB", "TIMESTAMP(6)"):
            with self.subTest(kind=kind):
                nullable = self.column(kind)
                required = self.column(kind, " NOT NULL")
                self.assertIs(nullable.explicit_not_null, False)
                self.assertIs(required.explicit_not_null, True)
                self.assertEqual(required, replace(nullable, explicit_not_null=True))

    def test_primary_key_does_not_fabricate_explicit_not_null(self):
        definition = migrate.validate_create_table(
            "CREATE TABLE employees (id NUMBER, code NUMBER NOT NULL, PRIMARY KEY (id, code))"
        )
        self.assertEqual(definition.primary_key.columns, ("ID", "CODE"))
        self.assertIs(definition.column_definitions[0].explicit_not_null, False)
        self.assertIs(definition.column_definitions[1].explicit_not_null, True)

    def test_named_primary_key_preserves_order_and_constraint_name(self):
        definition = migrate.validate_create_table(
            "CREATE TABLE employees (id NUMBER, code NUMBER, "
            "CONSTRAINT pk_employees PRIMARY KEY (code, id))"
        )
        self.assertEqual(
            definition.primary_key,
            migrate.PrimaryKeyDefinition(columns=("CODE", "ID"), constraint_name="PK_EMPLOYEES"),
        )
        self.assertEqual(definition.columns, ("ID", "CODE"))
        self.assertEqual(definition.primary_key_constraint_name, "PK_EMPLOYEES")

    def test_unnamed_primary_key_has_columns_but_no_explicit_name(self):
        definition = migrate.validate_create_table("CREATE TABLE employees (id NUMBER, PRIMARY KEY (id))")
        self.assertEqual(definition.primary_key, migrate.PrimaryKeyDefinition(("ID",)))
        self.assertIsNone(definition.primary_key_constraint_name)

    def test_table_without_primary_key_has_none(self):
        definition = migrate.validate_create_table("CREATE TABLE employees (id NUMBER)")
        self.assertIsNone(definition.primary_key)
        self.assertIsNone(definition.primary_key_constraint_name)

    def test_models_and_nested_members_are_frozen(self):
        definition = migrate.validate_create_table("CREATE TABLE employees (id NUMBER, PRIMARY KEY (id))")
        for model, field_name, changed in (
            (definition, "table_name", "OTHER"),
            (definition.column_definitions[0], "name", "OTHER"),
            (definition.primary_key, "constraint_name", "OTHER"),
            (definition.primary_key, "columns", ("OTHER",)),
        ):
            with self.subTest(model=type(model).__name__, field=field_name):
                self.assertTrue(type(model).__dataclass_params__.frozen)
                with self.assertRaises(FrozenInstanceError):
                    setattr(model, field_name, changed)
        self.assertIsInstance(definition.column_definitions, tuple)
        self.assertIsInstance(definition.primary_key.columns, tuple)
        self.assertIsInstance(definition.columns, tuple)
        with self.assertRaises(TypeError):
            definition.column_definitions[0] = migrate.ColumnDefinition("OTHER", "DATE")
        with self.assertRaises(TypeError):
            definition.primary_key.columns[0] = "OTHER"

    def test_compatibility_properties_are_derived_not_independent_fields(self):
        definition = migrate.validate_create_table(
            "CREATE TABLE employees (id NUMBER, CONSTRAINT pk_employees PRIMARY KEY (id))"
        )
        changed = replace(
            definition,
            column_definitions=(replace(definition.column_definitions[0], name="OTHER"),),
            primary_key=replace(definition.primary_key, constraint_name="PK_OTHER"),
        )
        self.assertEqual(changed.columns, ("OTHER",))
        self.assertEqual(changed.primary_key_constraint_name, "PK_OTHER")
        self.assertEqual(definition.columns, ("ID",))
        self.assertEqual(definition.primary_key_constraint_name, "PK_EMPLOYEES")
        self.assertNotIn("columns", migrate.CreateTableDefinition.__dataclass_fields__)
        self.assertNotIn("primary_key_constraint_name", migrate.CreateTableDefinition.__dataclass_fields__)

    def test_date_clob_and_mixed_columns_keep_order_without_type_field_leakage(self):
        definition = migrate.validate_create_table(
            "CREATE TABLE employees (name VARCHAR2(30 CHAR), amount NUMBER(8,2), "
            "created_at TIMESTAMP WITH TIME ZONE, hired_at DATE, notes CLOB, id NUMBER)"
        )
        self.assertEqual(definition.column_definitions, (
            migrate.ColumnDefinition("NAME", "VARCHAR2", char_length=30, char_semantics="CHAR"),
            migrate.ColumnDefinition("AMOUNT", "NUMBER", number_precision=8, number_scale=2),
            migrate.ColumnDefinition("CREATED_AT", "TIMESTAMP", timestamp_precision=6, with_time_zone=True),
            migrate.ColumnDefinition("HIRED_AT", "DATE"),
            migrate.ColumnDefinition("NOTES", "CLOB"),
            migrate.ColumnDefinition("ID", "NUMBER"),
        ))

    def test_unsupported_sql_forms_remain_rejected(self):
        for sql in (
            "CREATE TABLE employees (id NUMBER DEFAULT 0)",
            "CREATE TABLE employees (name VARCHAR2(10 BYTE))",
            "CREATE TABLE employees (id NUMBER CHECK (id IS NOT NULL))",
            "CREATE TABLE employees (id NUMBER UNIQUE)",
            "CREATE TABLE employees (id NUMBER, FOREIGN KEY (id) REFERENCES departments(id))",
            'CREATE TABLE "employees" (id NUMBER)',
            "CREATE TABLE employees AS SELECT id FROM departments",
            "CREATE TABLE employees (id NUMBER); DROP TABLE employees;",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                migrate.validate_create_table(sql)

    def test_prepared_checksum_and_sql_remain_source_based(self):
        content = b"-- source comment\r\ncreate table employees (id number(10) not null);\r\n"
        migration = local_migration(checksum=hashlib.sha256(content).hexdigest())
        prepared = migrate.prepare_migration_content(migration, content)
        self.assertEqual(prepared.content, content)
        self.assertEqual(prepared.migration.checksum, hashlib.sha256(content).hexdigest())
        self.assertEqual(prepared.definition.statement, "CREATE TABLE EMPLOYEES ( ID NUMBER ( 10 ) NOT NULL )")
        self.assertEqual(prepared.definition.column_definitions, (
            migrate.ColumnDefinition("ID", "NUMBER", number_precision=10, number_scale=0, explicit_not_null=True),
        ))


class DetailedModelPrecheckCompatibilityTests(MetadataPrecheckTestCase):
    def test_detailed_model_passes_existing_metadata_prechecks(self):
        self.definition = migrate.validate_create_table(
            "CREATE TABLE HR_CORE.employees (id NUMBER(10) NOT NULL, name VARCHAR2(40 CHAR), "
            "CONSTRAINT pk_employees PRIMARY KEY (id))"
        )
        self.set_metadata_results((0,), (0,))
        self.assertIsNone(migrate.validate_create_table_preconditions(self.connection, self.definition))
        self.assertEqual(self.metadata_cursor.execute.call_args_list[0].args[1],
                         {"owner": "HR_CORE", "object_name": "EMPLOYEES"})
        self.assertEqual(self.metadata_cursor.execute.call_args_list[1].args[1],
                         {"owner": "HR_CORE", "constraint_name": "PK_EMPLOYEES"})
        self.assert_no_connection_or_transaction_changes()

    def test_tampered_type_or_primary_key_details_are_rejected_before_query(self):
        for changed in (
            replace(self.definition, column_definitions=(
                replace(self.definition.column_definitions[0], number_precision=12, number_scale=0),
            )),
            replace(self.definition, primary_key=replace(self.definition.primary_key, columns=("OTHER",))),
        ):
            with self.subTest(definition=changed), self.assertRaises(migrate.MetadataPrecheckError):
                migrate.check_target_object_available(self.connection, changed)
        self.connection.cursor.assert_not_called()


class ColumnMetadataReaderTests(ConnectionHelperTestCase):
    """Sentetik sözleşme testleri; canlı Oracle metadata biçimini kanıtlamaz."""

    def setUp(self):
        super().setUp()
        self.session_cursor = self.cursor
        self.columns_cursor = MagicMock(spec=["__enter__", "__exit__", "execute", "fetchall"])
        self.columns_cursor.__enter__.return_value = self.columns_cursor
        self.columns_cursor.__exit__.return_value = False
        # Alanlar okuyucunun mevcut sekiz alanlı sözleşmesine göre düzenlenir.
        self.row = ("ID", 1, "NUMBER", 10, 0, None, None, "N")
        self.configure_rows([self.row])

    def configure_rows(self, rows):
        self.connection.cursor.reset_mock()
        self.connection.cursor.side_effect = [self.session_cursor, self.columns_cursor]
        self.session_cursor.reset_mock(side_effect=True)
        self.session_cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", "HR_CORE")
        self.columns_cursor.reset_mock(side_effect=True)
        self.columns_cursor.fetchall.return_value = rows

    def read(self):
        return migrate.read_table_column_metadata(self.connection, "EMPLOYEES")

    def changed_row(self, index, value):
        row = list(self.row)
        row[index] = value
        return tuple(row)

    def assert_rows_rejected(self, rows):
        self.configure_rows(rows)
        with self.assertRaises(migrate.ColumnMetadataReadError):
            self.read()
        self.columns_cursor.__exit__.assert_called_once_with(None, None, None)

    def assert_connection_untouched(self):
        self.driver.connect.assert_not_called()
        self.loader.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.__enter__.assert_not_called()
        self.connection.__exit__.assert_not_called()

    def test_query_binds_order_and_metadata_models(self):
        rows = [self.row, ("NAME", 2, "VARCHAR2", None, None, 40, "C", "Y")]
        self.configure_rows(rows)
        result = self.read()
        self.assertEqual(result, migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata(*rows[0]), migrate.OracleColumnMetadata(*rows[1]),
        )))
        self.columns_cursor.execute.assert_called_once_with(
            "SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE, DATA_PRECISION, DATA_SCALE, "
            "CHAR_LENGTH, CHAR_USED, NULLABLE FROM ALL_TAB_COLUMNS "
            "WHERE OWNER = :owner AND TABLE_NAME = :table_name ORDER BY COLUMN_ID",
            {"owner": "HR_CORE", "table_name": "EMPLOYEES"},
        )
        self.assertEqual([column.column_id for column in result.columns], [1, 2])
        self.assertNotIn("EMPLOYEES", self.columns_cursor.execute.call_args.args[0])
        self.session_cursor.fetchone.assert_called_once_with()
        self.assert_connection_untouched()

    def test_snapshot_and_nested_columns_are_immutable(self):
        result = self.read()
        self.assertIsInstance(result.columns, tuple)
        self.assertTrue(type(result).__dataclass_params__.frozen)
        self.assertTrue(type(result.columns[0]).__dataclass_params__.frozen)
        with self.assertRaises(FrozenInstanceError):
            result.owner = "OTHER"
        with self.assertRaises(FrozenInstanceError):
            result.columns[0].data_type = "DATE"
        with self.assertRaises(TypeError):
            result.columns[0] = result.columns[0]

    def test_column_id_gaps_are_preserved(self):
        self.configure_rows([self.changed_row(1, 2), ("CODE", 7, "NUMBER", None, None, 0, None, "Y")])
        self.assertEqual([column.column_id for column in self.read().columns], [2, 7])

    def test_none_values_are_preserved_not_defaulted(self):
        self.configure_rows([("VALUE_COL", 1, "NUMBER", None, None, None, None, "Y")])
        column, = self.read().columns
        self.assertEqual(column, migrate.OracleColumnMetadata("VALUE_COL", 1, "NUMBER", None, None, None, None, "Y"))

    def test_timestamp_raw_values_are_not_semantically_normalized(self):
        # Her iki scale değeri de okuyucunun sözleşmesinde geçerli test girdisidir;
        # Oracle 26ai'nin gerçekte hangisini döndürdüğüne ilişkin iddia değildir.
        for scale in (None, 6):
            with self.subTest(scale=scale):
                row = ("CREATED_AT", 1, "TIMESTAMP(6) WITH TIME ZONE", None, scale, 0, None, "N")
                self.configure_rows([row])
                self.assertEqual(self.read().columns[0], migrate.OracleColumnMetadata(*row))

    def test_reader_does_not_compare_to_supported_sql_types(self):
        self.configure_rows([("PAYLOAD", 1, "BLOB", None, None, 0, None, "Y")])
        self.assertEqual(self.read().columns[0].data_type, "BLOB")

    def test_empty_or_invalid_result_container_is_rejected(self):
        for rows in ([], (), None, "not-rows", 0, {}):
            with self.subTest(rows=rows):
                self.assert_rows_rejected(rows)

    def test_missing_extra_or_invalid_row_fields_are_rejected(self):
        for row in (self.row[:-1], self.row + ("EXTRA",), (), None, "row", {}):
            with self.subTest(row=row):
                self.assert_rows_rejected([row])

    def test_duplicate_column_names_are_rejected(self):
        self.assert_rows_rejected([self.row, self.changed_row(1, 2)])

    def test_duplicate_column_ids_are_rejected(self):
        self.assert_rows_rejected([self.row, self.changed_row(0, "OTHER")])

    def test_invalid_column_ids_are_rejected(self):
        for value in (None, 0, -1, 1.0, "1"):
            with self.subTest(value=value):
                self.assert_rows_rejected([self.changed_row(1, value)])

    def test_unsorted_column_ids_are_not_silently_sorted(self):
        self.assert_rows_rejected([self.changed_row(1, 3), ("OTHER", 2, "NUMBER", None, None, 0, None, "Y")])

    def test_invalid_data_type_values_are_rejected(self):
        for value in (None, "", "number", " NUMBER", "NUMBER ", "NUMBER; DROP TABLE X", 1, "A" * 129):
            with self.subTest(value=value):
                self.assert_rows_rejected([self.changed_row(2, value)])

    def test_invalid_char_used_values_are_rejected(self):
        for value in ("CHAR", "BYTE", "c", "", 0, False):
            with self.subTest(value=value):
                self.assert_rows_rejected([self.changed_row(6, value)])

    def test_invalid_nullable_values_are_rejected(self):
        for value in (None, "", "YES", "y", 0, False):
            with self.subTest(value=value):
                self.assert_rows_rejected([self.changed_row(7, value)])

    def test_invalid_numeric_metadata_fields_are_rejected(self):
        for index, values in (
            (3, (0, -1, 10.0, "10")),
            (4, (0.0, "0")),
            (5, (-1, 40.0, "40")),
        ):
            for value in values:
                with self.subTest(field=index, value=value):
                    self.assert_rows_rejected([self.changed_row(index, value)])

    def test_bool_is_never_accepted_as_integer_metadata(self):
        for index in (1, 3, 4, 5):
            for value in (False, True):
                with self.subTest(field=index, value=value):
                    self.assert_rows_rejected([self.changed_row(index, value)])

    def test_wrong_session_prevents_column_query(self):
        for target in (("OTHER", "FREEPDB1", "HR_CORE"),
                       ("HR_CORE", "OTHER", "HR_CORE"), ("HR_CORE", "FREEPDB1", "OTHER")):
            with self.subTest(target=target):
                self.configure_rows([self.row])
                self.session_cursor.fetchone.return_value = target
                with self.assertRaises(migrate.ColumnMetadataReadError):
                    self.read()
                self.columns_cursor.execute.assert_not_called()
                self.columns_cursor.fetchall.assert_not_called()
                self.assertEqual(self.connection.cursor.call_count, 1)
                self.assert_connection_untouched()

    def test_execute_error_is_sanitized_and_cursor_exited(self):
        raw_sql = "CREATE TABLE sensitive_table (secret NUMBER)"
        error = MockOracleError(self.password + self.settings["DB_HOST"] + raw_sql)
        self.columns_cursor.execute.side_effect = error
        with self.assertRaises(migrate.ColumnMetadataReadError) as caught:
            self.read()
        for secret in (self.password, self.settings["DB_HOST"], raw_sql):
            self.assertNotIn(secret, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.columns_cursor.__exit__.assert_called_once()
        self.assertIs(self.columns_cursor.__exit__.call_args.args[1], error)
        self.columns_cursor.fetchall.assert_not_called()
        self.assert_connection_untouched()
        self.assert_no_secret_output()

    def test_fetch_error_is_not_success_and_cursor_exited(self):
        error = self.oracle_error()
        self.columns_cursor.fetchall.side_effect = error
        with self.assertRaises(migrate.ColumnMetadataReadError) as caught:
            self.read()
        self.assertNotIn(self.password, str(caught.exception))
        self.columns_cursor.__exit__.assert_called_once()
        self.assertIs(self.columns_cursor.__exit__.call_args.args[1], error)
        self.assert_connection_untouched()

    def test_success_exits_both_cursors_without_owning_connection(self):
        self.read()
        self.session_cursor.__exit__.assert_called_once_with(None, None, None)
        self.columns_cursor.__exit__.assert_called_once_with(None, None, None)
        self.assertEqual(self.connection.cursor.call_count, 2)
        self.assert_connection_untouched()

    def test_invalid_table_name_is_rejected_before_any_query(self):
        for name in (None, "", "employees", "HR_CORE.EMPLOYEES", "EMPLOYEES; DROP TABLE X", "A" * 31):
            with self.subTest(name=name), self.assertRaises(migrate.ColumnMetadataReadError):
                migrate.read_table_column_metadata(self.connection, name)
        self.connection.cursor.assert_not_called()

    def test_invalid_column_names_are_rejected(self):
        for name in (None, "", "A" * 129, "ID\nSECRET", "ID\x00", "ID\x7f", 1):
            with self.subTest(name=name):
                self.assert_rows_rejected([self.changed_row(0, name)])

    def test_session_query_driver_error_prevents_metadata_read(self):
        self.session_cursor.execute.side_effect = self.oracle_error()
        with self.assertRaises(migrate.ColumnMetadataReadError) as caught:
            self.read()
        self.assertNotIn(self.password, str(caught.exception))
        self.session_cursor.__exit__.assert_called_once()
        self.columns_cursor.execute.assert_not_called()
        self.assert_connection_untouched()


class PrimaryKeyMetadataReaderTests(ConnectionHelperTestCase):
    """Sentetik PK metadata sözleşmesi; gerçek Oracle davranışına ilişkin kanıt değildir."""

    def setUp(self):
        super().setUp()
        self.session_cursor = self.cursor
        self.pk_cursor = MagicMock(spec=["__enter__", "__exit__", "execute", "fetchall"])
        self.pk_cursor.__enter__.return_value = self.pk_cursor
        self.pk_cursor.__exit__.return_value = False
        self.constraint = ("HR_CORE", "EMPLOYEES", "PK_EMPLOYEES")
        self.column = self.constraint + ("ID", 1)
        self.configure([self.constraint], [self.column])

    def configure(self, constraints, columns):
        self.connection.cursor.reset_mock()
        self.connection.cursor.side_effect = [self.session_cursor, self.pk_cursor]
        self.session_cursor.reset_mock(side_effect=True)
        self.session_cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", "HR_CORE")
        self.pk_cursor.reset_mock(side_effect=True)
        self.pk_cursor.fetchall.side_effect = [constraints, columns]

    def read(self):
        return migrate.read_table_primary_key_metadata(self.connection, "EMPLOYEES")

    def assert_owned_connection_untouched(self):
        self.driver.connect.assert_not_called()
        self.loader.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()
        self.connection.__enter__.assert_not_called()
        self.connection.__exit__.assert_not_called()

    def assert_rejected(self, constraints, columns, expected_queries):
        self.configure(constraints, columns)
        with self.assertRaises(migrate.PrimaryKeyMetadataReadError):
            self.read()
        self.assertEqual(self.pk_cursor.execute.call_count, expected_queries)
        self.pk_cursor.__exit__.assert_called_once()
        self.assert_owned_connection_untouched()

    def test_single_column_named_or_generated_primary_key(self):
        for name in ("PK_EMPLOYEES", "SYS_C0012345"):
            with self.subTest(constraint_name=name):
                constraint = ("HR_CORE", "EMPLOYEES", name)
                self.configure([constraint], [constraint + ("ID", 1)])
                self.assertEqual(self.read(), migrate.OraclePrimaryKeyMetadata(
                    "HR_CORE", "EMPLOYEES", name, ("ID",),
                ))
                self.assert_owned_connection_untouched()

    def test_composite_primary_key_preserves_position_not_alphabetical_order(self):
        self.configure([self.constraint], [self.constraint + ("Z_CODE", 1), self.constraint + ("A_ID", 2)])
        self.assertEqual(self.read().columns, ("Z_CODE", "A_ID"))

    def test_primary_key_model_and_columns_are_immutable(self):
        result = self.read()
        self.assertTrue(type(result).__dataclass_params__.frozen)
        self.assertIsInstance(result.columns, tuple)
        with self.assertRaises(FrozenInstanceError):
            result.constraint_name = "OTHER"
        with self.assertRaises(TypeError):
            result.columns[0] = "OTHER"

    def test_absent_pk_returns_none_without_column_query(self):
        self.configure([], [])
        self.assertIsNone(self.read())
        self.pk_cursor.execute.assert_called_once()
        self.pk_cursor.fetchall.assert_called_once_with()
        self.pk_cursor.__exit__.assert_called_once_with(None, None, None)
        self.assert_owned_connection_untouched()

    def test_multiple_constraint_rows_are_rejected_before_column_query(self):
        self.assert_rejected([self.constraint, self.constraint], [self.column], 1)

    def test_existing_pk_with_missing_columns_is_not_pk_absence(self):
        for rows in ([], (), None):
            with self.subTest(columns=rows):
                self.assert_rejected([self.constraint], rows, 2)

    def test_malformed_constraint_results_are_rejected(self):
        for rows in (None, "rows", {}, [None], [()], [self.constraint[:-1]],
                     [self.constraint + ("EXTRA",)]):
            with self.subTest(rows=rows):
                self.assert_rejected(rows, [self.column], 1)

    def test_malformed_column_results_are_rejected(self):
        for rows in ("rows", {}, [None], [()], [self.column[:-1]], [self.column + ("EXTRA",)]):
            with self.subTest(rows=rows):
                self.assert_rejected([self.constraint], rows, 2)

    def test_duplicate_columns_or_positions_are_rejected(self):
        for columns in (
            [self.column, self.constraint + ("ID", 2)],
            [self.column, self.constraint + ("CODE", 1)],
        ):
            with self.subTest(columns=columns):
                self.assert_rejected([self.constraint], columns, 2)

    def test_positions_start_at_one_are_contiguous_and_not_resorted(self):
        for positions in ((2,), (1, 3), (2, 1), (1, 3, 2)):
            with self.subTest(positions=positions):
                columns = [self.constraint + (f"COL_{index}", position)
                           for index, position in enumerate(positions)]
                self.assert_rejected([self.constraint], columns, 2)

    def test_invalid_positions_including_bool_are_rejected(self):
        for value in (None, 0, -1, True, False, 1.0, "1"):
            with self.subTest(position=value):
                self.assert_rejected([self.constraint], [self.constraint + ("ID", value)], 2)

    def test_constraint_owner_and_table_scope_must_match(self):
        for constraint in (("OTHER", "EMPLOYEES", "PK_EMPLOYEES"),
                           ("HR_CORE", "OTHER", "PK_EMPLOYEES")):
            with self.subTest(constraint=constraint):
                self.assert_rejected([constraint], [self.column], 1)

    def test_column_owner_table_and_constraint_scope_must_match(self):
        for identity in (("OTHER", "EMPLOYEES", "PK_EMPLOYEES"),
                         ("HR_CORE", "OTHER", "PK_EMPLOYEES"),
                         ("HR_CORE", "EMPLOYEES", "OTHER_PK")):
            with self.subTest(identity=identity):
                self.assert_rejected([self.constraint], [identity + ("ID", 1)], 2)

    def test_invalid_metadata_names_are_rejected_without_echoing_content(self):
        for name in (None, "", 1, "A" * 129, "NAME\n", "NAME\x00"):
            with self.subTest(name=name):
                self.assert_rejected([("HR_CORE", "EMPLOYEES", name)], [self.column], 1)
                self.assert_rejected([self.constraint], [self.constraint + (name, 1)], 2)

    def test_queries_use_owner_table_constraint_binds_and_full_join(self):
        self.read()
        self.assertEqual(self.pk_cursor.execute.call_args_list, [
            call(
                "SELECT OWNER, TABLE_NAME, CONSTRAINT_NAME FROM ALL_CONSTRAINTS "
                "WHERE OWNER = :owner AND TABLE_NAME = :table_name AND CONSTRAINT_TYPE = 'P'",
                {"owner": "HR_CORE", "table_name": "EMPLOYEES"},
            ),
            call(
                "SELECT c.OWNER, c.TABLE_NAME, c.CONSTRAINT_NAME, c.COLUMN_NAME, c.POSITION "
                "FROM ALL_CONS_COLUMNS c JOIN ALL_CONSTRAINTS p "
                "ON p.OWNER = c.OWNER AND p.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
                "AND p.TABLE_NAME = c.TABLE_NAME "
                "WHERE p.OWNER = :owner AND p.TABLE_NAME = :table_name "
                "AND p.CONSTRAINT_NAME = :constraint_name AND p.CONSTRAINT_TYPE = 'P' "
                "ORDER BY c.POSITION",
                {"owner": "HR_CORE", "table_name": "EMPLOYEES", "constraint_name": "PK_EMPLOYEES"},
            ),
        ])
        for invocation in self.pk_cursor.execute.call_args_list:
            self.assertNotIn("EMPLOYEES", invocation.args[0])
            self.assertTrue(invocation.args[0].startswith("SELECT "))

    def test_wrong_session_prevents_metadata_queries(self):
        for target in (("OTHER", "FREEPDB1", "HR_CORE"),
                       ("HR_CORE", "OTHER", "HR_CORE"), ("HR_CORE", "FREEPDB1", "OTHER")):
            with self.subTest(target=target):
                self.configure([self.constraint], [self.column])
                self.session_cursor.fetchone.return_value = target
                with self.assertRaises(migrate.PrimaryKeyMetadataReadError):
                    self.read()
                self.pk_cursor.execute.assert_not_called()
                self.pk_cursor.fetchall.assert_not_called()
                self.assert_owned_connection_untouched()

    def test_execute_and_fetch_errors_stop_at_failing_stage_and_are_sanitized(self):
        raw_sql = "CREATE TABLE secret_table (id NUMBER)"
        for method_name in ("execute", "fetchall"):
            for stage in (1, 2):
                with self.subTest(method=method_name, stage=stage):
                    self.configure([self.constraint], [self.column])
                    error = MockOracleError(self.password + self.settings["DB_HOST"] + raw_sql)
                    method = getattr(self.pk_cursor, method_name)
                    success_result = None if method_name == "execute" else [self.constraint]
                    method.side_effect = [error] if stage == 1 else [success_result, error]
                    with self.assertRaises(migrate.PrimaryKeyMetadataReadError) as caught:
                        self.read()
                    self.assertEqual(self.pk_cursor.execute.call_count, stage)
                    self.assertEqual(self.pk_cursor.fetchall.call_count,
                                     stage - 1 if method_name == "execute" else stage)
                    for secret in (self.password, self.settings["DB_HOST"], raw_sql):
                        self.assertNotIn(secret, str(caught.exception))
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.pk_cursor.__exit__.assert_called_once()
                    self.assertIs(self.pk_cursor.__exit__.call_args.args[1], error)
                    self.assert_owned_connection_untouched()
                    self.assert_no_secret_output()

    def test_session_driver_error_exits_cursor_and_skips_pk_queries(self):
        self.session_cursor.execute.side_effect = self.oracle_error()
        with self.assertRaises(migrate.PrimaryKeyMetadataReadError):
            self.read()
        self.session_cursor.__exit__.assert_called_once()
        self.pk_cursor.execute.assert_not_called()
        self.assert_owned_connection_untouched()

    def test_success_closes_cursors_but_not_existing_connection(self):
        self.read()
        self.session_cursor.__exit__.assert_called_once_with(None, None, None)
        self.pk_cursor.__exit__.assert_called_once_with(None, None, None)
        self.assertEqual(self.connection.cursor.call_count, 2)
        self.assert_owned_connection_untouched()

    def test_invalid_table_argument_is_rejected_before_query(self):
        for name in (None, "", "employees", "OTHER.EMPLOYEES", "X; DROP TABLE X", "A" * 31):
            with self.subTest(name=name), self.assertRaises(migrate.PrimaryKeyMetadataReadError):
                migrate.read_table_primary_key_metadata(self.connection, name)
        self.connection.cursor.assert_not_called()


def normal_safety(actual, primary_key):
    """Eski karşılaştırma testleri için açık, sentetik normal tablo özellikleri."""
    if type(actual) is not migrate.TableColumnMetadata or type(actual.columns) is not tuple:
        return None
    constraints = []
    if type(primary_key) is migrate.OraclePrimaryKeyMetadata:
        constraints.append(migrate.OracleConstraintMetadata(
            primary_key.constraint_name, "P", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", None))
    for index, column in enumerate(actual.columns):
        if column.nullable == "N":
            constraints.append(migrate.OracleConstraintMetadata(
                f"SYS_NN{index}", "C", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE",
                f'"{column.column_name}" IS NOT NULL'))
    return migrate.TableSafetyMetadata(
        "HR_CORE", "EMPLOYEES", (("TABLE", "VALID"),), "N", None, "NO", "N",
        tuple(migrate.OracleColumnProperties(c.column_name, c.column_id, None, "NO", "NO", "NO", "YES")
              for c in actual.columns), tuple(constraints))


class TableStructureComparisonTests(OfflineTestCase):
    """Sentetik metadata; 26ai tip gösterimleri için canlı doğrulama yerine geçmez."""

    def setUp(self):
        super().setUp()
        self.expected = migrate.validate_create_table(
            "CREATE TABLE employees (id NUMBER(10) NOT NULL, code VARCHAR2(40 CHAR), "
            "amount NUMBER(12,2), created_at TIMESTAMP(6) WITH TIME ZONE, "
            "event_at TIMESTAMP, hired_at DATE, notes CLOB, raw_number NUMBER, "
            "CONSTRAINT pk_employees PRIMARY KEY (id, code))"
        )
        # Okuyucu sözleşmesindeki alan sırası; parser'dan beklenen değer türetilmez.
        rows = (
            ("ID", 1, "NUMBER", 10, 0, 0, None, "N"),
            ("CODE", 2, "VARCHAR2", None, None, 40, "C", "N"),
            ("AMOUNT", 3, "NUMBER", 12, 2, 0, None, "Y"),
            ("CREATED_AT", 4, "TIMESTAMP(6) WITH TIME ZONE", None, 6, 0, None, "Y"),
            ("EVENT_AT", 5, "TIMESTAMP(6)", None, 6, 0, None, "Y"),
            ("HIRED_AT", 6, "DATE", None, None, 0, None, "Y"),
            ("NOTES", 7, "CLOB", None, None, 0, None, "Y"),
            ("RAW_NUMBER", 8, "NUMBER", None, None, 0, None, "Y"),
        )
        self.actual = migrate.TableColumnMetadata(
            "HR_CORE", "EMPLOYEES", tuple(migrate.OracleColumnMetadata(*row) for row in rows),
        )
        self.pk = migrate.OraclePrimaryKeyMetadata("HR_CORE", "EMPLOYEES", "PK_EMPLOYEES", ("ID", "CODE"))
        self.connect = self.enterContext(patch.object(migrate, "open_oracle_connection", side_effect=AssertionError("I/O yasak")))

    def compare_full(self, expected, actual, primary_key):
        return migrate.compare_table_structure(expected, actual, primary_key,
                                               safety=normal_safety(actual, primary_key))

    def compare(self, actual=None):
        return self.compare_full(self.expected, self.actual if actual is None else actual, self.pk)

    def altered_column(self, index, **changes):
        columns = list(self.actual.columns)
        columns[index] = replace(columns[index], **changes)
        return replace(self.actual, columns=tuple(columns))

    def assert_mismatch(self, result, path=None):
        self.assertIs(result.status, migrate.StructureComparisonStatus.MISMATCH)
        self.assertFalse(result.matched)
        self.assertTrue(result.differences)
        if path is not None:
            self.assertIn(path, [difference.field for difference in result.differences])

    def test_exact_structure_matches_without_io(self):
        result = self.compare()
        self.assertTrue(result.matched)
        self.assertEqual(result.differences, ())
        self.connect.assert_not_called()
        self.history.assert_not_called()

    def test_owner_and_table_name_must_match(self):
        for change, path in (({"owner": "OTHER"}, "table.owner"), ({"table_name": "OTHER"}, "table.name")):
            with self.subTest(change=change):
                self.assert_mismatch(self.compare(replace(self.actual, **change)), path)

    def test_missing_or_extra_columns_are_mismatches(self):
        for columns in (
            self.actual.columns[:-1],
            self.actual.columns + (migrate.OracleColumnMetadata("EXTRA", 9, "DATE", None, None, 0, None, "Y"),),
        ):
            with self.subTest(count=len(columns)):
                self.assert_mismatch(self.compare(replace(self.actual, columns=columns)), "columns.count")

    def test_column_order_is_significant(self):
        columns = list(self.actual.columns)
        columns[5], columns[6] = replace(columns[6], column_id=6), replace(columns[5], column_id=7)
        self.assert_mismatch(self.compare(replace(self.actual, columns=tuple(columns))), "columns[6].name")

    def test_data_type_family_mismatch(self):
        self.assert_mismatch(self.compare(self.altered_column(6, data_type="BLOB")), "columns[7].data_type")

    def test_number_precision_and_scale_must_match(self):
        for change in ({"data_precision": 11}, {"data_scale": 3}, {"data_scale": None}):
            with self.subTest(change=change):
                self.assert_mismatch(self.compare(self.altered_column(2, **change)))

    def test_number_p_and_p_zero_have_same_logical_comparison(self):
        expected = replace(self.expected, statement=self.expected.statement.replace("NUMBER ( 10 )", "NUMBER ( 10 , 0 )"))
        self.assertTrue(self.compare_full(expected, self.actual, self.pk).matched)

    def test_bare_number_is_not_number_38_zero(self):
        self.assert_mismatch(
            self.compare(self.altered_column(7, data_precision=38, data_scale=0)), "columns[8].data_precision",
        )

    def test_varchar_character_length_and_byte_semantics_differ(self):
        for change, path in (({"char_length": 39}, "columns[2].char_length"),
                             ({"char_used": "B"}, "columns[2].char_used")):
            with self.subTest(change=change):
                self.assert_mismatch(self.compare(self.altered_column(1, **change)), path)

    def test_timestamp_precision_timezone_and_unknown_scale_fail_closed(self):
        for change in (
            {"data_type": "TIMESTAMP(3) WITH TIME ZONE", "data_scale": 3},
            {"data_type": "TIMESTAMP(6)"}, {"data_type": "TIMESTAMP WITH TIME ZONE"},
            {"data_scale": None}, {"data_precision": 6},
        ):
            with self.subTest(change=change):
                self.assert_mismatch(self.compare(self.altered_column(3, **change)))

    def test_date_and_clob_preserve_type_and_have_no_numeric_scale(self):
        for index, change in ((5, {"data_type": "CLOB"}), (6, {"data_type": "DATE"}), (5, {"data_scale": 0})):
            with self.subTest(index=index, change=change):
                self.assert_mismatch(self.compare(self.altered_column(index, **change)))

    def test_explicit_not_null_and_nullable_columns(self):
        self.assert_mismatch(self.compare(self.altered_column(0, nullable="Y")), "columns[1].nullable")
        self.assert_mismatch(self.compare(self.altered_column(2, nullable="N")), "columns[3].nullable")

    def test_primary_key_implies_effective_not_null_without_changing_model(self):
        self.assertFalse(self.expected.column_definitions[1].explicit_not_null)
        self.assertTrue(self.compare().matched)
        self.assert_mismatch(self.compare(self.altered_column(1, nullable="Y")), "columns[2].nullable")
        self.assertFalse(self.expected.column_definitions[1].explicit_not_null)

    def test_missing_and_unexpected_primary_keys(self):
        self.assert_mismatch(self.compare_full(self.expected, self.actual, None), "primary_key.presence")
        expected = migrate.validate_create_table("CREATE TABLE employees (id NUMBER)")
        actual = migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata("ID", 1, "NUMBER", None, None, 0, None, "N"),
        ))
        pk = migrate.OraclePrimaryKeyMetadata("HR_CORE", "EMPLOYEES", "SYS_C123", ("ID",))
        self.assert_mismatch(self.compare_full(expected, actual, pk), "primary_key.presence")
        self.assertTrue(self.compare_full(
            expected, replace(actual, columns=(replace(actual.columns[0], nullable="Y"),)), None,
        ).matched)

    def test_primary_key_column_order_and_explicit_name_are_significant(self):
        for change, path in (({"columns": ("CODE", "ID")}, "primary_key.columns"),
                             ({"constraint_name": "SYS_C123"}, "primary_key.constraint_name")):
            with self.subTest(change=change):
                self.assert_mismatch(self.compare_full(
                    self.expected, self.actual, replace(self.pk, **change),
                ), path)

    def test_unnamed_primary_key_has_no_name_expectation(self):
        expected = migrate.validate_create_table("CREATE TABLE employees (id NUMBER, PRIMARY KEY (id))")
        actual = migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata("ID", 1, "NUMBER", None, None, 0, None, "N"),
        ))
        for name in ("SYS_C123", "OTHER_VALID_PK"):
            with self.subTest(name=name):
                pk = migrate.OraclePrimaryKeyMetadata("HR_CORE", "EMPLOYEES", name, ("ID",))
                self.assertTrue(self.compare_full(expected, actual, pk).matched)

    def test_pk_owner_and_table_scope_mismatches(self):
        for change in ({"owner": "OTHER"}, {"table_name": "OTHER"}):
            with self.subTest(change=change):
                self.assert_mismatch(self.compare_full(self.expected, self.actual, replace(self.pk, **change)))

    def test_broken_column_snapshots_are_not_definite_mismatches(self):
        cases = (None, replace(self.actual, columns=()), replace(self.actual, columns=list(self.actual.columns)),
                 self.altered_column(0, column_id=True), self.altered_column(0, data_type=None),
                 replace(self.actual, columns=(self.actual.columns[0], self.actual.columns[0])))
        for actual in cases:
            with self.subTest(actual=actual):
                result = self.compare_full(self.expected, actual, self.pk)
                self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_METADATA)
                self.assertFalse(result.matched)

    def test_broken_or_inconsistent_pk_snapshots_are_not_definite_mismatches(self):
        for pk in (replace(self.pk, columns=()), replace(self.pk, columns=("ID", "ID")),
                   replace(self.pk, columns=("UNKNOWN",)), replace(self.pk, columns=["ID"]),
                   replace(self.pk, constraint_name=None), "not-a-pk"):
            with self.subTest(pk=pk):
                result = self.compare_full(self.expected, self.actual, pk)
                self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_invalid_expectation_is_not_accepted(self):
        for expected in (None, replace(self.expected, table_name="OTHER"),
                         replace(self.expected, column_definitions=(replace(
                             self.expected.column_definitions[0], number_precision=True,
                         ),) + self.expected.column_definitions[1:])):
            with self.subTest(expected=expected):
                result = self.compare_full(expected, self.actual, self.pk)
                self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_EXPECTATION)

    def test_result_is_deterministic_immutable_and_contains_no_raw_secrets(self):
        secret = "SYNTHETIC_SECRET_DO_NOT_PRINT"
        actual = self.altered_column(5, column_name=secret)
        first, second = self.compare(actual), self.compare(actual)
        self.assertEqual(first, second)
        self.assertNotIn(secret, repr(first))
        self.assertNotIn(self.expected.statement, repr(first))
        self.assertIsInstance(first.differences, tuple)
        with self.assertRaises(FrozenInstanceError):
            first.status = migrate.StructureComparisonStatus.MATCH
        with self.assertRaises(FrozenInstanceError):
            first.differences[0].code = secret

    def test_comparator_does_not_silently_reorder_metadata(self):
        actual = replace(self.actual, columns=tuple(reversed(self.actual.columns)))
        result = self.compare(actual)
        self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_METADATA)


class TableSafetyComparisonTests(OfflineTestCase):
    """Sentetik dictionary sözleşmesi; Oracle 26ai davranış kanıtı değildir."""

    def setUp(self):
        super().setUp()
        self.expected = migrate.validate_create_table(
            "CREATE TABLE employees (id NUMBER NOT NULL, name VARCHAR2(20 CHAR), "
            "CONSTRAINT pk_employees PRIMARY KEY (id))")
        self.actual = migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata("ID", 1, "NUMBER", None, None, 0, None, "N"),
            migrate.OracleColumnMetadata("NAME", 2, "VARCHAR2", None, None, 20, "C", "Y")))
        self.pk = migrate.OraclePrimaryKeyMetadata("HR_CORE", "EMPLOYEES", "PK_EMPLOYEES", ("ID",))
        self.safety = normal_safety(self.actual, self.pk)

    def compare(self, safety):
        return migrate.compare_table_structure(self.expected, self.actual, self.pk, safety=safety)

    def assert_blocked(self, safety, status=migrate.StructureComparisonStatus.MISMATCH):
        result = self.compare(safety)
        self.assertIs(result.status, status)
        self.assertFalse(result.matched)
        classification = migrate.classify_execution_outcome(
            migrate.DDLOutcome.SUCCEEDED, metadata=result,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED)
        self.assertFalse(classification.can_mark_success)
        return result

    def property_change(self, **changes):
        return replace(self.safety, column_properties=(
            replace(self.safety.column_properties[0], **changes), self.safety.column_properties[1]))

    def test_normal_table_matches(self):
        self.assertTrue(self.compare(self.safety).matched)

    def test_legacy_comparator_call_cannot_match(self):
        result = migrate.compare_table_structure(self.expected, self.actual, self.pk)
        self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_missing_or_partial_safety_is_invalid(self):
        for safety in (None, (), replace(self.safety, column_properties=()),
                       replace(self.safety, object_types=())):
            with self.subTest(safety_type=type(safety).__name__):
                self.assert_blocked(safety, migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_view_materialized_view_and_synonym_cannot_match(self):
        for kind in ("VIEW", "MATERIALIZED VIEW", "SYNONYM"):
            with self.subTest(kind=kind):
                self.assert_blocked(replace(self.safety, object_types=((kind, "VALID"),)))
        self.assert_blocked(replace(self.safety, object_types=(("MATERIALIZED VIEW", "VALID"), ("TABLE", "VALID"))))

    def test_invalid_object_cannot_match(self):
        self.assert_blocked(replace(self.safety, object_types=(("TABLE", "INVALID"),)))

    def test_nonordinary_tables_cannot_match(self):
        for change in ({"temporary": "Y"}, {"iot_type": "IOT"}, {"nested": "YES"}, {"secondary": "Y"}):
            with self.subTest(change=change):
                self.assert_blocked(replace(self.safety, **change))

    def test_safety_scope_must_match(self):
        for change in ({"owner": "OTHER"}, {"table_name": "OTHER"}):
            with self.subTest(change=change):
                self.assert_blocked(replace(self.safety, **change))

    def test_unexpected_default_with_complete_metadata_is_mismatch(self):
        self.assert_blocked(self.property_change(default_length=4, default_expression="1", default_on_null="NO"))

    def test_zero_default_length_is_not_silently_normalized_to_absence(self):
        self.assert_blocked(self.property_change(default_length=0), migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_identity_and_virtual_are_mismatches(self):
        for change in ({"identity": "YES", "default_length": 30, "default_expression": '"HR_CORE"."ISEQ$$_42".nextval',
                        "default_on_null": "NO", "identity_generation": "ALWAYS", "identity_sequence": "ISEQ$$_42"},
                       {"virtual": "YES"}):
            with self.subTest(change=change):
                self.assert_blocked(self.property_change(**change))

    def test_user_invisible_column_is_mismatch(self):
        self.assert_blocked(self.property_change(hidden="YES"))

    def test_unmapped_internal_column_is_unverified_not_known_mismatch(self):
        result = self.assert_blocked(self.property_change(
            hidden="YES", user_generated="NO", column_id=None),
            migrate.StructureComparisonStatus.INVALID_METADATA)
        classification = migrate.classify_execution_outcome(
            migrate.DDLOutcome.SUCCEEDED, metadata=result,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED)
        self.assertFalse(classification.can_mark_failed)

    def test_sys_prefix_does_not_prove_internal_column_mapping(self):
        self.assert_blocked(self.property_change(column_name="SYS_NC00001$", hidden="YES", user_generated="NO"),
                            migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_properties_must_cover_same_ordered_columns(self):
        for columns in (self.safety.column_properties[:-1],
                        (replace(self.safety.column_properties[0], column_name="OTHER"), self.safety.column_properties[1]),
                        (replace(self.safety.column_properties[0], column_name="NAME"),
                         replace(self.safety.column_properties[1], column_name="ID")),
                        (self.safety.column_properties[0], replace(self.safety.column_properties[1], column_id=3))):
            with self.subTest(count=len(columns)):
                self.assert_blocked(replace(self.safety, column_properties=columns),
                                    migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_duplicate_properties_or_order_are_invalid(self):
        for columns in ((self.safety.column_properties[0],) * 2,
                        tuple(reversed(self.safety.column_properties))):
            with self.subTest(columns=columns):
                self.assert_blocked(replace(self.safety, column_properties=columns),
                                    migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_bad_property_flags_and_numbers_are_invalid(self):
        for change in ({"hidden": None}, {"virtual": "UNKNOWN"}, {"identity": True},
                       {"user_generated": None}, {"default_length": True}, {"default_length": -1},
                       {"column_id": True}):
            with self.subTest(change=change):
                self.assert_blocked(self.property_change(**change), migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_lob_like_default_metadata_is_never_read_or_stringified(self):
        class SecretLOB:
            def __str__(self):
                raise AssertionError("LOB string yapılmamalı")
            def read(self):
                raise AssertionError("LOB okunmamalı")
        self.assert_blocked(self.property_change(default_length=SecretLOB()),
                            migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_pk_disabled_unvalidated_or_deferrable_cannot_match(self):
        for change in ({"status": "DISABLED"}, {"validated": "NOT VALIDATED"},
                       {"deferrable": "DEFERRABLE"}, {"deferred": "DEFERRED"}):
            with self.subTest(change=change):
                self.assert_blocked(replace(self.safety, constraints=(
                    replace(self.safety.constraints[0], **change), self.safety.constraints[1])))

    def test_primary_key_evidence_must_agree_with_pk_reader(self):
        for constraints in (self.safety.constraints[1:], (
            replace(self.safety.constraints[0], constraint_name="OTHER"), self.safety.constraints[1])):
            with self.subTest(count=len(constraints)):
                self.assert_blocked(replace(self.safety, constraints=constraints),
                                    migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_additional_unique_foreign_key_and_check_are_rejected(self):
        for kind, condition in (("U", None), ("R", None), ("C", "ID > 0")):
            with self.subTest(kind=kind):
                extra = migrate.OracleConstraintMetadata(
                    "EXTRA", kind, "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", condition,
                    ("ID",) if kind in ("U", "R") else None,
                    migrate.OracleForeignKeyReference("HR_CORE", "OTHER", "PK_OTHER", "P", ("ID",),
                        "NO ACTION", "ENABLED", "VALIDATED") if kind == "R" else None)
                self.assert_blocked(replace(self.safety, constraints=self.safety.constraints + (extra,)))

    def test_safe_not_null_parentheses_and_keyword_case_are_accepted(self):
        for condition in ('"ID" IS NOT NULL', "id is not null", ' (( "ID" is NOT null )) '):
            with self.subTest(condition=condition):
                constraints = (self.safety.constraints[0], replace(self.safety.constraints[1], condition=condition))
                self.assertTrue(self.compare(replace(self.safety, constraints=constraints)).matched)

    def test_not_null_expression_injection_and_case_sensitive_identifier_rejected(self):
        for condition in ('"id" IS NOT NULL', "ID IS NOT NULL OR 1=1", "ID IS NOT NULL; SELECT 1",
                          "ID /* hidden */ IS NOT NULL", "'ID' IS NOT NULL", "(ID IS NOT NULL", "ID IS NOT NULL)",
                          "(ID) IS NOT NULL", "NAME IS NOT NULL", "ID IS NOT NULL " + " " * 1001):
            with self.subTest(condition=condition[:40]):
                constraints = (self.safety.constraints[0], replace(self.safety.constraints[1], condition=condition))
                self.assert_blocked(replace(self.safety, constraints=constraints))

    def test_explicit_not_null_constraint_cannot_disappear(self):
        self.assert_blocked(replace(self.safety, constraints=self.safety.constraints[:1]))

    def test_pk_only_nullability_does_not_require_extra_check(self):
        expected = migrate.validate_create_table("CREATE TABLE employees (id NUMBER, PRIMARY KEY (id))")
        actual = replace(self.actual, columns=self.actual.columns[:1])
        pk = replace(self.pk, constraint_name="SYS_C123")
        safety = replace(normal_safety(actual, pk), constraints=(
            migrate.OracleConstraintMetadata("SYS_C123", "P", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", None),))
        self.assertTrue(migrate.compare_table_structure(expected, actual, pk, safety=safety).matched)

    def test_duplicate_checks_are_not_silently_ignored(self):
        duplicate = replace(self.safety.constraints[1], constraint_name="OTHER_NN")
        self.assert_blocked(replace(self.safety, constraints=self.safety.constraints + (duplicate,)))

    def test_malformed_or_missing_constraint_state_is_invalid(self):
        for change in ({"status": None}, {"validated": "?"}, {"deferrable": None},
                       {"deferred": None}, {"condition": "SECRET"}):
            with self.subTest(change=change):
                self.assert_blocked(replace(self.safety, constraints=(
                    replace(self.safety.constraints[0], **change), self.safety.constraints[1])),
                    migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_nested_snapshots_are_frozen_and_repr_hides_check_text(self):
        self.assertIsInstance(self.safety.column_properties, tuple)
        self.assertIsInstance(self.safety.constraints, tuple)
        for model, name, value in ((self.safety, "owner", "OTHER"),
                                   (self.safety.column_properties[0], "hidden", "YES"),
                                   (self.safety.constraints[0], "status", "DISABLED")):
            with self.subTest(model_type=type(model).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(model, name, value)
        secret = "PASSWORD_AND_RAW_SQL"
        safety = replace(self.safety, constraints=(replace(self.safety.constraints[1], condition=secret),))
        self.assertNotIn(secret, repr(safety))
        self.assertNotIn(secret, repr(self.compare(safety)))


class TableSafetyReaderTests(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.metadata_cursor = MagicMock(spec=["__enter__", "__exit__", "execute", "fetchall"])
        self.metadata_cursor.__enter__.return_value = self.metadata_cursor
        self.metadata_cursor.__exit__.return_value = False
        self.objects = [("TABLE", "VALID")]
        self.tables = [("N", None, "NO", "N")]
        self.properties = [("ID", 1, None, "NO", "NO", "NO", "YES")]
        self.constraints = [("SYS_NN1", "C", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", '"ID" IS NOT NULL')]
        self.configure()

    def configure(self):
        self.connection.cursor.side_effect = [self.cursor, self.metadata_cursor]
        self.metadata_cursor.fetchall.side_effect = [self.objects, self.tables, self.properties, self.constraints]

    def read(self):
        return migrate.read_table_safety_metadata(self.connection, "EMPLOYEES")

    def assert_untouched(self):
        self.driver.connect.assert_not_called()
        self.loader.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_not_called()

    def test_queries_binds_and_snapshot(self):
        result = self.read()
        self.assertEqual(result.object_types, (("TABLE", "VALID"),))
        self.assertEqual(result.column_properties[0].column_name, "ID")
        self.assertEqual(result.constraints[0].deferred, "IMMEDIATE")
        queries = [item.args[0] for item in self.metadata_cursor.execute.call_args_list]
        for query in queries:
            self.assertTrue(query.startswith("SELECT "))
            self.assertIn("OWNER = :owner", query)
            self.assertNotIn("EMPLOYEES", query)
        for item in self.metadata_cursor.execute.call_args_list:
            self.assertEqual(item.args[1], {"owner": "HR_CORE", "table_name": "EMPLOYEES"})
        for table, query in zip(("ALL_OBJECTS", "ALL_TABLES", "ALL_TAB_COLS", "ALL_CONSTRAINTS"), queries):
            self.assertIn(table, query)
        self.assertIn("DEFAULT_LENGTH", queries[2])
        self.assertNotIn("DATA_DEFAULT", queries[2])
        self.assertIn("INTERNAL_COLUMN_ID", queries[2])
        for field in ("STATUS", "VALIDATED", "DEFERRABLE", "DEFERRED", "SEARCH_CONDITION_VC"):
            self.assertIn(field, queries[3])
        self.metadata_cursor.__exit__.assert_called_once_with(None, None, None)
        self.assert_untouched()

    def test_wrong_target_stops_before_metadata(self):
        for session in (("OTHER", "FREEPDB1", "HR_CORE"), ("HR_CORE", "OTHER", "HR_CORE"),
                        ("HR_CORE", "FREEPDB1", "OTHER")):
            with self.subTest(session=session):
                self.configure()
                self.cursor.fetchone.return_value = session
                with self.assertRaises(migrate.TableSafetyMetadataReadError):
                    self.read()
                self.metadata_cursor.execute.assert_not_called()
                self.assert_untouched()

    def test_execute_errors_are_safe_and_close_cursor(self):
        for stage in range(4):
            with self.subTest(stage=stage):
                self.metadata_cursor.reset_mock(side_effect=True)
                self.configure()
                self.metadata_cursor.execute.side_effect = [None] * stage + [self.oracle_error()]
                with self.assertRaises(migrate.TableSafetyMetadataReadError) as caught:
                    self.read()
                self.assertNotIn(self.password, str(caught.exception))
                self.assertNotIn(self.settings["DB_HOST"], str(caught.exception))
                self.assertEqual(self.metadata_cursor.execute.call_count, stage + 1)
                self.metadata_cursor.__exit__.assert_called_once()
                self.assert_untouched()

    def test_fetch_errors_are_safe_and_stop_queries(self):
        for stage in range(4):
            with self.subTest(stage=stage):
                self.metadata_cursor.reset_mock(side_effect=True)
                self.configure()
                results = [self.objects, self.tables, self.properties, self.constraints]
                self.metadata_cursor.fetchall.side_effect = results[:stage] + [self.oracle_error()]
                with self.assertRaises(migrate.TableSafetyMetadataReadError) as caught:
                    self.read()
                self.assertNotIn(self.password, str(caught.exception))
                self.assertEqual(self.metadata_cursor.execute.call_count, stage + 1)
                self.metadata_cursor.__exit__.assert_called_once()
                self.assert_untouched()

    def test_empty_missing_and_extra_rows_fail_closed(self):
        cases = (([], self.tables, self.properties, self.constraints),
                 (self.objects, [], self.properties, self.constraints),
                 (self.objects, self.tables * 2, self.properties, self.constraints),
                 (self.objects, [("N",)], self.properties, self.constraints),
                 (self.objects, self.tables, [], self.constraints),
                 (self.objects, self.tables, [("ID",)], self.constraints),
                 (self.objects, self.tables, self.properties, [("PK",)]))
        for rows in cases:
            with self.subTest(row_lengths=[len(value) for value in rows]):
                self.configure()
                self.metadata_cursor.fetchall.side_effect = rows
                with self.assertRaises(migrate.TableSafetyMetadataReadError):
                    self.read()
                self.assert_untouched()

    def test_no_constraints_is_a_valid_snapshot(self):
        self.constraints = []
        self.configure()
        self.assertEqual(self.read().constraints, ())

    def test_unknown_internal_column_is_not_dropped_from_inventory(self):
        self.properties.append(("SYS_NC00002$", None, None, "YES", "YES", "NO", "NO"))
        self.configure()
        snapshot = self.read()
        self.assertEqual(len(snapshot.column_properties), 2)
        self.assertEqual(snapshot.column_properties[1].user_generated, "NO")
        self.assertEqual(snapshot.column_properties[1].column_name, "SYS_NC00002$")

    def test_untrusted_table_name_rejected_before_cursor(self):
        with self.assertRaises(migrate.TableSafetyMetadataReadError):
            migrate.read_table_safety_metadata(self.connection, "EMPLOYEES; DROP TABLE X")
        self.connection.cursor.assert_not_called()


class SingleMigrationExecutorTests(ApplyPlanTestCase):
    """Gerçek plan/parser/sınıflandırıcı ve lock context; bütün I/O sınırları mock."""

    def setUp(self):
        super().setUp()
        self.connection.autocommit = False
        self.directory = Path("/synthetic/migrations")
        self.events = []
        self.collect = self.enterContext(patch.object(migrate, "collect_migrations", return_value=[self.local()]))
        self.settings_loader = self.enterContext(patch.object(
            migrate, "load_connection_settings", return_value=(self.settings, 1521)))
        self.target = self.enterContext(patch.object(migrate, "validate_target_session"))
        self.history_reader = self.enterContext(patch.object(migrate, "read_history_from_connection", return_value=[]))
        self.confirm = self.enterContext(patch.object(migrate, "request_apply_confirmation"))
        self.allocate = self.enterContext(patch.object(migrate, "allocate_migration_lock", return_value="HANDLE"))
        self.request = self.enterContext(patch.object(migrate, "request_migration_lock"))
        self.release = self.enterContext(patch.object(migrate, "release_migration_lock"))
        self.precheck = self.enterContext(patch.object(migrate, "validate_create_table_preconditions"))
        self.insert = self.enterContext(patch.object(migrate, "insert_running_migration"))
        self.success_writer = self.enterContext(patch.object(migrate, "mark_migration_success"))
        self.failed_writer = self.enterContext(patch.object(migrate, "mark_migration_failed"))
        self.actual = migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata("ID", 1, "NUMBER", None, None, 0, None, "Y"),))
        self.columns = self.enterContext(patch.object(migrate, "read_table_column_metadata", return_value=self.actual))
        self.pk = self.enterContext(patch.object(migrate, "read_table_primary_key_metadata", return_value=None))
        self.safety = self.enterContext(patch.object(migrate, "read_table_safety_metadata", return_value=normal_safety(self.actual, None)))
        self.build = self.enterContext(patch.object(migrate, "build_apply_plan", wraps=migrate.build_apply_plan))
        for name, mock in (("confirm", self.confirm), ("allocate", self.allocate),
                           ("request", self.request), ("release", self.release),
                           ("precheck", self.precheck), ("insert", self.insert),
                           ("success", self.success_writer), ("failed", self.failed_writer),
                           ("columns", self.columns), ("pk", self.pk), ("safety", self.safety)):
            value = mock.return_value
            mock.side_effect = lambda *args, _name=name, _value=value, **kwargs: self.event(_name, _value)
        self.cursor.execute.side_effect = lambda *args: self.event("ddl")
        self.connection.commit.side_effect = lambda: self.event("commit")
        self.connection.close.side_effect = lambda: self.event("close")

    def event(self, name, value=None):
        self.events.append(name)
        return value

    def execute(self):
        result = migrate.execute_one_migration(self.directory, oracledb=self.driver, load_dotenv=self.loader)
        self.loader.assert_not_called()
        self.connection.rollback.assert_not_called()
        self.assertNotIn(self.password, repr(result))
        self.assertNotIn(self.content.decode(), repr(result))
        self.assert_no_secret_output()
        return result

    def assert_no_writes(self):
        self.insert.assert_not_called()
        self.success_writer.assert_not_called()
        self.failed_writer.assert_not_called()
        self.connection.commit.assert_not_called()
        self.cursor.execute.assert_not_called()

    def assert_stopped(self, phase):
        result = self.execute()
        self.assertFalse(result.success)
        self.assertIs(result.phase, phase)
        self.connection.close.assert_called_once()
        return result

    def test_successful_single_migration_order_and_same_connection(self):
        result = self.execute()
        self.assertTrue(result.success)
        self.assertIs(result.phase, migrate.ExecutionPhase.COMPLETE)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.COMPLETE)
        self.assertEqual(self.events, ["confirm", "allocate", "request", "precheck", "insert", "commit",
                                       "ddl", "columns", "pk", "safety", "success", "commit", "release", "close"])
        self.driver.connect.assert_called_once()
        for mock in (self.allocate, self.request, self.release, self.precheck, self.insert,
                     self.success_writer, self.columns, self.pk, self.safety, self.history_reader):
            for invocation in mock.call_args_list:
                self.assertIs(invocation.args[0], self.connection)
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.cursor.__exit__.assert_called_once_with(None, None, None)

    def test_no_candidate_has_no_confirmation_lock_or_writes(self):
        self.collect.return_value = []
        result = self.execute()
        self.assertTrue(result.no_pending)
        self.assertFalse(result.classification.manual_review_required)
        self.confirm.assert_not_called()
        self.allocate.assert_not_called()
        self.assert_no_writes()
        self.connection.close.assert_called_once()

    def test_local_error_never_opens_connection_or_loads_settings(self):
        self.collect.side_effect = ValueError(self.password)
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.LOCAL_VALIDATION)
        self.driver.connect.assert_not_called()
        self.settings_loader.assert_not_called()
        self.connection.close.assert_not_called()
        self.assert_no_writes()

    def test_local_sql_validation_precedes_connection(self):
        self.reader.side_effect = ValueError("unsupported source")
        self.execute()
        self.driver.connect.assert_not_called()
        self.settings_loader.assert_not_called()

    def test_settings_error_is_safe_and_does_not_connect(self):
        self.settings_loader.side_effect = ValueError(self.password)
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.CONNECTION)
        self.driver.connect.assert_not_called()
        self.assert_no_writes()

    def test_connection_error_is_safe(self):
        self.driver.connect.side_effect = self.oracle_error()
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.CONNECTION)
        self.connection.close.assert_not_called()
        self.assert_no_writes()

    def test_wrong_initial_target_stops_before_history(self):
        self.target.side_effect = ValueError("wrong target")
        self.assert_stopped(migrate.ExecutionPhase.CONNECTION)
        self.history_reader.assert_not_called()
        self.assert_no_writes()

    def test_autocommit_is_explicitly_disabled_by_real_open_helper(self):
        self.connection.autocommit = True
        self.assertTrue(self.execute().success)
        self.assertIs(self.connection.autocommit, False)

    def test_blocking_history_stops_before_approval(self):
        self.history_reader.return_value = [history_row(status="RUNNING", checksum=self.checksum)]
        self.assert_stopped(migrate.ExecutionPhase.INITIAL_PLAN)
        self.confirm.assert_not_called()
        self.assert_no_writes()

    def test_initial_history_error_stops_before_approval(self):
        self.history_reader.side_effect = self.oracle_error()
        self.assert_stopped(migrate.ExecutionPhase.INITIAL_PLAN)
        self.confirm.assert_not_called()
        self.assert_no_writes()

    def test_confirmation_rejection_cannot_allocate_or_write(self):
        self.confirm.side_effect = migrate.ApplyApprovalError("rejected")
        self.assert_stopped(migrate.ExecutionPhase.CONFIRMATION)
        self.allocate.assert_not_called()
        self.assert_no_writes()

    def test_confirmation_interrupt_is_safe(self):
        self.confirm.side_effect = KeyboardInterrupt()
        result = self.assert_stopped(migrate.ExecutionPhase.CONFIRMATION)
        self.assertTrue(result.interrupted)
        self.assert_no_writes()

    def test_lock_allocation_error_stops_without_release(self):
        self.allocate.side_effect = self.oracle_error()
        self.assert_stopped(migrate.ExecutionPhase.LOCK)
        self.request.assert_not_called()
        self.release.assert_not_called()
        self.assert_no_writes()

    def test_lock_request_failure_stops_without_release(self):
        self.request.side_effect = migrate.MigrationLockError("not acquired")
        self.assert_stopped(migrate.ExecutionPhase.LOCK)
        self.release.assert_not_called()
        self.assert_no_writes()

    def test_plan_disappearing_under_lock_stops_and_releases(self):
        self.collect.side_effect = [[self.local()], []]
        self.assert_stopped(migrate.ExecutionPhase.REVALIDATION)
        self.release.assert_called_once()
        self.assert_no_writes()

    def test_new_history_under_lock_is_not_applied_again(self):
        self.history_reader.side_effect = [[], [history_row(checksum=self.checksum)]]
        self.assert_stopped(migrate.ExecutionPhase.REVALIDATION)
        self.release.assert_called_once()
        self.assert_no_writes()

    def test_changed_source_under_lock_requires_new_approval(self):
        self.collect.side_effect = [[self.local()], [local_migration(checksum=OTHER_CHECKSUM)]]
        self.assert_stopped(migrate.ExecutionPhase.REVALIDATION)
        self.assert_no_writes()

    def test_lost_session_under_lock_stops_before_dml(self):
        self.target.side_effect = [None, self.oracle_error()]
        self.assert_stopped(migrate.ExecutionPhase.REVALIDATION)
        self.assert_no_writes()
        self.release.assert_called_once()

    def test_preconditions_failure_stops_before_insert(self):
        self.precheck.side_effect = migrate.MetadataPrecheckError("collision")
        self.assert_stopped(migrate.ExecutionPhase.PRECONDITIONS)
        self.assert_no_writes()
        self.release.assert_called_once()

    def test_running_insert_error_is_possible_history_not_retry(self):
        self.insert.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.RUNNING_INSERT)
        self.assertIs(result.history_state, migrate.HistoryState.RUNNING_POSSIBLE)
        self.insert.assert_called_once()
        self.connection.commit.assert_not_called()
        self.cursor.execute.assert_not_called()
        self.failed_writer.assert_not_called()

    def test_running_commit_error_prevents_ddl(self):
        self.connection.commit.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.RUNNING_COMMIT)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.NOT_STARTED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.HISTORY_COMMIT_UNKNOWN)
        self.connection.commit.assert_called_once()
        self.cursor.execute.assert_not_called()
        self.failed_writer.assert_not_called()

    def test_running_commit_state_is_uncertain_before_call(self):
        # KeyboardInterrupt commit çağrısının en başında bile belirsizliktir.
        self.connection.commit.side_effect = KeyboardInterrupt()
        result = self.assert_stopped(migrate.ExecutionPhase.RUNNING_COMMIT)
        self.assertTrue(result.interrupted)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.cursor.execute.assert_not_called()

    def test_session_loss_after_running_commit_does_not_send_ddl(self):
        self.target.side_effect = [None, None, self.oracle_error()]
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.NOT_STARTED)
        self.assertIs(result.history_state, migrate.HistoryState.RUNNING_CONFIRMED)
        self.cursor.execute.assert_not_called()
        self.failed_writer.assert_not_called()

    def test_autocommit_change_after_running_commit_prevents_ddl(self):
        def changed():
            self.connection.autocommit = True
        self.connection.commit.side_effect = changed
        self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.cursor.execute.assert_not_called()
        self.failed_writer.assert_not_called()

    def test_ddl_error_is_unknown_not_failed_or_retried(self):
        self.cursor.execute.side_effect = self.oracle_error("ORA-00955")
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.DDL_UNKNOWN)
        self.cursor.execute.assert_called_once()
        self.failed_writer.assert_not_called()
        self.success_writer.assert_not_called()
        self.columns.assert_not_called()
        self.cursor.__exit__.assert_called_once()

    def test_keyboard_interrupt_during_ddl_is_unknown(self):
        self.cursor.execute.side_effect = KeyboardInterrupt()
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertTrue(result.interrupted)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.cursor.execute.assert_called_once()
        self.failed_writer.assert_not_called()

    def test_cursor_creation_error_does_not_claim_ddl_submission(self):
        self.connection.cursor.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.NOT_STARTED)
        self.failed_writer.assert_not_called()

    def test_cursor_exit_error_preserves_confirmed_ddl_success(self):
        self.cursor.__exit__.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.failed_writer.assert_not_called()

    def test_post_ddl_metadata_errors_do_not_erase_ddl_success(self):
        self.columns.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.METADATA)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.failed_writer.assert_not_called()
        self.success_writer.assert_not_called()

    def test_primary_key_metadata_error_preserves_confirmed_ddl(self):
        self.pk.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.METADATA)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.safety.assert_not_called()
        self.failed_writer.assert_not_called()
        self.success_writer.assert_not_called()

    def test_safety_metadata_error_preserves_confirmed_ddl(self):
        self.safety.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.METADATA)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.failed_writer.assert_not_called()
        self.success_writer.assert_not_called()

    def test_metadata_interrupt_is_unverified_not_unknown_ddl(self):
        self.safety.side_effect = KeyboardInterrupt()
        result = self.assert_stopped(migrate.ExecutionPhase.METADATA)
        self.assertTrue(result.interrupted)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.failed_writer.assert_not_called()

    def test_metadata_mismatch_records_confirmed_failed_without_retry(self):
        self.columns.return_value = replace(self.actual, columns=(replace(self.actual.columns[0], data_type="DATE"),))
        self.columns.side_effect = None
        result = self.execute()
        self.assertTrue(result.failed_history_confirmed)
        self.assertIs(result.history_state, migrate.HistoryState.FAILED_CONFIRMED)
        self.assertFalse(result.classification.can_mark_failed)
        self.assertFalse(result.success)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.DDL_METADATA_MISMATCH)
        self.failed_writer.assert_called_once()
        self.assertEqual(self.failed_writer.call_args.kwargs,
                         {"failure_reason": "POST_VALIDATION_FAILED", "outcome_confirmed": True})
        self.success_writer.assert_not_called()
        self.assertEqual(self.connection.commit.call_count, 2)
        self.cursor.execute.assert_called_once()

    def test_invalid_metadata_keeps_running_without_failed_or_success(self):
        self.safety.side_effect = None
        self.safety.return_value = None
        result = self.assert_stopped(migrate.ExecutionPhase.METADATA)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.failed_writer.assert_not_called()
        self.success_writer.assert_not_called()
        self.connection.commit.assert_called_once()

    def test_success_update_failure_preserves_ddl_verified_but_not_complete(self):
        self.success_writer.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.SUCCESS_UPDATE)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.history_state, migrate.HistoryState.RUNNING_CONFIRMED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.DDL_VERIFIED)
        self.failed_writer.assert_not_called()
        self.cursor.execute.assert_called_once()
        self.connection.commit.assert_called_once()

    def test_success_commit_error_is_uncertain_and_never_failed(self):
        self.connection.commit.side_effect = [None, self.oracle_error()]
        result = self.assert_stopped(migrate.ExecutionPhase.SUCCESS_COMMIT)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.failed_writer.assert_not_called()
        self.assertEqual(self.connection.commit.call_count, 2)
        self.cursor.execute.assert_called_once()

    def test_interrupt_during_success_commit_is_uncertain(self):
        self.connection.commit.side_effect = [None, KeyboardInterrupt()]
        result = self.assert_stopped(migrate.ExecutionPhase.SUCCESS_COMMIT)
        self.assertTrue(result.interrupted)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.failed_writer.assert_not_called()

    def test_failed_update_error_is_not_retried(self):
        self.columns.side_effect = None
        self.columns.return_value = replace(self.actual, columns=(replace(self.actual.columns[0], nullable="N"),))
        self.failed_writer.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.FAILED_UPDATE)
        self.assertFalse(result.failed_history_confirmed)
        self.failed_writer.assert_called_once()
        self.connection.commit.assert_called_once()

    def test_failed_commit_error_is_uncertain_not_confirmed_failed(self):
        self.columns.side_effect = None
        self.columns.return_value = replace(self.actual, columns=(replace(self.actual.columns[0], nullable="N"),))
        self.connection.commit.side_effect = [None, self.oracle_error()]
        result = self.assert_stopped(migrate.ExecutionPhase.FAILED_COMMIT)
        self.assertFalse(result.failed_history_confirmed)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.failed_writer.assert_called_once()

    def test_only_smallest_pending_is_sent_exactly_once(self):
        self.collect.return_value = [self.local(2), self.local(1)]
        result = self.execute()
        self.assertTrue(result.success)
        self.assertEqual(self.insert.call_args.args[1].version, 1)
        self.assertEqual(self.success_writer.call_args.args[1].version, 1)
        self.cursor.execute.assert_called_once_with(self.insert.call_args.args[1].statement)
        self.assertEqual(self.build.call_count, 2)

    def test_unexpected_release_error_after_success_is_reported_without_rewriting_history(self):
        self.release.side_effect = self.oracle_error()
        result = self.execute()
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_failed)
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.COMPLETE)
        self.failed_writer.assert_not_called()
        self.cursor.execute.assert_called_once()
        self.connection.close.assert_called_once()

    def test_release_error_cannot_hide_primary_ddl_error(self):
        self.cursor.execute.side_effect = self.oracle_error()
        self.release.side_effect = self.oracle_error()
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertTrue(result.cleanup_failed)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.failed_writer.assert_not_called()
        self.connection.close.assert_called_once()

    def test_connection_close_error_is_safe_and_not_success(self):
        self.connection.close.side_effect = self.oracle_error()
        result = self.execute()
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_failed)
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)
        self.connection.close.assert_called_once()
        self.failed_writer.assert_not_called()

    def test_keyboard_interrupt_during_release_is_reported(self):
        self.release.side_effect = KeyboardInterrupt()
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertTrue(result.cleanup_failed)
        self.assertFalse(result.success)
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)

    def test_close_failure_preserves_primary_phase_and_ddl_uncertainty(self):
        self.cursor.execute.side_effect = self.oracle_error()
        self.connection.close.side_effect = self.oracle_error()
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertTrue(result.cleanup_failed)
        self.failed_writer.assert_not_called()
        self.connection.close.assert_called_once()

    def test_interruption_before_ddl_keeps_confirmed_running(self):
        self.target.side_effect = [None, None, KeyboardInterrupt()]
        result = self.assert_stopped(migrate.ExecutionPhase.DDL)
        self.assertTrue(result.interrupted)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.NOT_STARTED)
        self.assertIs(result.history_state, migrate.HistoryState.RUNNING_CONFIRMED)
        self.cursor.execute.assert_not_called()
        self.failed_writer.assert_not_called()

    def test_confirmed_failed_history_is_not_treated_as_running_or_rewritable(self):
        metadata = migrate.TableStructureComparison(
            migrate.StructureComparisonStatus.MISMATCH, (migrate.StructureDifference("table", "DIFF"),))
        result = migrate.classify_execution_outcome(migrate.DDLOutcome.SUCCEEDED, metadata=metadata,
                                                   history_state=migrate.HistoryState.FAILED_CONFIRMED)
        self.assertFalse(result.can_mark_failed)
        self.assertFalse(result.can_mark_success)
        self.assertTrue(result.manual_review_required)
        self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_METADATA_MISMATCH)
        invalid = migrate.classify_execution_outcome(migrate.DDLOutcome.NOT_STARTED,
                                                     history_state=migrate.HistoryState.FAILED_CONFIRMED)
        self.assertIs(invalid.outcome, migrate.ExecutionOutcome.INVALID_EVIDENCE)

    def test_result_is_frozen_and_never_contains_raw_error(self):
        self.cursor.execute.side_effect = RuntimeError(self.password + " CREATE TABLE secret")
        result = self.execute()
        self.assertNotIn("CREATE TABLE", repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.phase = migrate.ExecutionPhase.COMPLETE

    def test_parser_lists_apply_without_executing_it(self):
        with patch.object(migrate, "execute_one_migration", side_effect=AssertionError("Parser executor çağıramaz")) as executor:
            self.assertEqual(set(migrate.build_parser()._subparsers._group_actions[0].choices),
                             {"validate", "plan", "status", "db-plan", "apply"})
        executor.assert_not_called()


class SyntheticExecutorConnection:
    """Yalnızca Python verisi; Oracle/soket/.env erişimi olmayan birleşik test sınırı."""

    def __init__(self):
        self.events = []
        self.cursors = []
        self.history = None
        self.pending = None
        self.created = False
        self.commits = 0
        self.commit_errors = {}
        self.commit_after_apply = set()
        self.errors = {}
        self.close_error = None
        self.setup_error = None
        self._autocommit = True
        self.columns = [("ID", 1, "NUMBER", 10, 0, 0, None, "N"),
                        ("CODE", 2, "VARCHAR2", None, None, 20, "C", "Y")]
        self.properties = [("ID", 1, None, "NO", "NO", "NO", "YES"),
                           ("CODE", 2, None, "NO", "NO", "NO", "YES")]
        self.constraints = [("PK_EMPLOYEES", "P", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", None),
                            ("SYS_NN1", "C", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", '"ID" IS NOT NULL')]

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if self.setup_error is not None:
            raise self.setup_error
        self._autocommit = value

    def cursor(self):
        cursor = SyntheticExecutorCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1
        self.events.append(("COMMIT", self.commits))
        error = self.commit_errors.get(self.commits)
        if error is not None and self.commits not in self.commit_after_apply:
            raise error
        if self.pending is not None:
            self.history = self.pending.copy()
            self.pending = None
        if error is not None:
            raise error

    def close(self):
        self.events.append(("CLOSE",))
        if self.close_error is not None:
            raise self.close_error
        self.pending = None

    def rollback(self):
        raise AssertionError("Executor otomatik rollback çağırmamalı")


class SyntheticExecutorCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0
        self.rows = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def var(self, kind, size):
        return SimpleNamespace(getvalue=lambda: "SYNTHETIC_HANDLE")

    def callproc(self, name, parameters):
        self.connection.events.append(("ALLOCATE", name, parameters[0]))
        if "ALLOCATE" in self.connection.errors:
            raise self.connection.errors["ALLOCATE"]

    def callfunc(self, name, kind, parameters):
        operation = "RELEASE" if name.endswith(".RELEASE") else "REQUEST"
        self.connection.events.append((operation, tuple(parameters)))
        if operation in self.connection.errors:
            raise self.connection.errors[operation]
        return 0

    def execute(self, sql, parameters=None):
        connection = self.connection
        query = sql.upper()
        self.rows = []
        self.rowcount = 0
        if query.startswith("CREATE TABLE"):
            operation = "DDL"
        elif query.startswith("INSERT INTO HR_CORE.SCHEMA_MIGRATIONS"):
            operation = "INSERT"
        elif query.startswith("UPDATE HR_CORE.SCHEMA_MIGRATIONS"):
            operation = "UPDATE_" + parameters["status"]
        else:
            operation = "SELECT"
        connection.events.append((operation, sql, parameters))
        if operation in connection.errors:
            raise connection.errors[operation]
        if operation == "DDL":
            if connection.created:
                raise AssertionError("DDL ikinci kez gönderildi")
            connection.created = True
        elif operation == "INSERT":
            if connection.history is not None:
                raise AssertionError("Mevcut history üzerine INSERT")
            connection.pending = dict(parameters)
            self.rowcount = 1
        elif operation.startswith("UPDATE_"):
            if connection.history is None or connection.history["status"] != "RUNNING":
                raise AssertionError("Yalnız eşleşen RUNNING güncellenebilir")
            for field in ("version", "script_name", "checksum"):
                if parameters[field] != connection.history[field]:
                    raise AssertionError("History bind kimliği farklı")
            connection.pending = dict(parameters)
            self.rowcount = 1
        elif "SYS_CONTEXT" in query:
            self.rows = [("HR_CORE", "FREEPDB1", "HR_CORE")]
        elif "FROM HR_CORE.SCHEMA_MIGRATIONS" in query:
            record = connection.history
            self.rows = [] if record is None else [(
                record["version"], record["script_name"], record["checksum"], record["status"], None, None)]
        elif "COUNT(*)" in query:
            self.rows = [(0,)]
        elif "FROM ALL_TAB_COLUMNS" in query:
            if "COLUMNS" in connection.errors:
                raise connection.errors["COLUMNS"]
            self.rows = list(connection.columns)
        elif "FROM ALL_TAB_COLS" in query:
            self.rows = list(connection.properties)
        elif "FROM ALL_OBJECTS" in query:
            self.rows = [("TABLE", "VALID")]
        elif "FROM ALL_TABLES" in query:
            self.rows = [("N", None, "NO", "N")]
        elif "FROM ALL_CONS_COLUMNS" in query:
            self.rows = [("HR_CORE", "EMPLOYEES", "PK_EMPLOYEES", "ID", 1)]
        elif "SEARCH_CONDITION_VC" in query:
            self.rows = list(connection.constraints)
        elif "FROM ALL_CONSTRAINTS" in query:
            self.rows = [("HR_CORE", "EMPLOYEES", "PK_EMPLOYEES")]
        else:
            raise AssertionError("Sentetik sürücüde beklenmeyen SQL")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class IntegratedExecutorTests(ConnectionHelperTestCase):
    """Gerçek bağlantı/history/metadata/onay/lock yardımcıları; yalnız sınırlar sahte."""

    def setUp(self):
        super().setUp()
        self.synthetic = SyntheticExecutorConnection()
        self.driver.connect.return_value = self.synthetic
        self.directory = Path("/synthetic/migrations")
        self.content = (b"CREATE TABLE employees (id NUMBER(10) NOT NULL, code VARCHAR2(20 CHAR), "
                        b"CONSTRAINT pk_employees PRIMARY KEY (id));")
        self.checksum = hashlib.sha256(self.content).hexdigest()
        self.local = local_migration(checksum=self.checksum)
        self.collect = self.enterContext(patch.object(migrate, "collect_migrations", return_value=[self.local]))
        self.enterContext(patch.object(migrate, "read_migration_for_apply", side_effect=lambda migration, directory:
                                      migrate.prepare_migration_content(migration, self.content)))
        self.enterContext(patch.object(sys.stdin, "isatty", return_value=True))
        self.user_input = self.enterContext(patch("builtins.input", return_value=(
            f"APPLY HR_CORE/FREEPDB1 V0001 {self.checksum}")))

    def execute(self):
        result = migrate.execute_one_migration(self.directory, oracledb=self.driver, load_dotenv=self.loader)
        # Gerçek load_dotenv değil, verilen Mock çağrılır; .env içeriğine erişim yok.
        self.loader.assert_called_once_with(migrate.PROJECT_ROOT / ".env", override=False)
        self.driver.connect.assert_called_once()
        self.assertTrue(all(cursor.closed for cursor in self.synthetic.cursors))
        self.assert_no_secret_output()
        self.assertNotIn(self.content.decode().rstrip(";"), repr(result))
        self.assertNotIn(self.password, repr(result))
        self.assertLessEqual(self.operations().count("DDL"), 1)
        self.assertEqual(self.operations().count("CLOSE"), 1)
        return result

    def operations(self):
        return [event[0] for event in self.synthetic.events]

    def writes(self):
        return [operation for operation in self.operations() if operation in
                ("INSERT", "COMMIT", "DDL", "UPDATE_SUCCESS", "UPDATE_FAILED")]

    def test_real_helpers_success_order_metadata_and_binds(self):
        result = self.execute()
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.manual_review_required)
        self.assertEqual(self.writes(), ["INSERT", "COMMIT", "DDL", "UPDATE_SUCCESS", "COMMIT"])
        self.assertEqual(self.synthetic.history["status"], "SUCCESS")
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)
        queries = [e for e in self.synthetic.events if e[0] == "SELECT"]
        for view in ("ALL_TAB_COLUMNS", "ALL_TAB_COLS", "ALL_CONSTRAINTS", "ALL_CONS_COLUMNS", "ALL_OBJECTS", "ALL_TABLES"):
            self.assertTrue(any(view in e[1] for e in queries))
        for event in queries:
            if "FROM ALL_" in event[1]:
                self.assertEqual(event[2]["owner"], "HR_CORE")
        allocation = next(e for e in self.synthetic.events if e[0] == "ALLOCATE")
        self.assertEqual(allocation[2], migrate.MIGRATION_LOCK_NAME)
        request = next(e for e in self.synthetic.events if e[0] == "REQUEST")
        self.assertEqual(request[1], ("SYNTHETIC_HANDLE", 6, 0, False))

    def test_conflicting_column_names_preserve_running(self):
        self.synthetic.properties[1] = ("OTHER", 2, None, "NO", "NO", "NO", "YES")
        result = self.execute()
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.assertTrue(result.manual_review_required)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertEqual(self.writes(), ["INSERT", "COMMIT", "DDL"])

    def test_conflicting_column_ids_preserve_running(self):
        row = list(self.synthetic.properties[1])
        row[1] = 3
        self.synthetic.properties[1] = tuple(row)
        result = self.execute()
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.assertTrue(result.manual_review_required)
        self.assertNotIn("UPDATE_FAILED", self.operations())

    def test_conflicting_column_order_is_not_a_definite_structure_failure(self):
        self.synthetic.properties = [("CODE", 1, None, "NO", "NO", "NO", "YES"),
                                     ("ID", 2, None, "NO", "NO", "NO", "YES")]
        result = self.execute()
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertTrue(result.manual_review_required)
        self.assertNotIn("UPDATE_FAILED", self.operations())
        self.assertNotIn("UPDATE_SUCCESS", self.operations())

    def test_consistent_wrong_type_remains_mismatch_and_confirmed_failed(self):
        row = list(self.synthetic.columns[1])
        row[2], row[5], row[6] = "DATE", 0, None
        self.synthetic.columns[1] = tuple(row)
        result = self.execute()
        self.assertTrue(result.failed_history_confirmed)
        self.assertEqual(self.synthetic.history["status"], "FAILED")
        self.assertTrue(self.synthetic.created)
        self.assertTrue(result.manual_review_required)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.writes(), ["INSERT", "COMMIT", "DDL", "UPDATE_FAILED", "COMMIT"])

    def test_running_commit_failure_prevents_create(self):
        self.synthetic.commit_errors[1] = self.oracle_error()
        result = self.execute()
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertFalse(self.synthetic.created)
        self.assertEqual(self.writes(), ["INSERT", "COMMIT"])
        self.assertTrue(result.manual_review_required)

    def test_running_commit_can_apply_then_raise_without_create(self):
        self.synthetic.commit_errors[1] = self.oracle_error()
        self.synthetic.commit_after_apply.add(1)
        result = self.execute()
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertNotIn("DDL", self.operations())

    def test_success_commit_failure_never_repeats_create(self):
        self.synthetic.commit_errors[2] = self.oracle_error()
        result = self.execute()
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertEqual(self.operations().count("DDL"), 1)
        self.assertNotIn("UPDATE_FAILED", self.operations())
        self.assertTrue(result.manual_review_required)

    def test_success_commit_can_apply_then_raise_and_stays_unknown(self):
        self.synthetic.commit_errors[2] = self.oracle_error()
        self.synthetic.commit_after_apply.add(2)
        result = self.execute()
        self.assertEqual(self.synthetic.history["status"], "SUCCESS")
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.manual_review_required)

    def test_metadata_read_error_preserves_created_table_and_running(self):
        self.synthetic.errors["COLUMNS"] = self.oracle_error()
        result = self.execute()
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertTrue(result.manual_review_required)
        self.assertNotIn("UPDATE_FAILED", self.operations())

    def test_ddl_driver_error_never_marks_failed_or_retries(self):
        self.synthetic.errors["DDL"] = self.oracle_error()
        result = self.execute()
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertEqual(self.operations().count("DDL"), 1)
        self.assertNotIn("UPDATE_FAILED", self.operations())

    def test_setup_and_close_failure_are_reported_without_driver_text(self):
        self.synthetic.setup_error = self.oracle_error()
        self.synthetic.close_error = RuntimeError(self.password)
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.CONNECTION)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.manual_review_required)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.operations(), ["CLOSE"])

    def test_setup_failure_with_successful_close_does_not_claim_cleanup_failure(self):
        self.synthetic.setup_error = self.oracle_error()
        result = self.execute()
        self.assertFalse(result.cleanup_failed)
        self.assertEqual(result.exit_code, 1)
        self.assertIs(result.phase, migrate.ExecutionPhase.CONNECTION)

    def test_setup_interrupt_with_close_error_is_preserved(self):
        self.synthetic.setup_error = KeyboardInterrupt()
        self.synthetic.close_error = self.oracle_error()
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.manual_review_required)
        self.assertEqual(self.operations(), ["CLOSE"])

    def test_setup_error_with_cleanup_system_exit_is_safe(self):
        self.synthetic.setup_error = self.oracle_error()
        self.synthetic.close_error = SystemExit("SECRET")
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertTrue(result.cleanup_failed)
        self.assertIs(result.phase, migrate.ExecutionPhase.CONNECTION)
        self.assertEqual(result.exit_code, 1)

    def test_successful_history_with_release_and_close_failures_requires_review(self):
        self.synthetic.errors["RELEASE"] = self.oracle_error()
        self.synthetic.close_error = self.oracle_error()
        result = self.execute()
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)
        self.assertIs(result.classification.outcome, migrate.ExecutionOutcome.COMPLETE)
        self.assertFalse(result.classification.manual_review_required)
        self.assertFalse(result.success)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.manual_review_required)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.synthetic.history["status"], "SUCCESS")

    def test_close_failure_alone_requires_review_after_confirmed_success(self):
        self.synthetic.close_error = self.oracle_error()
        result = self.execute()
        self.assertTrue(result.manual_review_required)
        self.assertTrue(result.cleanup_failed)
        self.assertIs(result.history_state, migrate.HistoryState.SUCCESS_CONFIRMED)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("RELEASE", self.operations())

    def test_primary_ddl_error_survives_release_and_close_failures(self):
        self.synthetic.errors["DDL"] = self.oracle_error()
        self.synthetic.errors["RELEASE"] = self.oracle_error()
        self.synthetic.close_error = self.oracle_error()
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.manual_review_required)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")

    def test_real_confirmation_ctrl_c_is_distinct_from_rejection(self):
        self.user_input.side_effect = KeyboardInterrupt()
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertEqual(result.exit_code, 1)
        self.assertIs(result.phase, migrate.ExecutionPhase.CONFIRMATION)
        self.assertNotIn("ALLOCATE", self.operations())
        self.assertEqual(self.writes(), [])

    def test_real_confirmation_wrong_answer_is_not_interruption(self):
        self.user_input.return_value = "NO"
        result = self.execute()
        self.assertFalse(result.interrupted)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.writes(), [])

    def test_real_confirmation_system_exit_stops_without_lock(self):
        self.user_input.side_effect = SystemExit(9)
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.writes(), [])
        self.assertNotIn("ALLOCATE", self.operations())

    def test_keyboard_interrupt_during_ddl_keeps_running(self):
        self.synthetic.errors["DDL"] = KeyboardInterrupt()
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertNotIn("UPDATE_FAILED", self.operations())

    def test_system_exit_during_running_commit_never_sends_create(self):
        self.synthetic.commit_errors[1] = SystemExit(8)
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertNotIn("DDL", self.operations())

    def test_custom_base_exception_during_success_commit_is_uncertain(self):
        class StopNow(BaseException):
            pass
        self.synthetic.commit_errors[2] = StopNow(self.password)
        result = self.execute()
        self.assertTrue(result.interrupted)
        self.assertIs(result.history_state, migrate.HistoryState.COMMIT_UNKNOWN)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.operations().count("DDL"), 1)
        self.assertNotIn("UPDATE_FAILED", self.operations())

    def test_secondary_release_interrupt_does_not_hide_primary_ddl_phase(self):
        self.synthetic.errors["DDL"] = self.oracle_error()
        self.synthetic.errors["RELEASE"] = KeyboardInterrupt()
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertTrue(result.interrupted)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.manual_review_required)

    def test_empty_plan_is_successful_noop_by_total_exit_contract(self):
        self.collect.return_value = []
        result = self.execute()
        self.assertTrue(result.no_pending)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.manual_review_required)
        self.assertEqual(self.writes(), [])
        self.assertNotIn("ALLOCATE", self.operations())

    def test_secondary_warning_interrupt_keeps_primary_error_and_cleanup_status(self):
        self.synthetic.errors["DDL"] = self.oracle_error()
        self.synthetic.errors["RELEASE"] = self.oracle_error()
        with patch.object(sys.stderr, "write", side_effect=KeyboardInterrupt()):
            result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.DDL)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.UNKNOWN)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.interrupted)
        self.assertEqual(result.exit_code, 1)

    def test_empty_plan_close_failure_is_not_a_successful_noop(self):
        self.collect.return_value = []
        self.synthetic.close_error = self.oracle_error()
        result = self.execute()
        self.assertTrue(result.no_pending)
        self.assertTrue(result.cleanup_failed)
        self.assertTrue(result.manual_review_required)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.writes(), [])

    def test_setup_base_exception_and_cleanup_interrupt_preserve_both_flags(self):
        class SetupAbort(BaseException):
            pass
        self.synthetic.setup_error = SetupAbort(self.password)
        self.synthetic.close_error = KeyboardInterrupt()
        with self.assertRaises(migrate.ConnectionSetupError) as caught:
            migrate.open_oracle_connection(self.settings, 1521, self.driver)
        error = caught.exception
        self.assertTrue(error.setup_interrupted)
        self.assertTrue(error.cleanup_interrupted)
        self.assertTrue(error.cleanup_failed)
        self.assertNotIn(self.password, str(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertEqual(self.operations(), ["CLOSE"])
        self.driver.connect.assert_called_once()

    def test_success_update_driver_error_keeps_running_and_never_retries(self):
        self.synthetic.errors["UPDATE_SUCCESS"] = self.oracle_error()
        result = self.execute()
        self.assertIs(result.phase, migrate.ExecutionPhase.SUCCESS_UPDATE)
        self.assertIs(result.ddl_outcome, migrate.DDLOutcome.SUCCEEDED)
        self.assertEqual(self.synthetic.history["status"], "RUNNING")
        self.assertEqual(self.operations().count("UPDATE_SUCCESS"), 1)
        self.assertEqual(self.operations().count("DDL"), 1)
        self.assertNotIn("UPDATE_FAILED", self.operations())
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(result.manual_review_required)

    def test_second_pending_is_never_executed(self):
        self.collect.return_value = [local_migration(2, self.checksum), self.local]
        result = self.execute()
        self.assertTrue(result.success)
        self.assertEqual(self.synthetic.history["version"], 1)
        self.assertEqual(self.operations().count("DDL"), 1)
        self.assertEqual(self.operations().count("INSERT"), 1)
        self.assertEqual(self.operations().count("UPDATE_SUCCESS"), 1)


class ExecutionClassificationTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.match = migrate.TableStructureComparison(migrate.StructureComparisonStatus.MATCH)
        self.mismatch = migrate.TableStructureComparison(
            migrate.StructureComparisonStatus.MISMATCH,
            (migrate.StructureDifference("columns[1].data_type", "VALUE_MISMATCH"),),
        )
        # Sınıflandırma hiçbir history yazma/kilit/bağlantı yolunu kullanamaz.
        self.forbidden = []
        for name in ("open_oracle_connection", "insert_running_migration", "mark_migration_success",
                     "mark_migration_failed", "allocate_migration_lock", "read_table_column_metadata"):
            self.forbidden.append(self.enterContext(patch.object(
                migrate, name, side_effect=AssertionError("Sınıflandırıcı I/O yapamaz"),
            )))

    def tearDown(self):
        for method in self.forbidden:
            method.assert_not_called()
        super().tearDown()

    def classify(self, ddl, metadata=None, history=None):
        return migrate.classify_execution_outcome(
            ddl, metadata=metadata,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED if history is None else history,
        )

    def assert_no_automatic_recovery(self, result):
        self.assertFalse(result.retry_allowed)
        self.assertFalse(result.automatic_cleanup_allowed)
        self.assertFalse(result.ddl_rollback_allowed)

    def test_not_started_with_no_history_needs_no_manual_recovery(self):
        result = self.classify(migrate.DDLOutcome.NOT_STARTED, history=migrate.HistoryState.NOT_RECORDED)
        self.assertIs(result.outcome, migrate.ExecutionOutcome.NOT_EXECUTED)
        self.assertFalse(result.manual_review_required)
        self.assertFalse(result.can_mark_failed)
        self.assertFalse(result.can_mark_success)

    def test_not_started_with_possible_or_confirmed_running_requires_review(self):
        for history in (migrate.HistoryState.RUNNING_POSSIBLE, migrate.HistoryState.RUNNING_CONFIRMED):
            with self.subTest(history=history):
                result = self.classify(migrate.DDLOutcome.NOT_STARTED, history=history)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.NOT_EXECUTED)
                self.assertTrue(result.manual_review_required)
                self.assertFalse(result.can_mark_failed)

    def test_confirmed_rejection_can_only_propose_failed_for_confirmed_running(self):
        result = self.classify(migrate.DDLOutcome.CONFIRMED_REJECTED)
        self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_REJECTED)
        self.assertTrue(result.can_mark_failed)
        self.assertEqual(result.failed_reason, "DDL_REJECTED")
        self.assertTrue(result.manual_review_required)
        self.assertFalse(result.can_mark_success)

    def test_verified_ddl_is_not_yet_committed_history_success(self):
        result = self.classify(migrate.DDLOutcome.SUCCEEDED, self.match)
        self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_VERIFIED)
        self.assertTrue(result.can_mark_success)
        self.assertTrue(result.manual_review_required)
        self.assertFalse(result.can_mark_failed)

    def test_complete_requires_confirmed_success_history(self):
        result = self.classify(migrate.DDLOutcome.SUCCEEDED, self.match, migrate.HistoryState.SUCCESS_CONFIRMED)
        self.assertIs(result.outcome, migrate.ExecutionOutcome.COMPLETE)
        self.assertFalse(result.manual_review_required)
        self.assertFalse(result.can_mark_success)
        self.assertFalse(result.can_mark_failed)

    def test_definite_post_ddl_structure_mismatch_has_fixed_failed_reason(self):
        result = self.classify(migrate.DDLOutcome.SUCCEEDED, self.mismatch)
        self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_METADATA_MISMATCH)
        self.assertEqual(result.failed_reason, "POST_VALIDATION_FAILED")
        self.assertTrue(result.can_mark_failed)
        self.assertTrue(result.manual_review_required)

    def test_unknown_ddl_is_not_resolved_by_metadata_match(self):
        for metadata in (None, self.match, self.mismatch):
            with self.subTest(metadata=metadata):
                result = self.classify(migrate.DDLOutcome.UNKNOWN, metadata)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_UNKNOWN)
                self.assertTrue(result.manual_review_required)
                self.assertFalse(result.can_mark_failed)
                self.assertFalse(result.can_mark_success)

    def test_commit_uncertainty_overrides_every_ddl_outcome(self):
        for ddl in migrate.DDLOutcome:
            with self.subTest(ddl=ddl):
                result = self.classify(ddl, self.match, migrate.HistoryState.COMMIT_UNKNOWN)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.HISTORY_COMMIT_UNKNOWN)
                self.assertTrue(result.manual_review_required)
                self.assertFalse(result.can_mark_failed)
                self.assertFalse(result.can_mark_success)
                self.assert_no_automatic_recovery(result)

    def test_metadata_read_or_shape_failure_does_not_mean_failed_ddl(self):
        for metadata in (
            None,
            migrate.TableStructureComparison(migrate.StructureComparisonStatus.INVALID_METADATA),
            migrate.TableStructureComparison(migrate.StructureComparisonStatus.INVALID_EXPECTATION),
        ):
            with self.subTest(metadata=metadata):
                result = self.classify(migrate.DDLOutcome.SUCCEEDED, metadata)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
                self.assertFalse(result.can_mark_failed)
                self.assertFalse(result.can_mark_success)

    def test_inconsistent_metadata_results_are_not_accepted(self):
        for metadata in (
            replace(self.match, differences=self.mismatch.differences),
            replace(self.mismatch, differences=()),
            replace(self.match, status="MATCH"),
            replace(self.match, differences=[]),
            replace(self.mismatch, differences=("raw-driver-message",)),
        ):
            with self.subTest(metadata=metadata):
                result = self.classify(migrate.DDLOutcome.SUCCEEDED, metadata)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.METADATA_UNVERIFIED)
                self.assertFalse(result.can_mark_failed)

    def test_contradictory_or_untyped_execution_evidence_fails_closed(self):
        cases = (
            ("SUCCEEDED", self.match, migrate.HistoryState.RUNNING_CONFIRMED),
            (migrate.DDLOutcome.SUCCEEDED, self.match, "RUNNING_CONFIRMED"),
            (migrate.DDLOutcome.NOT_STARTED, self.match, migrate.HistoryState.RUNNING_CONFIRMED),
            (migrate.DDLOutcome.CONFIRMED_REJECTED, None, migrate.HistoryState.NOT_RECORDED),
            (migrate.DDLOutcome.SUCCEEDED, self.match, migrate.HistoryState.RUNNING_POSSIBLE),
        )
        for ddl, metadata, history in cases:
            with self.subTest(ddl=ddl, history=history):
                result = self.classify(ddl, metadata, history)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.INVALID_EVIDENCE)
                self.assertTrue(result.manual_review_required)
                self.assertFalse(result.can_mark_failed)
                self.assertFalse(result.can_mark_success)

    def test_generic_exceptions_after_submission_never_prove_rejection(self):
        for error in (TimeoutError("network"), ConnectionError("lost"), MockOracleError("ORA-00955"),
                      RuntimeError("driver"), KeyboardInterrupt()):
            with self.subTest(error_type=type(error).__name__):
                result = migrate.classify_execution_exception(
                    error, ddl_submitted=True, history_state=migrate.HistoryState.RUNNING_CONFIRMED,
                )
                self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_UNKNOWN)
                self.assertFalse(result.can_mark_failed)
                self.assert_no_automatic_recovery(result)

    def test_exception_during_commit_is_uncertain_before_or_after_ddl(self):
        for submitted in (False, True):
            with self.subTest(submitted=submitted):
                result = migrate.classify_execution_exception(
                    ConnectionError("lost"), ddl_submitted=submitted, commit_in_progress=True,
                    history_state=migrate.HistoryState.RUNNING_CONFIRMED,
                )
                self.assertIs(result.outcome, migrate.ExecutionOutcome.HISTORY_COMMIT_UNKNOWN)
                self.assertFalse(result.can_mark_failed)
                self.assertFalse(result.can_mark_success)

    def test_exception_before_ddl_keeps_running_for_review(self):
        result = migrate.classify_execution_exception(
            RuntimeError("preflight"), ddl_submitted=False,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED,
        )
        self.assertIs(result.outcome, migrate.ExecutionOutcome.NOT_EXECUTED)
        self.assertTrue(result.manual_review_required)
        self.assertFalse(result.can_mark_failed)

    def test_exception_text_and_code_are_not_inspected_or_leaked(self):
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError("Ham hata metni okunmamalı")

        secret = "PASSWORD_HOST_AND_RAW_SQL"
        result = migrate.classify_execution_exception(UnprintableError(secret), ddl_submitted=True)
        self.assertNotIn(secret, repr(result))
        self.assertIs(result.outcome, migrate.ExecutionOutcome.DDL_UNKNOWN)

    def test_exception_stage_flags_are_strictly_typed(self):
        for kwargs in (
            {"ddl_submitted": 1}, {"ddl_submitted": True, "commit_in_progress": "yes"},
            {"ddl_submitted": False, "history_state": "COMMIT_UNKNOWN"},
        ):
            with self.subTest(kwargs=kwargs):
                result = migrate.classify_execution_exception(RuntimeError(), **kwargs)
                self.assertIs(result.outcome, migrate.ExecutionOutcome.INVALID_EVIDENCE)
                self.assertFalse(result.can_mark_failed)

    def test_all_classification_paths_disallow_automatic_recovery(self):
        for ddl in migrate.DDLOutcome:
            for history in migrate.HistoryState:
                for metadata in (None, self.match, self.mismatch):
                    with self.subTest(ddl=ddl, history=history, metadata=metadata):
                        self.assert_no_automatic_recovery(self.classify(ddl, metadata, history))

    def test_result_is_immutable_and_deterministic(self):
        first = self.classify(migrate.DDLOutcome.UNKNOWN)
        self.assertEqual(first, self.classify(migrate.DDLOutcome.UNKNOWN))
        with self.assertRaises(FrozenInstanceError):
            first.failed_reason = "DDL_REJECTED"


class UniqueParserTests(OfflineTestCase):
    def parse(self, tail, columns="id NUMBER(19,0) NOT NULL, code VARCHAR2(30 CHAR), email VARCHAR2(50 CHAR)"):
        return migrate.validate_create_table(f"CREATE TABLE employees ({columns}, {tail});")

    def test_named_single_unique_with_pk(self):
        model = self.parse("CONSTRAINT pk_employees PRIMARY KEY(id), CONSTRAINT uq_code UNIQUE(code)")
        self.assertEqual(model.unique_constraints, (migrate.UniqueConstraintDefinition("UQ_CODE", "CODE"),))
        self.assertEqual(model.primary_key.columns, ("ID",))
        self.assertEqual(model.primary_key_constraint_name, "PK_EMPLOYEES")

    def test_two_distinct_unique_constraints(self):
        model = self.parse("CONSTRAINT uq_code UNIQUE(code), CONSTRAINT uq_email UNIQUE(email)")
        self.assertEqual(tuple((c.constraint_name, c.column_name) for c in model.unique_constraints),
                         (("UQ_CODE", "CODE"), ("UQ_EMAIL", "EMAIL")))
        self.assertIsNone(model.primary_key)

    def test_unique_before_unnamed_composite_pk(self):
        model = self.parse("CONSTRAINT uq_code UNIQUE(code), PRIMARY KEY(email,id)")
        self.assertEqual(model.primary_key.columns, ("EMAIL", "ID"))
        self.assertIsNone(model.primary_key_constraint_name)

    def test_unique_models_are_immutable_and_do_not_imply_not_null(self):
        model = self.parse("CONSTRAINT uq_code UNIQUE(code)")
        self.assertIs(type(model.unique_constraints), tuple)
        self.assertFalse(model.column_definitions[1].explicit_not_null)
        with self.assertRaises(FrozenInstanceError):
            model.unique_constraints[0].column_name = "EMAIL"
        with self.assertRaises(FrozenInstanceError):
            model.unique_constraints = ()

    def test_missing_unique_column_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("CONSTRAINT uq_code UNIQUE(missing)")

    def test_duplicate_constraint_names_including_pk_rejected(self):
        for tail in (
            "CONSTRAINT uq_code UNIQUE(code), CONSTRAINT uq_code UNIQUE(email)",
            "CONSTRAINT uq_code PRIMARY KEY(id), CONSTRAINT uq_code UNIQUE(code)",
            "CONSTRAINT uq_code UNIQUE(code), CONSTRAINT uq_code PRIMARY KEY(id)",
        ):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                self.parse(tail)

    def test_duplicate_unique_column_rejected(self):
        with self.assertRaises(ValueError):
            self.parse("CONSTRAINT uq_code UNIQUE(code), CONSTRAINT uq_other UNIQUE(code)")

    def test_composite_unnamed_and_function_unique_rejected(self):
        for tail in ("CONSTRAINT uq_code UNIQUE(code,email)", "UNIQUE(code)",
                     "CONSTRAINT uq_code UNIQUE(LOWER(code))"):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                self.parse(tail)

    def test_inline_unique_rejected(self):
        for column in ("code VARCHAR2(30 CHAR) UNIQUE", "code VARCHAR2(30 CHAR) CONSTRAINT uq_code UNIQUE"):
            with self.subTest(column=column), self.assertRaises(ValueError):
                migrate.validate_create_table(f"CREATE TABLE employees ({column})")

    def test_unique_options_rejected(self):
        for option in ("DEFERRABLE", "ENABLE", "DISABLE", "NOVALIDATE", "USING INDEX"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.parse(f"CONSTRAINT uq_code UNIQUE(code) {option}")

    def test_identifiers_and_statement_boundary_remain_strict(self):
        for name in ('"UQ_CODE"', "HR_CORE.UQ_CODE", "UQ$CODE", "U" * 31, "SYS_UQ_CODE"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.parse(f"CONSTRAINT {name} UNIQUE(code)")
        for suffix in ("; CREATE TABLE other (id NUMBER)", " USING INDEX", " ENABLE"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                migrate.validate_create_table("CREATE TABLE employees (code NUMBER, CONSTRAINT uq_code UNIQUE(code))" + suffix)

    def test_columns_after_constraints_and_second_pk_rejected(self):
        for tail in ("CONSTRAINT uq_code UNIQUE(code), extra NUMBER", "PRIMARY KEY(id), PRIMARY KEY(code)"):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                self.parse(tail)

    def test_v0001_bytes_checksum_and_model_unchanged(self):
        path = migrate.MIGRATIONS_DIRECTORY / "V0001__create_hrms_it_a101_exec_r01.sql"
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
                         "5c92f2817932a27dca61f1b937cecdb08d40ac3c8495ca7eab429eab984aeb2b")
        model = migrate.validate_create_table(raw.decode("utf-8"))
        self.assertEqual(model.unique_constraints, ())
        self.assertEqual(model.primary_key.columns, ("ID",))
        self.assertIsNone(model.primary_key_constraint_name)


class UniqueComparisonTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.expected = migrate.validate_create_table(
            "CREATE TABLE employees (id NUMBER NOT NULL, code VARCHAR2(30 CHAR), "
            "email VARCHAR2(50 CHAR), CONSTRAINT pk_employees PRIMARY KEY(id), "
            "CONSTRAINT uq_code UNIQUE(code))")
        self.actual = migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata("ID", 1, "NUMBER", None, None, 0, None, "N"),
            migrate.OracleColumnMetadata("CODE", 2, "VARCHAR2", None, None, 30, "C", "Y"),
            migrate.OracleColumnMetadata("EMAIL", 3, "VARCHAR2", None, None, 50, "C", "Y")))
        self.pk = migrate.OraclePrimaryKeyMetadata("HR_CORE", "EMPLOYEES", "PK_EMPLOYEES", ("ID",))
        self.unique = migrate.OracleConstraintMetadata(
            "UQ_CODE", "U", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", None, ("CODE",))
        self.safety = replace(normal_safety(self.actual, self.pk),
                              constraints=normal_safety(self.actual, self.pk).constraints + (self.unique,))

    def compare(self, constraints=None, expected=None):
        safety = self.safety if constraints is None else replace(self.safety, constraints=constraints)
        return migrate.compare_table_structure(expected or self.expected, self.actual, self.pk, safety=safety)

    def test_exact_unique_match_and_nullable_column(self):
        self.assertTrue(self.compare().matched)
        self.assertFalse(self.expected.column_definitions[1].explicit_not_null)

    def test_two_unique_constraints_match_independent_of_constraint_order(self):
        expected = migrate.validate_create_table(self.expected.statement[:-1] + ", CONSTRAINT uq_email UNIQUE(email))")
        extra = replace(self.unique, constraint_name="UQ_EMAIL", columns=("EMAIL",))
        self.assertTrue(self.compare((extra,) + self.safety.constraints, expected).matched)

    def test_missing_unique_mismatches(self):
        self.assertIs(self.compare(self.safety.constraints[:-1]).status, migrate.StructureComparisonStatus.MISMATCH)

    def test_additional_unique_mismatches(self):
        extra = replace(self.unique, constraint_name="UQ_EMAIL", columns=("EMAIL",))
        self.assertIs(self.compare(self.safety.constraints + (extra,)).status, migrate.StructureComparisonStatus.MISMATCH)

    def test_wrong_name_column_and_composite_mismatch(self):
        for unique in (replace(self.unique, constraint_name="OTHER"),
                       replace(self.unique, columns=("EMAIL",)), replace(self.unique, columns=("CODE", "EMAIL"))):
            with self.subTest(unique=unique):
                self.assertIs(self.compare(self.safety.constraints[:-1] + (unique,)).status,
                              migrate.StructureComparisonStatus.MISMATCH)

    def test_pk_is_not_counted_as_unique(self):
        self.assertTrue(self.compare().matched)
        self.assertFalse(self.compare(self.safety.constraints[:-1]).matched)

    def test_incomplete_or_unknown_column_unique_metadata_is_invalid(self):
        for columns in (None, (), ["CODE"], ("MISSING",), ("CODE", "CODE")):
            with self.subTest(columns=columns):
                unique = replace(self.unique, columns=columns)
                self.assertIs(self.compare(self.safety.constraints[:-1] + (unique,)).status,
                              migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_unique_must_be_enabled_validated_and_immediate(self):
        for changes in ({"status": "DISABLED"}, {"validated": "NOT VALIDATED"},
                        {"deferrable": "DEFERRABLE"}, {"deferred": "DEFERRED"}):
            with self.subTest(changes=changes):
                unique = replace(self.unique, **changes)
                self.assertFalse(self.compare(self.safety.constraints[:-1] + (unique,)).matched)

    def test_tampered_expected_unique_model_is_invalid(self):
        expected = replace(self.expected, unique_constraints=(migrate.UniqueConstraintDefinition("OTHER", "CODE"),))
        self.assertIs(self.compare(expected=expected).status, migrate.StructureComparisonStatus.INVALID_EXPECTATION)


class UniqueMetadataReaderTests(ConnectionHelperTestCase):
    """Sentetik dictionary satırları; canlı Oracle davranışının kanıtı değildir."""
    def setUp(self):
        super().setUp()
        self.metadata_cursor = MagicMock()
        self.metadata_cursor.__enter__.return_value = self.metadata_cursor
        self.connection.cursor.side_effect = [self.cursor, self.metadata_cursor]
        self.constraints = [("UQ_CODE", "U", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", None)]
        self.rows = [("HR_CORE", "EMPLOYEES", "UQ_CODE", "CODE", 1)]
        self.configure()

    def configure(self):
        self.connection.cursor.side_effect = [self.cursor, self.metadata_cursor]
        self.metadata_cursor.fetchall.side_effect = [
            [("TABLE", "VALID")], [("N", None, "NO", "N")],
            [("CODE", 1, None, "NO", "NO", "NO", "YES")], self.constraints, self.rows]

    def read(self):
        return migrate.read_table_safety_metadata(self.connection, "EMPLOYEES")

    def test_unique_query_binds_join_and_immutable_snapshot(self):
        result = self.read()
        unique, = result.constraints
        self.assertEqual(unique.columns, ("CODE",))
        self.assertEqual(unique.constraint_type, "U")
        query, params = self.metadata_cursor.execute.call_args_list[-1].args
        for fragment in ("ALL_CONS_COLUMNS", "ALL_CONSTRAINTS", "u.OWNER = c.OWNER",
                         "u.TABLE_NAME = c.TABLE_NAME", "u.CONSTRAINT_NAME = c.CONSTRAINT_NAME",
                         "u.CONSTRAINT_TYPE = 'U'", "ORDER BY c.CONSTRAINT_NAME, c.POSITION"):
            self.assertIn(fragment, query)
        self.assertEqual(params, {"owner": "HR_CORE", "table_name": "EMPLOYEES"})
        self.assertNotIn("EMPLOYEES", query)
        self.assertNotIn("INDEX", query)
        with self.assertRaises(FrozenInstanceError):
            unique.columns = ()
        self.driver.connect.assert_not_called()
        self.loader.assert_not_called()
        for method in (self.connection.commit, self.connection.rollback, self.connection.close):
            method.assert_not_called()

    def test_real_reader_and_comparator_produce_match_without_pk(self):
        safety = self.read()
        expected = migrate.validate_create_table(
            "CREATE TABLE employees (code NUMBER, CONSTRAINT uq_code UNIQUE(code))")
        actual = migrate.TableColumnMetadata("HR_CORE", "EMPLOYEES", (
            migrate.OracleColumnMetadata("CODE", 1, "NUMBER", None, None, 0, None, "Y"),))
        result = migrate.compare_table_structure(expected, actual, None, safety=safety)
        self.assertTrue(result.matched)
        self.assertEqual(self.metadata_cursor.execute.call_count, 5)
        self.driver.connect.assert_not_called()

    def test_duplicate_unique_identity_in_first_snapshot_is_rejected(self):
        self.constraints *= 2
        self.configure()
        with self.assertRaises(migrate.TableSafetyMetadataReadError):
            self.read()
        self.assertEqual(self.metadata_cursor.execute.call_count, 4)

    def test_multiple_unique_column_mappings(self):
        self.constraints += [("UQ_EMAIL", "U", "ENABLED", "VALIDATED", "NOT DEFERRABLE", "IMMEDIATE", None)]
        self.rows += [("HR_CORE", "EMPLOYEES", "UQ_EMAIL", "EMAIL", 1)]
        self.configure()
        self.assertEqual(tuple(c.columns for c in self.read().constraints), (("CODE",), ("EMAIL",)))

    def test_absent_unique_avoids_extra_query(self):
        self.constraints = []
        self.configure()
        self.assertEqual(self.read().constraints, ())
        self.assertEqual(self.metadata_cursor.execute.call_count, 4)

    def test_missing_or_malformed_unique_columns_fail_closed(self):
        for rows in ([], [()], [self.rows[0] + ("EXTRA",)], [None], None):
            with self.subTest(rows=rows):
                self.rows = rows
                self.configure()
                with self.assertRaises(migrate.TableSafetyMetadataReadError):
                    self.read()

    def test_wrong_scope_constraint_column_and_position_rejected(self):
        valid = ("HR_CORE", "EMPLOYEES", "UQ_CODE", "CODE", 1)
        for index, values in ((0, ("OTHER",)), (1, ("OTHER",)), (2, ("OTHER",)),
                              (3, (None, "")), (4, (True, None, 0, -1, 2, "1"))):
            for value in values:
                row = list(valid)
                row[index] = value
                self.rows = [tuple(row)]
                self.configure()
                with self.subTest(index=index, value=value), self.assertRaises(migrate.TableSafetyMetadataReadError):
                    self.read()

    def test_duplicate_gapped_or_unsorted_positions_rejected(self):
        first = ("HR_CORE", "EMPLOYEES", "UQ_CODE", "CODE", 1)
        for rows in ([first, first], [first, first[:-2] + ("EMAIL", 3)],
                     [first[:-2] + ("EMAIL", 2), first], [first, first[:-1] + (2,)]):
            self.rows = rows
            self.configure()
            with self.subTest(rows=rows), self.assertRaises(migrate.TableSafetyMetadataReadError):
                self.read()

    def test_composite_actual_metadata_is_preserved_for_mismatch(self):
        self.rows += [("HR_CORE", "EMPLOYEES", "UQ_CODE", "EMAIL", 2)]
        self.configure()
        self.assertEqual(self.read().constraints[0].columns, ("CODE", "EMAIL"))

    def test_unique_query_execute_and_fetch_errors_are_safe_and_close_cursor(self):
        for stage in ("execute", "fetch"):
            self.configure()
            self.metadata_cursor.reset_mock()
            secret = "secret-password-host-raw-sql"
            if stage == "execute":
                self.metadata_cursor.execute.side_effect = [None] * 4 + [MockOracleError(secret)]
            else:
                self.metadata_cursor.execute.side_effect = None
                self.metadata_cursor.fetchall.side_effect = list(self.metadata_cursor.fetchall.side_effect)[:4] + [MockOracleError(secret)]
            with self.subTest(stage=stage), self.assertRaises(migrate.TableSafetyMetadataReadError) as caught:
                self.read()
            self.assertNotIn(secret, str(caught.exception))
            self.metadata_cursor.__exit__.assert_called_once()
            self.connection.commit.assert_not_called()
            self.connection.rollback.assert_not_called()
            self.connection.close.assert_not_called()
            self.driver.connect.assert_not_called()


class UniquePrecheckTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.definition = migrate.validate_create_table(
            "CREATE TABLE employees (code NUMBER, email NUMBER, "
            "CONSTRAINT uq_code UNIQUE(code), CONSTRAINT uq_email UNIQUE(email))")
        self.connection = Mock()

    def test_unique_constraint_names_prechecked_with_owner_binds(self):
        with patch.object(migrate, "_read_precheck_count", return_value=0) as read:
            migrate.check_unique_constraint_names_available(self.connection, self.definition)
        self.assertEqual(read.call_count, 2)
        self.assertEqual([c.args[2] for c in read.call_args_list], [
            {"owner": "HR_CORE", "constraint_name": "UQ_CODE"},
            {"owner": "HR_CORE", "constraint_name": "UQ_EMAIL"}])
        for item in read.call_args_list:
            self.assertIs(item.args[0], self.connection)
            self.assertIn("ALL_CONSTRAINTS", item.args[1])
            self.assertNotIn("UQ_CODE", item.args[1])

    def test_precondition_order_includes_unique_after_object_and_pk(self):
        events = []
        with patch.object(migrate, "check_target_object_available", side_effect=lambda *a: events.append("object")), \
             patch.object(migrate, "check_primary_key_name_available", side_effect=lambda *a: events.append("pk")), \
             patch.object(migrate, "check_unique_constraint_names_available", side_effect=lambda *a: events.append("unique")):
            migrate.validate_create_table_preconditions(self.connection, self.definition)
        self.assertEqual(events, ["object", "pk", "unique"])

    def test_no_unique_requires_no_query(self):
        definition = migrate.validate_create_table("CREATE TABLE employees (id NUMBER, PRIMARY KEY(id))")
        with patch.object(migrate, "_read_precheck_count") as read:
            migrate.check_unique_constraint_names_available(self.connection, definition)
        read.assert_not_called()

    def test_precheck_read_failure_is_not_assumed_available(self):
        with patch.object(migrate, "_read_precheck_count", side_effect=migrate.MetadataPrecheckError("Güvenli hata")) as read:
            with self.assertRaises(migrate.MetadataPrecheckError):
                migrate.check_unique_constraint_names_available(self.connection, self.definition)
        read.assert_called_once()

    def test_collision_stops_without_retry(self):
        with patch.object(migrate, "_read_precheck_count", return_value=1) as read:
            with self.assertRaises(migrate.MetadataPrecheckError):
                migrate.check_unique_constraint_names_available(self.connection, self.definition)
        read.assert_called_once()

    def test_tampered_unique_model_is_rejected_before_query(self):
        bad = replace(self.definition, unique_constraints=())
        with patch.object(migrate, "_read_precheck_count") as read:
            with self.assertRaises(migrate.MetadataPrecheckError):
                migrate.check_unique_constraint_names_available(self.connection, bad)
        read.assert_not_called()


# Yalnız bellek içi tasarım örnekleri; migration dosyası veya canlı fixture değildir.
DEPARTMENT_SQL = """CREATE TABLE DEPARTMENT (
ID NUMBER(19,0) GENERATED ALWAYS AS IDENTITY,
DEPARTMENT_CODE VARCHAR2(30 CHAR) NOT NULL,
DEPARTMENT_NAME VARCHAR2(150 CHAR) NOT NULL,
IS_ACTIVE NUMBER(1,0) DEFAULT 1 NOT NULL,
CREATED_AT TIMESTAMP(6) WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
CONSTRAINT PK_DEPARTMENT PRIMARY KEY (ID),
CONSTRAINT UQ_DEPARTMENT_CODE UNIQUE (DEPARTMENT_CODE),
CONSTRAINT CK_DEPARTMENT_ACTIVE CHECK (IS_ACTIVE IN (0,1))
);"""
EMPLOYEE_SQL = """CREATE TABLE EMPLOYEE (
ID NUMBER(19,0) GENERATED ALWAYS AS IDENTITY,
EMPLOYEE_NO VARCHAR2(30 CHAR) NOT NULL,
FIRST_NAME VARCHAR2(100 CHAR) NOT NULL,
LAST_NAME VARCHAR2(100 CHAR) NOT NULL,
EMAIL VARCHAR2(254 CHAR),
HIRE_DATE DATE NOT NULL,
DEPARTMENT_ID NUMBER(19,0) NOT NULL,
IS_ACTIVE NUMBER(1,0) DEFAULT 1 NOT NULL,
CREATED_AT TIMESTAMP(6) WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
CONSTRAINT PK_EMPLOYEE PRIMARY KEY (ID),
CONSTRAINT UQ_EMPLOYEE_NO UNIQUE (EMPLOYEE_NO),
CONSTRAINT FK_EMPLOYEE_DEPARTMENT FOREIGN KEY (DEPARTMENT_ID) REFERENCES DEPARTMENT (ID),
CONSTRAINT CK_EMPLOYEE_ACTIVE CHECK (IS_ACTIVE IN (0,1))
);"""


def hrms_snapshot(expected):
    """Sentetik sözleşme örneği; gerçek Oracle metadata davranışı iddiası taşımaz."""
    columns, properties, constraints = [], [], []
    pk_columns = expected.primary_key.columns if expected.primary_key else ()
    for index, column in enumerate(expected.column_definitions, 1):
        data_type = column.data_type
        if data_type == "TIMESTAMP":
            data_type = "TIMESTAMP(6)" + (" WITH TIME ZONE" if column.with_time_zone else "")
        mandatory = column.explicit_not_null or column.identity or column.name in pk_columns
        columns.append(migrate.OracleColumnMetadata(column.name, index, data_type,
            column.number_precision, 6 if column.data_type == "TIMESTAMP" else column.number_scale,
            column.char_length or 0, "C" if column.char_semantics else None, "N" if mandatory else "Y"))
        expression = column.default
        sequence = None
        if column.identity:
            sequence = "ISEQ$$_42"
            expression = '\"HR_CORE\".\"ISEQ$$_42\".nextval'
        properties.append(migrate.OracleColumnProperties(column.name, index,
            len(expression) if expression else None, "NO", "NO", "YES" if column.identity else "NO", "YES",
            expression, "NO" if expression else None, "ALWAYS" if column.identity else None, sequence))
        if column.explicit_not_null or column.identity:
            constraints.append(migrate.OracleConstraintMetadata(f"SYS_NN_{index}", "C", "ENABLED", "VALIDATED",
                "NOT DEFERRABLE", "IMMEDIATE", f'"{column.name}" IS NOT NULL'))
    pk = None
    if expected.primary_key:
        pk = migrate.OraclePrimaryKeyMetadata("HR_CORE", expected.table_name,
            expected.primary_key.constraint_name or "SYS_PK_42", expected.primary_key.columns)
        constraints.append(migrate.OracleConstraintMetadata(pk.constraint_name, "P", "ENABLED", "VALIDATED",
            "NOT DEFERRABLE", "IMMEDIATE", None))
    for unique in expected.unique_constraints:
        constraints.append(migrate.OracleConstraintMetadata(unique.constraint_name, "U", "ENABLED", "VALIDATED",
            "NOT DEFERRABLE", "IMMEDIATE", None, (unique.column_name,)))
    for check in expected.active_checks:
        constraints.append(migrate.OracleConstraintMetadata(check.constraint_name, "C", "ENABLED", "VALIDATED",
            "NOT DEFERRABLE", "IMMEDIATE", '"IS_ACTIVE" IN (0,1)'))
    for fk in expected.foreign_keys:
        constraints.append(migrate.OracleConstraintMetadata(fk.constraint_name, "R", "ENABLED", "VALIDATED",
            "NOT DEFERRABLE", "IMMEDIATE", None, (fk.column_name,),
            migrate.OracleForeignKeyReference("HR_CORE", fk.referenced_table, "PK_" + fk.referenced_table,
                "P", (fk.referenced_column,), "NO ACTION", "ENABLED", "VALIDATED")))
    actual = migrate.TableColumnMetadata("HR_CORE", expected.table_name, tuple(columns))
    safety = migrate.TableSafetyMetadata("HR_CORE", expected.table_name, (("TABLE", "VALID"),),
        "N", None, "NO", "N", tuple(properties), tuple(constraints))
    return actual, pk, safety


class HrmsGrammarTests(OfflineTestCase):
    def test_department_model_has_identity_defaults_check_and_unique(self):
        model = migrate.validate_create_table(DEPARTMENT_SQL)
        self.assertTrue(model.column_definitions[0].identity)
        self.assertFalse(model.column_definitions[0].explicit_not_null)
        self.assertEqual(model.column_definitions[3].default, "1")
        self.assertEqual(model.column_definitions[4].default, "SYSTIMESTAMP")
        self.assertEqual(model.active_checks, (migrate.ActiveCheckDefinition("CK_DEPARTMENT_ACTIVE"),))
        self.assertEqual(len(model.unique_constraints), 1)

    def test_employee_model_has_single_column_fk_and_nullable_email(self):
        model = migrate.validate_create_table(EMPLOYEE_SQL)
        self.assertEqual(model.foreign_keys, (migrate.ForeignKeyDefinition(
            "FK_EMPLOYEE_DEPARTMENT", "DEPARTMENT_ID", "DEPARTMENT", "ID"),))
        self.assertFalse(model.column_definitions[4].explicit_not_null)
        self.assertEqual(len(model.columns), 9)

    def test_explicit_hr_core_reference_normalizes_to_same_target(self):
        model = migrate.validate_create_table(EMPLOYEE_SQL.replace("REFERENCES DEPARTMENT", "REFERENCES HR_CORE.DEPARTMENT"))
        self.assertEqual(model.foreign_keys, migrate.validate_create_table(EMPLOYEE_SQL).foreign_keys)

    def test_new_models_are_frozen_and_tuple_based(self):
        model = migrate.validate_create_table(EMPLOYEE_SQL)
        self.assertIs(type(model.foreign_keys), tuple)
        self.assertIs(type(model.active_checks), tuple)
        for obj, attribute, value in ((model.foreign_keys[0], "referenced_table", "OTHER"),
                                      (model.active_checks[0], "column_name", "OTHER"),
                                      (model.column_definitions[0], "identity", False)):
            with self.subTest(attribute=attribute), self.assertRaises(FrozenInstanceError):
                setattr(obj, attribute, value)

    def test_foreign_key_options_and_composite_rejected(self):
        original = "FOREIGN KEY (DEPARTMENT_ID) REFERENCES DEPARTMENT (ID)"
        for replacement in ("FOREIGN KEY (DEPARTMENT_ID,ID) REFERENCES DEPARTMENT (ID,ID)",
                            "FOREIGN KEY (DEPARTMENT_ID) REFERENCES DEPARTMENT (ID,DEPARTMENT_CODE)",
                            original + " ON DELETE CASCADE", original + " DEFERRABLE", original + " DISABLE",
                            "FOREIGN KEY (DEPARTMENT_ID) REFERENCES OTHER.DEPARTMENT (ID)",
                            "FOREIGN KEY (DEPARTMENT_ID) REFERENCES DEPARTMENT",
                            "FOREIGN KEY (MISSING) REFERENCES DEPARTMENT (ID)",
                            "FOREIGN KEY (DEPARTMENT_ID) REFERENCES SCHEMA_MIGRATIONS (VERSION)"):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                migrate.validate_create_table(EMPLOYEE_SQL.replace(original, replacement))

    def test_inline_and_unnamed_foreign_key_rejected(self):
        for sql in ("CREATE TABLE t (id NUMBER REFERENCES DEPARTMENT(ID))",
                    "CREATE TABLE t (id NUMBER, FOREIGN KEY(id) REFERENCES DEPARTMENT(ID))"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                migrate.validate_create_table(sql)

    def test_duplicate_fk_column_and_constraint_name_rejected(self):
        original = "CONSTRAINT FK_EMPLOYEE_DEPARTMENT FOREIGN KEY (DEPARTMENT_ID) REFERENCES DEPARTMENT (ID)"
        for extra in (original.replace("FK_EMPLOYEE_DEPARTMENT", "FK_OTHER"),
                      original.replace("DEPARTMENT_ID", "ID")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                migrate.validate_create_table(EMPLOYEE_SQL.replace(original, original + ", " + extra))

    def test_check_only_fixed_named_numeric_active_domain(self):
        original = "CHECK (IS_ACTIVE IN (0,1))"
        for check in ("CHECK (IS_ACTIVE IN (1,0))", "CHECK (IS_ACTIVE IN (0,2))",
                      "CHECK (IS_ACTIVE = 1)", "CHECK (ID IN (0,1))", "CHECK (IS_ACTIVE IN ('0','1'))",
                      "CHECK (IS_ACTIVE IN (0,1) OR 1=1)", original + " NOVALIDATE"):
            with self.subTest(check=check), self.assertRaises(ValueError):
                migrate.validate_create_table(DEPARTMENT_SQL.replace(original, check))

    def test_unnamed_duplicate_and_wrong_type_check_rejected(self):
        for sql in (DEPARTMENT_SQL.replace("CONSTRAINT CK_DEPARTMENT_ACTIVE ", ""),
                    DEPARTMENT_SQL.replace("NUMBER(1,0)", "NUMBER(2,0)"),
                    DEPARTMENT_SQL.replace("CONSTRAINT CK_DEPARTMENT_ACTIVE CHECK (IS_ACTIVE IN (0,1))",
                        "CONSTRAINT CK_DEPARTMENT_ACTIVE CHECK (IS_ACTIVE IN (0,1)), CONSTRAINT CK_OTHER CHECK (IS_ACTIVE IN (0,1))")):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                migrate.validate_create_table(sql)

    def test_default_restricted_to_approved_column_type_and_value(self):
        for original, replacement in (("DEFAULT 1", "DEFAULT 0"), ("DEFAULT 1", "DEFAULT (1)"),
                                      ("DEFAULT 1", "DEFAULT ON NULL 1"), ("DEFAULT 1", "DEFAULT '1'"),
                                      ("DEFAULT SYSTIMESTAMP", "DEFAULT SYSDATE"),
                                      ("NUMBER(1,0)", "NUMBER(2,0)"),
                                      ("IS_ACTIVE NUMBER", "FLAG NUMBER"),
                                      ("CREATED_AT TIMESTAMP", "OTHER_AT TIMESTAMP"),
                                      ("TIMESTAMP(6) WITH TIME ZONE", "TIMESTAMP(6)")):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                migrate.validate_create_table(DEPARTMENT_SQL.replace(original, replacement))

    def test_identity_only_id_number19_always_without_options(self):
        for original, replacement in (("ID NUMBER(19,0)", "OTHER NUMBER(19,0)"),
                                      ("ID NUMBER(19,0)", "ID NUMBER(18,0)"),
                                      ("ID NUMBER(19,0)", "ID NUMBER"),
                                      ("GENERATED ALWAYS", "GENERATED BY DEFAULT"),
                                      ("AS IDENTITY", "AS IDENTITY (START WITH 1)"),
                                      ("AS IDENTITY", "AS IDENTITY DEFAULT 1")):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                migrate.validate_create_table(DEPARTMENT_SQL.replace(original, replacement))

    def test_named_constraints_share_one_namespace(self):
        for name in ("PK_EMPLOYEE", "UQ_EMPLOYEE_NO", "CK_EMPLOYEE_ACTIVE"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                migrate.validate_create_table(EMPLOYEE_SQL.replace("FK_EMPLOYEE_DEPARTMENT", name))

    def test_additional_constraint_names_are_prechecked_without_transactions(self):
        model = migrate.validate_create_table(EMPLOYEE_SQL)
        connection = Mock()
        with patch.object(migrate, "_read_precheck_count", return_value=0) as read:
            migrate.check_unique_constraint_names_available(connection, model)
        self.assertEqual([c.args[2]["constraint_name"] for c in read.call_args_list],
                         ["UQ_EMPLOYEE_NO", "FK_EMPLOYEE_DEPARTMENT", "CK_EMPLOYEE_ACTIVE"])
        for invocation in read.call_args_list:
            self.assertIs(invocation.args[0], connection)
            self.assertEqual(invocation.args[2]["owner"], "HR_CORE")
        connection.commit.assert_not_called()
        connection.rollback.assert_not_called()

    def test_new_grammar_keeps_single_statement_and_literal_boundary(self):
        for sql in (DEPARTMENT_SQL + " CREATE TABLE other (id NUMBER);",
                    DEPARTMENT_SQL.replace("DEFAULT 1", "DEFAULT 1+0"),
                    EMPLOYEE_SQL.replace("REFERENCES DEPARTMENT", 'REFERENCES "DEPARTMENT"')):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                migrate.validate_create_table(sql)


class HrmsMetadataComparisonTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.expected = migrate.validate_create_table(EMPLOYEE_SQL)
        self.actual, self.pk, self.safety = hrms_snapshot(self.expected)

    def compare(self, safety=None, expected=None):
        return migrate.compare_table_structure(expected or self.expected, self.actual, self.pk,
                                               safety=safety if safety is not None else self.safety)

    def mutate_column(self, name, **changes):
        return replace(self.safety, column_properties=tuple(
            replace(c, **changes) if c.column_name == name else c for c in self.safety.column_properties))

    def mutate_constraint(self, name, **changes):
        return replace(self.safety, constraints=tuple(
            replace(c, **changes) if c.constraint_name == name else c for c in self.safety.constraints))

    def test_complete_employee_and_department_snapshots_match(self):
        self.assertTrue(self.compare().matched)
        expected = migrate.validate_create_table(DEPARTMENT_SQL)
        actual, pk, safety = hrms_snapshot(expected)
        self.assertTrue(migrate.compare_table_structure(expected, actual, pk, safety=safety).matched)

    def test_wrong_defaults_mismatch(self):
        for name, value in (("IS_ACTIVE", "0"), ("IS_ACTIVE", "1+0"),
                            ("CREATED_AT", "SYSDATE"), ("CREATED_AT", "SYSTIMESTAMP AT TIME ZONE 'UTC'")):
            with self.subTest(name=name, value=value):
                self.assertIs(self.compare(self.mutate_column(name, default_expression=value)).status,
                              migrate.StructureComparisonStatus.MISMATCH)

    def test_missing_default_is_mismatch_but_partial_evidence_invalid(self):
        absent = self.mutate_column("IS_ACTIVE", default_length=None, default_expression=None, default_on_null=None)
        self.assertIs(self.compare(absent).status, migrate.StructureComparisonStatus.MISMATCH)
        self.assertIs(self.compare(self.mutate_column("IS_ACTIVE", default_expression=None)).status,
                      migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_unexpected_default_and_on_null_are_rejected(self):
        self.assertFalse(self.compare(self.mutate_column("EMAIL", default_length=1,
            default_expression="1", default_on_null="NO")).matched)
        self.assertFalse(self.compare(self.mutate_column("IS_ACTIVE", default_on_null="YES")).matched)

    def test_default_keyword_case_and_whitespace_normalization_is_narrow(self):
        self.assertTrue(self.compare(self.mutate_column("CREATED_AT", default_expression=" systimestamp ")).matched)
        self.assertTrue(self.compare(self.mutate_column("IS_ACTIVE", default_expression=" 1 ")).matched)

    def test_identity_generation_and_sequence_mismatch(self):
        for changes in ({"identity_generation": "BY DEFAULT"}, {"identity_sequence": "ISEQ$$_OTHER"},
                        {"default_expression": '\"OTHER\".\"ISEQ$$_42\".nextval'},
                        {"default_expression": '\"HR_CORE\".\"ISEQ$$_42\".nextval+1'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.compare(self.mutate_column("ID", **changes)).matched)

    def test_identity_sequence_default_conflict_requires_manual_review_not_failed(self):
        for changes in ({"identity_sequence": "ISEQ$$_OTHER"},
                        {"default_expression": '\"OTHER\".\"ISEQ$$_42\".nextval'},
                        {"default_expression": '\"HR_CORE\".\"ISEQ$$_42\".nextval+1'}):
            with self.subTest(changes=changes):
                result = self.compare(self.mutate_column("ID", **changes))
                self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_METADATA)
                decision = migrate.classify_execution_outcome(migrate.DDLOutcome.SUCCEEDED,
                    metadata=result, history_state=migrate.HistoryState.RUNNING_CONFIRMED)
                self.assertFalse(decision.can_mark_success)
                self.assertFalse(decision.can_mark_failed)
                self.assertTrue(decision.manual_review_required)

    def test_identity_partial_evidence_is_invalid(self):
        for changes in ({"identity_generation": None}, {"identity_sequence": None}, {"default_length": None}):
            with self.subTest(changes=changes):
                self.assertIs(self.compare(self.mutate_column("ID", **changes)).status,
                              migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_identity_not_null_is_implicit_without_setting_explicit_flag(self):
        self.assertFalse(self.expected.column_definitions[0].explicit_not_null)
        actual = replace(self.actual, columns=(replace(self.actual.columns[0], nullable="Y"),) + self.actual.columns[1:])
        self.assertFalse(migrate.compare_table_structure(self.expected, actual, self.pk, safety=self.safety).matched)

    def test_missing_extra_and_wrong_check_are_mismatches(self):
        name = "CK_EMPLOYEE_ACTIVE"
        for safety in (replace(self.safety, constraints=tuple(c for c in self.safety.constraints if c.constraint_name != name)),
                       self.mutate_constraint(name, condition='"IS_ACTIVE" IN (0,2)'),
                       self.mutate_constraint(name, constraint_name="CK_OTHER")):
            with self.subTest(safety=safety):
                self.assertIs(self.compare(safety).status, migrate.StructureComparisonStatus.MISMATCH)

    def test_active_check_normalizer_rejects_expression_and_identifier_injection(self):
        for text in ('"is_active" IN (0,1)', 'IS_ACTIVE IN (0,1) OR 1=1', "IS_ACTIVE IN ('0','1')",
                     'IS_ACTIVE IN (0,1);', 'IS_ACTIVE IN (0,1) /* x */', '((IS_ACTIVE IN (0,1))',
                     'IS_ACTIVE IN (1,0)', 'IS_ACTIVE IN (0,1))'):
            with self.subTest(text=text):
                self.assertFalse(migrate._is_active_check(text))
        for text in ('"IS_ACTIVE" in (0,1)', ' ((IS_ACTIVE IN (0,1))) '):
            self.assertTrue(migrate._is_active_check(text))

    def test_fk_missing_extra_wrong_name_and_local_column(self):
        name = "FK_EMPLOYEE_DEPARTMENT"
        for safety in (replace(self.safety, constraints=tuple(c for c in self.safety.constraints if c.constraint_name != name)),
                       self.mutate_constraint(name, constraint_name="FK_OTHER"),
                       self.mutate_constraint(name, columns=("ID",))):
            with self.subTest(safety=safety):
                self.assertIs(self.compare(safety).status, migrate.StructureComparisonStatus.MISMATCH)

    def test_fk_target_table_column_type_and_delete_policy(self):
        fk = next(c for c in self.safety.constraints if c.constraint_type == "R")
        for changes in ({"table_name": "OTHER"}, {"columns": ("OTHER",)}, {"delete_rule": "CASCADE"},
                        {"status": "DISABLED"}, {"validated": "NOT VALIDATED"}):
            with self.subTest(changes=changes):
                self.assertIs(self.compare(self.mutate_constraint(fk.constraint_name,
                    reference=replace(fk.reference, **changes))).status, migrate.StructureComparisonStatus.MISMATCH)
        for changes in ({"owner": "OTHER"}, {"constraint_type": "C"}, {"columns": ()}):
            self.assertIs(self.compare(self.mutate_constraint(fk.constraint_name,
                reference=replace(fk.reference, **changes))).status, migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_fk_may_reference_unique_key_without_confusing_index(self):
        fk = next(c for c in self.safety.constraints if c.constraint_type == "R")
        self.assertTrue(self.compare(self.mutate_constraint(fk.constraint_name,
            reference=replace(fk.reference, constraint_type="U", constraint_name="UQ_DEPARTMENT_ID"))).matched)

    def test_missing_fk_reference_is_invalid_not_known_ddl_failure(self):
        result = self.compare(self.mutate_constraint("FK_EMPLOYEE_DEPARTMENT", reference=None))
        self.assertIs(result.status, migrate.StructureComparisonStatus.INVALID_METADATA)
        decision = migrate.classify_execution_outcome(migrate.DDLOutcome.SUCCEEDED, metadata=result,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED)
        self.assertFalse(decision.can_mark_failed)
        self.assertFalse(decision.can_mark_success)
        self.assertTrue(decision.manual_review_required)

    def test_known_structure_mismatch_retains_existing_failed_policy(self):
        result = self.compare(self.mutate_constraint("CK_EMPLOYEE_ACTIVE", condition="IS_ACTIVE IN (0,2)"))
        decision = migrate.classify_execution_outcome(migrate.DDLOutcome.SUCCEEDED, metadata=result,
            history_state=migrate.HistoryState.RUNNING_CONFIRMED)
        self.assertEqual(decision.failed_reason, "POST_VALIDATION_FAILED")
        self.assertTrue(decision.manual_review_required)

    def test_tampered_new_expected_fields_fail_closed(self):
        for expected in (replace(self.expected, foreign_keys=()), replace(self.expected, active_checks=()),
                         replace(self.expected, foreign_keys=list(self.expected.foreign_keys)),
                         replace(self.expected, active_checks=(migrate.ActiveCheckDefinition("OTHER"),))):
            with self.subTest(expected=expected):
                self.assertIs(self.compare(expected=expected).status, migrate.StructureComparisonStatus.INVALID_EXPECTATION)

    def test_additional_fk_constraint_cannot_match(self):
        fk = next(c for c in self.safety.constraints if c.constraint_type == "R")
        extra = replace(fk, constraint_name="FK_EXTRA")
        self.assertFalse(self.compare(replace(self.safety, constraints=self.safety.constraints + (extra,))).matched)

    def test_duplicate_constraint_and_partial_reference_are_invalid(self):
        fk = next(c for c in self.safety.constraints if c.constraint_type == "R")
        for safety in (replace(self.safety, constraints=self.safety.constraints + (fk,)),
                       self.mutate_constraint(fk.constraint_name, reference=replace(fk.reference, columns=("ID", "OTHER")))):
            self.assertIs(self.compare(safety).status, migrate.StructureComparisonStatus.INVALID_METADATA)

    def test_identity_without_pk_still_requires_nonnullable_column(self):
        expected = migrate.validate_create_table("CREATE TABLE t (id NUMBER(19,0) GENERATED ALWAYS AS IDENTITY)")
        actual, pk, safety = hrms_snapshot(expected)
        self.assertTrue(migrate.compare_table_structure(expected, actual, pk, safety=safety).matched)
        actual = replace(actual, columns=(replace(actual.columns[0], nullable="Y"),))
        self.assertFalse(migrate.compare_table_structure(expected, actual, pk, safety=safety).matched)

    def test_sensitive_metadata_is_excluded_from_repr(self):
        marker = "SENSITIVE_EXPRESSION"
        self.assertNotIn(marker, repr(self.mutate_column("IS_ACTIVE", default_expression=marker)))
        self.assertNotIn(marker, repr(self.mutate_constraint("CK_EMPLOYEE_ACTIVE", condition=marker)))


class HrmsExtendedReaderTests(ConnectionHelperTestCase):
    def setUp(self):
        super().setUp()
        self.expected = migrate.validate_create_table(EMPLOYEE_SQL)
        self.actual, self.pk, self.safety = hrms_snapshot(self.expected)
        self.base = MagicMock()
        self.base.__enter__.return_value = self.base
        self.fk_cursor = MagicMock()
        self.fk_cursor.__enter__.return_value = self.fk_cursor
        self.values_cursor = MagicMock()
        self.values_cursor.__enter__.return_value = self.values_cursor
        self.fk_rows = [("HR_CORE", "EMPLOYEE", "FK_EMPLOYEE_DEPARTMENT", "DEPARTMENT_ID", 1,
            "HR_CORE", "DEPARTMENT", "PK_DEPARTMENT", "P", "ID", 1, "NO ACTION", "ENABLED", "VALIDATED", 1)]
        self.configure()

    def configure(self):
        self.connection.cursor.side_effect = [self.cursor, self.base, self.fk_cursor, self.values_cursor]
        properties = self.safety.column_properties
        constraints = self.safety.constraints
        self.base.fetchall.side_effect = [list(self.safety.object_types),
            [("N", None, "NO", "N")],
            [(p.column_name, p.column_id, p.default_length, p.hidden, p.virtual, p.identity, p.user_generated) for p in properties],
            [(c.constraint_name, c.constraint_type, c.status, c.validated, c.deferrable, c.deferred, c.condition) for c in constraints],
            [("HR_CORE", "EMPLOYEE", "UQ_EMPLOYEE_NO", "EMPLOYEE_NO", 1)]]
        self.fk_cursor.fetchall.return_value = self.fk_rows
        self.values_cursor.fetchall.side_effect = [
            [(p.column_name, p.default_expression, p.default_on_null) for p in properties if p.default_length is not None],
            [(p.column_name, p.identity_generation, p.identity_sequence) for p in properties if p.identity == "YES"]]

    def read(self):
        return migrate.read_table_safety_metadata(self.connection, "EMPLOYEE")

    def test_real_readers_and_comparator_work_together_with_synthetic_metadata(self):
        safety = self.read()
        result = migrate.compare_table_structure(self.expected, self.actual, self.pk, safety=safety)
        self.assertTrue(result.matched)
        self.driver.connect.assert_not_called()
        self.loader.assert_not_called()
        for method in (self.connection.commit, self.connection.rollback, self.connection.close):
            method.assert_not_called()
        for cursor in (self.base, self.fk_cursor, self.values_cursor):
            cursor.__exit__.assert_called_once_with(None, None, None)

    def test_extended_queries_are_select_bound_and_schema_limited(self):
        self.read()
        for cursor in (self.fk_cursor, self.values_cursor):
            for invocation in cursor.execute.call_args_list:
                query, params = invocation.args
                self.assertTrue(query.startswith("SELECT "))
                self.assertEqual(params, {"owner": "HR_CORE", "table_name": "EMPLOYEE"})
                self.assertNotIn("EMPLOYEE", query)
        fk_query = self.fk_cursor.execute.call_args.args[0]
        for fragment in ("ALL_CONSTRAINTS", "ALL_CONS_COLUMNS", "r.OWNER=:owner", "rc.POSITION=c.POSITION"):
            self.assertIn(fragment, fk_query)
        self.assertIn("DATA_DEFAULT", self.values_cursor.execute.call_args_list[0].args[0])
        self.assertIn("ALL_TAB_IDENTITY_COLS", self.values_cursor.execute.call_args_list[1].args[0])

    def test_absent_fk_details_are_not_absent_fk(self):
        self.fk_rows = []
        self.configure()
        with self.assertRaises(migrate.TableSafetyMetadataReadError):
            self.read()
        self.values_cursor.execute.assert_not_called()

    def test_malformed_fk_rows_scopes_positions_and_references_fail_closed(self):
        original = self.fk_rows[0]
        cases = [(), original + ("EXTRA",), None]
        for index, value in ((0, "OTHER"), (1, "OTHER"), (2, "OTHER"), (4, True), (4, 2),
                             (5, "OTHER"), (8, "R"), (10, True), (10, 2), (11, "UNKNOWN"),
                             (14, True), (14, 0), (14, 2)):
            row = list(original)
            row[index] = value
            cases.append(tuple(row))
        for row in cases:
            self.fk_rows = [row]
            self.configure()
            with self.subTest(row=row), self.assertRaises(migrate.TableSafetyMetadataReadError):
                self.read()

    def test_duplicate_fk_rows_are_rejected(self):
        self.fk_rows *= 2
        self.configure()
        with self.assertRaises(migrate.TableSafetyMetadataReadError):
            self.read()

    def test_missing_extra_and_non_string_defaults_fail_closed(self):
        valid = [(p.column_name, p.default_expression, p.default_on_null)
                 for p in self.safety.column_properties if p.default_length is not None]
        class Unreadable:
            def __str__(self):
                raise AssertionError("Driver değeri metne çevrilmemeli")
            def read(self):
                raise AssertionError("LOB okunmamalı")
        for rows in ([], valid[:-1], valid + [("OTHER", "1", "NO")],
                     [("ID", Unreadable(), "NO")], [("ID", None, "NO")], [("ID", "1", "UNKNOWN")],
                     [("ID", "1")], valid + [valid[0]]):
            self.configure()
            self.values_cursor.fetchall.side_effect = [rows, []]
            with self.subTest(row_count=len(rows)), self.assertRaises(migrate.TableSafetyMetadataReadError):
                self.read()

    def test_missing_wrong_and_duplicate_identity_rows_fail_closed(self):
        default_rows = [(p.column_name, p.default_expression, p.default_on_null)
                        for p in self.safety.column_properties if p.default_length is not None]
        for rows in ([], [("OTHER", "ALWAYS", "ISEQ$$_42")], [("ID", "UNKNOWN", "ISEQ$$_42")],
                     [("ID", "ALWAYS", None)], [("ID", "ALWAYS", "bad.sequence")],
                     [("ID", "ALWAYS")], [("ID", "ALWAYS", "ISEQ$$_42")] * 2):
            self.configure()
            self.values_cursor.fetchall.side_effect = [default_rows, rows]
            with self.subTest(rows=rows), self.assertRaises(migrate.TableSafetyMetadataReadError):
                self.read()

    def test_extended_execute_fetch_errors_are_sanitized_and_close_cursors(self):
        secret = "RAW_SQL_PASSWORD_DSN"
        for target, stage in ((self.fk_cursor, "execute"), (self.fk_cursor, "fetch"),
                              (self.values_cursor, "execute"), (self.values_cursor, "fetch")):
            self.configure()
            self.fk_cursor.execute.side_effect = None
            self.values_cursor.execute.side_effect = None
            target.reset_mock()
            if stage == "execute":
                target.execute.side_effect = MockOracleError(secret)
            else:
                target.fetchall.side_effect = MockOracleError(secret)
            with self.subTest(stage=stage), self.assertRaises(migrate.TableSafetyMetadataReadError) as error:
                self.read()
            self.assertNotIn(secret, str(error.exception))
            target.__exit__.assert_called_once()
            self.connection.commit.assert_not_called()
            self.connection.rollback.assert_not_called()
            self.connection.close.assert_not_called()
            self.driver.connect.assert_not_called()
            self.fk_cursor.fetchall.side_effect = None

    def test_wrong_session_never_enters_extended_readers(self):
        self.cursor.fetchone.return_value = ("HR_CORE", "FREEPDB1", "OTHER")
        with self.assertRaises(migrate.TableSafetyMetadataReadError):
            self.read()
        for cursor in (self.base, self.fk_cursor, self.values_cursor):
            cursor.execute.assert_not_called()


class HrmsMigrationFileTests(OfflineTestCase):
    """Repository SQL dosyalarını oku; Oracle veya executor çağrısı yoktur."""

    def prepared(self, version, filename):
        path = migrate.MIGRATIONS_DIRECTORY / filename
        content = path.read_bytes()
        migration = migrate.Migration(version, path, hashlib.sha256(content).hexdigest())
        prepared = migrate.read_migration_for_apply(migration)
        self.assertEqual(prepared.content, content)
        self.assertEqual(prepared.migration.checksum, hashlib.sha256(prepared.content).hexdigest())
        return prepared

    def test_department_file_matches_approved_model_and_synthetic_metadata(self):
        prepared = self.prepared(2, "V0002__create_department.sql")
        self.assertEqual(prepared.definition, migrate.validate_create_table(DEPARTMENT_SQL))
        actual, pk, safety = hrms_snapshot(prepared.definition)
        self.assertTrue(migrate.compare_table_structure(prepared.definition, actual, pk, safety=safety).matched)

    def test_employee_file_matches_approved_model_and_synthetic_metadata(self):
        prepared = self.prepared(3, "V0003__create_employee.sql")
        self.assertEqual(prepared.definition, migrate.validate_create_table(EMPLOYEE_SQL))
        actual, pk, safety = hrms_snapshot(prepared.definition)
        self.assertTrue(migrate.compare_table_structure(prepared.definition, actual, pk, safety=safety).matched)
        email = next(c for c in prepared.definition.column_definitions if c.name == "EMAIL")
        self.assertFalse(email.explicit_not_null)
        self.assertFalse(email.identity)
        self.assertIsNone(email.default)

    def test_department_precedes_employee_and_constraint_names_do_not_collide(self):
        department = self.prepared(2, "V0002__create_department.sql")
        employee = self.prepared(3, "V0003__create_employee.sql")
        discovered = migrate.collect_migrations(migrate.MIGRATIONS_DIRECTORY)
        versions = {m.path.name: m.version for m in discovered}
        self.assertLess(versions[department.migration.path.name], versions[employee.migration.path.name])
        names = []
        for definition in (department.definition, employee.definition):
            names.append(definition.primary_key_constraint_name)
            names.extend(c.constraint_name for c in (*definition.unique_constraints,
                         *definition.foreign_keys, *definition.active_checks))
        self.assertEqual(len(names), len(set(names)))
        fk, = employee.definition.foreign_keys
        self.assertEqual(fk.referenced_table, department.definition.table_name)
        self.assertEqual((fk.referenced_column,), department.definition.primary_key.columns)


if __name__ == "__main__":
    unittest.main()
