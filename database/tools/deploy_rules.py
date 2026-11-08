"""Tek sürümlü HRMS_RULES deployment; mevcut CREATE TABLE runner'ını değiştirmez.

Her dosya tek Oracle package DDL'sidir; SQL bölme yok. Yeni kurulum yalnız gerçek
TTY + checksum onayıyla. Kısmi kurulumda otomatik retry/replace/repair yok.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
FILES = ('V0001__hrms_rules_spec.sql', 'V0001__hrms_rules_body.sql')


def sources():
    result = []
    root = (ROOT / 'database' / 'plsql').resolve(strict=True)
    for name, prefix in zip(FILES, ('CREATE OR REPLACE PACKAGE HRMS_RULES AS', 'CREATE OR REPLACE PACKAGE BODY HRMS_RULES AS')):
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError('Unsafe source path')
        content = path.read_bytes()
        text = content.decode('utf-8')
        if not text.startswith(prefix + '\n') or not text.rstrip().endswith('END HRMS_RULES;'):
            raise ValueError('Unexpected package source')
        if re.search(r'\b(COMMIT|ROLLBACK|AUTONOMOUS_TRANSACTION|EXECUTE\s+IMMEDIATE|DBMS_\w+)\b', text, re.I):
            raise ValueError('Unsupported deployment content')
        result.append((name, content, text))
    fingerprint = hashlib.sha256(b'\0'.join(name.encode()+b'\0'+raw for name,raw,_ in result)).hexdigest()
    manifest_path = root / 'manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if (manifest.get('version') != 1 or manifest.get('package') != 'HRMS_RULES'
            or manifest.get('sha256') != fingerprint
            or manifest.get('files') != {name: hashlib.sha256(raw).hexdigest() for name,raw,_ in result}):
            raise ValueError('Versioned source manifest mismatch')
    else:
        raise ValueError('Versioned source manifest missing')
    return result, fingerprint


def run():
    artifacts, fingerprint = sources()
    if '--fingerprint' in sys.argv:
        print(fingerprint)
        return 0
    spec = importlib.util.spec_from_file_location('rules_migration_connection', ROOT/'database/tools/migrate.py')
    migration = importlib.util.module_from_spec(spec);sys.modules[spec.name] = migration;spec.loader.exec_module(migration)
    import oracledb
    from dotenv import load_dotenv
    connection = None
    try:
        settings, port = migration.load_connection_settings(load_dotenv)
        connection = migration.open_oracle_connection(settings, port, oracledb)
        migration.validate_target_session(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT OBJECT_TYPE, STATUS FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name AND OBJECT_TYPE IN ('PACKAGE','PACKAGE BODY')", {'owner':'HR_CORE','name':'HRMS_RULES'})
            existing = cursor.fetchall()
            if existing:
                if set(existing) != {('PACKAGE','VALID'),('PACKAGE BODY','VALID')}:
                    raise ValueError('Partial deployment; manual review required')
                for (_, _, text), kind in zip(artifacts, ('PACKAGE','PACKAGE BODY')):
                    cursor.execute('SELECT TEXT FROM ALL_SOURCE WHERE OWNER=:owner AND NAME=:name AND TYPE=:kind ORDER BY LINE', {'owner':'HR_CORE','name':'HRMS_RULES','kind':kind})
                    if ''.join(row[0] for row in cursor.fetchall()).strip() != text.removeprefix('CREATE OR REPLACE ').strip():
                        raise ValueError('Existing source mismatch; no replacement')
                print('HRMS_RULES v1 kaynakları eşleşiyor ve VALID; değişiklik yapılmadı.')
                return 0
        print('Hedef HR_CORE/FREEPDB1; HRMS_RULES v1; SHA-256: '+fingerprint)
        print('İki package DDL implicit commit yapar; DBMS_LOCK kullanılır; kısmi sonuçta tekrar denemeyin.')
        if '--apply' not in sys.argv:
            print('Plan modu; DDL çalıştırılmadı. Uygulama için --apply ve gerçek TTY gerekir.')
            return 0
        if not sys.stdin.isatty():
            raise ValueError('Real TTY required')
        expected = 'DEPLOY HR_CORE/FREEPDB1 HRMS_RULES V0001 '+fingerprint
        answer = input("Onaylamak için tam olarak '"+expected+"' yazın: ")
        if answer != expected or sources()[1] != fingerprint:
            raise ValueError('Approval or content changed')
        with migration.migration_lock(connection):
            with connection.cursor() as cursor:
                # Migration ile aynı advisory kilit; dış DDL oturumlarını engellemez.
                cursor.execute("SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name", {'owner':'HR_CORE','name':'HRMS_RULES'})
                if cursor.fetchone() != (0,):
                    raise ValueError('Object appeared; stop')
                for (_, _, text), kind in zip(artifacts, ('PACKAGE','PACKAGE BODY')):
                    cursor.execute(text)
                    cursor.execute("SELECT COUNT(*) FROM ALL_ERRORS WHERE OWNER=:owner AND NAME=:name AND TYPE=:kind AND ATTRIBUTE='ERROR'", {'owner':'HR_CORE','name':'HRMS_RULES','kind':kind})
                    if cursor.fetchone() != (0,):
                        raise ValueError('Compilation errors; manual review required')
                cursor.execute("SELECT OBJECT_TYPE, STATUS FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name AND OBJECT_TYPE IN ('PACKAGE','PACKAGE BODY')", {'owner':'HR_CORE','name':'HRMS_RULES'})
                if set(cursor.fetchall()) != {('PACKAGE','VALID'),('PACKAGE BODY','VALID')}:
                    raise ValueError('Not valid')
                for (_, _, text), kind in zip(artifacts, ('PACKAGE','PACKAGE BODY')):
                    cursor.execute('SELECT TEXT FROM ALL_SOURCE WHERE OWNER=:owner AND NAME=:name AND TYPE=:kind ORDER BY LINE', {'owner':'HR_CORE','name':'HRMS_RULES','kind':kind})
                    if ''.join(row[0] for row in cursor.fetchall()).strip() != text.removeprefix('CREATE OR REPLACE ').strip():
                        raise ValueError('Deployed source mismatch')
        print('HRMS_RULES v1 deployment başarılı; specification/body VALID ve kaynaklar eşleşiyor.')
        return 0
    except BaseException:
        print('Deployment doğrulanamadı; otomatik retry/repair yok. Ham hata ayrıntıları gizlendi.', file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                print('Bağlantı kapatılması doğrulanamadı; manuel inceleme gerekiyor.', file=sys.stderr)
                # DDL başarılı olsa bile cleanup hatası sessiz başarı değildir.
                raise SystemExit(1) from None


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except (OSError, UnicodeError, ValueError):
        print('Yerel deployment kaynakları doğrulanamadı.', file=sys.stderr)
        raise SystemExit(1) from None
