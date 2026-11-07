"""Yerel migration dosyalarını doğrula ve listele; veritabanına bağlanmaz."""

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIRECTORY = PROJECT_ROOT / "database" / "migrations"
MIGRATION_NAME = re.compile(r"V([0-9]{4})__([a-z][a-z0-9]*(?:_[a-z0-9]+)*)\.sql")


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path
    checksum: str


def collect_migrations(directory: Path) -> list[Migration]:
    """Dosya adlarını ve versiyonları doğrula; SHA-256 özetlerini hesapla."""
    if not directory.is_dir():
        raise ValueError(f"Migration klasörü bulunamadı: {directory}")

    migrations: list[Migration] = []
    versions: dict[int, str] = {}
    errors: list[str] = []

    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue

        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            errors.append(
                f"Geçersiz migration dosya adı: {path.name}. "
                "Beklenen: VNNNN__description.sql; açıklama küçük İngilizce "
                "harfler, rakamlar ve kelimeler arasında alt çizgi içermelidir."
            )
            continue

        version = int(match.group(1))
        if version in versions:
            errors.append(
                f"Tekrarlanan versiyon V{version:04d}: "
                f"{versions[version]} ve {path.name}"
            )
            continue

        versions[version] = path.name
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        migrations.append(Migration(version=version, path=path, checksum=checksum))

    if errors:
        raise ValueError("\n".join(errors))

    return sorted(migrations, key=lambda migration: migration.version)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Oracle migration dosyalarını yalnızca yerel olarak incele."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="Dosya adlarını, versiyonları ve checksum'ları doğrula.")
    commands.add_parser("plan", help="Dosyaları sayısal versiyon sırasıyla ve checksum'larıyla göster.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        migrations = collect_migrations(MIGRATIONS_DIRECTORY)
    except (OSError, ValueError) as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1

    if not migrations:
        print("Henüz migration dosyası bulunmuyor; bu normal bir durumdur.")
        return 0

    if args.command == "validate":
        print(f"Yerel doğrulama başarılı: {len(migrations)} migration dosyası.")
        print("SHA-256 checksum değerleri hesaplandı; kayıtlı geçmişle karşılaştırılmadı.")
    else:
        print("Yerel migration sırası (veritabanındaki uygulanma durumu bilinmiyor):")
        for migration in migrations:
            print(
                f"V{migration.version:04d} | {migration.path.name} | "
                f"SHA-256: {migration.checksum}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
