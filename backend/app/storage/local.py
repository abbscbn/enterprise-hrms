import os
import re
from pathlib import Path
from uuid import uuid4
from ..core.errors import NotFound

KEY=re.compile(r'[0-9a-f]{32}\.(?:webp|pdf|jpg|png|docx)')

class LocalStorage:
    def __init__(self,root=None):
        self.root=Path(root or os.environ.get('HRMS_STORAGE_ROOT','/var/lib/hrms-files')).resolve()
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
    def path(self,key):
        if type(key) is not str or not KEY.fullmatch(key):raise NotFound()
        path=self.root/key
        if path.is_symlink() or not path.is_file() or path.resolve().parent!=self.root:raise NotFound()
        return path
    def save(self,content,extension):
        key=uuid4().hex+'.'+extension
        if not KEY.fullmatch(key):raise ValueError('Unsupported storage extension')
        temp=self.root/('.partial_'+uuid4().hex);target=self.root/key;linked=False
        try:
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as file:file.write(content);file.flush();os.fsync(file.fileno())
            os.link(temp,target);linked=True
            return key
        except BaseException:
            if linked:target.unlink(missing_ok=True)
            raise
        finally:temp.unlink(missing_ok=True)
    def discard_uncommitted(self,key):
        self.path(key).unlink()

    def read(self,key,max_bytes):
        with self.path(key).open('rb') as file:data=file.read(max_bytes+1)
        if len(data)>max_bytes:raise NotFound()
        return data
