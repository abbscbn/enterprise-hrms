from ..core.errors import ApplicationError


def validate_target_session(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT SYS_CONTEXT('USERENV','SESSION_USER'), SYS_CONTEXT('USERENV','CON_NAME'), "
                       "SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL")
        if cursor.fetchone() != ('HR_CORE', 'FREEPDB1', 'HR_CORE'):
            raise ApplicationError()
