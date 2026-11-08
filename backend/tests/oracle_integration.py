"""Açık opt-in ile yerel HR_CORE; ayırt edilebilir test kayıtları pasif bırakılır.

Çalıştırma: HRMS_RUN_ORACLE_TESTS=1 .venv/bin/python -B -m backend.tests.oracle_integration
Gerçek commit belirsizliği/ağ kopması enjekte edilmez; o yollar unit testlerindedir.
"""
import json
import os
import uuid
from datetime import date, datetime
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.db.oracle import create_oracle_connection
from backend.app.db.session import validate_target_session
from backend.app.db.dependencies import get_db_connection
from backend.app.repositories.department import DepartmentRepository


def checked_connection():
    connection = create_oracle_connection()
    try:
        validate_target_session(connection)
    except BaseException:
        connection.close()
        raise
    return connection


def dependency():
    connection = checked_connection()
    try:
        yield connection
    finally:
        connection.close()


def run():
    if os.environ.get('HRMS_RUN_ORACLE_TESTS') != '1':
        raise RuntimeError('Açık canlı test opt-in gerekir.')
    tag = 'MVPIT_' + uuid.uuid4().hex[:12].upper()
    print(json.dumps({'fixture_tag': tag, 'phase': 'start'}), flush=True)
    app.dependency_overrides[get_db_connection] = dependency
    checks = []
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            def request(method, url, expected, payload=None):
                response = client.request(method, url, json=payload) if payload is not None else client.request(method, url)
                if response.status_code != expected:
                    # Yanıt içeriği, driver veya gizli değer gösterilmez; belirsiz yazma tekrarlanmaz.
                    raise RuntimeError(f'Kontrol başarısız: {method} {url} HTTP {response.status_code}')
                checks.append(f'{method} {url}: {expected}')
                print(json.dumps({'check': checks[-1]}), flush=True)
                return response.json()
            departments = []
            for suffix in ('A', 'B'):
                departments.append(request('POST', '/departments', 201,
                    {'department_code': tag + suffix, 'department_name': 'MVP Integration ' + suffix}))
            a, b = departments
            request('POST', '/departments', 409, {'department_code': tag + 'A', 'department_name': 'Duplicate test'})
            employee = request('POST', '/employees', 201, {'employee_no': tag + 'E', 'first_name': 'Integration',
                'last_name': 'Fixture', 'email': None, 'hire_date': '2026-01-02', 'department_id': a['id']})
            if type(employee['id']) is not int or datetime.fromisoformat(employee['created_at']).utcoffset() is None:
                raise RuntimeError('Identity veya timezone sözleşmesi başarısız.')
            request('POST', '/departments/' + str(a['id']) + '/deactivate', 409)
            duplicate = {'employee_no': tag + 'E', 'first_name': 'Integration', 'last_name': 'Fixture',
                         'hire_date': '2026-01-02', 'department_id': a['id']}
            request('POST', '/employees', 409, duplicate)
            updated = request('PATCH', '/employees/' + str(employee['id']), 200,
                              {'department_id': b['id'], 'email': 'integration@example.invalid'})
            if updated['department_id'] != b['id'] or updated['hire_date'] != '2026-01-02':
                raise RuntimeError('FK/tarih eşleme kontrolü başarısız.')
            request('GET', '/employees?page=1&size=5', 200)
            request('GET', '/employees/' + str(employee['id']), 200)
            request('PATCH', '/employees/' + str(employee['id']), 200, {'email': None})
            request('POST', '/employees/' + str(employee['id']) + '/deactivate', 200)
            for department in departments:
                request('POST', '/departments/' + str(department['id']) + '/deactivate', 200)
            request('POST', '/employees', 409, {**duplicate, 'employee_no': tag + 'INACTIVE'})
        connection = checked_connection()
        try:
            # Commit edilmeyen yeni test kaydı rollback sonrası görünmemeli.
            DepartmentRepository(connection).create({'department_code': tag + 'ROLLBACK', 'department_name': 'Rollback fixture'})
            connection.rollback()
            with connection.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM HR_CORE.DEPARTMENT WHERE DEPARTMENT_CODE=:code', {'code': tag + 'ROLLBACK'})
                if cursor.fetchone() != (0,):
                    raise RuntimeError('Rollback doğrulanamadı.')
            checks.append('Oracle rollback: confirmed')
        finally:
            connection.close()
        print(json.dumps({'passed': True, 'fixture_tag': tag, 'department_ids': [a['id'], b['id']],
                          'employee_id': employee['id'], 'checks': checks}, ensure_ascii=False))
    finally:
        app.dependency_overrides.clear()


if __name__ == '__main__':
    try:
        run()
    except BaseException:
        print('{"passed": false, "action": "STOP; no retry; inspect isolated fixture state"}')
        raise SystemExit(1) from None
