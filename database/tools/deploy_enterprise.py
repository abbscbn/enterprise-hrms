"""Kontrollü V2 bundle: sabit şema üreticisi + hash manifest + gerçek TTY.

V1 parser/executor değişmez. History yardımcıları ve DBMS_LOCK sözleşmesi korunur.
DDL/history belirsizliğinde RUNNING korunur, otomatik retry/repair yoktur.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from database.enterprise.schema import TABLES


def sources():
    result=[]
    for table in TABLES:
        path=ROOT/'database/migrations'/table.filename
        if path.is_symlink() or path.read_bytes()!=table.sql().encode():raise ValueError('Controlled schema source mismatch')
        result.append((path.relative_to(ROOT).as_posix(),path.read_bytes()))
    for name in ('HRMS_ENTERPRISE_spec.sql','HRMS_ENTERPRISE_body.sql','seed_guard.sql'):
        path=ROOT/'database/enterprise'/name
        if path.is_symlink():raise ValueError('Source symlink')
        content=path.read_bytes();text=content.decode()
        if name=='seed_guard.sql':
            if text!='INSERT INTO HRMS_HIERARCHY_GUARD (ID) VALUES (1);\n':raise ValueError('Unexpected seed')
        else:
            expected='CREATE OR REPLACE PACKAGE '+('BODY ' if 'body' in name else '')+'HRMS_ENTERPRISE AS\n'
            if not text.startswith(expected) or not text.rstrip().endswith('END HRMS_ENTERPRISE;'):raise ValueError('Package source invalid')
            if re.search(r'\b(COMMIT|ROLLBACK|DROP|TRUNCATE|DELETE|AUTONOMOUS_TRANSACTION|EXECUTE\s+IMMEDIATE)\b',text,re.I):raise ValueError('Unsupported package operation')
        result.append((path.relative_to(ROOT).as_posix(),content))
    fingerprint=hashlib.sha256(b'\0'.join(n.encode()+b'\0'+b for n,b in result)).hexdigest()
    manifest=json.loads((ROOT/'database/enterprise/manifest.json').read_text())
    if manifest!={'version':1,'sha256':fingerprint,'files':{n:hashlib.sha256(b).hexdigest() for n,b in result}}:raise ValueError('Manifest mismatch')
    return result,fingerprint


def normalize(condition):
    if type(condition) is not str:raise ValueError('Missing CHECK expression')
    pattern=r"'(?:''|[^'])*'|\"[A-Z][A-Z0-9_]*\"|[A-Za-z][A-Za-z0-9_]*|[0-9]+|<>|[(),=<>]"
    tokens=[];position=0
    while position<len(condition):
        if condition[position].isspace():position+=1;continue
        match=re.match(pattern,condition[position:])
        if not match:raise ValueError('Unsupported CHECK metadata')
        token=match[0];tokens.append(token if token.startswith("'") else token.strip('"').upper());position+=len(token)
    while tokens and tokens[0]=='(' and tokens[-1]==')':
        depth=0
        for index,token in enumerate(tokens):
            depth+=(token=='(')-(token==')')
            if depth==0:break
        if index!=len(tokens)-1:break
        tokens=tokens[1:-1]
    return tuple(tokens)


def verify_table(m,c,table):
    columns=m.read_table_column_metadata(c,table.name);pk=m.read_table_primary_key_metadata(c,table.name);safety=m.read_table_safety_metadata(c,table.name)
    if (columns.owner!='HR_CORE' or safety.owner!='HR_CORE' or columns.table_name!=table.name or safety.table_name!=table.name or
        safety.object_types!=(('TABLE','VALID'),) or (safety.temporary,safety.iot_type,safety.nested,safety.secondary)!=('N',None,'NO','N')):raise ValueError('Table kind mismatch')
    if tuple((x.column_name,x.column_id) for x in columns.columns)!=tuple((x.column_name,x.column_id) for x in safety.column_properties):raise ValueError('Contradictory column views')
    if len(columns.columns)!=len(table.columns):raise ValueError('Column count mismatch')
    for expected,actual,prop in zip(table.columns,columns.columns,safety.column_properties):
        number=re.fullmatch(r'NUMBER\((\d+),(\d+)\)',expected.kind)
        varchar=re.fullmatch(r'VARCHAR2\((\d+) CHAR\)',expected.kind)
        precision=int(number[1]) if number else None
        scale=int(number[2]) if number else (6 if expected.kind.startswith('TIMESTAMP') else None)
        actual_values=(actual.column_name,actual.data_type,actual.data_precision,actual.data_scale,actual.nullable)
        if actual_values!=(expected.name,expected.kind if not varchar and not number else ('VARCHAR2' if varchar else 'NUMBER'),precision,scale,'Y' if expected.nullable else 'N'):raise ValueError('Column metadata mismatch')
        if varchar and (actual.char_used!='C' or actual.char_length!=int(varchar[1])):raise ValueError('CHAR semantics mismatch')
        if not varchar and actual.char_used is not None:raise ValueError('Unexpected CHAR semantics')
        if (prop.hidden,prop.virtual,prop.user_generated)!=('NO','NO','YES') or prop.identity!=('YES' if expected.identity else 'NO'):raise ValueError('Unsupported column property')
        if expected.identity:
            if prop.identity_generation!='ALWAYS' or not m._identity_default_matches(prop):raise ValueError('Identity mismatch')
        elif expected.default is None:
            if prop.default_length is not None:raise ValueError('Unexpected default')
        elif prop.default_expression.strip().upper()!=expected.default.upper():raise ValueError('Default mismatch')
        if prop.default_length is not None and prop.default_on_null!='NO':raise ValueError('Unexpected DEFAULT ON NULL')
    if pk is None or pk.constraint_name!='PK_'+table.name or pk.columns!=table.primary:raise ValueError('PK mismatch')
    if tuple(x.constraint_name for x in safety.constraints if x.constraint_type=='P')!=('PK_'+table.name,):raise ValueError('Contradictory PK views')
    actual={x.constraint_name:x for x in safety.constraints}
    wanted={'PK_'+table.name}|{x[0] for x in table.unique+table.foreign+table.checks}
    nn=set()
    for item in safety.constraints:
        if (item.status,item.validated,item.deferrable,item.deferred)!=('ENABLED','VALIDATED','NOT DEFERRABLE','IMMEDIATE'):raise ValueError('Constraint state mismatch')
        if item.constraint_name not in wanted:
            column=m._not_null_column(item.condition) if item.constraint_type=='C' else None
            if column is None or column in nn or column not in {x.name for x in table.columns if not x.nullable}:raise ValueError('Unexpected constraint')
            nn.add(column)
    for name,column in table.unique:
        item=actual.get(name)
        if item is None or item.constraint_type!='U' or item.columns!=(column,):raise ValueError('Unique mismatch')
    for name,column,reference,target in table.foreign:
        item=actual.get(name)
        if item is None or item.constraint_type!='R' or item.columns!=(column,) or item.reference is None:raise ValueError('FK mismatch')
        ref=item.reference
        if (ref.owner,ref.table_name,ref.columns,ref.delete_rule,ref.status,ref.validated)!=('HR_CORE',reference,(target,),'NO ACTION','ENABLED','VALIDATED'):raise ValueError('FK target mismatch')
    for name,condition in table.checks:
        item=actual.get(name)
        if item is None or item.constraint_type!='C' or normalize(item.condition)!=normalize(condition):raise ValueError('CHECK mismatch')
    if not {x.name for x in table.columns if not x.nullable and not x.identity}.issubset(nn|set(table.primary)):raise ValueError('NOT NULL evidence missing')


def package_state(c,artifacts):
    with c.cursor() as cursor:
        cursor.execute("SELECT OBJECT_TYPE,STATUS FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name AND OBJECT_TYPE IN ('PACKAGE','PACKAGE BODY')",{'owner':'HR_CORE','name':'HRMS_ENTERPRISE'})
        objects=cursor.fetchall()
        if not objects:return False
        if set(objects)!={('PACKAGE','VALID'),('PACKAGE BODY','VALID')}:raise ValueError('Partial package deployment')
        for suffix,kind in (('spec','PACKAGE'),('body','PACKAGE BODY')):
            text=dict(artifacts)['database/enterprise/HRMS_ENTERPRISE_'+suffix+'.sql'].decode()
            cursor.execute('SELECT TEXT FROM ALL_SOURCE WHERE OWNER=:owner AND NAME=:name AND TYPE=:kind ORDER BY LINE',{'owner':'HR_CORE','name':'HRMS_ENTERPRISE','kind':kind})
            if ''.join(r[0] for r in cursor.fetchall()).strip()!=text.removeprefix('CREATE OR REPLACE ').strip():raise ValueError('Package source mismatch')
    return True


def run():
    artifacts,fingerprint=sources()
    if '--fingerprint' in sys.argv:print(fingerprint);return 0
    spec=importlib.util.spec_from_file_location('enterprise_migration_helpers',ROOT/'database/tools/migrate.py')
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
    import oracledb
    from dotenv import load_dotenv
    c=None
    try:
        settings,port=m.load_connection_settings(load_dotenv);c=m.open_oracle_connection(settings,port,oracledb);m.validate_target_session(c)
        migrations=m.collect_migrations(m.MIGRATIONS_DIRECTORY);history=m.read_history_from_connection(c);entries=m.compare_migrations(migrations,history)
        if any(e.status not in ('APPLIED','PENDING') or e.script_name_mismatch for e in entries):raise ValueError('Blocking history')
        if any(e.status!='APPLIED' for e in entries if e.version<5) or {e.version for e in entries}!={t.version for t in TABLES}|{1,2,3,4}:raise ValueError('Unexpected versions')
        by_version={x.version:x for x in migrations};applied={e.version for e in entries if e.status=='APPLIED'}
        for table in TABLES:
            if table.version in applied:verify_table(m,c,table)
            else:
                count=m._read_precheck_count(c,"SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name AND OBJECT_TYPE IN ('TABLE','VIEW','SEQUENCE','SYNONYM','PACKAGE','PROCEDURE','FUNCTION','TYPE','MATERIALIZED VIEW')",{'owner':'HR_CORE','name':table.name})
                if count:raise ValueError('Object collision')
                names=['PK_'+table.name]+[x[0] for x in table.unique+table.foreign+table.checks]
                for name in names:
                    if m._read_precheck_count(c,'SELECT COUNT(*) FROM ALL_CONSTRAINTS WHERE OWNER=:owner AND CONSTRAINT_NAME=:name',{'owner':'HR_CORE','name':name}):raise ValueError('Constraint collision')
        installed=package_state(c,artifacts)
        if all(t.version in applied for t in TABLES) and installed:
            with c.cursor() as cursor:
                cursor.execute('SELECT ID FROM HR_CORE.HRMS_HIERARCHY_GUARD ORDER BY ID')
                if cursor.fetchall()!=[(1,)]:raise ValueError('Guard seed mismatch')
            print('V2 bundle: V0005–V0013 APPLIED/MATCH, HRMS_ENTERPRISE VALID/source MATCH; read-only.');return 0
        print('Hedef HR_CORE/FREEPDB1; V0005–V0013 ve HRMS_ENTERPRISE v1; SHA-256: '+fingerprint)
        print('İleri yönlü yeni tablolar; mevcut kayıtlar değişmez. DDL implicit commit; kısmi sonuçta retry yok.')
        if '--apply' not in sys.argv:print('Plan modu; DDL/DML çalışmadı.');return 0
        if not sys.stdin.isatty():raise ValueError('Real TTY required')
        approval='DEPLOY HR_CORE/FREEPDB1 ENTERPRISE_V2 '+fingerprint
        if input("Tam onay: '"+approval+"': ")!=approval or sources()[1]!=fingerprint:raise ValueError('Approval/content mismatch')
        with m.migration_lock(c):
            # Aynı bağlantı, kilit altında history yeniden doğrulanır; bir dış DDL yazıcısını durdurmaz.
            current=m.read_history_from_connection(c)
            if current!=history:raise ValueError('History changed since approval')
            for table in TABLES:
                if table.version in applied:continue
                if m._read_precheck_count(c,'SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER=:owner AND OBJECT_NAME=:name',{'owner':'HR_CORE','name':table.name}):raise ValueError('Object appeared')
                migration=by_version[table.version];raw=dict(artifacts)['database/migrations/'+table.filename]
                plan=m.ApplyPlan('HR_CORE','FREEPDB1',table.version,table.filename,migration.checksum,table.name,raw.decode().strip().removesuffix(';'),raw,tuple(entries))
                m.insert_running_migration(c,plan);c.commit()
                with c.cursor() as cursor:cursor.execute(plan.statement)
                if table.version==13:
                    with c.cursor() as cursor:cursor.execute(dict(artifacts)['database/enterprise/seed_guard.sql'].decode().strip().removesuffix(';'))
                verify_table(m,c,table)
                m.mark_migration_success(c,plan);c.commit()
                print(f'V{table.version:04d} SUCCESS/MATCH',flush=True)
            if not installed:
                if package_state(c,artifacts):raise ValueError('Package appeared')
                for suffix,kind in (('spec','PACKAGE'),('body','PACKAGE BODY')):
                    with c.cursor() as cursor:
                        cursor.execute(dict(artifacts)['database/enterprise/HRMS_ENTERPRISE_'+suffix+'.sql'].decode())
                        cursor.execute("SELECT COUNT(*) FROM ALL_ERRORS WHERE OWNER=:owner AND NAME=:name AND TYPE=:kind AND ATTRIBUTE='ERROR'",{'owner':'HR_CORE','name':'HRMS_ENTERPRISE','kind':kind})
                        if cursor.fetchone()!=(0,):raise ValueError('Package compilation failed')
                if not package_state(c,artifacts):raise ValueError('Package verification failed')
            print('Enterprise v2 deployment başarılı; package VALID/source MATCH.')
        return 0
    except BaseException:
        print('V2 deployment doğrulanamadı; RUNNING/kısmi nesneler korunur. Retry/repair yapmayın.',file=sys.stderr);return 1
    finally:
        if c is not None:
            try:c.close()
            except BaseException:
                print('Bağlantı kapatılması doğrulanamadı; manuel inceleme gerekiyor.',file=sys.stderr);raise SystemExit(1) from None

if __name__=='__main__':
    try:raise SystemExit(run())
    except (OSError,UnicodeError,ValueError):
        print('Yerel v2 kaynak/manifest doğrulaması başarısız.',file=sys.stderr);raise SystemExit(1) from None
