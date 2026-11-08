"""Read-only private storage reconciliation. Never removes or repairs files."""
import hashlib
from pathlib import Path
from .local import LocalStorage
from .validation import DOCUMENT_LIMIT
from ..security.repository import Repository
from ..core.errors import NotFound


def inspect_storage(connection,storage=None):
    storage=storage or LocalStorage()
    from ..db.session import validate_target_session
    validate_target_session(connection)
    rows=Repository(connection).all('SELECT ID,STORAGE_KEY,SIZE_BYTES,CHECKSUM FROM HR_CORE.HRMS_FILE ORDER BY ID')
    known=set();missing=[];corrupt=[]
    for row in rows:
        known.add(row['storage_key'])
        try:data=storage.read(row['storage_key'],DOCUMENT_LIMIT)
        except NotFound:missing.append(row['id']);continue
        if len(data)!=row['size_bytes'] or hashlib.sha256(data).hexdigest()!=row['checksum']:corrupt.append(row['id'])
    # Only report counts; storage keys, content and absolute paths stay private.
    orphan_count=sum(1 for item in storage.root.iterdir() if item.name not in known)
    return {'records':len(rows),'missing_ids':missing,'corrupt_ids':corrupt,'unreferenced_entries':orphan_count,
            'manual_review_required':bool(missing or corrupt or orphan_count)}


def main():
    from ..db.oracle import create_oracle_connection
    connection=None
    try:
        connection=create_oracle_connection()
        print(inspect_storage(connection));return 0
    except BaseException:
        print('Storage incelemesi tamamlanamadı; otomatik onarım yapılmadı.');return 1
    finally:
        if connection is not None:
            try:connection.close()
            except BaseException:print('Bağlantı temizliği doğrulanamadı; manuel inceleme gerekiyor.')

if __name__=='__main__':raise SystemExit(main())
