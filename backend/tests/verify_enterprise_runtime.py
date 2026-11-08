"""Opt-in SELECT-only checks for V2 fixture delivery, schema and session identity."""
import json,os,sys
import httpx
from backend.app.db.oracle import create_oracle_connection
from backend.app.db.session import validate_target_session


def run():
    if os.environ.get('HRMS_RUN_ORACLE_TESTS')!='1':return 1
    connection=None
    try:
        connection=create_oracle_connection();validate_target_session(connection)
        with connection.cursor() as cursor:
            cursor.execute('SELECT ID FROM HR_CORE.EMPLOYEE WHERE EMPLOYEE_NO LIKE :prefix',{'prefix':'V2IT%'})
            ids={row[0] for row in cursor.fetchall()}
            if not ids:raise ValueError()
            cursor.execute('SELECT EVENT_ID,PAYLOAD,PUBLISHED_AT FROM HR_CORE.HRMS_OUTBOX WHERE EVENT_TYPE=:kind',{'kind':'EMPLOYEE_CREATED'})
            events=[]
            for identifier,payload,published in cursor.fetchall():
                event=json.loads(payload.read() if hasattr(payload,'read') else payload)
                if event['employee_id'] in ids:
                    if published is None:raise ValueError()
                    events.append(identifier)
            if len(events)!=len(ids):raise ValueError()
        with httpx.Client(timeout=10) as client:
            for event in events:
                response=client.get('http://127.0.0.1:5001/receipts/'+event)
                if response.status_code!=200 or response.json().get('processed') is not True:raise ValueError()
        print('HR_CORE/FREEPDB1/HR_CORE verified; V2 fixture outbox + RabbitMQ + Flask receipts:',len(events),'MATCH; SELECT only.')
        return 0
    except BaseException:
        print('Runtime doğrulaması tamamlanamadı; otomatik retry/onarım yapılmadı.');return 1
    finally:
        if connection is not None:
            try:connection.close()
            except BaseException:raise SystemExit('Bağlantı temizliği doğrulanamadı.') from None

if __name__=='__main__':sys.exit(run())
