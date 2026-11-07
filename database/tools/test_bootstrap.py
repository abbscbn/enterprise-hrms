"""CHECK normalizasyonunu Oracle bağlantısı olmadan unittest ile doğrula."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock


# Doğrudan çalıştırma ve unittest discovery aynı yerel modülü yükler.
# main() çağrılmaz; .env okunmaz ve Oracle bağımlılıkları yüklenmez.
BOOTSTRAP_PATH = Path(__file__).resolve().with_name("bootstrap.py")
SPEC = importlib.util.spec_from_file_location("hrms_bootstrap_under_test", BOOTSTRAP_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError("Yerel bootstrap.py modülü yüklenemedi.")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


EXPECTED_CHECK = "STATUS IN ('RUNNING', 'SUCCESS', 'FAILED')"


class CheckConstraintNormalizationTests(unittest.TestCase):
    """Normalizasyonu ve validate_table içindeki kabul/ret kararını kontrol et."""

    def make_cursor(self, condition: str) -> Mock:
        # Bunlar canlı Oracle sonucu değil, yalnızca geçerli yapıyı temsil eden
        # sabit test verileridir. execute() tamamen mock nesnesi üzerinde çalışır.
        columns = [
            ("VERSION", "NUMBER", 4, 0, 0, None, "N", None, "NO", "NO", "NO"),
            ("SCRIPT_NAME", "VARCHAR2", None, None, 255, "C", "N", None, "NO", "NO", "NO"),
            ("CHECKSUM", "VARCHAR2", None, None, 64, "C", "N", None, "NO", "NO", "NO"),
            ("STATUS", "VARCHAR2", None, None, 7, "C", "N", None, "NO", "NO", "NO"),
            ("STARTED_AT", "TIMESTAMP(6) WITH TIME ZONE", None, 6, 0, None,
             "N", "SYSTIMESTAMP", "NO", "NO", "NO"),
            ("FINISHED_AT", "TIMESTAMP(6) WITH TIME ZONE", None, 6, 0, None,
             "Y", None, "NO", "NO", "NO"),
            ("ERROR_MESSAGE", "CLOB", None, None, 0, None, "Y", None, "NO", "NO", "NO"),
        ]
        constraints = [
            ("PK_SCHEMA_MIGRATIONS", "P", "ENABLED", "VALIDATED", "NOT DEFERRABLE", None),
            ("CK_SCHEMA_MIGRATIONS_STATUS", "C", "ENABLED", "VALIDATED", "NOT DEFERRABLE", condition),
        ]
        for column in ("VERSION", "SCRIPT_NAME", "CHECKSUM", "STATUS", "STARTED_AT"):
            constraints.append(
                (f"NN_{column}", "C", "ENABLED", "VALIDATED", "NOT DEFERRABLE",
                 f'"{column}" IS NOT NULL')
            )
        cursor = Mock(spec=["execute", "fetchall"])
        cursor.fetchall.side_effect = [columns, constraints, [("VERSION",)]]
        return cursor

    def assert_check_accepted(self, expression: str) -> None:
        self.assertEqual(
            bootstrap.normalize_expression(expression),
            bootstrap.normalize_expression(EXPECTED_CHECK),
        )
        self.assertIsNone(bootstrap.validate_table(self.make_cursor(expression)))

    def assert_check_rejected(self, expression: str) -> None:
        # Normalizasyon sözdizimi hatasında ValueError verir; sözdizimi geçerli
        # fakat farklı ifadelerde token'lar eşleşmez. Her iki durumda da
        # validate_table gerçek kabul/ret kararında ifadeyi reddetmelidir.
        try:
            normalized = bootstrap.normalize_expression(expression)
        except ValueError:
            pass
        else:
            self.assertNotEqual(normalized, bootstrap.normalize_expression(EXPECTED_CHECK))
        with self.assertRaisesRegex(ValueError, r"SEARCH_CONDITION_VC"):
            bootstrap.validate_table(self.make_cursor(expression))

    def test_expected_check_is_accepted(self):
        self.assert_check_accepted(EXPECTED_CHECK)

    def test_keyword_case_differences_are_accepted(self):
        for keyword in ("in", "In", "iN"):
            with self.subTest(keyword=keyword):
                self.assert_check_accepted(f"status {keyword} ('RUNNING', 'SUCCESS', 'FAILED')")

    def test_safe_outer_parentheses_are_accepted(self):
        for expression in (
            f"({EXPECTED_CHECK})",
            f"((({EXPECTED_CHECK})))",
            f" \n ( ( {EXPECTED_CHECK} ) ) \t",
        ):
            with self.subTest(expression=expression):
                self.assert_check_accepted(expression)

    def test_uppercase_quoted_identifier_is_equivalent(self):
        self.assert_check_accepted('"STATUS" IN (\'RUNNING\', \'SUCCESS\', \'FAILED\')')

    def test_lowercase_quoted_identifier_is_rejected(self):
        self.assert_check_rejected('"status" IN (\'RUNNING\', \'SUCCESS\', \'FAILED\')')

    def test_lowercase_literal_is_not_equivalent(self):
        self.assert_check_rejected("STATUS IN ('running', 'SUCCESS', 'FAILED')")

    def test_whitespace_inside_literal_is_rejected(self):
        self.assert_check_rejected("STATUS IN ('RUN NING', 'SUCCESS', 'FAILED')")

    def test_unbalanced_parentheses_are_rejected(self):
        for expression in (f"({EXPECTED_CHECK}", f"{EXPECTED_CHECK})", f")({EXPECTED_CHECK}"):
            with self.subTest(expression=expression):
                with self.assertRaises(ValueError):
                    bootstrap.normalize_expression(expression)
                self.assert_check_rejected(expression)

    def test_additional_conditions_are_rejected(self):
        for expression in (
            f"{EXPECTED_CHECK} OR STATUS IS NULL",
            f"({EXPECTED_CHECK}) AND STATUS IS NOT NULL",
            f"({EXPECTED_CHECK}) OR (STATUS IS NULL)",
            f"{EXPECTED_CHECK} OR 1=1",
        ):
            with self.subTest(expression=expression):
                self.assert_check_rejected(expression)

    def test_unexpected_literal_contents_are_rejected(self):
        for literal in (
            "'PENDING'", "'RUNNING '", "'RUN(NING)'", "'RUN\"NING'",
            "'RUN''NING'", "'RUN\nNING'", "''",
        ):
            with self.subTest(literal=literal):
                expression = f"STATUS IN ({literal}, 'SUCCESS', 'FAILED')"
                self.assert_check_rejected(expression)
                self.assertIn(("literal", literal), bootstrap.normalize_expression(expression))


if __name__ == "__main__":
    unittest.main()
