"""Migration karşılaştırmasını sahte verilerle, Oracle ve dosya I/O olmadan test et."""

import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
