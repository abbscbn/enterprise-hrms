import hashlib
from ..security.repository import Repository
from ..security.dependencies import own_employee,deny
from ..core.errors import NotFound,CommitOutcomeUnknown,TransactionCleanupError,Forbidden
from ..db.transactions import transaction
from ..enterprise.repository import ProfileRepository
from ..repositories.employee import EmployeeRepository
from ..repositories import audit
from .local import LocalStorage
from .schemas import FileRead
from .validation import DOCUMENT_LIMIT

PROJECTION="ID,EMPLOYEE_ID,CATEGORY,DOCUMENT_TYPE,ORIGINAL_NAME,STORAGE_KEY,MIME_TYPE,SIZE_BYTES,CHECKSUM,UPLOADED_BY,DESCRIPTION,VISIBILITY,TO_CHAR(CREATED_AT,'YYYY-MM-DD\"T\"HH24:MI:SS.FF6TZH:TZM') AS CREATED_AT"

class FileService:
    def __init__(self,connection,actor,storage=None):self.connection=connection;self.actor=actor;self.repo=Repository(connection);self._storage=storage
    @property
    def storage(self):
        if self._storage is None:self._storage=LocalStorage()
        return self._storage
    def authorize(self,employee_id,write=False,photo=False):
        own_employee(self.connection,self.actor,employee_id)
        if write and not photo and self.actor.role=='EMPLOYEE':deny(self.connection,self.actor,entity='DOCUMENT',identifier=employee_id)
    def visible(self,row):
        return self.actor.role=='ADMIN' or (self.actor.role=='HR_MANAGER' and row['visibility']!='ADMIN_ONLY') or (row['employee_id']==self.actor.employee_id and row['visibility']=='SELF')
    def metadata(self,employee_id,identifier,photo=False):
        self.authorize(employee_id)
        row=self.repo.one('SELECT '+PROJECTION+' FROM HR_CORE.HRMS_FILE WHERE ID=:id AND EMPLOYEE_ID=:employee_id AND CATEGORY=:category AND ARCHIVED_AT IS NULL',{'id':identifier,'employee_id':employee_id,'category':'PHOTO' if photo else 'DOCUMENT'})
        if row is None:raise NotFound()
        if not self.visible(row):deny(self.connection,self.actor,entity='DOCUMENT',identifier=identifier)
        return row
    def upload(self,employee_id,validated,document_type,visibility,description,photo=False):
        self.authorize(employee_id,write=True,photo=photo)
        if EmployeeRepository(self.connection).get(employee_id) is None:raise NotFound()
        if visibility=='ADMIN_ONLY' and self.actor.role!='ADMIN':deny(self.connection,self.actor,entity='DOCUMENT',identifier=employee_id)
        key=self.storage.save(validated.content,validated.extension)
        try:
            with transaction(self.connection):
                ProfileRepository(self.connection).lock()
                params={'employee_id':employee_id,'category':'PHOTO' if photo else 'DOCUMENT','document_type':document_type,
                    'name':validated.original_name,'key':key,'mime':validated.mime,'file_size':len(validated.content),
                    'checksum':hashlib.sha256(validated.content).hexdigest(),'actor':self.actor.id,'description':description,'visibility':visibility}
                identifier=self.repo.insert_id('INSERT INTO HR_CORE.HRMS_FILE (EMPLOYEE_ID,CATEGORY,DOCUMENT_TYPE,ORIGINAL_NAME,STORAGE_KEY,MIME_TYPE,SIZE_BYTES,CHECKSUM,UPLOADED_BY,DESCRIPTION,VISIBILITY) '
                    'VALUES (:employee_id,:category,:document_type,:name,:key,:mime,:file_size,:checksum,:actor,:description,:visibility)',params)
                if photo:
                    previous=self.repo.one('SELECT PHOTO_ID FROM HR_CORE.EMPLOYEE_PROFILE WHERE EMPLOYEE_ID=:id',{'id':employee_id})
                    if previous and previous['photo_id'] is not None:self.repo.execute('UPDATE HR_CORE.HRMS_FILE SET ARCHIVED_AT=SYSTIMESTAMP WHERE ID=:id AND EMPLOYEE_ID=:employee_id',{'id':previous['photo_id'],'employee_id':employee_id})
                    self.repo.execute('MERGE INTO HR_CORE.EMPLOYEE_PROFILE T USING (SELECT :id AS K FROM DUAL) S ON (T.EMPLOYEE_ID=S.K) '
                        'WHEN MATCHED THEN UPDATE SET T.PHOTO_ID=:photo,T.UPDATED_BY=:actor,T.UPDATED_AT=SYSTIMESTAMP '
                        'WHEN NOT MATCHED THEN INSERT (EMPLOYEE_ID,PHOTO_ID,UPDATED_BY,UPDATED_AT) VALUES (:id,:photo,:actor,SYSTIMESTAMP)',{'id':employee_id,'photo':identifier,'actor':self.actor.id})
                audit.write(self.connection,self.actor,'PHOTO_UPLOADED' if photo else 'DOCUMENT_UPLOADED','FILE',identifier)
                row=self.metadata(employee_id,identifier,photo)
                result=FileRead.model_validate(row)
            return result
        except (CommitOutcomeUnknown,TransactionCleanupError):
            # Metadata may exist: retain file. Never delete on an unknown commit/rollback.
            raise
        except BaseException as error:
            try:self.storage.discard_uncommitted(key)
            except BaseException:
                if isinstance(error,Exception):raise TransactionCleanupError() from None
                error.add_note("Geçici dosya temizliği doğrulanamadı; manuel inceleme gerekiyor.")
            raise
    def list(self,employee_id,page,size,document_type=None):
        self.authorize(employee_id)
        where='EMPLOYEE_ID=:employee_id AND CATEGORY=\'DOCUMENT\' AND ARCHIVED_AT IS NULL';params={'employee_id':employee_id,'offset':(page-1)*size,'limit':size}
        if document_type:where+=' AND DOCUMENT_TYPE=:kind';params['kind']=document_type
        if self.actor.role=='EMPLOYEE':where+=" AND VISIBILITY='SELF'"
        elif self.actor.role=='HR_MANAGER':where+=" AND VISIBILITY<>'ADMIN_ONLY'"
        rows=self.repo.all('SELECT '+PROJECTION+' FROM HR_CORE.HRMS_FILE WHERE '+where+' ORDER BY ID DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY',params)
        return {'items':[FileRead.model_validate(row) for row in rows],'page':page,'size':size}
    def download(self,employee_id,identifier,photo=False):
        row=self.metadata(employee_id,identifier,photo);data=self.storage.read(row['storage_key'],DOCUMENT_LIMIT)
        if len(data)!=row['size_bytes']:raise NotFound()
        # Hash check detects storage/metadata drift; never execute/open document content.
        if hashlib.sha256(data).hexdigest()!=row['checksum']:raise NotFound()
        with transaction(self.connection):audit.write(self.connection,self.actor,'PHOTO_DOWNLOADED' if photo else 'DOCUMENT_DOWNLOADED','FILE',identifier,outcome='AUTHORIZED')
        return row,data
    def archive(self,employee_id,identifier):
        self.authorize(employee_id,write=True);row=self.metadata(employee_id,identifier)
        with transaction(self.connection):
            ProfileRepository(self.connection).lock()
            count=self.repo.execute('UPDATE HR_CORE.HRMS_FILE SET ARCHIVED_AT=SYSTIMESTAMP WHERE ID=:id AND EMPLOYEE_ID=:employee_id AND ARCHIVED_AT IS NULL',{'id':identifier,'employee_id':employee_id})
            if count!=1:raise NotFound()
            audit.write(self.connection,self.actor,'DOCUMENT_ARCHIVED','FILE',identifier)
        # Archiving retains physical content; never erase existing user documents.
        return {'status':'archived'}
