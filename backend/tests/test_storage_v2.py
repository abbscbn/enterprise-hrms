import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock,Mock,patch
from PIL import Image
from pypdf import PdfWriter
from backend.app.storage.validation import validate_file,PHOTO_LIMIT,DOCUMENT_LIMIT
from backend.app.storage.local import LocalStorage
from backend.app.storage.service import FileService
from backend.app.security.models import Principal
from backend.app.core.errors import UploadRejected,NotFound,Forbidden,CommitOutcomeUnknown,ApplicationError


def image_bytes(fmt='PNG'):
    output=io.BytesIO();image=Image.new('RGB',(20,20),'blue');image.save(output,format=fmt);return output.getvalue()

def pdf_bytes():
    output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.write(output);return output.getvalue()

class ValidationTests(unittest.TestCase):
    def test_photo_jpeg_png_webp_normalize_to_metadata_free_webp(self):
        for suffix,fmt,mime in [('jpg','JPEG','image/jpeg'),('png','PNG','image/png'),('webp','WEBP','image/webp')]:
            result=validate_file('photo.'+suffix,mime,image_bytes(fmt),True)
            self.assertEqual(result.extension,'webp')
            with Image.open(io.BytesIO(result.content)) as image:self.assertFalse(image.getexif());self.assertLessEqual(image.width,1024)

    def test_document_pdf_and_images_valid(self):
        for name,mime,data in [('cv.pdf','application/pdf',pdf_bytes()),('scan.jpg','image/jpeg',image_bytes('JPEG')),('scan.png','image/png',image_bytes())]:
            result=validate_file(name,mime,data);self.assertEqual(result.content,data)

    def test_wrong_content_and_mime_rejected(self):
        for name,mime,data in [('cv.pdf','application/pdf',b'not pdf'),('photo.png','image/jpeg',image_bytes()),('evil.svg','image/svg+xml',b'<svg/>'),('x.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',b'PKfake')]:
            with self.subTest(name=name),self.assertRaises(UploadRejected):validate_file(name,mime,data)

    def test_size_empty_and_traversal_rejected(self):
        for name,data in [('../x.png',image_bytes()),('..\\x.png',image_bytes()),('C:x.png',image_bytes()),('x\r\n.png',image_bytes()),('x.png',b''),('x.png',b'x'*(PHOTO_LIMIT+1))]:
            with self.subTest(name=name),self.assertRaises(UploadRejected):validate_file(name,'image/png',data,True)

    def test_docx_external_targets_and_zip_traversal_rejected(self):
        for name,body in [('../evil.xml',b'<x/>'),('word/_rels/document.xml.rels',b'<Relationships><Relationship TargetMode="External" Target="https://attacker.invalid"/></Relationships>')]:
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:
                archive.writestr('[Content_Types].xml','<Types><Override ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
                archive.writestr('word/document.xml','<document/>');archive.writestr(name,body)
            with self.assertRaises(UploadRejected):validate_file('cv.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',output.getvalue())

    def test_storage_keys_do_not_use_duplicate_original_filename(self):
        with tempfile.TemporaryDirectory() as root:
            storage=LocalStorage(root);one=storage.save(b'first','pdf');two=storage.save(b'second','pdf')
            self.assertNotEqual(one,two);self.assertEqual(storage.read(one,100),b'first');self.assertEqual(storage.read(two,100),b'second')
            self.assertFalse(list(Path(root).glob('.partial_*')))
            with self.assertRaises(NotFound):storage.read('../secret.pdf',100)
            storage.discard_uncommitted(one);self.assertEqual(storage.read(two,100),b'second')

    def test_storage_symlink_and_missing_file_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            storage=LocalStorage(root);key='a'*32+'.pdf';(Path(root)/key).symlink_to('/etc/hosts')
            with self.assertRaises(NotFound):storage.read(key,100)
            with self.assertRaises(NotFound):storage.read('b'*32+'.pdf',100)

class StorageTransactionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('oracledb.connect',side_effect=AssertionError('Real Oracle forbidden')))
        self.enterContext(patch('backend.app.db.transactions.validate_target_session'))
        self.enterContext(patch('backend.app.storage.service.ProfileRepository.lock'))
        self.enterContext(patch('backend.app.storage.service.audit.write'))
        self.enterContext(patch('backend.app.storage.service.EmployeeRepository.get',return_value={'id':1}))
        self.connection=MagicMock();self.connection.autocommit=False
        self.storage=Mock();self.storage.save.return_value='a'*32+'.pdf'
        self.actor=Principal(1,'hr','hr@example.invalid','HR_MANAGER')
        self.service=FileService(self.connection,self.actor,self.storage);self.service.repo=Mock()
        self.validated=validate_file('cv.pdf','application/pdf',pdf_bytes())
        self.row={'id':7,'employee_id':1,'category':'DOCUMENT','document_type':'CV','original_name':'cv.pdf','storage_key':'a'*32+'.pdf','mime_type':'application/pdf','size_bytes':len(self.validated.content),'checksum':__import__('hashlib').sha256(self.validated.content).hexdigest(),'uploaded_by':1,'description':None,'visibility':'SELF','created_at':'2026-11-08T00:00:00+00:00'}
        self.service.repo.insert_id.return_value=7;self.service.repo.one.return_value=self.row

    def test_upload_commits_metadata_and_audit_without_storage_key_response(self):
        result=self.service.upload(1,self.validated,'CV','SELF',None)
        self.connection.commit.assert_called_once();self.storage.discard_uncommitted.assert_not_called()
        self.assertNotIn('storage_key',result.model_dump())
        params=self.service.repo.insert_id.call_args.args[1]
        self.assertEqual(params['file_size'],len(self.validated.content))
        self.assertNotIn(':size,',self.service.repo.insert_id.call_args.args[0])

    def test_metadata_failure_discards_only_new_uncommitted_file(self):
        self.service.repo.insert_id.side_effect=RuntimeError()
        with self.assertRaises(ApplicationError):self.service.upload(1,self.validated,'CV','SELF',None)
        self.connection.rollback.assert_called_once();self.storage.discard_uncommitted.assert_called_once_with('a'*32+'.pdf')

    def test_storage_failure_never_starts_metadata_transaction(self):
        self.storage.save.side_effect=OSError()
        with self.assertRaises(OSError):self.service.upload(1,self.validated,'CV','SELF',None)
        self.service.repo.insert_id.assert_not_called();self.connection.commit.assert_not_called()

    def test_commit_unknown_preserves_physical_file(self):
        self.connection.commit.side_effect=RuntimeError()
        with self.assertRaises(CommitOutcomeUnknown):self.service.upload(1,self.validated,'CV','SELF',None)
        self.storage.discard_uncommitted.assert_not_called();self.connection.rollback.assert_not_called()

    def test_employee_cannot_read_another_employee_file_or_upload_document(self):
        self.service.actor=Principal(2,'self','self@example.invalid','EMPLOYEE',employee_id=2)
        with patch('backend.app.security.dependencies.audit.write'):
            with self.assertRaises(Forbidden):self.service.metadata(1,7)
            self.service.actor=Principal(2,'self','self@example.invalid','EMPLOYEE',employee_id=1)
            with self.assertRaises(Forbidden):self.service.upload(1,self.validated,'CV','SELF',None)
        self.storage.save.assert_not_called();self.storage.read.assert_not_called()

    def test_hr_cannot_read_admin_only_document(self):
        self.service.repo.one.return_value={**self.row,'visibility':'ADMIN_ONLY'}
        with patch('backend.app.security.dependencies.audit.write'),self.assertRaises(Forbidden):self.service.download(1,7)
        self.storage.read.assert_not_called()

    def test_authorized_download_checks_content_and_audits(self):
        self.storage.read.return_value=self.validated.content
        row,data=self.service.download(1,7)
        self.assertEqual(data,self.validated.content);self.connection.commit.assert_called_once()

    def test_storage_metadata_drift_is_detected(self):
        self.storage.read.return_value=b'wrong'
        with self.assertRaises(NotFound):self.service.download(1,7)
        self.connection.commit.assert_not_called()

    def test_archive_retains_file(self):
        self.service.repo.execute.return_value=1
        self.assertEqual(self.service.archive(1,7),{'status':'archived'})
        self.storage.discard_uncommitted.assert_not_called();self.connection.commit.assert_called_once()

class AdditionalFileSecurityTests(unittest.TestCase):
    def setUp(self):self.enterContext(patch('oracledb.connect',side_effect=AssertionError('No live Oracle')))

    def docx(self,body='<document/>',extra=None):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('[Content_Types].xml','<Types><Override ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr('word/document.xml',body)
            if extra:
                info,data=extra;archive.writestr(info,data)
        return output.getvalue()

    def test_valid_docx_and_dde_rejection(self):
        mime='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        self.assertEqual(validate_file('cv.docx',mime,self.docx()).extension,'docx')
        with self.assertRaises(UploadRejected):validate_file('cv.docx',mime,self.docx('<document>DDEAUTO command</document>'))

    def test_docx_symlink_rejected(self):
        import stat
        info=zipfile.ZipInfo('word/link');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
        with self.assertRaises(UploadRejected):validate_file('cv.docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',self.docx(extra=(info,b'/etc/passwd')))

    def test_pdf_active_content_rejected(self):
        writer=PdfWriter();writer.add_blank_page(width=72,height=72);writer._root_object.update({__import__("pypdf").generic.NameObject("/OpenAction"):__import__("pypdf").generic.DictionaryObject()});output=io.BytesIO();writer.write(output)
        with self.assertRaises(UploadRejected):validate_file('cv.pdf','application/pdf',output.getvalue())

    def test_reconciliation_detects_missing_corrupt_and_orphan_without_deleting(self):
        from backend.app.storage.reconcile import inspect_storage
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            storage=LocalStorage(root);good=storage.save(b'good','pdf');bad=storage.save(b'bad','pdf');orphan=storage.save(b'orphan','pdf')
            rows=[{'id':1,'storage_key':good,'size_bytes':4,'checksum':hashlib.sha256(b'good').hexdigest()},
                {'id':2,'storage_key':bad,'size_bytes':3,'checksum':'0'*64},
                {'id':3,'storage_key':'a'*32+'.pdf','size_bytes':1,'checksum':'0'*64}]
            with patch('backend.app.storage.reconcile.Repository') as repo,patch('backend.app.db.session.validate_target_session'):
                repo.return_value.all.return_value=rows;result=inspect_storage(Mock(),storage)
            self.assertEqual(result['missing_ids'],[3]);self.assertEqual(result['corrupt_ids'],[2]);self.assertEqual(result['unreferenced_entries'],1)
            self.assertTrue(result['manual_review_required']);self.assertTrue((Path(root)/orphan).exists())
