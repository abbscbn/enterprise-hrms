"""Opt-in live V2 test. Only owns V2IT fixtures; no delete/retry/repair."""
import io,os,secrets,sys,time
from pathlib import Path
import httpx
from PIL import Image
from pypdf import PdfWriter

class CheckFailed(Exception):pass

def run():
    if os.environ.get('HRMS_RUN_ORACLE_TESTS')!='1':raise CheckFailed('Explicit live-test opt-in required')
    root=Path(__file__).resolve().parents[2]
    base='http://127.0.0.1:5173/api'
    stage='owner_login';checks=0
    clients=[]
    def client():
        c=httpx.Client(base_url=base,timeout=30);clients.append(c);return c
    def request(c,method,path,status=200,**kwargs):
        nonlocal checks,stage
        stage=method+' '+path
        csrf=c.cookies.get('hrms_csrf')
        headers=kwargs.pop('headers',{})
        if csrf:headers['X-CSRF-Token']=csrf
        r=c.request(method,path,headers=headers,**kwargs)
        if r.status_code!=status:raise CheckFailed('Unexpected HTTP status '+str(r.status_code)+'; no retry')
        checks+=1;return r
    suffix=secrets.token_hex(4);prefix='V2IT'+suffix.upper();password=secrets.token_urlsafe(24)
    admin=client();employee=client();hr=client();anonymous=client()
    try:
        credential=Path(os.environ.get('HRMS_TEST_PASSWORD_FILE',str(root/'.secrets/initial-admin-password')))
        if credential.is_symlink() or credential.stat().st_mode & 0o077:raise CheckFailed('Private credential file permissions required')
        request(admin,'POST','/auth/login',json={'login':os.environ.get('HRMS_TEST_USERNAME','admin'),'password':credential.read_text().strip()})
        request(admin,'GET','/auth/me')
        request(anonymous,'GET','/employees',401)
        d=request(admin,'POST','/departments',201,json={'department_code':prefix+'A','department_name':'V2 Integration A','description':'Owned isolated fixture'}).json()
        d2=request(admin,'POST','/departments',201,json={'department_code':prefix+'B','department_name':'V2 Integration B','parent_id':d['id']}).json()
        request(admin,'PATCH',f"/departments/{d['id']}",409,json={'parent_id':d2['id']})
        request(admin,'PATCH',f"/departments/{d['id']}",409,json={'parent_id':d['id']})
        def person(n):return request(admin,'POST','/employees',201,json={'employee_no':prefix+str(n),'first_name':'V2 Test','last_name':str(n),'hire_date':'2026-01-01','department_id':d['id'],'job_title':'Integration','employment_type':'FULL_TIME','work_location':'Test'}).json()
        e=person(1);other=person(2)
        request(admin,'PATCH',f"/employees/{e['id']}",json={'manager_id':other['id']})
        request(admin,'PATCH',f"/employees/{other['id']}",409,json={'manager_id':e['id']})
        request(admin,'PATCH',f"/employees/{e['id']}",409,json={'manager_id':e['id']})
        request(admin,'PATCH',f"/departments/{d['id']}",json={'manager_id':other['id']})
        user=request(admin,'POST','/users',201,json={'username':'v2it_'+suffix,'email':suffix+'@example.invalid','password':password,'role':'EMPLOYEE','employee_id':e['id']}).json()
        hruser=request(admin,'POST','/users',201,json={'username':'v2hr_'+suffix,'email':'hr'+suffix+'@example.invalid','password':password,'role':'HR_MANAGER'}).json()
        request(employee,'POST','/auth/login',json={'login':user['username'],'password':password})
        request(hr,'POST','/auth/login',json={'login':hruser['username'],'password':password})
        request(employee,'GET','/me/profile')
        request(employee,'GET',f"/employees/{other['id']}",403)
        request(employee,'GET','/employees',403)
        request(employee,'GET','/users',403)
        request(hr,'GET','/users')
        request(hr,'GET','/audit',403)
        request(employee,'PATCH','/me/profile',json={'phone_number':'+90 555 000 0000','personal_email':'fixture@example.invalid'})
        request(employee,'PATCH','/me/profile',403,json={'job_title':'Unauthorized'})
        request(employee,'PATCH',f"/users/{user['id']}",403,json={'role':'ADMIN'})
        request(admin,'POST',f"/departments/{d['id']}/deactivate",409)
        picture=io.BytesIO();Image.new('RGB',(32,32),'green').save(picture,format='PNG');image=picture.getvalue()
        photo=request(employee,'POST',f"/employees/{e['id']}/photo",201,files={'file':('avatar.png',image,'image/png')}).json()
        response=request(employee,'GET',f"/employees/{e['id']}/photo")
        if response.headers['content-type']!='image/webp':raise CheckFailed('Photo conversion mismatch')
        request(hr,'POST',f"/employees/{e['id']}/photo",422,files={'file':('bad.png',b'not image','image/png')})
        request(hr,'POST',f"/employees/{e['id']}/documents",422,files={'file':('../bad.png',image,'image/png')})
        pdf=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=72,height=72);writer.write(pdf);content=pdf.getvalue()
        doc=request(hr,'POST',f"/employees/{e['id']}/documents",201,files={'file':('test.pdf',content,'application/pdf')},data={'document_type':'CV','visibility':'SELF'}).json()
        response=request(employee,'GET',f"/employees/{e['id']}/documents/{doc['id']}/download")
        if response.content!=content or 'attachment' not in response.headers.get('content-disposition',''):raise CheckFailed('Private download mismatch')
        secret=request(admin,'POST',f"/employees/{e['id']}/documents",201,files={'file':('test.pdf',content,'application/pdf')},data={'document_type':'CONTRACT','visibility':'ADMIN_ONLY'}).json()
        for c in (employee,hr):request(c,'GET',f"/employees/{e['id']}/documents/{secret['id']}/download",403)
        request(employee,'GET',f"/employees/{other['id']}/documents/{doc['id']}/download",403)
        request(employee,'POST',f"/employees/{e['id']}/documents",403,files={'file':('test.pdf',content,'application/pdf')})
        request(employee,'GET',f"/employees/{e['id']}/documents")
        request(hr,'POST',f"/employees/{e['id']}/documents/{doc['id']}/archive")
        request(employee,'GET',f"/employees/{e['id']}/documents/{doc['id']}/download",404)
        # Expire only this fixture's access token. Refresh lifetime is unchanged.
        from backend.app.db.oracle import create_oracle_connection
        from backend.app.db.transactions import transaction
        from backend.app.security.passwords import digest
        db=None
        try:
            db=create_oracle_connection()
            with transaction(db):
                with db.cursor() as cursor:
                    cursor.execute("UPDATE HR_CORE.HRMS_SESSION SET EXPIRES_AT=SYSTIMESTAMP-NUMTODSINTERVAL(1,'SECOND') WHERE USER_ID=:user_id AND ACCESS_HASH=:token AND REVOKED_AT IS NULL",{'user_id':user['id'],'token':digest(employee.cookies.get('hrms_access'))})
                    if cursor.rowcount!=1:raise CheckFailed('Fixture expiry target mismatch')
        finally:
            if db is not None:db.close()
        request(employee,'GET','/auth/me',401)
        request(employee,'POST','/auth/refresh')
        request(employee,'GET','/auth/me')
        # Rotation/replay revokes this isolated user's session, never an owner session.
        old_refresh=employee.cookies.get('hrms_refresh');old_csrf=employee.cookies.get('hrms_csrf')
        request(employee,'POST','/auth/refresh')
        replay=client();replay.cookies.set('hrms_refresh',old_refresh);replay.cookies.set('hrms_csrf',old_csrf)
        request(replay,'POST','/auth/refresh',401)
        request(employee,'GET','/auth/me',401)
        request(employee,'POST','/auth/login',json={'login':user['username'],'password':password})
        request(employee,'POST','/auth/password',json={'current_password':password,'new_password':password+'new'})
        request(employee,'GET','/auth/me',401)
        request(employee,'POST','/auth/login',json={'login':user['username'],'password':password+'new'})
        request(admin,'PATCH',f"/users/{user['id']}",json={'is_active':False})
        request(employee,'GET','/auth/me',401)
        request(admin,'GET','/audit')
        request(admin,'GET','/department-hierarchy')
        request(admin,'GET','/employees',params={'q':prefix,'status':'ACTIVE'})
        request(admin,'PATCH',f"/employees/{e['id']}",json={'manager_id':None})
        for item in (e,other):request(admin,'POST',f"/employees/{item['id']}/deactivate")
        for item in (d2,d):request(admin,'POST',f"/departments/{item['id']}/deactivate")
        request(admin,'PATCH',f"/users/{hruser['id']}",json={'is_active':False})
        request(admin,'POST',f"/employees/{e['id']}/documents/{secret['id']}/archive")
        request(admin,'POST','/auth/logout')
        request(admin,'GET','/auth/me',401)
        print('V2 live HTTP/Oracle checks:',checks,'PASS; isolated fixtures deactivated, documents archived; no deletion.')
        print('Fixture prefix:',prefix,'photo metadata id:',photo['id'])
    except BaseException as error:
        print('V2 live test stopped at:',stage,'; no retry or automatic repair. Type:',type(error).__name__)
        return 1
    finally:
        for c in clients:c.close()
    return 0

if __name__=='__main__':sys.exit(run())
