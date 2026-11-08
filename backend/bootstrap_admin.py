"""Yalnız ilk ADMIN: ayrıcalıklı yerel operator CLI, HTTP bootstrap endpoint'i yok.
--generate-password private dosyaya rastgele parola yazar; terminale asla basmaz.
Mevcut kullanıcı varsa hesap/parola değiştirmez. Belirsiz commit'te tekrar denemez.
"""
import argparse
import getpass
import secrets
from pathlib import Path
from backend.app.db.oracle import create_oracle_connection
from backend.app.db.session import validate_target_session
from backend.app.security.models import UserCreate
from backend.app.security.service import AuthService
from backend.app.security.repository import Repository


def run():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--generate-password',action='store_true');parser.add_argument('--username',default='admin');parser.add_argument('--email',default='admin@hrms.local');args=parser.parse_args()
    connection=None
    try:
        connection=create_oracle_connection();validate_target_session(connection)
        if Repository(connection).one('SELECT COUNT(*) AS N FROM HR_CORE.HRMS_USER')['n']:
            print('Kullanıcılar mevcut; bootstrap hesap/parola değiştirmedi.');return 0
        if not args.apply:print('İlk ADMIN henüz yok. Açık --apply gerekir.');return 0
        if args.generate_password:
            root=Path(__file__).resolve().parents[1]/'.secrets'
            if root.is_symlink():raise ValueError('Unsafe secret directory')
            root.mkdir(mode=0o700,exist_ok=True);root.chmod(0o700)
            target=root/'initial-admin-password'
            if target.exists():raise ValueError('Existing credential file; inspect, do not overwrite')
            password=secrets.token_urlsafe(32)
            import os
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as file:file.write(password+'\n');file.flush();os.fsync(file.fileno())
        else:
            password=getpass.getpass('İlk ADMIN parolası (en az 12 karakter): ')
            if password!=getpass.getpass('Parolayı tekrar girin: '):raise ValueError('Password mismatch')
        user=AuthService(connection).create_user(UserCreate(username=args.username,email=args.email,password=password,role='ADMIN'),bootstrap=True)
        print(f'İlk ADMIN oluşturuldu: {user.username}; ID={user.id}. Parola gösterilmedi.')
        if args.generate_password:print('Parola yalnız .secrets/initial-admin-password dosyasında (0600). İlk girişten sonra değiştirin.')
        return 0
    except BaseException:
        print('ADMIN bootstrap doğrulanamadı; parolayı veya işlemi otomatik tekrar etmeyin.');return 1
    finally:
        if connection is not None:
            try:connection.close()
            except BaseException:
                print('ADMIN bağlantı kapanışı doğrulanamadı; manuel inceleme gerekir.');raise SystemExit(1) from None

if __name__=='__main__':raise SystemExit(run())
