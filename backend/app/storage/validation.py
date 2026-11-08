import io
import re
import unicodedata
import zipfile
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from PIL import Image,ImageOps,UnidentifiedImageError
from pypdf import PdfReader
from defusedxml import ElementTree
from ..core.errors import UploadRejected

Image.MAX_IMAGE_PIXELS=25_000_000
PHOTO_LIMIT=5*1024*1024
DOCUMENT_LIMIT=10*1024*1024

@dataclass(frozen=True)
class ValidatedFile:
    original_name:str
    content:bytes
    extension:str
    mime:str


def safe_name(name):
    if type(name) is not str:raise UploadRejected()
    name=unicodedata.normalize('NFC',name)
    if not 1<=len(name)<=255 or name in ('.','..') or any(ord(ch)<32 for ch in name) or any(ch in name for ch in ('/','\\',':')):raise UploadRejected()
    return name


def image_bytes(content,photo):
    with Image.open(io.BytesIO(content)) as image:
        if image.format not in ('JPEG','PNG','WEBP') or image.width>10000 or image.height>10000 or image.width*image.height>25_000_000:raise UploadRejected()
        image.verify()
    with Image.open(io.BytesIO(content)) as image:
        image=ImageOps.exif_transpose(image)
        image.load()
        if photo:
            image.thumbnail((1024,1024));image=image.convert('RGB');image.info.clear();output=io.BytesIO();image.save(output,format='WEBP',quality=85)
            return output.getvalue(),'webp','image/webp'
        output=io.BytesIO();image=image.convert('RGB');image.save(output,format='PNG')
        data=output.getvalue()
        if len(data)>DOCUMENT_LIMIT:raise UploadRejected()
        return data,'png','image/png'


def validate_pdf(content):
    if not content.startswith(b'%PDF-'):raise UploadRejected()
    reader=PdfReader(io.BytesIO(content),strict=True)
    if reader.is_encrypted or not 1<=len(reader.pages)<=200:raise UploadRejected()
    visited=set()
    def inspect(obj,depth=0):
        if depth>40:raise UploadRejected()
        if hasattr(obj,'get_object'):
            resolved=obj.get_object()
            if resolved is not obj:return inspect(resolved,depth+1)
        identity=id(obj)
        if identity in visited:return
        visited.add(identity)
        if isinstance(obj,dict):
            for key,value in obj.items():
                if str(key) in ('/OpenAction','/AA','/JavaScript','/JS','/Launch','/EmbeddedFiles','/RichMedia'):raise UploadRejected()
                inspect(value,depth+1)
        elif isinstance(obj,list):
            for value in obj:inspect(value,depth+1)
    inspect(reader.trailer)


def validate_docx(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos=archive.infolist();names=[i.filename for i in infos]
        if len(infos)>500 or len(names)!=len(set(names)) or '[Content_Types].xml' not in names or 'word/document.xml' not in names:raise UploadRejected()
        if sum(i.file_size for i in infos)>20*1024*1024:raise UploadRejected()
        for item in infos:
            path=PurePosixPath(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16) or path.is_absolute() or '..' in path.parts or '\\' in item.filename or item.flag_bits&1 or item.file_size>max(item.compress_size,1)*100 or 'vbaproject' in item.filename.lower():raise UploadRejected()
            if item.filename.endswith(('.xml','.rels')):
                data=archive.read(item)
                element=ElementTree.fromstring(data)
                if any('DDEAUTO' in (node.text or '').upper() or 'DDE ' in (node.text or '').upper() for node in element.iter()):raise UploadRejected()
                if item.filename.endswith('.rels') and any(node.attrib.get('TargetMode')=='External' for node in element.iter()):raise UploadRejected()
        types=archive.read('[Content_Types].xml')
        if b'macroEnabled' in types or b'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' not in types:raise UploadRejected()


def validate_file(name,mime,content,photo=False):
    name=safe_name(name)
    if not content or len(content)>(PHOTO_LIMIT if photo else DOCUMENT_LIMIT):raise UploadRejected()
    suffix=name.rsplit('.',1)[-1].lower()
    expected={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp','pdf':'application/pdf','docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
    allowed=('jpg','jpeg','png','webp') if photo else ('pdf','jpg','jpeg','png','docx')
    if suffix not in allowed or mime!=expected[suffix]:raise UploadRejected()
    try:
        if suffix in ('jpg','jpeg','png','webp'):
            with Image.open(io.BytesIO(content)) as image:
                actual={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}.get(image.format)
                if actual!=mime:raise UploadRejected()
            result,extension,stored_mime=image_bytes(content,photo)
            return ValidatedFile(name,result,extension,stored_mime) if photo else ValidatedFile(name,content,'jpg' if suffix=='jpeg' else suffix,mime)
        if suffix=='pdf':validate_pdf(content)
        else:validate_docx(content)
        return ValidatedFile(name,content,suffix,expected[suffix])
    except UploadRejected:raise
    except Exception:raise UploadRejected() from None
