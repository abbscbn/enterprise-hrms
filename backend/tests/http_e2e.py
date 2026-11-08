"""Gerçek Compose ingress -> API -> Oracle outbox -> RabbitMQ -> Flask testidir.
Yalnız HRMS_RUN_ORACLE_TESTS=1 opt-in. HTTP yazma hatasında otomatik retry yok.
"""
import json
import os
import time
import uuid
from urllib.request import Request,urlopen
from urllib.error import HTTPError


def request(method,path,payload=None,expected=200):
    raw=json.dumps(payload).encode() if payload is not None else None
    try:
        with urlopen(Request('http://127.0.0.1:5173/api'+path,data=raw,method=method,
                            headers={'Content-Type':'application/json'}),timeout=20) as response:
            status=response.status;result=json.load(response)
    except HTTPError as error:
        status=error.code;result={}
    if status!=expected:raise RuntimeError('HTTP state unconfirmed; do not retry')
    return result


def run():
    if os.environ.get('HRMS_RUN_ORACLE_TESTS')!='1':raise RuntimeError()
    tag='MVPE2E_'+uuid.uuid4().hex[:10].upper()
    print(json.dumps({'phase':'start','fixture_tag':tag}),flush=True)
    department=request('POST','/departments',{'department_code':tag,'department_name':'End-to-end fixture'},201)
    employee=request('POST','/employees',{'employee_no':tag,'first_name':'Integration','last_name':'EndToEnd',
        'email':None,'hire_date':'2026-01-01','department_id':department['id']},201)
    if request('GET','/employees/'+str(employee['id']))['employee_no']!=tag:raise RuntimeError()
    request('PATCH','/employees/'+str(employee['id']),{'email':'e2e@example.invalid'})
    request('POST','/departments/'+str(department['id'])+'/deactivate',expected=409)
    # Independently correlate persisted event with durable consumer receipt.
    from backend.tests.oracle_integration import checked_connection
    c=checked_connection()
    try:
        with c.cursor() as cursor:
            cursor.execute('SELECT EVENT_ID, PAYLOAD FROM HR_CORE.HRMS_OUTBOX WHERE EVENT_TYPE=:kind',{'kind':'EMPLOYEE_CREATED'})
            matches=[]
            for event_id,payload in cursor.fetchall():
                event=json.loads(payload.read() if hasattr(payload,'read') else payload)
                if event['employee_id']==employee['id']:matches.append(event_id)
        if len(matches)!=1:raise RuntimeError()
        event_id=matches[0]
        # Read-only polling is permitted; never retry a database write/commit.
        published=False
        for _ in range(30):
            with c.cursor() as cursor:
                cursor.execute('SELECT PUBLISHED_AT FROM HR_CORE.HRMS_OUTBOX WHERE EVENT_ID=:event_id',{'event_id':event_id})
                published=cursor.fetchone()[0] is not None
            if published:break
            time.sleep(.2)
        if not published:raise RuntimeError()
    finally:c.close()
    # /receipts is dev-only and exposes only one receipt's presence, never employee data.
    consumed=False
    for _ in range(30):
        with urlopen('http://127.0.0.1:5001/receipts/'+event_id,timeout=5) as response:consumed=json.load(response)['processed']
        if consumed:break
        time.sleep(.2)
    if not consumed:raise RuntimeError()
    request('POST','/employees/'+str(employee['id'])+'/deactivate')
    request('POST','/departments/'+str(department['id'])+'/deactivate')
    print(json.dumps({'passed':True,'fixture_tag':tag,'employee_id':employee['id'],'event_id':event_id,
        'oracle_outbox_published':True,'flask_receipt_confirmed':True,'http_crud_confirmed':True,'test_records_deactivated':True}))


if __name__=='__main__':
    try:run()
    except BaseException:
        print('{"passed":false,"action":"STOP; inspect tagged fixture; no retry"}')
        raise SystemExit(1) from None
