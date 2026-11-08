# enterprise-hrms

## Mimari ve teknolojiler

Oracle SQL/PLSQL ana kalıcı veri ve ortak veri bütünlüğü katmanıdır. FastAPI ana
REST API, Flask olay işleyen dahili servis, RabbitMQ asenkron mesajlaşma katmanıdır.
React, TypeScript, Redux Toolkit ve RTK Query arayüzü oluşturur. Docker Compose
servisleri birlikte çalıştırır. Git/GitHub sürüm kontrolüdür. Katmanları ayrı tut.

## Kalıcı çalışma politikası

1. İstenen kapsamı planla; rutin geliştirme, test ve hata düzeltmelerinde bağımsız ilerle.
2. Kullanıcının görev kapsamında verdiği kurulum, test, servis başlatma, commit ve
   normal push izinlerini alt görevlerde yeniden isteme. Bu izinleri başka görev,
   ortam veya production için sınırsız yetki olarak yorumlama.
3. Oracle canlı işlemleri yalnız görev için açıkça yetkilendirilmiş ortam ve kapsamda
   yapılır. Yerel geliştirme hedefi SESSION_USER=HR_CORE, CON_NAME=FREEPDB1,
   CURRENT_SCHEMA=HR_CORE olarak doğrulanmadan yazma yapılmaz.
4. Veri kaybına yol açabilecek DROP/TRUNCATE, geri dönüşsüz migration, production,
   Oracle container/volume silme, geniş kapsamlı belirsiz sistem değişikliği, force
   push, hard reset ve Git geçmişi değiştirme için ayrı açık onay al.
5. Gerçek TTY onayı, sandbox ve uygulama izin mekanizmaları korunur. Onay piped input,
   sahte terminal, otomatik giriş veya alternatif SQL yürütme yolu ile atlatılmaz.
6. SQL/PLSQL değişikliklerini sürümlü repository dosyalarında tut. Mevcut migration
   sözleşmesini koru; desteklenmeyen package DDL'sini CREATE TABLE parser'ına zorlama.
7. DDL implicit commit ve belirsiz commit sonucunda otomatik tekrar yapma; migration
   geçmişini elle düzeltme. Mevcut kayıtları koru; test verilerini açıkça ayırt et.
8. Şifre, token, .env içeriği, DSN ve özel anahtarları kaynak koduna, loglara,
   çıktılara veya Git'e ekleme. Gizli bilgileri dış servislere aktarmak ayrıca onay gerektirir.
9. İş kurallarının hangi katmanda güvenilir kaynak olduğunu açıkla. Ortak Oracle
   kuralları için PL/SQL package önceliklidir; gereksiz kural kopyalamadan kaçın.
10. Kod/dosya adları İngilizce, açıklamalar ve görev raporları Türkçe olsun.
11. Değişen alana uygun testleri çalıştır; canlı test yetkisini mock test izninden
    çıkarma. Başarılı kontrolleri sebepsiz tekrarlama; hataları gizleme.
12. Commit öncesi kapsam, gizli dosya, diff ve ilgili testleri kontrol et. Yalnız
    görev dosyalarını seçerek stage et. Push başarısızsa force/rebase ile aşma.
13. Mevcut Oracle container ve kalıcı verileri koru; ikinci bağımsız Oracle oluşturma.
14. Sonunda değişiklikleri, test/entegrasyon kanıtlarını ve kalan engelleri Türkçe özetle.
