-- HR_CORE şemasında migration geçmişini tutar.
-- Tekrar çalıştırmada mevcut tablo ve migration geçmişi korunmalıdır.
-- Tablo zaten varsa CREATE TABLE hata verir; tabloyu silmeyin veya boşaltmayın.
-- Mevcut yapıyı doğrulayıp bootstrap adımını atlayın; hatayı körlemesine yoksaymayın.

CREATE TABLE HR_CORE.SCHEMA_MIGRATIONS (
    VERSION         NUMBER(4, 0) NOT NULL,
    SCRIPT_NAME     VARCHAR2(255 CHAR) NOT NULL,
    CHECKSUM        VARCHAR2(64 CHAR) NOT NULL,
    STATUS          VARCHAR2(7 CHAR) NOT NULL,
    STARTED_AT      TIMESTAMP(6) WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    FINISHED_AT     TIMESTAMP(6) WITH TIME ZONE,
    ERROR_MESSAGE   CLOB,
    CONSTRAINT PK_SCHEMA_MIGRATIONS PRIMARY KEY (VERSION),
    CONSTRAINT CK_SCHEMA_MIGRATIONS_STATUS
        CHECK (STATUS IN ('RUNNING', 'SUCCESS', 'FAILED'))
);
