"""
Doküman Yönetim Sistemi (DYS) — Veritabanı Modelleri
======================================================
IATF 16949, ISO 14001, ISO 45001, ISO 27001 uyumlu
7 ana tablo:
  - User                  : Kullanıcı yönetimi
  - Process               : Süreç tanımları
  - Document              : Doküman ana kayıtları
  - DocumentRevision      : Revizyon geçmişi
  - DocumentApproval      : Onay iş akışı
  - DocumentDistribution  : Dağıtım kayıtları
  - AuditLog              : Erişim/değişiklik kayıtları
"""

import os
import json
from datetime import datetime, date

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float,
    ForeignKey, Integer, LargeBinary, String, Text, Unicode, JSON, UniqueConstraint,
    create_engine, event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, backref

# ── Sabitler ────────────────────────────────────────────────────────────────

KULLANICI_ROLLERI = ("Admin", "Doküman Kontrol", "Kullanıcı", "Sadece Görüntüleme")

DOKUMAN_TIPLERI = (
    "Politika", "El Kitabı", "Prosedür", "İş Akışı", "Talimat", "Görev Tanımı",
    "Plan", "Spesifikasyon", "Form", "Liste",
    "Dış Kaynaklı Doküman",
)

# 11 süreçlik yapı (2026): şirket numara biçimi "Süreç[.Alt] Harf NN" (ör. D04 P02, D04.5 F04).
# DYS_NUMARA_FORMATI=debak iken kullanılır; varsayılan (eski) biçim "D04-PR-001" değişmeden kalır.
DEBAK_TIP_HARFLERI = {
    "Politika": "K", "El Kitabı": "K", "Prosedür": "P", "İş Akışı": "A",
    "Talimat": "T", "Görev Tanımı": "GT", "Plan": "T", "Spesifikasyon": "T", "Form": "F", "Liste": "F",
}   # Görev Tanımı (2026): D01 GT01 … — talimatlardan ayrı numaralanır

DOKUMAN_SEVIYELERI = {
    1: "Politikalar & El Kitapları",
    2: "Prosedürler",
    3: "Talimatlar / Görev Tanımları / Planlar / Spesifikasyonlar",
    4: "Formlar / Kayıtlar / Listeler",
    5: "Dış Kaynaklı Dokümanlar",
}

TIP_KISALTMALARI = {
    "Politika": "PO",
    "El Kitabı": "EK",
    "Prosedür": "PR",
    "İş Akışı": "IA",
    "Talimat": "TL",
    "Görev Tanımı": "GT",
    "Plan": "PL",
    "Spesifikasyon": "SP",
    "Form": "FR",
    "Liste": "LS",
    "Dış Kaynaklı Doküman": "DK",
}

DOKUMAN_DURUMLARI = ("Taslak", "İncelemede", "Onaylı", "İptal", "Eskimiş")

GUVENLIK_SINIFLARI = ("Gizli", "Hizmete Özel", "Genel")

SUREC_KATEGORILERI = ("Destek", "Ana", "Yönetim")

ONAY_ADIMLARI = ("Hazırlama", "Kontrol", "Onay")
ONAY_DURUMLARI = ("Bekliyor", "Onaylandı", "Reddedildi")

ISLEM_TIPLERI = (
    "Görüntüleme", "İndirme", "Oluşturma", "Düzenleme",
    "Onay", "Red", "Silme", "Giriş", "Çıkış",
)

STANDARTLAR = (
    "IATF 16949",
    "ISO 14001",
    "ISO 45001",
    "ISO 27001",
)

# ── Standart Ana Maddeleri (Uyumluluk Matrisi referansı) ─────────────────────
# Plastik enjeksiyon sektörü için ilgili yönetim sistemi standart maddeleri.
STANDART_MADDELERI = {
    "IATF 16949": [
        ("4", "Kuruluşun bağlamı"),
        ("5", "Liderlik"),
        ("6", "Planlama (risk & fırsat)"),
        ("7", "Destek (kaynaklar, yetkinlik)"),
        ("8.3", "Ürün tasarımı ve geliştirme"),
        ("8.4", "Dış kaynaklı süreç kontrolü (tedarikçi)"),
        ("8.5", "Üretim ve hizmet sunumu (kalıp, proses)"),
        ("8.6", "Ürün serbest bırakma / PPAP"),
        ("8.7", "Uygun olmayan ürün kontrolü"),
        ("9", "Performans değerlendirme (izleme, ölçme)"),
        ("10", "İyileştirme (DÖF, 8D)"),
    ],
    "ISO 14001": [
        ("4", "Kuruluşun bağlamı"),
        ("6.1.2", "Çevre boyut ve etkileri"),
        ("6.1.3", "Uygunluk yükümlülükleri"),
        ("7", "Destek ve bilinç"),
        ("8", "Operasyonel kontrol (atık, kimyasal, geri dönüşüm)"),
        ("9", "Performans değerlendirme"),
        ("10", "İyileştirme"),
    ],
    "ISO 45001": [
        ("4", "Kuruluşun bağlamı"),
        ("5.4", "Çalışan katılımı ve danışma"),
        ("6.1.2", "Tehlike tanımlama ve risk değerlendirme"),
        ("7", "Destek ve yetkinlik"),
        ("8", "Operasyonel kontrol (İSG, acil durum)"),
        ("9", "Performans değerlendirme"),
        ("10", "İyileştirme (olay/kaza)"),
    ],
    "ISO 27001": [
        ("5", "Bilgi güvenliği politikaları"),
        ("6", "Organizasyon ve roller"),
        ("7", "İnsan kaynakları güvenliği"),
        ("8", "Varlık yönetimi ve sınıflandırma"),
        ("9", "Erişim kontrolü"),
        ("12", "İşletim güvenliği (yedekleme, loglama)"),
        ("16", "Bilgi güvenliği olay yönetimi"),
        ("18", "Uyum ve denetim"),
    ],
}

# ── Doküman Tipi Bazlı İçerik Şablonları ─────────────────────────────────────
# Plastik enjeksiyon firması + IATF 16949 / ISO 14001 / ISO 45001 / ISO 27001
# uyumlu doküman gövde şablonları. Her bölüm (anahtar, başlık, yer tutucu).
DOKUMAN_SABLONLARI = {
    "Politika": [
        ("amac", "Amaç ve Taahhüt", "Üst yönetimin kalite/çevre/İSG/bilgi güvenliği taahhüdünü belirtin."),
        ("kapsam", "Kapsam", "Politikanın uygulandığı faaliyet, saha ve ürün gruplarını yazın."),
        ("ilkeler", "Temel İlkeler", "Müşteri odaklılık, yasal uyum, sürekli iyileştirme ilkelerini maddeleyin."),
        ("hedefler", "Politika Hedefleri", "Ölçülebilir yönetim sistemi hedeflerini belirtin."),
        ("sorumluluklar", "Sorumluluklar", "Üst yönetim ve çalışan sorumluluklarını tanımlayın."),
        ("gozden_gecirme", "Gözden Geçirme", "Politikanın YGG'de gözden geçirilme sıklığını belirtin."),
    ],
    "El Kitabı": [
        ("amac", "Amaç", "El kitabının amacını ve kapsadığı yönetim sistemlerini yazın."),
        ("kapsam", "Kapsam ve Hariç Tutmalar", "Kapsam sınırlarını ve varsa hariç tutulan maddeleri belirtin."),
        ("kurulus_baglami", "Kuruluşun Bağlamı", "İç/dış hususlar ve ilgili taraf beklentilerini özetleyin."),
        ("surec_etkilesim", "Süreç Etkileşimi", "Ana/destek/yönetim süreçlerinin etkileşimini açıklayın."),
        ("sorumluluklar", "Sorumluluk ve Yetkiler", "Organizasyon ve süreç sahiplerini tanımlayın."),
        ("referanslar", "İlgili Standart ve Dokümanlar", "IATF 16949, ISO 14001/45001/27001 madde referanslarını yazın."),
    ],
    "Prosedür": [
        ("amac", "1.0 AMAÇ", "Prosedürün amacını yazın (hangi ihtiyacı karşıladığı)."),
        ("uygulama_alani", "2.0 UYGULAMA ALANI", "Kapsanan süreç, faaliyet, ürün ve sınırları yazın."),
        ("tanimlar", "3.0 TANIMLAR", "Terim ve kısaltmaları numaralandırarak tanımlayın (ör. 3.1 PPAP: …)."),
        ("sorumluluklar", "4.0 SORUMLULUKLAR", "Rolleri ve sorumluluklarını madde madde yazın."),
        ("uygulama_sekli", "5.0 UYGULAMA ŞEKLİ", "Uygulama adımlarını alt başlıklar (A, B, C…) ve numaralı maddelerle yazın."),
        ("kayitlar", "6.0 KAYITLARIN MUHAFAZASI", "Doküman listesinden arayıp kayıt formlarını seçin."),
        ("ilgili_dokumanlar", "7.0 İLGİLİ DOKÜMANLAR", "Doküman listesinden arayıp ilgili dokümanları seçin."),
    ],
    "İş Akışı": [
        ("amac", "Amaç", "İş akışının gösterdiği faaliyet zincirini yazın."),
        ("ilgili_prosedur", "İlgili Prosedür", "Akışın bağlı olduğu prosedür kodu (ör. D04 P02)."),
        ("akis", "Akış Adımları", "Md. | Adım | Faaliyet | Sorumlu | Doküman | Kayıt — her satıra bir adım."),
        ("notlar", "Notlar", "Akışa girmeyen kurallar, müşteri özel istekleri."),
    ],
    "Talimat": [
        ("amac", "1. Amaç", "Talimatın amacını yazın (ör. enjeksiyon makinesi ayarı)."),
        ("kapsam", "2. Kapsam", "Talimatın uygulandığı makine/istasyon/ürünü belirtin."),
        ("malzeme_ekipman", "3. Malzeme ve Ekipman", "Gerekli hammadde, kalıp, aparat ve ekipmanı listeleyin."),
        ("uygulama", "4. Uygulama Adımları", "İşlem basamaklarını sıra ile yazın (sıcaklık, basınç, çevrim süresi vb.)."),
        ("proses_parametreleri", "5. Proses Parametreleri", "Kritik proses parametrelerini ve tolerans aralıklarını belirtin."),
        ("isg_cevre", "6. İSG ve Çevre Notları", "Kişisel koruyucu donanım, atık/kimyasal ve tehlike uyarılarını yazın."),
        ("kalite_kontrol", "7. Kalite Kontrol", "Kontrol noktaları, ölçüm ve kabul kriterlerini belirtin."),
    ],
    "Plan": [
        ("amac", "1. Amaç", "Planın amacını ve kapsadığı ürün/proje ailesini yazın."),
        ("kapsam", "2. Kapsam", "Planın geçerli olduğu ürün, proses ve aşamaları belirtin."),
        ("kontrol_noktalari", "3. Kontrol Noktaları / Karakteristikler", "Ürün ve proses karakteristiklerini, özel karakteristikleri belirtin."),
        ("yontem_ekipman", "4. Yöntem ve Ekipman", "Ölçüm/analiz yöntemi, ekipman ve numune büyüklüğünü yazın."),
        ("reaksiyon_plani", "5. Reaksiyon Planı", "Uygunsuzluk halinde yapılacak aksiyonları tanımlayın."),
        ("sorumluluklar", "6. Sorumluluklar", "Plan uygulama ve izleme sorumlularını belirtin."),
    ],
    "Spesifikasyon": [
        ("amac", "1. Amaç", "Spesifikasyonun kapsadığı ürün/malzemeyi yazın."),
        ("teknik_gereksinimler", "2. Teknik Gereksinimler", "Boyut, malzeme (reçine tipi), renk, mekanik özellikleri belirtin."),
        ("kabul_kriterleri", "3. Kabul Kriterleri", "Kabul/ret sınırlarını ve tolerans değerlerini yazın."),
        ("test_yontemleri", "4. Test / Muayene Yöntemleri", "Uygulanacak test ve muayene yöntemlerini belirtin."),
        ("ilgili_standartlar", "5. İlgili Standart Maddeleri", "Bağlı standart ve müşteri özel gerekliliklerini yazın."),
    ],
    "Form": [
        ("amac", "Amaç", "Formun hangi kayıt için kullanıldığını yazın."),
        ("kullanim", "Kullanım Talimatı", "Formun nasıl ve ne zaman doldurulacağını açıklayın."),
        ("alanlar", "Alan Açıklamaları", "Formdaki alanların ne anlama geldiğini tanımlayın."),
        ("saklama", "Saklama ve Arşivleme", "Kaydın saklama süresi ve arşiv yöntemini belirtin."),
    ],
    "Liste": [
        ("amac", "Amaç", "Listenin amacını yazın (ör. onaylı tedarikçi listesi)."),
        ("kapsam", "Kapsam", "Listenin kapsadığı öğeleri belirtin."),
        ("guncelleme", "Güncelleme Kuralları", "Listenin güncellenme sıklığı ve sorumlusunu yazın."),
    ],
    "Dış Kaynaklı Doküman": [
        ("kaynak", "Kaynak Kurum / Referans", "Dokümanın kaynağını (müşteri, standart kuruluşu vb.) yazın."),
        ("gecerlilik", "Geçerlilik ve Sürüm", "Sürüm/baskı bilgisi ve geçerlilik tarihini belirtin."),
        ("kullanim_alani", "Kullanım Alanı", "Firmada hangi süreçte kullanıldığını yazın."),
        ("kontrol_yontemi", "Güncellik Kontrol Yöntemi", "Güncelliğin nasıl takip edildiğini açıklayın."),
    ],
}

# ── Ek Sabitler: Yeni Uyum Modülleri ────────────────────────────────────────
CAPA_KAYNAKLARI = (
    "İç Tetkik", "Müşteri Şikayeti", "Saha İade",
    "Garanti Talebi", "Denetim", "Uygunsuzluk",
    "Öneri", "Performans / KPI", "QRQC", "Diğer",
)
CAPA_DURUMLARI = ("Açık", "Devam Ediyor", "Tamamlandı", "Etkinlik Kontrolünde", "Kapatıldı")

# QRQC (Hızlı Yanıt Kalite Kontrolü) Sabitleri
QRQC_DURUMLARI = ("Açık", "Geçici Aksiyonda", "Kapatıldı", "DÖF Tetiklendi")
QRQC_KATEGORILERI = ("Kalite", "Güvenlik", "Lojistik", "Bakım", "İSG", "Yöntem")
QRQC_SEVIYELERI = ("Hat / Saha", "Fabrika / Yönetim", "Müşteri / Garanti")
QRQC_VARDIYALAR = ("1. Vardiya", "2. Vardiya", "3. Vardiya")


# Poka-Yoke sabitleri (IATF 16949 § 10.2.4)
POKAYOKE_DURUMLARI = ("Aktif", "Test Başarısız", "Devre Dışı", "Bakımda")
POKAYOKE_TIPLERI = ("Mekanik", "Elektrik/Sensör", "Yazılım", "Görsel", "Diğer")
POKAYOKE_TEST_SONUCLARI = ("Başarılı", "Başarısız")

# Vardiya sabitleri (IATF 16949 § 9.2.2.3)
VARDIYALAR = ("1. Vardiya", "2. Vardiya", "3. Vardiya", "Tüm Vardiyalar")

AUDIT_DURUMLARI = ("Planlandı", "Devam Ediyor", "Tamamlandı", "Kapatıldı")
AUDIT_TIPLERI = ("Sistem Denetimi", "Proses Denetimi", "Ürün Denetimi")
AUDIT_CEVAPLARI = ("Uygun", "Uygunsuz", "Kısmen Uygun", "N/A", "Değerlendirilmedi")
AUDIT_KANIT_TIPLERI = ("Ölçüm Raporu", "Talimat", "Kayıt", "Prosedür", "Fotoğraf", "Form", "Diğer")
BULGU_TIPLERI = ("Uygunsuzluk (Majör)", "Uygunsuzluk (Minör)", "Gözlem", "İyileştirme Fırsatı", "Olumlu")

EGITIM_TIPLERI = ("Oryantasyon", "İSG", "Kalite", "Çevre", "Teknik", "Bilgi Güvenliği", "Diğer")

# ── Faz 8: DÖF kök neden yöntemi ve yetkinlik matrisi sabitleri ───────────────
DOF_YONTEMLERI = ("5 Neden", "Ishikawa (Balık Kılçığı)", "8D", "A3", "Diğer")
YETKINLIK_KAPSAM_TIPLERI = ("Rol", "Departman", "Süreç")

RISK_KATEGORILERI = ("Kalite", "Çevre", "İSG", "Bilgi Güvenliği")
RISK_DURUMLARI = ("Açık", "Aksiyon Planlandı", "Devam Ediyor", "Kapatıldı")

DEGISIKLIK_KATEGORILERI = ("Editöryal", "İçerik", "Kritik")

PPAP_ONAY_ADIMLARI = ("Kalite", "Mühendislik", "Müşteri Temsilcisi")

# ── IMS Ortak Çekirdeği Sabitleri ────────────────────────────────────────────
# Gereklilik uygulama durumu (kanıt temelli uyumluluk matrisi için).
IMS_UYGULAMA_DURUMLARI = ("Uygulanmadı", "Planlandı", "Kısmen Uygulandı", "Uygulandı")
# Kontrol/gereklilik etkinlik değerlendirmesi.
IMS_ETKINLIK_DURUMLARI = ("Değerlendirilmedi", "Etkisiz", "Kısmen Etkili", "Etkili")
# Uygulanabilirlik kararı (SoA mantığıyla uyumlu).
IMS_UYGULANABILIRLIK = ("Uygulanabilir", "Uygulanamaz")
# Kanıt türleri.
KANIT_TIPLERI = ("Doküman", "Kayıt", "Ölçüm/Test", "Fotoğraf", "Tetkik Bulgusu",
                 "Eğitim", "Sözleşme", "Diğer")
# Kuruluş bağlamı hususu türü.
BAGLAM_TURLERI = ("İç Husus", "Dış Husus")
# Hedef durumu.
HEDEF_DURUMLARI = ("Açık", "Devam Ediyor", "Ulaşıldı", "Ulaşılamadı", "İptal")
# Süreç performans KPI ölçüm sonucu
KPI_OLCUM_DURUMLARI = ("Uyumlu", "Uyumsuz", "İzleme", "Girilmedi")
# Yönetimin gözden geçirmesi durumu.
YGG_DURUMLARI = ("Planlandı", "Yapıldı", "Aksiyonlar Takipte", "Kapatıldı")
# Yasal/mevzuat uygunluk durumu.
UYGUNLUK_DURUMLARI = ("Değerlendirilmedi", "Uygun", "Kısmen Uygun", "Uygun Değil")
# Yasal gereklilik türü.
YASAL_TURLERI = ("Çevre", "İSG", "Kalite", "Bilgi Güvenliği", "Genel")

# Kayıt imha talebi durumları (çift onaylı imha akışı — Faz 3).
IMHA_DURUMLARI = ("Talep Edildi", "Onaylandı", "İmha Edildi", "Reddedildi")

# ── IATF 16949 Modül Sabitleri (Faz 4) ───────────────────────────────────────
TEDARIKCI_DURUMLARI = ("Aday", "Onaylı", "Şartlı", "Askıda", "Reddedildi")
TEDARIKCI_KATEGORILERI = ("Hammadde", "Yarı Mamul", "Hizmet", "Kalıp/Aparat", "Lojistik", "Diğer")
APQP_FAZLARI = {
    1: "Planlama ve Program Tanımı",
    2: "Ürün Tasarımı ve Geliştirme",
    3: "Proses Tasarımı ve Geliştirme",
    4: "Ürün ve Proses Doğrulama",
    5: "Geri Besleme, Değerlendirme ve Düzeltici Faaliyet",
}
APQP_DURUMLARI = ("Planlandı", "Devam Ediyor", "Gecikmeli", "Tamamlandı", "İptal")
KAPI_DURUMLARI = ("Bekliyor", "Onaylandı", "Şartlı", "Reddedildi")
FMEA_TIPLERI = ("DFMEA", "PFMEA")
# VDA/AIAG FMEA Handbook 2019 — Action Priority seviyeleri (RPN yerine)
AP_SEVIYELERI = ("H", "M", "L")  # High, Medium, Low
# 7-Adım FMEA yaklaşımı
FMEA_ADIMLARI = (
    "1-Planlama", "2-Yapı Analizi", "3-Fonksiyon Analizi",
    "4-Hata Analizi", "5-Risk Analizi", "6-Optimizasyon",
    "7-Dokümantasyon",
)
FMEA_AKSIYON_DURUMLARI = ("Açık", "Devam Ediyor", "Tamamlandı", "İptal")
OZEL_KARAKTERISTIK_TIPLERI = ("CC", "SC", "HI", "YC", "Yok")
KONTROL_PLANI_TIPLERI = ("Prototip", "Ön Üretim", "Üretim")
KALIBRASYON_DURUMLARI = ("Geçerli", "Süresi Doldu", "Kalibrasyonda", "Kullanım Dışı", "Arızalı", "Pasif")
UOU_KARARLARI = ("Karantina", "Yeniden İşleme", "Tamir", "Taviz (Sapma)", "Hurda", "İade")
UOU_DURUMLARI = ("Açık", "Karar Verildi", "Kapatıldı")
CSR_DURUMLARI = ("Açık", "Karşılandı", "İzleniyor", "Kapatıldı")

# ── ISO 14001 Çevre Modülü Sabitleri (Faz 5) ─────────────────────────────────
CEVRE_KOSULLARI = ("Normal", "Anormal", "Acil Durum")
CEVRE_BOYUT_TURLERI = ("Atık", "Emisyon", "Su Deşarjı", "Enerji Tüketimi",
                       "Kimyasal Kullanımı", "Gürültü", "Doğal Kaynak", "Diğer")
CEVRE_OLAY_TURLERI = ("Sızıntı/Dökülme", "Emisyon Aşımı", "Atık Uygunsuzluğu",
                      "Şikayet", "Tatbikat", "Diğer")

# ── ISO 45001 İSG Modülü Sabitleri (Faz 6) ───────────────────────────────────
ISG_KONTROL_HIYERARSISI = ("Eleme", "İkame", "Mühendislik Kontrolü",
                           "İdari Kontrol", "KKD")
ISG_OLAY_TURLERI = ("Kaza", "Ramak Kala", "Meslek Hastalığı", "Çevresel", "Tatbikat")
ISG_KATILIM_TURLERI = ("Öneri", "Danışma", "Kurul Kararı", "Geri Bildirim", "Şikayet")
ISG_RISK_DURUMLARI = ("Açık", "Kontrol Altında", "Kapatıldı")
KKD_DURUMLARI = ("Verildi", "İade")
KKD_BIRIMLERI = ("Adet", "Çift", "Takım", "Litre")

# ── ISO 27001 ISMS Modülü Sabitleri (Faz 7) ──────────────────────────────────
CIA_SEVIYELERI = ("Düşük", "Orta", "Yüksek")
VERI_SINIFLARI = ("Genel", "İç Kullanım", "Gizli", "Çok Gizli")
SOA_DURUMLARI = ("Uygulanmadı", "Kısmen", "Uygulandı")
ISMS_TEDAVI_SECENEKLERI = ("Azalt", "Kabul Et", "Aktar", "Kaçın")
ISMS_OLAY_TURLERI = ("Yetkisiz Erişim", "Veri Sızıntısı", "Kötücül Yazılım",
                     "Kimlik Avı", "Hizmet Kesintisi", "Fiziksel", "Diğer")
YEDEK_TIPLERI = ("Tam", "Artımlı", "Dosya")
AUDIT_PROGRAM_DURUMLARI = ("Planlandı", "Devam Ediyor", "Tamamlandı")
AUDIT_PROGRAM_ITEM_DURUMLARI = ("Planlandı", "Atandı", "Tamamlandı", "İptal")


# ── SQLAlchemy Temelleri ────────────────────────────────────────────────────
# config dotenv'i yükler; DATABASE_URL tek kaynaktan gelir.
from config import Config  # noqa: E402

DATABASE_URL = Config.DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")
_is_mssql = DATABASE_URL.startswith("mssql")
_engine_kwargs = {"echo": False, "future": True, "pool_pre_ping": True}
if _is_sqlite:
    # Aynı SQLite bağlantısının birden çok thread'de kullanılabilmesi + zaman aşımı
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
elif _is_mssql:
    _engine_kwargs["pool_size"] = int(os.environ.get("DYS_DB_POOL_SIZE", "5"))
    _engine_kwargs["max_overflow"] = int(os.environ.get("DYS_DB_MAX_OVERFLOW", "10"))

engine = create_engine(DATABASE_URL, **_engine_kwargs)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        """WAL modu ve foreign key zorlaması ile eşzamanlı erişimi iyileştirir."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()


SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ═══════════════════════════════════════════════════════════════════════════
#  1) Users — Kullanıcılar
# ═══════════════════════════════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad_soyad = Column(String(100), nullable=False)
    eposta = Column(String(100), nullable=False, unique=True)
    sifre_hash = Column(String(200), nullable=False)
    departman = Column(String(50), nullable=False)
    unvan = Column(String(50), nullable=False)
    rol = Column(String(30), nullable=False, default="Kullanıcı")
    aktif = Column(Boolean, default=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    # Giriş güvenliği (kaba kuvvet saldırısına karşı geçici kilitleme)
    basarisiz_giris_sayisi = Column(Integer, nullable=False, default=0)
    kilit_tarihi = Column(DateTime, nullable=True)

    # İlişkiler
    hazirlanan_dokumanlar = relationship(
        "Document", foreign_keys="Document.hazirlayan_id", back_populates="hazirlayan"
    )
    kontrol_edilen_dokumanlar = relationship(
        "Document", foreign_keys="Document.kontrol_eden_id", back_populates="kontrol_eden"
    )
    onaylanan_dokumanlar = relationship(
        "Document", foreign_keys="Document.onaylayan_id", back_populates="onaylayan"
    )

    def __repr__(self):
        return f"<User(id={self.id}, ad_soyad='{self.ad_soyad}', rol='{self.rol}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  2) Processes — Süreçler
# ═══════════════════════════════════════════════════════════════════════════
class Process(Base):
    __tablename__ = "processes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kod = Column(String(10), nullable=False, unique=True)         # D01, M01, Y01
    ad = Column(String(150), nullable=False)
    kategori = Column(String(20), nullable=False)                 # Destek / Ana / Yönetim
    ilgili_standartlar = Column(Text, nullable=True)              # Virgülle ayrılmış
    sorumlu = Column(String(100), nullable=True)
    aciklama = Column(Text, nullable=True)
    # 11 süreçlik yapı (2026) — geriye dönük uyumlu, boş bırakılabilir kolonlar:
    # alt süreçler (D04.5 vb.) üst süreçlerine bağlanır; eski 16 süreç silinmez, aktif=False yapılır.
    ust_surec_id = Column(Integer, ForeignKey("processes.id"), nullable=True)
    aktif = Column(Boolean, nullable=True, default=True)

    # İlişki
    documents = relationship("Document", back_populates="surec", lazy="selectin")
    ust_surec = relationship("Process", remote_side=[id], backref="alt_surecler")

    @property
    def aktif_mi(self):
        return self.aktif is None or bool(self.aktif)

    def __repr__(self):
        return f"<Process(kod='{self.kod}', ad='{self.ad}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  3) Documents — Dokümanlar
# ═══════════════════════════════════════════════════════════════════════════
class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dokuman_no = Column(String(30), nullable=False, unique=True)  # D06-PR-001
    baslik = Column(String(200), nullable=False)
    surec_id = Column(Integer, ForeignKey("processes.id"), nullable=False)
    dokuman_tipi = Column(String(30), nullable=False)             # Prosedür, Talimat, Form...
    dokuman_seviyesi = Column(Integer, nullable=False, default=2)  # 1-5
    guvenlik_sinifi = Column(String(20), nullable=False, default="Genel")

    # İlgili standart maddeleri (JSON dizisi olarak saklayacağız string'le)
    ilgili_standartlar = Column(Text, nullable=True)              # "IATF 16949 - 7.5, ISO 14001 - 7.5"

    durum = Column(String(20), nullable=False, default="Taslak")
    hazirlayan_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    kontrol_eden_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    onaylayan_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    yururluk_tarihi = Column(Date, nullable=True)
    sonraki_gozden_gecirme = Column(Date, nullable=True)
    saklama_suresi_ay = Column(Integer, nullable=True, default=36)

    # Yaşam döngüsü (Faz 3): iptal/eskime ve saklama/legal hold
    iptal_nedeni = Column(Text, nullable=True)
    iptal_tarihi = Column(Date, nullable=True)
    # MSSQL: self-FK + ON DELETE SET NULL → multiple cascade paths (1785); uygulama NULL yapar
    yerine_gecen_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    legal_hold = Column(Boolean, default=False)         # yasal saklama: imha engellenir

    revizyon_no = Column(Integer, nullable=False, default=0)
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)

    # Tip bazlı şablon içeriği (JSON string: {"bolum_anahtari": "metin"})
    icerik_json = Column(Text, nullable=True)
    govde_html = Column(Text, nullable=True)                      # 2026 — dosyadan çıkarılan okunur içerik (HTML; icerik_cikar.py)
    govde_not = Column(Unicode(300), nullable=True)               # çıkarma notu (kesildi / okunamadı …)

    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # İlişkiler
    surec = relationship("Process", back_populates="documents")
    hazirlayan = relationship("User", foreign_keys=[hazirlayan_id], back_populates="hazirlanan_dokumanlar")
    kontrol_eden = relationship("User", foreign_keys=[kontrol_eden_id], back_populates="kontrol_edilen_dokumanlar")
    onaylayan = relationship("User", foreign_keys=[onaylayan_id], back_populates="onaylanan_dokumanlar")

    revisions = relationship("DocumentRevision", back_populates="document", cascade="all, delete-orphan", lazy="selectin")
    approvals = relationship("DocumentApproval", back_populates="document", cascade="all, delete-orphan", lazy="selectin")
    distributions = relationship("DocumentDistribution", back_populates="document", cascade="all, delete-orphan", lazy="selectin")

    def __repr__(self):
        return f"<Document(no='{self.dokuman_no}', baslik='{self.baslik}', durum='{self.durum}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  4) DocumentRevisions — Revizyon Geçmişi
# ═══════════════════════════════════════════════════════════════════════════
class DocumentRevision(Base):
    __tablename__ = "document_revisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    revizyon_no = Column(Integer, nullable=False)
    degisiklik_aciklamasi = Column(Text, nullable=True)
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    dosya_boyutu = Column(Float, nullable=True)                   # KB cinsinden
    hazirlayan_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    tarih = Column(DateTime, default=datetime.now)
    # Onay durumu: Bekliyor / Onaylandı / Reddedildi
    # (Reddedilen revizyon talebini geri almak için kullanılır; NULL = geçmiş/onaylı kayıt)
    onay_durumu = Column(String(20), nullable=True, default="Bekliyor")

    # Bütünlük: onaylandığında dosyanın SHA-256 özeti (inkar edilemezlik/doğrulama)
    icerik_hash = Column(String(64), nullable=True)

    # Değişiklik Yönetimi (MOC): revizyonun sınıfı ve etki değerlendirmesi
    degisiklik_kategorisi = Column(String(20), nullable=True)     # Editöryal / İçerik / Kritik
    etki_degerlendirmesi = Column(Text, nullable=True)

    # İlişkiler
    document = relationship("Document", back_populates="revisions")
    hazirlayan = relationship("User")

    def __repr__(self):
        return f"<Revision(doc_id={self.document_id}, rev={self.revizyon_no})>"


# ═══════════════════════════════════════════════════════════════════════════
#  5) DocumentApprovals — Onay İş Akışı
# ═══════════════════════════════════════════════════════════════════════════
class DocumentApproval(Base):
    __tablename__ = "document_approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    onay_adimi = Column(String(20), nullable=False)               # Hazırlama / Kontrol / Onay
    kullanici_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    durum = Column(String(20), nullable=False, default="Bekliyor") # Bekliyor / Onaylandı / Reddedildi
    aciklama = Column(Text, nullable=True)
    tarih = Column(DateTime, default=datetime.now)
    # Aktif onay çevrimini gruplar (aynı dokümanın tekrarlanan onay turlarını ayırır).
    workflow_no = Column(Integer, nullable=False, default=1)

    # İlişkiler
    document = relationship("Document", back_populates="approvals")
    kullanici = relationship("User")

    def __repr__(self):
        return f"<Approval(doc_id={self.document_id}, adim='{self.onay_adimi}', durum='{self.durum}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  6) DocumentDistributions — Dağıtım Kayıtları
# ═══════════════════════════════════════════════════════════════════════════
class DocumentDistribution(Base):
    __tablename__ = "document_distributions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    kullanici_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    dagitim_tarihi = Column(DateTime, default=datetime.now)
    okundu_mu = Column(Boolean, default=False)
    okunma_tarihi = Column(DateTime, nullable=True)

    # İlişkiler
    document = relationship("Document", back_populates="distributions")
    kullanici = relationship("User")

    def __repr__(self):
        return f"<Distribution(doc_id={self.document_id}, user_id={self.kullanici_id})>"


# ═══════════════════════════════════════════════════════════════════════════
#  7) AuditLog — Denetim / Erişim Kayıtları
# ═══════════════════════════════════════════════════════════════════════════
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kullanici_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    islem_tipi = Column(String(30), nullable=False)               # Görüntüleme, İndirme, Düzenleme...
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    detay = Column(Text, nullable=True)
    ip_adresi = Column(String(50), nullable=True)
    tarih = Column(DateTime, default=datetime.now)

    # Değiştirilemez (append-only) hash zinciri: tahrifat tespiti (Faz 3).
    onceki_hash = Column(String(64), nullable=True)
    kayit_hash = Column(String(64), nullable=True)

    # İlişkiler
    kullanici = relationship("User")
    document = relationship("Document")

    def __repr__(self):
        return f"<AuditLog(islem='{self.islem_tipi}', tarih={self.tarih})>"


# ═══════════════════════════════════════════════════════════════════════════
#  8) PPAPSubmission — PPAP Sunum Ana Kaydı
# ═══════════════════════════════════════════════════════════════════════════
PPAP_DURUMLARI = ("Hazırlanıyor", "İncelemede", "Onaylandı", "Reddedildi", "Koşullu Onay")

PPAP_SUNUM_SEVIYELERI = {
    1: "Sadece PSW (ve AAR) müşteriye sunulur",
    2: "PSW + numune parçalar + sınırlı destek verisi",
    3: "PSW + numune parçalar + tam destek verisi (varsayılan)",
    4: "PSW + müşterinin belirlediği diğer gereksinimler",
    5: "PSW + numune + tam destek verisi, tedarikçi lokasyonunda inceleme",
}

PPAP_ELEMENTLERI = [
    (1, "Design Records", "Tasarım Kayıtları"),
    (2, "Engineering Change Documents", "Mühendislik Değişiklik Dokümanları"),
    (3, "Customer Engineering Approval", "Müşteri Mühendislik Onayı"),
    (4, "Design FMEA", "Tasarım FMEA"),
    (5, "Process Flow Diagram", "Proses Akış Diyagramı"),
    (6, "Process FMEA", "Proses FMEA"),
    (7, "Control Plan", "Kontrol Planı"),
    (8, "MSA Studies", "Ölçüm Sistemi Analizi (MSA)"),
    (9, "Dimensional Results", "Boyutsal Sonuçlar"),
    (10, "Material / Performance Tests", "Malzeme / Performans Testleri"),
    (11, "Initial Process Studies", "İlk Proses Yeterlilik Çalışmaları"),
    (12, "Qualified Laboratory Documentation", "Akredite Laboratuvar Dokümanları"),
    (13, "Appearance Approval Report (AAR)", "Görünüm Onay Raporu"),
    (14, "Sample Production Parts", "Numune Üretim Parçaları"),
    (15, "Master Sample", "Master Numune"),
    (16, "Checking Aids", "Kontrol Aparatları"),
    (17, "Customer-Specific Requirements", "Müşteriye Özgü Gereklilikler"),
    (18, "Part Submission Warrant (PSW)", "Parça Sunum Garanti Belgesi (PSW)"),
]

PPAP_ELEMENT_DURUMLARI = ("Hazırlanmadı", "Hazırlanıyor", "Tamamlandı", "Uygulanamaz")


class PPAPSubmission(Base):
    __tablename__ = "ppap_submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parca_no = Column(String(50), nullable=False)
    parca_adi = Column(String(200), nullable=False)
    musteri = Column(String(150), nullable=False)
    sunum_seviyesi = Column(Integer, nullable=False, default=3)          # 1-5
    durum = Column(String(30), nullable=False, default="Hazırlanıyor")   # PPAP_DURUMLARI
    revizyon_nedeni = Column(Text, nullable=True)
    notlar = Column(Text, nullable=True)
    sunum_tarihi = Column(Date, nullable=True)
    onay_tarihi = Column(Date, nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    surec_id = Column(Integer, ForeignKey("processes.id"), nullable=True)  # M02
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # İlişkiler
    sorumlu = relationship("User")
    surec = relationship("Process")
    elements = relationship("PPAPElement", back_populates="ppap", cascade="all, delete-orphan", lazy="selectin")
    approvals = relationship("PPAPApproval", back_populates="ppap", cascade="all, delete-orphan", lazy="selectin")

    @property
    def tamamlanma_orani(self):
        """18 elementten kaçının tamamlandığını yüzde olarak döndürür."""
        if not self.elements:
            return 0
        uygulanabilir = [e for e in self.elements if e.durum != "Uygulanamaz"]
        if not uygulanabilir:
            return 100
        tamamlanan = [e for e in uygulanabilir if e.durum == "Tamamlandı"]
        return round(len(tamamlanan) / len(uygulanabilir) * 100)

    def __repr__(self):
        return f"<PPAPSubmission(parca='{self.parca_no}', musteri='{self.musteri}', durum='{self.durum}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  9) PPAPElement — PPAP Element Durumları
# ═══════════════════════════════════════════════════════════════════════════
class PPAPElement(Base):
    __tablename__ = "ppap_elements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ppap_id = Column(Integer, ForeignKey("ppap_submissions.id", ondelete="CASCADE"), nullable=False)
    element_no = Column(Integer, nullable=False)                         # 1-18
    element_adi_en = Column(String(100), nullable=False)
    element_adi_tr = Column(String(100), nullable=False)
    durum = Column(String(20), nullable=False, default="Hazırlanmadı")   # PPAP_ELEMENT_DURUMLARI
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    notlar = Column(Text, nullable=True)
    # Element, ayrı kopya yerine kontrollü bir Document'a bağlanabilir
    ilgili_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # İlişki
    ppap = relationship("PPAPSubmission", back_populates="elements")
    ilgili_document = relationship("Document")

    def __repr__(self):
        return f"<PPAPElement(ppap_id={self.ppap_id}, no={self.element_no}, durum='{self.durum}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  10) PPAPApproval — PPAP Çok Adımlı Onay
# ═══════════════════════════════════════════════════════════════════════════
class PPAPApproval(Base):
    __tablename__ = "ppap_approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ppap_id = Column(Integer, ForeignKey("ppap_submissions.id", ondelete="CASCADE"), nullable=False)
    onay_adimi = Column(String(30), nullable=False)               # Kalite / Mühendislik / Müşteri Temsilcisi
    kullanici_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    durum = Column(String(20), nullable=False, default="Bekliyor")  # Bekliyor / Onaylandı / Reddedildi
    aciklama = Column(Text, nullable=True)
    tarih = Column(DateTime, default=datetime.now)

    ppap = relationship("PPAPSubmission", back_populates="approvals")
    kullanici = relationship("User")


# ═══════════════════════════════════════════════════════════════════════════
#  11) CorrectiveAction — DÖF / Düzeltici-Önleyici Faaliyet (CAPA)
# ═══════════════════════════════════════════════════════════════════════════
class CorrectiveAction(Base):
    __tablename__ = "corrective_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dof_no = Column(String(30), nullable=False, unique=True)      # DÖF-2026-001
    baslik = Column(String(200), nullable=False)
    kaynak_tipi = Column(String(30), nullable=False, default="Uygunsuzluk")
    ilgili_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    ilgili_surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    tespit_tarihi = Column(Date, nullable=True)
    acan_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    tanim = Column(Text, nullable=True)                           # Uygunsuzluk tanımı
    kok_neden = Column(Text, nullable=True)
    kok_neden_yontemi = Column(String(40), nullable=True)         # DOF_YONTEMLERI
    duzeltici_faaliyet = Column(Text, nullable=True)
    onleyici_faaliyet = Column(Text, nullable=True)
    planlanan_tarih = Column(Date, nullable=True)
    durum = Column(String(30), nullable=False, default="Açık")    # CAPA_DURUMLARI
    etkinlik_kontrolu = Column(Text, nullable=True)
    kapanma_tarihi = Column(Date, nullable=True)
    # 8D adımları (kok_neden_yontemi == "8D" iken doldurulur)
    d1_team = Column(Text, nullable=True)
    d2_problem = Column(Text, nullable=True)
    d3_containment = Column(Text, nullable=True)
    d4_root_cause = Column(Text, nullable=True)
    d5_corrective = Column(Text, nullable=True)
    d6_implement = Column(Text, nullable=True)
    d7_prevent = Column(Text, nullable=True)
    d8_congratulate = Column(Text, nullable=True)
    etkinlik_bekleme_gunu = Column(Integer, nullable=False, default=30)
    yeniden_acildi = Column(Boolean, nullable=False, default=False)
    # IATF 16949 § 10.2.3: DÖF → PFMEA/CP güncelleme bağlantısı
    fmea_guncellendi = Column(Boolean, nullable=False, default=False)
    cp_guncellendi = Column(Boolean, nullable=False, default=False)
    ilgili_fmea_id = Column(Integer, ForeignKey("fmeas.id", ondelete="SET NULL"), nullable=True)
    ilgili_cp_id = Column(Integer, ForeignKey("control_plans.id", ondelete="SET NULL"), nullable=True)
    benzer_analiz = Column(Text, nullable=True)                   # "Bu hata başka nerede olabilir?"
    containment_aksiyonu = Column(Text, nullable=True)            # tüm yöntemler için ara önlem
    # Müşteri şikayeti / Saha İade / Garanti ek alanları
    sikayet_musteri = Column(String(200), nullable=True)
    # 2026 — Performans (KPI) modülündeki NCR ile bağ (boş bırakılabilir)
    dis_kimlik = Column(String(40), nullable=True)          # ör. ncr-M04-001
    aksiyonlar_json = Column(Text, nullable=True)           # [{tur, aciklama, sorumlu, termin, durum, tamamlanma}]
    sikayet_parca_no = Column(String(100), nullable=True)
    sikayet_miktar = Column(Integer, nullable=True)
    sikayet_aciliyet = Column(String(20), nullable=True)          # Yüksek/Orta/Düşük
    garanti_talebi_mi = Column(Boolean, nullable=False, default=False)
    saha_iade_mi = Column(Boolean, nullable=False, default=False)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    ilgili_document = relationship("Document")
    ilgili_surec = relationship("Process")
    acan = relationship("User", foreign_keys=[acan_id])
    sorumlu = relationship("User", foreign_keys=[sorumlu_id])
    ilgili_fmea = relationship("FMEA")
    ilgili_cp = relationship("ControlPlan")

    @property
    def gecikti_mi(self):
        return (
            self.durum not in ("Kapatıldı",)
            and self.planlanan_tarih is not None
            and self.planlanan_tarih < date.today()
        )





# ═══════════════════════════════════════════════════════════════════════════
#  11.5) QRQCItem — Quick Response Quality Control (Hızlı Yanıt Kalite Kontrolü)
# ═══════════════════════════════════════════════════════════════════════════
class QRQCItem(Base):
    __tablename__ = "qrqc_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    qrqc_no = Column(String(30), nullable=False, unique=True)        # QRQC-2026-001
    baslik = Column(String(200), nullable=False)
    kategori = Column(String(40), nullable=False, default="Kalite")  # QRQC_KATEGORILERI
    seviye = Column(String(30), nullable=False, default="Hat / Saha") # QRQC_SEVIYELERI
    hat_istasyon = Column(String(100), nullable=True)                # ör. Enjeksiyon-04
    vardiya = Column(String(20), nullable=True)                      # QRQC_VARDIYALAR
    tespit_tarihi = Column(DateTime, default=datetime.now)
    bildiren_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    problem_tanimi = Column(Text, nullable=True)                     # 5W2H açıklaması
    etkilenen_miktar = Column(Integer, nullable=True, default=0)
    gecici_onlem = Column(Text, nullable=True)                       # 24 saatlik acil müdahale / karantina
    kok_neden = Column(Text, nullable=True)                          # 5 Neden özeti
    kalici_aksiyon = Column(Text, nullable=True)
    durum = Column(String(30), nullable=False, default="Açık")       # QRQC_DURUMLARI
    dof_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    hedef_kapanis_tarihi = Column(Date, nullable=True)
    kapanis_tarihi = Column(DateTime, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    bildiren = relationship("User", foreign_keys=[bildiren_id])
    sorumlu = relationship("User", foreign_keys=[sorumlu_id])
    surec = relationship("Process")
    dof = relationship("CorrectiveAction", foreign_keys=[dof_id], backref=backref("qrqc_records", lazy="selectin"))

    @property
    def gecikti_mi(self):
        if self.durum == "Kapatıldı" or not self.hedef_kapanis_tarihi:
            return False
        return self.hedef_kapanis_tarihi < date.today()

    @property
    def mudahele_suresi_astimi(self):
        """24 saat içerisinde geçici önlem alınmamış açık kaydı kontrol eder."""
        if self.durum == "Kapatıldı" or self.gecici_onlem:
            return False
        gecen = datetime.now() - (self.tespit_tarihi or self.olusturma_tarihi or datetime.now())
        return gecen > timedelta(hours=24)

    def __repr__(self):
        return f"<QRQCItem(no='{self.qrqc_no}', baslik='{self.baslik}', durum='{self.durum}')>"


# ═══════════════════════════════════════════════════════════════════════════
#  12) InternalAudit + AuditFinding — İç Tetkik
# ═══════════════════════════════════════════════════════════════════════════
class InternalAudit(Base):
    __tablename__ = "internal_audits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tetkik_no = Column(String(30), nullable=False, unique=True)   # IT-2026-001
    baslik = Column(String(200), nullable=False)
    denetim_tipi = Column(String(30), nullable=False, default="Sistem Denetimi")  # AUDIT_TIPLERI
    planlanan_tarih = Column(Date, nullable=True)
    gerceklesen_tarih = Column(Date, nullable=True)
    tetkik_eden_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # IATF 16949 § 9.2.2.3: Tüm vardiyaların tetkik edilmesi
    vardiya = Column(String(30), nullable=True)                   # VARDIYALAR
    fmea_referans_id = Column(Integer, ForeignKey("fmeas.id", ondelete="SET NULL"), nullable=True)
    cp_referans_id = Column(Integer, ForeignKey("control_plans.id", ondelete="SET NULL"), nullable=True)
    denetlenen_surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    ilgili_standart = Column(String(30), nullable=True)
    urun_adi = Column(String(200), nullable=True)                 # Ürün Denetimi için
    kapsam = Column(Text, nullable=True)
    sonuc_ozeti = Column(Text, nullable=True)
    durum = Column(String(20), nullable=False, default="Planlandı")  # AUDIT_DURUMLARI
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    # 2026 — otomatik denetim (boş bırakılabilir; eski sürüm görmezden gelir)
    checklist_id = Column(Integer, ForeignKey("audit_checklists.id", ondelete="SET NULL"), nullable=True)
    ekip_json = Column(Text, nullable=True)           # [{"user_id":..,"ad":..,"rol":"Baş Denetçi|Denetçi|Stajer"}]
    otomatik = Column(Boolean, nullable=True)         # plandan otomatik açıldı

    tetkik_eden = relationship("User")
    denetlenen_surec = relationship("Process")
    checklist = relationship("AuditChecklist")
    findings = relationship("AuditFinding", back_populates="audit", cascade="all, delete-orphan", lazy="selectin")


class AuditFinding(Base):
    __tablename__ = "audit_findings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(Integer, ForeignKey("internal_audits.id", ondelete="CASCADE"), nullable=False)
    bulgu_tipi = Column(String(30), nullable=False, default="Gözlem")  # BULGU_TIPLERI
    aciklama = Column(Text, nullable=False)
    ilgili_madde = Column(String(50), nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    answer_id = Column(Integer, ForeignKey("audit_answers.id", ondelete="SET NULL"), nullable=True)  # 2026: sorudan otomatik

    audit = relationship("InternalAudit", back_populates="findings")
    capa = relationship("CorrectiveAction")


# ═══════════════════════════════════════════════════════════════════════════
#  İç Tetkik Soru Listeleri + Kanıt
# ═══════════════════════════════════════════════════════════════════════════
class AuditChecklist(Base):
    """Denetim soru listesi şablonu (Y03 Fxx)."""
    __tablename__ = "audit_checklists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kod = Column(String(40), nullable=False, unique=True)         # Y03-F21-D01
    ad = Column(String(200), nullable=False)
    denetim_tipi = Column(String(30), nullable=False)             # AUDIT_TIPLERI
    surec_kod = Column(String(10), nullable=True)                 # D01… (sistem)
    kaynak_dosya = Column(String(260), nullable=True)
    aktif = Column(Boolean, nullable=False, default=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    questions = relationship(
        "AuditChecklistQuestion", back_populates="checklist",
        cascade="all, delete-orphan", lazy="selectin",
    )


class AuditChecklistQuestion(Base):
    """Şablon sorusu."""
    __tablename__ = "audit_checklist_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    checklist_id = Column(Integer, ForeignKey("audit_checklists.id", ondelete="CASCADE"), nullable=False)
    sira = Column(Integer, nullable=False, default=0)
    bolum = Column(String(120), nullable=True)                    # sayfa / bölüm başlığı
    soru_no = Column(String(20), nullable=True)
    soru_metin = Column(Text, nullable=False)
    sart_no = Column(String(40), nullable=True)                   # ISO madde
    kime = Column(String(200), nullable=True)                     # 2026: sorulacak rol
    ipucu = Column(Text, nullable=True)                           # 2026: kaynak liste / önceki denetim gözlemi

    checklist = relationship("AuditChecklist", back_populates="questions")


class AuditAnswer(Base):
    """Bir tetkikte soruya verilen cevap + gözlem."""
    __tablename__ = "audit_answers"
    __table_args__ = (UniqueConstraint("audit_id", "question_id", name="uq_audit_answer"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(Integer, ForeignKey("internal_audits.id", ondelete="CASCADE"), nullable=False)
    question_id = Column(Integer, ForeignKey("audit_checklist_questions.id", ondelete="CASCADE"), nullable=False)
    sonuc = Column(String(30), nullable=False, default="Değerlendirilmedi")  # AUDIT_CEVAPLARI
    gozlem = Column(Text, nullable=True)
    bulgu_derecesi = Column(String(30), nullable=True)            # 2026: BULGU_TIPLERI (Uygunsuz/Kısmen için)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    audit = relationship("InternalAudit", backref=backref("answers", cascade="all, delete-orphan", lazy="selectin"))
    question = relationship("AuditChecklistQuestion")
    evidences = relationship(
        "AuditEvidence", back_populates="answer",
        cascade="all, delete-orphan", lazy="selectin",
    )


class AuditEvidence(Base):
    """Soru cevabına bağlı kanıt dokümanı (ölçüm raporu, talimat vb.)."""
    __tablename__ = "audit_evidences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    answer_id = Column(Integer, ForeignKey("audit_answers.id", ondelete="CASCADE"), nullable=False)
    kanit_tipi = Column(String(40), nullable=True)                # AUDIT_KANIT_TIPLERI
    aciklama = Column(String(300), nullable=True)
    dosya_adi = Column(Unicode(260), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    yukleyen_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    answer = relationship("AuditAnswer", back_populates="evidences")
    yukleyen = relationship("User")


# ═══════════════════════════════════════════════════════════════════════════
#  13) TrainingRecord — Eğitim Kayıtları
# ═══════════════════════════════════════════════════════════════════════════
class TrainingRecord(Base):
    __tablename__ = "training_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kullanici_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    egitim_adi = Column(String(200), nullable=False)
    egitim_tipi = Column(String(30), nullable=False, default="Diğer")   # EGITIM_TIPLERI
    egitim_tarihi = Column(Date, nullable=True)
    gecerlilik_tarihi = Column(Date, nullable=True)
    veren_kurum = Column(String(150), nullable=True)
    aciklama = Column(Text, nullable=True)
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    kullanici = relationship("User")

    @property
    def gecerlilik_kalan_gun(self):
        if not self.gecerlilik_tarihi:
            return None
        return (self.gecerlilik_tarihi - date.today()).days


# ═══════════════════════════════════════════════════════════════════════════
#  Faz 8) RequiredCompetency — Yetkinlik Matrisi (rol/departman/süreç bazlı)
# ═══════════════════════════════════════════════════════════════════════════
class RequiredCompetency(Base):
    __tablename__ = "required_competencies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad = Column(String(200), nullable=False)                       # gerekli yetkinlik/eğitim adı
    kapsam_tipi = Column(String(20), nullable=False, default="Rol")  # YETKINLIK_KAPSAM_TIPLERI
    kapsam_deger = Column(String(120), nullable=False)            # rol adı / departman / süreç kodu
    gecerlilik_ay = Column(Integer, nullable=True)               # periyodik tazeleme süresi
    kritik_mi = Column(Boolean, default=False)                    # kritik görev için engelleyici
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


# ═══════════════════════════════════════════════════════════════════════════
#  14) RiskRegisterEntry — Risk Kaydı
# ═══════════════════════════════════════════════════════════════════════════
class RiskRegisterEntry(Base):
    __tablename__ = "risk_register"

    id = Column(Integer, primary_key=True, autoincrement=True)
    risk_no = Column(String(30), nullable=False, unique=True)     # RSK-2026-001
    surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    kategori = Column(String(30), nullable=False, default="Kalite")  # RISK_KATEGORILERI
    tanim = Column(Text, nullable=False)
    olasilik = Column(Integer, nullable=False, default=1)          # 1-5
    etki = Column(Integer, nullable=False, default=1)              # 1-5
    mevcut_kontroller = Column(Text, nullable=True)
    aksiyon_plani = Column(Text, nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    hedef_tarih = Column(Date, nullable=True)
    durum = Column(String(30), nullable=False, default="Açık")     # RISK_DURUMLARI
    gozden_gecirme_tarihi = Column(Date, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    surec = relationship("Process")
    sorumlu = relationship("User")

    @property
    def risk_skoru(self):
        return (self.olasilik or 0) * (self.etki or 0)

    @property
    def risk_seviyesi(self):
        skor = self.risk_skoru
        if skor >= 15:
            return "Yüksek"
        if skor >= 8:
            return "Orta"
        return "Düşük"


# ═══════════════════════════════════════════════════════════════════════════
#  15) DistributionGroup + Members — Dağıtım Grupları
# ═══════════════════════════════════════════════════════════════════════════
class DistributionGroup(Base):
    __tablename__ = "distribution_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad = Column(String(100), nullable=False, unique=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    members = relationship("DistributionGroupMember", back_populates="group", cascade="all, delete-orphan", lazy="selectin")


class DistributionGroupMember(Base):
    __tablename__ = "distribution_group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("distribution_groups.id", ondelete="CASCADE"), nullable=False)
    kullanici_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    group = relationship("DistributionGroup", back_populates="members")
    kullanici = relationship("User")


# ═══════════════════════════════════════════════════════════════════════════
#  IMS ORTAK ÇEKİRDEĞİ (Faz 2) — Standart/Gereklilik/Kanıt/Bağlam/Hedef/YGG
# ═══════════════════════════════════════════════════════════════════════════
class StandardEdition(Base):
    """Sürümlü standart baskısı (ör. IATF 16949 / 2016). Normatif metin içermez;
    yalnızca yapısal referans ve yetkili yöneticinin girdiği açıklamalar tutulur."""
    __tablename__ = "standard_editions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    standart = Column(String(40), nullable=False)                 # STANDARTLAR
    surum = Column(String(20), nullable=False)                    # ör. "2016", "2022"
    aciklama = Column(String(200), nullable=True)
    aktif = Column(Boolean, default=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    requirements = relationship("Requirement", back_populates="edition",
                                cascade="all, delete-orphan", lazy="selectin")


class Requirement(Base):
    """Standart gerekliliği (madde). Kısa başlık kamuya açık referanstır;
    tam normatif metin lisanslı olduğundan yalnızca yetkili kullanıcı ekler."""
    __tablename__ = "requirements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    edition_id = Column(Integer, ForeignKey("standard_editions.id", ondelete="CASCADE"), nullable=False)
    madde_no = Column(String(30), nullable=False)                 # ör. "8.5", "A.5.1"
    baslik = Column(String(300), nullable=False)
    aciklama = Column(Text, nullable=True)                        # yetkili tarafından girilir
    sira = Column(Integer, default=0)

    edition = relationship("StandardEdition", back_populates="requirements")
    status = relationship("RequirementStatus", back_populates="requirement",
                          uselist=False, cascade="all, delete-orphan")
    evidences = relationship("Evidence", back_populates="requirement", lazy="selectin")


class RequirementStatus(Base):
    """Bir gerekliliğin uygulanabilirlik, uygulama ve etkinlik durumu."""
    __tablename__ = "requirement_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"),
                            nullable=False, unique=True)
    uygulanabilirlik = Column(String(20), default="Uygulanabilir")   # IMS_UYGULANABILIRLIK
    gerekce = Column(Text, nullable=True)                            # uygulanamaz ise gerekçe
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    uygulama_durumu = Column(String(30), default="Uygulanmadı")      # IMS_UYGULAMA_DURUMLARI
    etkinlik = Column(String(20), default="Değerlendirilmedi")       # IMS_ETKINLIK_DURUMLARI
    son_dogrulama_tarihi = Column(Date, nullable=True)
    notlar = Column(Text, nullable=True)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    requirement = relationship("Requirement", back_populates="status")
    sorumlu = relationship("User")


class Evidence(Base):
    """Uyumluluk kanıtı; bir gerekliliğe ve/veya dokümana bağlanabilir."""
    __tablename__ = "evidences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="CASCADE"), nullable=True)
    baslik = Column(String(200), nullable=False)
    kanit_tipi = Column(String(30), nullable=False, default="Doküman")   # KANIT_TIPLERI
    ilgili_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    aciklama = Column(Text, nullable=True)
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    tarih = Column(Date, nullable=True)
    ekleyen_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    requirement = relationship("Requirement", back_populates="evidences")
    ilgili_document = relationship("Document")
    ekleyen = relationship("User")


class OrganizationContext(Base):
    """Kuruluşun bağlamı: iç/dış hususlar (2024 iklim değişikliği eki dahil)."""
    __tablename__ = "organization_context"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tur = Column(String(20), nullable=False, default="İç Husus")     # BAGLAM_TURLERI
    konu = Column(String(200), nullable=False)
    aciklama = Column(Text, nullable=True)
    ilgili_standartlar = Column(String(120), nullable=True)          # virgülle ayrık
    iklim_ile_ilgili = Column(Boolean, default=False)               # ISO 2024 Amd.
    etki_degerlendirmesi = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class InterestedParty(Base):
    """İlgili taraf ve beklentileri."""
    __tablename__ = "interested_parties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad = Column(String(150), nullable=False)
    beklentiler = Column(Text, nullable=True)
    etki_derecesi = Column(String(20), nullable=True)               # Yüksek/Orta/Düşük
    ilgili_standartlar = Column(String(120), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class Objective(Base):
    """Eski IMS hedef tablosu (modül kaldırıldı; tablo geriye uyumluluk için duruyor)."""
    __tablename__ = "objectives"

    id = Column(Integer, primary_key=True, autoincrement=True)
    baslik = Column(String(200), nullable=False)
    standart = Column(String(40), nullable=True)                    # STANDARTLAR
    ilgili_surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    hedef_deger = Column(Float, nullable=True)
    mevcut_deger = Column(Float, nullable=True)
    birim = Column(String(30), nullable=True)
    termin = Column(Date, nullable=True)
    durum = Column(String(20), nullable=False, default="Açık")      # HEDEF_DURUMLARI
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ilgili_surec = relationship("Process")
    sorumlu = relationship("User")

    @property
    def ilerleme_yuzde(self):
        if self.hedef_deger in (None, 0) or self.mevcut_deger is None:
            return None
        return round(min(100.0, (self.mevcut_deger / self.hedef_deger) * 100), 1)


class ManagementReview(Base):
    """Yönetimin gözden geçirmesi (YGG) toplantısı ve kararları."""
    __tablename__ = "management_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    baslik = Column(String(200), nullable=False)
    toplanti_tarihi = Column(Date, nullable=True)
    katilimcilar = Column(Text, nullable=True)
    girdiler = Column(Text, nullable=True)                          # zorunlu girdi özetleri
    kararlar = Column(Text, nullable=True)
    aksiyonlar = Column(Text, nullable=True)
    durum = Column(String(30), nullable=False, default="Planlandı")  # YGG_DURUMLARI
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    olusturan_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    olusturan = relationship("User")


class LegalRequirement(Base):
    """Yasal ve diğer yükümlülükler (mevzuat/izin/müşteri özel şartı)."""
    __tablename__ = "legal_requirements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    baslik = Column(String(200), nullable=False)
    tur = Column(String(30), nullable=False, default="Genel")       # YASAL_TURLERI
    referans = Column(String(200), nullable=True)                   # kanun/yönetmelik no
    yukumluluk = Column(Text, nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    uygunluk_durumu = Column(String(20), nullable=False, default="Değerlendirilmedi")  # UYGUNLUK_DURUMLARI
    son_degerlendirme_tarihi = Column(Date, nullable=True)
    sonraki_degerlendirme_tarihi = Column(Date, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sorumlu = relationship("User")
    degerlendirmeler = relationship("ComplianceEvaluation", back_populates="yasal",
                                    cascade="all, delete-orphan", lazy="selectin")


class ComplianceEvaluation(Base):
    """Yasal yükümlülük için periyodik uygunluk değerlendirmesi kaydı."""
    __tablename__ = "compliance_evaluations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    legal_id = Column(Integer, ForeignKey("legal_requirements.id", ondelete="CASCADE"), nullable=False)
    tarih = Column(Date, nullable=True)
    sonuc = Column(String(20), nullable=False, default="Uygun")     # UYGUNLUK_DURUMLARI
    kanit = Column(Text, nullable=True)
    degerlendiren_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    yasal = relationship("LegalRequirement", back_populates="degerlendirmeler")
    degerlendiren = relationship("User")


# ═══════════════════════════════════════════════════════════════════════════
#  IATF 16949 MODÜLLERİ (Faz 4)
# ═══════════════════════════════════════════════════════════════════════════
class Supplier(Base):
    """Tedarikçi ana kaydı ve onay/risk durumu."""
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kod = Column(String(30), nullable=False, unique=True)
    ad = Column(String(200), nullable=False)
    kategori = Column(String(30), nullable=False, default="Hammadde")   # TEDARIKCI_KATEGORILERI
    onay_durumu = Column(String(20), nullable=False, default="Aday")    # TEDARIKCI_DURUMLARI
    risk_sinifi = Column(String(20), nullable=True)                     # Yüksek/Orta/Düşük
    iletisim = Column(String(200), nullable=True)
    iatf_sertifikali = Column(Boolean, default=False)
    sertifika_bitis = Column(Date, nullable=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    scorecards = relationship("SupplierScorecard", back_populates="supplier",
                              cascade="all, delete-orphan", lazy="selectin")

    @property
    def guncel_puan(self):
        if not self.scorecards:
            return None
        son = sorted(self.scorecards, key=lambda s: s.donem or "")[-1]
        return son.puan


class SupplierScorecard(Base):
    """Tedarikçi performans karnesi (dönemsel)."""
    __tablename__ = "supplier_scorecards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    donem = Column(String(20), nullable=False)                          # 2026-Q1
    kalite_ppm = Column(Float, nullable=True)
    teslimat_yuzde = Column(Float, nullable=True)
    puan = Column(Float, nullable=True)                                 # 0-100
    notlar = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    supplier = relationship("Supplier", back_populates="scorecards")


class APQPProject(Base):
    """APQP proje planı ve faz takibi."""
    __tablename__ = "apqp_projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    proje_no = Column(String(30), nullable=False, unique=True)
    ad = Column(String(200), nullable=False)
    musteri = Column(String(150), nullable=True)
    parca_no = Column(String(100), nullable=True)
    faz = Column(Integer, nullable=False, default=1)                    # 1-5
    durum = Column(String(20), nullable=False, default="Planlandı")     # APQP_DURUMLARI
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    baslangic_tarihi = Column(Date, nullable=True)
    hedef_tarih = Column(Date, nullable=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sorumlu = relationship("User")
    gates = relationship("APQPGate", back_populates="project",
                         cascade="all, delete-orphan", lazy="selectin")


class APQPGate(Base):
    """APQP kapı (gate) onayı."""
    __tablename__ = "apqp_gates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("apqp_projects.id", ondelete="CASCADE"), nullable=False)
    kapi_no = Column(Integer, nullable=False)                           # 1-5
    durum = Column(String(20), nullable=False, default="Bekliyor")      # KAPI_DURUMLARI
    tarih = Column(Date, nullable=True)
    karar = Column(Text, nullable=True)

    project = relationship("APQPProject", back_populates="gates")


class FMEA(Base):
    """DFMEA/PFMEA ana kaydı — VDA/AIAG FMEA Handbook 7-adım yapısı."""
    __tablename__ = "fmeas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fmea_no = Column(String(30), nullable=False, unique=True)
    tip = Column(String(10), nullable=False, default="PFMEA")           # FMEA_TIPLERI
    kapsam = Column(String(200), nullable=True)
    parca_proses = Column(String(200), nullable=True)
    musteri = Column(String(150), nullable=True)                        # müşteri/proje referansı
    ekip = Column(String(300), nullable=True)
    ilgili_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    apqp_id = Column(Integer, ForeignKey("apqp_projects.id", ondelete="SET NULL"), nullable=True)
    surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    tarih = Column(Date, nullable=True)
    revizyon = Column(String(10), nullable=True, default="00")
    mevcut_adim = Column(String(30), nullable=True, default="1-Planlama")  # FMEA_ADIMLARI
    baslatma_tarihi = Column(Date, nullable=True)
    tamamlanma_tarihi = Column(Date, nullable=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ilgili_document = relationship("Document")
    apqp = relationship("APQPProject")
    surec = relationship("Process")
    items = relationship("FMEAItem", back_populates="fmea",
                         cascade="all, delete-orphan", lazy="selectin")

    @property
    def max_rpn(self):
        """Geriye uyumluluk: eski RPN hesabı."""
        return max((i.rpn for i in self.items), default=0)

    @property
    def ap_ozet(self):
        """AP dağılım özeti: {H: n, M: n, L: n}"""
        from fmea_ap_matrix import ap_ozet
        return ap_ozet(self.items)


class FMEAItem(Base):
    """FMEA satırı: hata türü, S/O/D ve Action Priority (VDA/AIAG 2019)."""
    __tablename__ = "fmea_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fmea_id = Column(Integer, ForeignKey("fmeas.id", ondelete="CASCADE"), nullable=False)
    fonksiyon = Column(String(300), nullable=True)
    hata_turu = Column(String(300), nullable=True)
    etki = Column(String(300), nullable=True)
    siddet = Column(Integer, nullable=False, default=1)                 # S 1-10
    neden = Column(String(300), nullable=True)
    olusma = Column(Integer, nullable=False, default=1)                 # O 1-10
    # Handbook: Önleme ve Tespit kontrolleri AYRI tutulmalı
    mevcut_kontrol = Column(String(300), nullable=True)                 # geriye uyumluluk
    onleme_kontrolu = Column(String(300), nullable=True)                # Prevention Control (PC)
    tespit_kontrolu = Column(String(300), nullable=True)                # Detection Control (DC)
    tespit = Column(Integer, nullable=False, default=1)                 # D 1-10
    # Özel karakteristik
    ozel_karakteristik = Column(Boolean, default=False)
    ozel_karakteristik_tip = Column(String(10), nullable=True)          # CC/SC/HI/YC
    filtre_kodu = Column(String(50), nullable=True)                     # FMEA Filter Code
    # Aksiyon
    onerilen_aksiyon = Column(Text, nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    # Aksiyon sonrası yeniden değerlendirme (Adım 6 — Optimizasyon)
    aksiyon_durumu = Column(String(20), nullable=True)                  # FMEA_AKSIYON_DURUMLARI
    aksiyon_alinan = Column(Text, nullable=True)
    aksiyon_tamamlanma_tarihi = Column(Date, nullable=True)
    yeni_siddet = Column(Integer, nullable=True)                        # Revize S
    yeni_olusma = Column(Integer, nullable=True)                        # Revize O
    yeni_tespit = Column(Integer, nullable=True)                        # Revize D
    # Kontrol planı bağlantısı
    kontrol_plani_item_id = Column(Integer, ForeignKey("control_plan_items.id", ondelete="SET NULL"), nullable=True)

    fmea = relationship("FMEA", back_populates="items")
    sorumlu = relationship("User")
    capa = relationship("CorrectiveAction")

    @property
    def rpn(self):
        """Geriye uyumluluk: eski RPN hesabı."""
        return (self.siddet or 0) * (self.olusma or 0) * (self.tespit or 0)

    @property
    def action_priority(self):
        """C2.5 PFMEA AP tablosuna göre Action Priority hesaplar."""
        from fmea_ap_matrix import hesapla_ap
        try:
            return hesapla_ap(self.siddet or 1, self.olusma or 1, self.tespit or 1)
        except (ValueError, TypeError):
            return "L"

    @property
    def yeni_action_priority(self):
        """Aksiyon sonrası revize AP hesaplar."""
        if not self.yeni_siddet or not self.yeni_olusma or not self.yeni_tespit:
            return None
        from fmea_ap_matrix import hesapla_ap
        try:
            return hesapla_ap(self.yeni_siddet, self.yeni_olusma, self.yeni_tespit)
        except (ValueError, TypeError):
            return None


class ControlPlan(Base):
    """Kontrol planı ana kaydı."""
    __tablename__ = "control_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cp_no = Column(String(30), nullable=False, unique=True)
    tip = Column(String(20), nullable=False, default="Üretim")          # KONTROL_PLANI_TIPLERI
    parca_no = Column(String(100), nullable=True)
    parca_adi = Column(String(200), nullable=True)
    proses = Column(String(200), nullable=True)
    ilgili_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    fmea_id = Column(Integer, ForeignKey("fmeas.id", ondelete="SET NULL"), nullable=True)
    revizyon = Column(String(10), nullable=True, default="00")
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ilgili_document = relationship("Document")
    items = relationship("ControlPlanItem", back_populates="plan",
                         cascade="all, delete-orphan", lazy="selectin")


class ControlPlanItem(Base):
    """Kontrol planı satırı: karakteristik ve kontrol yöntemi."""
    __tablename__ = "control_plan_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("control_plans.id", ondelete="CASCADE"), nullable=False)
    proses_adimi = Column(String(200), nullable=True)
    karakteristik = Column(String(200), nullable=True)
    ozel_karakteristik = Column(Boolean, default=False)
    spesifikasyon = Column(String(200), nullable=True)
    olcum_yontemi = Column(String(200), nullable=True)
    numune_buyuklugu = Column(String(50), nullable=True)
    siklik = Column(String(50), nullable=True)
    reaksiyon_plani = Column(Text, nullable=True)

    plan = relationship("ControlPlan", back_populates="items")


class CalibrationEquipment(Base):
    """Ölçüm ekipmanı ve kalibrasyon takibi (MSA bağlantılı)."""
    __tablename__ = "calibration_equipment"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ekipman_no = Column(String(30), nullable=False, unique=True)
    ad = Column(String(200), nullable=False)
    tip = Column(String(100), nullable=True)
    konum = Column(String(150), nullable=True)
    kalibrasyon_araligi_ay = Column(Integer, nullable=True, default=12)
    son_kalibrasyon = Column(Date, nullable=True)
    sonraki_kalibrasyon = Column(Date, nullable=True)
    izin_verilen_hata = Column(String(150), nullable=True)               # İzin Verilen Toplam Ölçüm Hatası
    kalibrasyon_sapma = Column(String(150), nullable=True)              # Kalibrasyon Sonucu (sapma)
    msa_sonucu = Column(String(100), nullable=True)                     # ör. GRR %8.5
    durum = Column(String(20), nullable=False, default="Geçerli")       # KALIBRASYON_DURUMLARI
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    # 2026 — D04 F06 İzleme & Ölçme Cihazları ve Kalibrasyon Listesi alanları (boş bırakılabilir)
    cihaz_kodu = Column(String(60), nullable=True)                      # listedeki kod (benzersiz olmayabilir: GO / NO GO)
    cihaz_turu = Column(String(60), nullable=True)                      # Ölçme / İzleme Cihazı / Kalibratör (Etalon)
    olcum_tipi = Column(String(40), nullable=True)                      # Tahribatsız / Tahribatlı / Otomatik
    seri_no = Column(String(100), nullable=True)
    imalatci = Column(String(100), nullable=True)
    kullanici_bolum = Column(String(100), nullable=True)
    hassasiyet = Column(String(60), nullable=True)
    olcum_araligi = Column(String(100), nullable=True)
    kullanim_araligi = Column(String(100), nullable=True)
    birim = Column(String(30), nullable=True)
    ozel_karakteristik = Column(Boolean, nullable=True)
    sertifika_no = Column(String(120), nullable=True)                   # güncel sertifika
    dogrulama_periyodu = Column(String(40), nullable=True)
    dogrulama_yontemi = Column(String(100), nullable=True)
    msa_tipi = Column(String(40), nullable=True)
    karar = Column(String(200), nullable=True)                          # karar + gerekçe
    kullanima_onay_veren = Column(String(100), nullable=True)
    ek_json = Column(Text, nullable=True)                               # listedeki diğer sütunlar
    kaynak = Column(String(120), nullable=True)                         # 'D04 F06 2026 satır 12'

    sorumlu = relationship("User")
    sertifikalar = relationship("KalibrasyonSertifika", back_populates="ekipman", cascade="all, delete-orphan",
                                order_by="KalibrasyonSertifika.kalibrasyon_tarihi.desc()", lazy="selectin")

    @property
    def ek(self):
        try:
            return json.loads(self.ek_json or "{}") or {}
        except ValueError:
            return {}

    @property
    def gecerli_sertifika(self):
        return next((c for c in self.sertifikalar if c.gecerli), None)

    @property
    def kalan_gun(self):
        if not self.sonraki_kalibrasyon:
            return None
        return (self.sonraki_kalibrasyon - date.today()).days


class KalibrasyonSertifika(Base):
    """Ölçü aleti kalibrasyon sertifikası (dosya + sonuç). Yeni tablo (2026); eski sürüm kullanmaz."""
    __tablename__ = "kalibrasyon_sertifikalari"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ekipman_id = Column(Integer, ForeignKey("calibration_equipment.id", ondelete="CASCADE"), nullable=False)
    sertifika_no = Column(String(120), nullable=True)
    kalibrasyon_tarihi = Column(Date, nullable=True)
    sonraki_kalibrasyon = Column(Date, nullable=True)
    kurum = Column(String(150), nullable=True)                          # kalibrasyon laboratuvarı
    sonuc = Column(String(30), nullable=True)                           # Uygun / Uygun Değil
    sapma = Column(String(200), nullable=True)
    aciklama = Column(Text, nullable=True)
    dosya_adi = Column(Unicode(260), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    gecerli = Column(Boolean, nullable=False, default=True)             # yeni sertifika gelince eskisi False
    yukleyen_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ekipman = relationship("CalibrationEquipment", back_populates="sertifikalar")
    yukleyen = relationship("User")
    depo_id = Column(Integer, nullable=True)                            # dosya_depo.id (2026) — dosya veritabanında da saklanır

class EnjeksiyonUrun(Base):
    """M03 F08 Ürün Enjeksiyon Parametreleri — parametre kartı olan ürün (2026, yeni tablo)."""
    __tablename__ = "enjeksiyon_urunleri"

    id = Column(Integer, primary_key=True, autoincrement=True)
    urun_adi = Column(String(200), nullable=False)
    musteri = Column(String(100), nullable=True)
    tanim = Column(String(250), nullable=True)
    sablon = Column(String(40), nullable=True)                           # Termoset (Bagalit) / Termoplast
    kaynak = Column(String(300), nullable=True)                          # 'M03 F21 … WMF-SEB.xls / Perfect Neu E1'
    aktif = Column(Boolean, nullable=False, default=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    kayitlar = relationship("EnjeksiyonParametreKaydi", back_populates="urun", cascade="all, delete-orphan",
                            order_by="EnjeksiyonParametreKaydi.id", lazy="selectin")

    @property
    def gecerli(self):
        """Onaylı kayıtlar içinde en yeni tarihli (eşitse en son girilen)."""
        ok = [k for k in self.kayitlar if k.durum == "Onaylandı"]
        return max(ok, key=lambda k: (k.tarih or date.min, k.id)) if ok else None


class EnjeksiyonParametreKaydi(Base):
    """Bir ürün için ayar (parametre seti) kaydı: tarih, makine, malzeme, parametreler (değer ± sapma)."""
    __tablename__ = "enjeksiyon_parametre_kayitlari"

    id = Column(Integer, primary_key=True, autoincrement=True)
    urun_id = Column(Integer, ForeignKey("enjeksiyon_urunleri.id", ondelete="CASCADE"), nullable=False)
    tarih = Column(Date, nullable=True)
    makina = Column(String(60), nullable=True)
    malzeme = Column(String(100), nullable=True)
    parametreler_json = Column(Text, nullable=False, default="[]")      # [{"ad","deger","sapma"}]
    kaynak = Column(String(30), nullable=True)                          # 'Excel geçmişi' / 'DYS'
    durum = Column(String(20), nullable=False, default="Onayda")        # Onayda / Onaylandı / Reddedildi
    aciklama = Column(Text, nullable=True)
    kaydeden_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    onaylayan_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    onay_tarihi = Column(DateTime, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    urun = relationship("EnjeksiyonUrun", back_populates="kayitlar")
    kaydeden = relationship("User", foreign_keys=[kaydeden_id])
    onaylayan = relationship("User", foreign_keys=[onaylayan_id])

    @property
    def parametreler(self):
        try:
            return json.loads(self.parametreler_json or "[]") or []
        except ValueError:
            return []


class NonconformingProduct(Base):
    """Uygun olmayan ürün kaydı (karantina/karar/izlenebilirlik)."""
    __tablename__ = "nonconforming_products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nc_no = Column(String(30), nullable=False, unique=True)
    parca_no = Column(String(100), nullable=True)
    parca_adi = Column(String(200), nullable=True)
    miktar = Column(Float, nullable=True)
    tespit_yeri = Column(String(150), nullable=True)
    parti_izlenebilirlik = Column(String(200), nullable=True)
    tanim = Column(Text, nullable=True)
    karar = Column(String(30), nullable=True)                           # UOU_KARARLARI
    musteriye_bildirim = Column(Boolean, default=False)
    durum = Column(String(20), nullable=False, default="Açık")          # UOU_DURUMLARI
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    tespit_tarihi = Column(Date, nullable=True)
    kapanma_tarihi = Column(Date, nullable=True)
    # IATF 16949 § 8.7.1.1: Sapma onayı
    sapma_onay_gerekli = Column(Boolean, default=False)
    sapma_onay_tarihi = Column(Date, nullable=True)
    sapma_onay_ref = Column(String(100), nullable=True)                 # müşteri onay referansı
    # IATF 16949 § 8.7.1.2: Yeniden işleme/tamir doğrulama
    yeniden_islem_dogrulama = Column(Text, nullable=True)
    dogrulama_tarihi = Column(Date, nullable=True)
    dogrulayan_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sorumlu = relationship("User", foreign_keys=[sorumlu_id])
    capa = relationship("CorrectiveAction")
    dogrulayan = relationship("User", foreign_keys=[dogrulayan_id])


class SPCRecord(Base):
    """İstatistiksel proses kontrol ölçüm kaydı (Cp/Cpk)."""
    __tablename__ = "spc_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parca_no = Column(String(100), nullable=True)
    karakteristik = Column(String(200), nullable=True)
    olcum_tarihi = Column(Date, nullable=True)
    deger = Column(Float, nullable=True)
    usl = Column(Float, nullable=True)
    lsl = Column(Float, nullable=True)
    ortalama = Column(Float, nullable=True)
    std_sapma = Column(Float, nullable=True)
    cp = Column(Float, nullable=True)
    cpk = Column(Float, nullable=True)
    yorum = Column(Text, nullable=True)
    olcumler = Column(JSON, nullable=True)                          # 30+ ölçüm değeri saklamak için JSON liste
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class CustomerSpecificRequirement(Base):
    """Müşteriye özel gereklilik (CSR) kaydı."""
    __tablename__ = "customer_specific_requirements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    musteri = Column(String(200), nullable=False)
    madde_no = Column(String(50), nullable=True)
    aciklama = Column(Text, nullable=True)
    ilgili_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    durum = Column(String(30), nullable=False, default="Açık")          # CSR_DURUMLARI
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ilgili_document = relationship("Document")


class RecordDisposal(Base):
    """Kayıt/doküman imha talebi ve çift onaylı imha akışı (Faz 3)."""
    __tablename__ = "record_disposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    talep_eden_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    talep_tarihi = Column(DateTime, default=datetime.now)
    gerekce = Column(Text, nullable=True)
    yasal_dayanak = Column(String(200), nullable=True)
    # Çift onay: iki farklı yetkili gerekir.
    onay1_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    onay1_tarihi = Column(DateTime, nullable=True)
    onay2_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    onay2_tarihi = Column(DateTime, nullable=True)
    durum = Column(String(20), nullable=False, default="Talep Edildi")   # IMHA_DURUMLARI
    imha_tarihi = Column(DateTime, nullable=True)

    document = relationship("Document")
    talep_eden = relationship("User", foreign_keys=[talep_eden_id])
    onay1 = relationship("User", foreign_keys=[onay1_id])
    onay2 = relationship("User", foreign_keys=[onay2_id])


# ═══════════════════════════════════════════════════════════════════════════
#  ISO 14001 ÇEVRE MODÜLLERİ (Faz 5)
# ═══════════════════════════════════════════════════════════════════════════
class EnvironmentalAspect(Base):
    """Çevre boyut/etki sicili."""
    __tablename__ = "environmental_aspects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    faaliyet = Column(String(200), nullable=False)
    boyut = Column(String(200), nullable=False)                     # çevre boyutu
    etki = Column(String(200), nullable=True)                       # çevresel etki
    tur = Column(String(30), nullable=True)                         # CEVRE_BOYUT_TURLERI
    kosul = Column(String(20), nullable=False, default="Normal")    # CEVRE_KOSULLARI
    yasam_dongusu = Column(String(100), nullable=True)
    olasilik = Column(Integer, default=1)                           # 1-5
    siddet = Column(Integer, default=1)                             # 1-5
    onemli_mi = Column(Boolean, default=False)
    operasyonel_kontrol = Column(Text, nullable=True)
    ilgili_surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ilgili_surec = relationship("Process")
    sorumlu = relationship("User")

    @property
    def onem_skoru(self):
        return (self.olasilik or 0) * (self.siddet or 0)


class EnvironmentalMeasurement(Base):
    """Çevresel ölçüm/izleme kaydı (atık, emisyon, su, enerji vb.)."""
    __tablename__ = "environmental_measurements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tur = Column(String(30), nullable=False, default="Atık")        # CEVRE_BOYUT_TURLERI
    tarih = Column(Date, nullable=True)
    deger = Column(Float, nullable=True)
    birim = Column(String(30), nullable=True)
    limit_deger = Column(Float, nullable=True)
    asim_mi = Column(Boolean, default=False)
    aksiyon = Column(Text, nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    capa = relationship("CorrectiveAction")


class EnvironmentalIncident(Base):
    """Çevre olayı / acil durum / tatbikat kaydı."""
    __tablename__ = "environmental_incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    olay_no = Column(String(30), nullable=True)
    tur = Column(String(30), nullable=False, default="Sızıntı/Dökülme")  # CEVRE_OLAY_TURLERI
    tarih = Column(Date, nullable=True)
    tanim = Column(Text, nullable=True)
    etki = Column(Text, nullable=True)
    aksiyon = Column(Text, nullable=True)
    tatbikat_mi = Column(Boolean, default=False)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    capa = relationship("CorrectiveAction")
    sorumlu = relationship("User")


# ═══════════════════════════════════════════════════════════════════════════
#  ISO 45001 İSG MODÜLLERİ (Faz 6)
# ═══════════════════════════════════════════════════════════════════════════
class OHSHazard(Base):
    """Tehlike tanımlama ve İSG risk değerlendirmesi."""
    __tablename__ = "ohs_hazards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    faaliyet = Column(String(200), nullable=False)
    tehlike = Column(String(300), nullable=False)
    risk = Column(String(300), nullable=True)
    calisan_grubu = Column(String(150), nullable=True)
    rutin_mi = Column(Boolean, default=True)
    olasilik = Column(Integer, default=1)                           # 1-5
    siddet = Column(Integer, default=1)                             # 1-5
    kontrol_hiyerarsisi = Column(String(30), nullable=True)         # ISG_KONTROL_HIYERARSISI
    mevcut_kontroller = Column(Text, nullable=True)
    artik_risk = Column(Integer, nullable=True)
    durum = Column(String(20), nullable=False, default="Açık")      # ISG_RISK_DURUMLARI
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    ilgili_surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sorumlu = relationship("User")
    ilgili_surec = relationship("Process")

    @property
    def risk_skoru(self):
        return (self.olasilik or 0) * (self.siddet or 0)

    @property
    def risk_seviyesi(self):
        s = self.risk_skoru
        return "Yüksek" if s >= 15 else ("Orta" if s >= 8 else "Düşük")


class OHSIncident(Base):
    """İSG olayı: kaza, ramak kala, meslek hastalığı, tatbikat."""
    __tablename__ = "ohs_incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    olay_no = Column(String(30), nullable=True)
    tur = Column(String(30), nullable=False, default="Ramak Kala")  # ISG_OLAY_TURLERI
    tarih = Column(Date, nullable=True)
    yaralanan = Column(String(150), nullable=True)
    yer = Column(String(150), nullable=True)
    tanim = Column(Text, nullable=True)
    kok_neden = Column(Text, nullable=True)
    kayip_gun = Column(Integer, nullable=True)
    bildirim_yapildi = Column(Boolean, default=False)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    capa = relationship("CorrectiveAction")
    sorumlu = relationship("User")


class OHSParticipation(Base):
    """Çalışan katılımı ve danışma kaydı."""
    __tablename__ = "ohs_participation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tur = Column(String(30), nullable=False, default="Öneri")       # ISG_KATILIM_TURLERI
    tarih = Column(Date, nullable=True)
    konu = Column(String(300), nullable=False)
    katilimci = Column(String(200), nullable=True)
    karar = Column(Text, nullable=True)
    kapatildi = Column(Boolean, default=False)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class PPEItem(Base):
    """Kişisel koruyucu donanım (KKD) stok kalemi."""
    __tablename__ = "ppe_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kod = Column(String(50), nullable=True)
    ad = Column(String(200), nullable=False)
    beden = Column(String(50), nullable=True)
    stok = Column(Integer, nullable=False, default=0)
    min_stok = Column(Integer, nullable=False, default=0)
    birim = Column(String(20), nullable=True, default="Adet")       # KKD_BIRIMLERI
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    issues = relationship("PPEIssue", back_populates="ppe", cascade="all, delete-orphan")


class PPEIssue(Base):
    """KKD zimmet / iade kaydı."""
    __tablename__ = "ppe_issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ppe_id = Column(Integer, ForeignKey("ppe_items.id", ondelete="CASCADE"), nullable=False)
    kullanici_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    miktar = Column(Integer, nullable=False, default=1)
    verilis_tarihi = Column(Date, nullable=True)
    iade_tarihi = Column(Date, nullable=True)
    durum = Column(String(20), nullable=False, default="Verildi")   # KKD_DURUMLARI

    ppe = relationship("PPEItem", back_populates="issues")
    kullanici = relationship("User")


class EmergencyDrill(Base):
    """Acil durum tatbikatı kaydı."""
    __tablename__ = "emergency_drills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    drill_no = Column(String(30), nullable=True)
    planlanan_tarihi = Column(Date, nullable=True)
    gerceklesen_tarihi = Column(Date, nullable=True)
    senaryo = Column(Text, nullable=True)
    katilimci_sayisi = Column(Integer, nullable=True)
    # JSON kullanıcı id listesi: [1, 2, 3]
    katilimci_ids = Column(Text, nullable=True)
    etkinlik_sonucu = Column(Text, nullable=True)
    aksiyon = Column(Text, nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    capa = relationship("CorrectiveAction")

    def katilimci_id_listesi(self):
        if not self.katilimci_ids:
            return []
        try:
            data = json.loads(self.katilimci_ids)
            if isinstance(data, list):
                return [int(x) for x in data if str(x).isdigit() or isinstance(x, int)]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return []


# ═══════════════════════════════════════════════════════════════════════════
#  Süreç Performans KPI (Y03 F06)
# ═══════════════════════════════════════════════════════════════════════════
class ProcessKPI(Base):
    """Süreç performans parametresi (Y03 F06 tablosundan)."""
    __tablename__ = "process_kpis"

    id = Column(Integer, primary_key=True, autoincrement=True)
    surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    surec_kod = Column(String(10), nullable=False)                   # Y01, M04...
    sira_no = Column(Integer, nullable=True)
    prosedure = Column(String(300), nullable=True)
    parametre = Column(String(300), nullable=False)
    gg_periyot = Column(String(20), nullable=True)                   # 1Y, 6A, 3A, 1A
    hedef_metin = Column(String(120), nullable=True)                 # ham hedef ifadesi
    hedef_tip = Column(String(20), nullable=True)                    # min/max/eq/range/monitor
    hedef_deger = Column(Float, nullable=True)
    hedef_deger_ust = Column(Float, nullable=True)                   # range üst sınırı
    birim = Column(String(40), nullable=True)
    aktif = Column(Boolean, nullable=False, default=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    surec = relationship("Process")
    olcumler = relationship("ProcessKPIMeasurement", back_populates="kpi",
                            cascade="all, delete-orphan", lazy="selectin")


class ProcessKPIMeasurement(Base):
    """KPI dönemsel gerçekleşen değer kaydı."""
    __tablename__ = "process_kpi_measurements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kpi_id = Column(Integer, ForeignKey("process_kpis.id", ondelete="CASCADE"), nullable=False)
    donem = Column(String(20), nullable=False)                       # 2026, 2026-01, 2026-Q1
    gerceklesen = Column(Float, nullable=True)
    gerceklesen_metin = Column(String(200), nullable=True)           # sayısal olmayan giriş
    durum = Column(String(20), nullable=False, default="Girilmedi")  # KPI_OLCUM_DURUMLARI
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    notlar = Column(Text, nullable=True)
    giren_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    kpi = relationship("ProcessKPI", back_populates="olcumler")
    capa = relationship("CorrectiveAction")
    giren = relationship("User")


# ═══════════════════════════════════════════════════════════════════════════
#  ISO 27001 ISMS MODÜLLERİ (Faz 7)
# ═══════════════════════════════════════════════════════════════════════════
class InformationAsset(Base):
    """Bilgi varlığı envanteri (CIA değerlemesi)."""
    __tablename__ = "information_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    varlik_no = Column(String(30), nullable=True)
    ad = Column(String(200), nullable=False)
    tur = Column(String(100), nullable=True)                        # Sunucu, Uygulama, Veri, Personel...
    sahip_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    gizlilik = Column(String(20), nullable=True)                    # CIA_SEVIYELERI
    butunluk = Column(String(20), nullable=True)
    erisilebilirlik = Column(String(20), nullable=True)
    konum = Column(String(150), nullable=True)
    veri_sinifi = Column(String(20), nullable=True)                 # VERI_SINIFLARI
    tedarikci = Column(String(150), nullable=True)
    saklama_kurali = Column(String(200), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sahip = relationship("User")

    @property
    def kritiklik(self):
        seviye = {"Düşük": 1, "Orta": 2, "Yüksek": 3}
        return max(seviye.get(self.gizlilik, 0), seviye.get(self.butunluk, 0),
                   seviye.get(self.erisilebilirlik, 0))


class SoAControl(Base):
    """ISO/IEC 27001:2022 Annex A kontrolü (uygulanabilirlik beyanı)."""
    __tablename__ = "soa_controls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    kontrol_no = Column(String(20), nullable=False, unique=True)    # A.5.1 ...
    baslik = Column(String(300), nullable=True)                     # yetkili girer (normatif metin değil)
    uygulanabilir_mi = Column(Boolean, default=True)
    gerekce = Column(Text, nullable=True)
    uygulama_durumu = Column(String(20), nullable=False, default="Uygulanmadı")  # SOA_DURUMLARI
    kontrol_sahibi_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    kanit = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    kontrol_sahibi = relationship("User")


class ISMSRisk(Base):
    """Bilgi güvenliği riski, tedavi planı ve artık risk."""
    __tablename__ = "isms_risks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    risk_no = Column(String(30), nullable=True)
    asset_id = Column(Integer, ForeignKey("information_assets.id", ondelete="SET NULL"), nullable=True)
    tehdit = Column(String(300), nullable=False)
    zafiyet = Column(String(300), nullable=True)
    olasilik = Column(Integer, default=1)                           # 1-5
    etki = Column(Integer, default=1)                               # 1-5
    tedavi = Column(String(20), nullable=True)                      # ISMS_TEDAVI_SECENEKLERI
    tedavi_plani = Column(Text, nullable=True)
    soa_kontrol = Column(String(100), nullable=True)                # ilgili Annex A kontrol no(ları)
    artik_risk = Column(Integer, nullable=True)
    kabul_edildi = Column(Boolean, default=False)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    asset = relationship("InformationAsset")
    sorumlu = relationship("User")

    @property
    def risk_skoru(self):
        return (self.olasilik or 0) * (self.etki or 0)


class SecurityIncident(Base):
    """Bilgi güvenliği olayı."""
    __tablename__ = "security_incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    olay_no = Column(String(30), nullable=True)
    tur = Column(String(30), nullable=False, default="Diğer")       # ISMS_OLAY_TURLERI
    tarih = Column(Date, nullable=True)
    tanim = Column(Text, nullable=True)
    etki = Column(Text, nullable=True)
    mudahale = Column(Text, nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    kapatildi = Column(Boolean, default=False)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    capa = relationship("CorrectiveAction")
    sorumlu = relationship("User")


class AccessReview(Base):
    """Periyodik erişim gözden geçirmesi."""
    __tablename__ = "access_reviews"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tarih = Column(Date, nullable=True)
    kapsam = Column(String(200), nullable=False)
    inceleyen_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    bulgu = Column(Text, nullable=True)
    aksiyon = Column(Text, nullable=True)
    tamamlandi = Column(Boolean, default=False)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    inceleyen = relationship("User")


class BackupEvidence(Base):
    """Yedekleme kanıtı ve geri yükleme testi kaydı."""
    __tablename__ = "backup_evidence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    yedek_tarihi = Column(DateTime, nullable=True)
    yedek_tipi = Column(String(20), nullable=False, default="Tam")  # YEDEK_TIPLERI
    konum = Column(String(300), nullable=True)
    boyut_mb = Column(Float, nullable=True)
    geri_yukleme_testi_mi = Column(Boolean, default=False)
    test_tarihi = Column(Date, nullable=True)
    test_sonucu = Column(Text, nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sorumlu = relationship("User")


class AuditProgram(Base):
    """Yıllık iç tetkik programı (risk temelli planlama)."""
    __tablename__ = "audit_programs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    yil = Column(Integer, nullable=False)
    baslik = Column(String(200), nullable=False)
    kapsam = Column(Text, nullable=True)
    risk_temelli_mi = Column(Boolean, default=True)
    durum = Column(String(30), nullable=False, default="Planlandı")  # AUDIT_PROGRAM_DURUMLARI
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    items = relationship("AuditProgramItem", back_populates="program",
                         cascade="all, delete-orphan", lazy="selectin")


class AuditProgramItem(Base):
    """Yıllık tetkik programı kalemi."""
    __tablename__ = "audit_program_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    program_id = Column(Integer, ForeignKey("audit_programs.id", ondelete="CASCADE"), nullable=False)
    planlanan_ay = Column(Integer, nullable=True)                   # 1-12
    denetim_tipi = Column(String(30), nullable=False, default="Sistem Denetimi")  # AUDIT_TIPLERI
    surec_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    ilgili_standart = Column(String(50), nullable=True)
    urun_adi = Column(String(200), nullable=True)
    tetkik_eden_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    audit_id = Column(Integer, ForeignKey("internal_audits.id", ondelete="SET NULL"), nullable=True)
    durum = Column(String(30), nullable=False, default="Planlandı")  # AUDIT_PROGRAM_ITEM_DURUMLARI
    # 2026 — F06 İç&Dış Denetim Planı (boş bırakılabilir)
    kalem_adi = Column(String(200), nullable=True)                 # 'D06 KALİTE KONTROL YÖNETİMİ', 'Kalıphane Saha Denetimi'
    kategori = Column(String(30), nullable=True)                   # DENETIM_KATEGORILERI
    plan_gunu = Column(Integer, nullable=True)
    denetci_metin = Column(String(200), nullable=True)             # plandaki denetçi yazımı (ör. '3&7&10', 'İSG UZMANI')
    ekip_json = Column(Text, nullable=True)                        # atanan ekip
    checklist_id = Column(Integer, ForeignKey("audit_checklists.id", ondelete="SET NULL"), nullable=True)
    notlar = Column(Text, nullable=True)

    program = relationship("AuditProgram", back_populates="items")
    surec = relationship("Process")
    tetkik_eden = relationship("User")
    audit = relationship("InternalAudit")


DENETIM_KATEGORILERI = ("Süreç", "Saha", "Proses", "Ürün", "Tedarikçi", "Sertifikasyon", "Diğer")
DENETCI_NITELIKLERI = (  # (anahtar, etiket) — F06 'Denetçi Kalifikasyonları' matrisi
    ("iatf", "IATF 16949"), ("iso19011", "ISO 19011"), ("fmea", "FMEA"), ("ppap", "PPAP"), ("apqp", "APQP"),
    ("msa", "MSA"), ("spc", "SPC"), ("csr", "Müşteri Özel İstekleri"), ("sektorel", "Sektörel Tecrübe"),
    ("iso14001", "ISO 14001"), ("iso45001", "ISO 45001"), ("iso27001", "ISO 27001"),
)


class DenetciYetkinlik(Base):
    """İç denetçi yetkinlik kaydı (IATF 16949 § 7.2.3). Yeni tablo; eski sürüm kullanmaz."""
    __tablename__ = "denetci_yetkinlikleri"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    denetci_no = Column(Integer, nullable=True)                    # F06 matrisindeki sıra no (plan kodları buna göre)
    ad_soyad = Column(String(100), nullable=False)
    nitelikler_json = Column(Text, nullable=True)                  # {"iatf":true,"fmea":true,...}
    denetim_turleri = Column(String(20), nullable=True)            # "S,P,Ü"
    kendi_surecleri = Column(String(200), nullable=True)           # bağımsızlık: "D04,M03" — bu süreçleri denetleyemez
    rol = Column(String(30), nullable=True)                        # Baş Denetçi / Denetçi / Stajer
    gecerlilik_tarihi = Column(Date, nullable=True)                # yetkinlik geçerlilik sonu (eğitim yenileme)
    son_egitim_tarihi = Column(Date, nullable=True)
    yillik_hedef = Column(Integer, nullable=True)
    gecen_yil_denetim = Column(Integer, nullable=True)
    aktif = Column(Boolean, nullable=False, default=True)
    notlar = Column(Text, nullable=True)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User")

    @property
    def nitelikler(self):
        try:
            return json.loads(self.nitelikler_json or "{}") or {}
        except ValueError:
            return {}

    @property
    def turler(self):
        return [t.strip() for t in (self.denetim_turleri or "").split(",") if t.strip()]

    @property
    def surec_listesi(self):
        return [t.strip().upper() for t in (self.kendi_surecleri or "").replace(";", ",").split(",") if t.strip()]


# ═══════════════════════════════════════════════════════════════════════════
#  Poka-Yoke (Hata Önleme Cihazı) — IATF 16949 § 10.2.4
# ═══════════════════════════════════════════════════════════════════════════
class PokaYokeDevice(Base):
    """Hata önleme cihazı ve periyodik test takibi."""
    __tablename__ = "pokayoke_devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad = Column(String(200), nullable=False)
    tip = Column(String(30), nullable=False, default="Mekanik")       # POKAYOKE_TIPLERI
    konum = Column(String(150), nullable=True)
    makine = Column(String(150), nullable=True)
    ilgili_proses_id = Column(Integer, ForeignKey("processes.id", ondelete="SET NULL"), nullable=True)
    fmea_item_id = Column(Integer, ForeignKey("fmea_items.id", ondelete="SET NULL"), nullable=True)
    test_periyodu_gun = Column(Integer, nullable=True, default=30)
    son_test_tarihi = Column(Date, nullable=True)
    sonraki_test_tarihi = Column(Date, nullable=True)
    durum = Column(String(20), nullable=False, default="Aktif")       # POKAYOKE_DURUMLARI
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    ilgili_proses = relationship("Process")
    sorumlu = relationship("User")
    tests = relationship("PokaYokeTest", back_populates="device",
                         cascade="all, delete-orphan", lazy="selectin")

    @property
    def kalan_gun(self):
        if not self.sonraki_test_tarihi:
            return None
        return (self.sonraki_test_tarihi - date.today()).days

    @property
    def test_gecikti(self):
        return self.kalan_gun is not None and self.kalan_gun < 0


class PokaYokeTest(Base):
    """Poka-Yoke cihazı periyodik test kaydı."""
    __tablename__ = "pokayoke_tests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(Integer, ForeignKey("pokayoke_devices.id", ondelete="CASCADE"), nullable=False)
    test_tarihi = Column(Date, nullable=True)
    sonuc = Column(String(20), nullable=False, default="Başarılı")    # POKAYOKE_TEST_SONUCLARI
    tester_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    bulgu = Column(Text, nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    device = relationship("PokaYokeDevice", back_populates="tests")
    tester = relationship("User")
    capa = relationship("CorrectiveAction")


# ═══════════════════════════════════════════════════════════════════════════
#  Garanti Yönetimi & Tedarikçi Geliştirme & Yerleşim Muayenesi (Faz C)
# ═══════════════════════════════════════════════════════════════════════════
class WarrantyClaim(Base):
    """Müşteri şikayeti ve garanti analiz sistemi (IATF 16949 § 10.2.5)."""
    __tablename__ = "warranty_claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    musteri = Column(String(200), nullable=False)
    parca_no = Column(String(100), nullable=False)
    miktar = Column(Integer, nullable=False, default=1)
    talep_tarihi = Column(Date, nullable=True)
    tutar = Column(Float, nullable=True)
    analiz_sonucu = Column(Text, nullable=True)                   # Kullanıcı Hatası, Üretim Hatası, Tasarım Hatası vb.
    durum = Column(String(30), nullable=False, default="Açık")    # Açık / Analiz Ediliyor / Sonuçlandırıldı
    analiz_tarihi = Column(Date, nullable=True)
    analiz_eden_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    capa_id = Column(Integer, ForeignKey("corrective_actions.id", ondelete="SET NULL"), nullable=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    analiz_eden = relationship("User", foreign_keys=[analiz_eden_id])
    capa = relationship("CorrectiveAction")


class SupplierDevelopmentPlan(Base):
    """Tedarikçi kalite yönetim sistemi geliştirme planı (IATF 16949 § 8.4.2.3)."""
    __tablename__ = "supplier_development_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tedarikci_adi = Column(String(200), nullable=False)
    baslangic_tarihi = Column(Date, nullable=True)
    hedef_tarih = Column(Date, nullable=True)
    durum = Column(String(30), nullable=False, default="Planlandı")  # Planlandı / Devam Ediyor / Tamamlandı / İptal
    hedef_sertifikasyon = Column(String(100), nullable=True)        # ISO 9001, IATF 16949 vb.
    son_denetim_skoru = Column(Float, nullable=True)
    notlar = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class LayoutInspection(Base):
    """Yerleşim planı muayenesi ve fonksiyonel test takibi (IATF 16949 § 8.6.2)."""
    __tablename__ = "layout_inspections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parca_no = Column(String(100), nullable=False)
    parca_adi = Column(String(200), nullable=False)
    periyot_ay = Column(Integer, nullable=False, default=12)
    son_muayene_tarihi = Column(Date, nullable=True)
    sonraki_muayene_tarihi = Column(Date, nullable=True)
    sonuc = Column(String(30), nullable=False, default="Değerlendirilmedi")  # Uygun / Uygunsuz / Değerlendirilmedi
    rapor_no = Column(String(100), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    aciklama = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    sorumlu = relationship("User")



# ═══════════════════════════════════════════════════════════════════════════
#  Dinamik Formlar (2026) — DYS'de elektronik doldurulan formlar
#  Mevcut modüllere (DÖF, Uygun Olmayan Ürün, Kalibrasyon vb.) uymayan formlar
#  için alan tanımı JSON ile yapılır; her doldurma bir kayıt (FormKayit) olur.
# ═══════════════════════════════════════════════════════════════════════════
FORM_ALAN_TIPLERI = ("baslik", "metin", "uzun_metin", "sayi", "tarih", "saat", "secim", "coklu_secim", "evet_hayir",
                     "kullanici", "tablo", "kontrol_listesi", "hesap")
# hesap: {"ad","kod","tip":"hesap","formul":"a*b","birim","ondalik","ozet"} — form_hesap.py (sayı alanları "kod" ile değişken olur)
# kontrol_listesi: {"ad","tip":"kontrol_listesi","maddeler":[...],"secenekler":["Uygun","Uygun Değil","Kapsam Dışı"],
#                   "aciklama_zorunlu":["Uygun Değil"]}  · tablo: {"ad","tip":"tablo","sutunlar":[...],"satir":5}
# baslik: yalnızca ara başlık (veri tutmaz)
FORM_KAYIT_DURUMLARI = ("Taslak", "Onayda", "Onaylandı", "Reddedildi", "İptal")


class FormTanim(Base):
    __tablename__ = "form_tanimlari"

    id = Column(Integer, primary_key=True, autoincrement=True)
    form_kodu = Column(String(30), nullable=False, unique=True)       # D04.5 F04
    eski_kod = Column(String(30), nullable=True)                      # D06 F04
    ad = Column(Unicode(200), nullable=False)
    surec_id = Column(Integer, ForeignKey("processes.id"), nullable=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)  # bağlı Form doküman kaydı
    revizyon_no = Column(Integer, nullable=False, default=0)
    alanlar_json = Column(Text, nullable=False, default="[]")         # [{"ad","tip","zorunlu","secenekler",...}]
    onay_akisi = Column(String(200), nullable=True, default="Dolduran,Onaylayan")
    saklama_suresi_ay = Column(Integer, nullable=True)
    aktif = Column(Boolean, nullable=True, default=True)
    # KVKK — kısıtlı erişim: {"kisitli": true, "kullanicilar": [user_id, ...], "gerekce": "..."}; kayıtları yalnız kaydı giren,
    # listedeki yetkililer ve Admin görür / onaylar; ortak "Form Verileri" klasörüne yazılmaz (form_erisim.py)
    erisim_json = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    surec = relationship("Process")
    document = relationship("Document")

    @property
    def erisim(self):
        try:
            d = json.loads(self.erisim_json or "{}") or {}
        except ValueError:
            d = {}
        return d if isinstance(d, dict) else {}

    @property
    def kisitli(self):
        return bool(self.erisim.get("kisitli"))


class FormKayit(Base):
    __tablename__ = "form_kayitlari"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tanim_id = Column(Integer, ForeignKey("form_tanimlari.id"), nullable=False)
    kayit_no = Column(String(40), nullable=False, unique=True)        # D04.5 F04-2026-0001
    form_revizyon_no = Column(Integer, nullable=True)
    veriler_json = Column(Text, nullable=False, default="{}")
    durum = Column(String(20), nullable=False, default="Onayda")
    olusturan_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    onaylayan_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    onay_notu = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    onay_tarihi = Column(DateTime, nullable=True)

    tanim = relationship("FormTanim", backref="kayitlar")
    olusturan = relationship("User", foreign_keys=[olusturan_id])
    onaylayan = relationship("User", foreign_keys=[onaylayan_id])


class SistemAyar(Base):
    """Basit anahtar–değer sistem ayarları (ör. kapalı modüller)."""
    __tablename__ = "sistem_ayarlari"

    anahtar = Column(String(100), primary_key=True)
    deger = Column(Text, nullable=True)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# ═══════════════════════════════════════════════════════════════════════════
#  Stratejik Planlama (2026) — Y01 F02 / F03 yerine modül:
#  bağlam analizi → paydaş analizi → SWOT → stratejiler → öncelikli stratejiler (uygulama ve gözden geçirme)
# ═══════════════════════════════════════════════════════════════════════════
STRATEJI_BOLUMLERI = ("baglam", "paydas", "swot", "strateji", "eylem")
STRATEJI_KAPSAMLARI = ("Kurumsal", "İSG & Çevre")
STRATEJI_BAGLAM_BILESENLERI = ("TEKNOLOJİ", "EKONOMİ", "POLİTİKA", "SOSYOLOJİ VE KÜLTÜR", "EKOLOJİ ve GÜVENLİK",
                               "PAZAR VE RAKİPLER", "YASAL DÜZENLEMELER", "İÇ ÇEVRE")
STRATEJI_SWOT_TURLERI = ("Güçlü", "Zayıf", "Fırsat", "Tehdit")
STRATEJI_EYLEM_DURUMLARI = ("Planlandı", "Devam Ediyor", "Tamamlandı", "Ertelendi", "İptal")


class StratejiDonem(Base):
    """Stratejik planlama dönemi (yıl): bölüm bazında gözden geçirme tarihleri ve notlar."""
    __tablename__ = "strateji_donemleri"

    id = Column(Integer, primary_key=True, autoincrement=True)
    yil = Column(Integer, nullable=False, unique=True)
    gozden_gecirme_json = Column(Text, nullable=True)     # {"baglam": "2026-06-13", "swot": "2026-08-21", ...}
    notlar = Column(Text, nullable=True)
    kaynak = Column(Unicode(300), nullable=True)          # içe aktarılan Excel dosyası
    olusturma_tarihi = Column(DateTime, default=datetime.now)

    @property
    def gozden_gecirme(self):
        try:
            return json.loads(self.gozden_gecirme_json or "{}") or {}
        except ValueError:
            return {}


class StratejiKalem(Base):
    """Stratejik planlamanın tek bir satırı; 'bolum' hangi tablo olduğunu, 'veri_json' bölüme özgü alanları taşır:
    baglam  : grup = çevre bileşeni, metin = konu, veri {kaynak, analiz}
    paydas  : metin = paydaş, veri {statu, etki, faaliyet_etkisi, beklenti, isg_cevre_beklenti}
    swot    : grup = Güçlü / Zayıf / Fırsat / Tehdit, metin
    strateji: kod, metin, veri {ilgili_paydas, swot}
    eylem   : kod, metin (öncelik sırasıyla), termin, durum, sorumlu, veri {kbf, kaynak, gg_notu, gerceklesen}"""
    __tablename__ = "strateji_kalemleri"

    id = Column(Integer, primary_key=True, autoincrement=True)
    yil = Column(Integer, nullable=False, index=True)
    bolum = Column(String(20), nullable=False)
    kapsam = Column(String(30), nullable=False, default="Kurumsal")
    grup = Column(Unicode(80), nullable=True)
    sira = Column(Integer, nullable=True)
    kod = Column(String(20), nullable=True)
    metin = Column(Text, nullable=False)
    veri_json = Column(Text, nullable=True)
    termin = Column(Date, nullable=True)
    durum = Column(String(20), nullable=True)
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    sorumlu = relationship("User")

    @property
    def veri(self):
        try:
            return json.loads(self.veri_json or "{}") or {}
        except ValueError:
            return {}


# ═══════════════════════════════════════════════════════════════════════════
#  Toplantı Tutanakları (2026) — Y01.4 İletişim: F02 Günlük Toplantı Üretim & Bakım, F03 İç Paydaş Toplantı Tutanağı,
#  F04 (Aylık Kalite) Toplantı Tutanağı yerine; kararlar sorumlu / termin ile takip edilir, gerekirse DÖF açılır.
# ═══════════════════════════════════════════════════════════════════════════
TOPLANTI_TURLERI = ("Günlük Toplantı – Üretim & Bakım", "Aylık Kalite Toplantısı", "İç Paydaş Toplantısı",
                    "Proje Toplantısı", "Diğer Toplantı")
TOPLANTI_MADDE_DURUMLARI = ("Açık", "Tamamlandı", "İptal")


class ToplantiTutanagi(Base):
    __tablename__ = "toplanti_tutanaklari"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tutanak_no = Column(String(30), nullable=False, unique=True)      # TT-2026-0001
    tur = Column(String(60), nullable=False)
    tarih = Column(Date, nullable=False)
    saat = Column(String(10), nullable=True)
    yer = Column(Unicode(120), nullable=True)
    konu = Column(Unicode(250), nullable=True)
    katilimcilar = Column(Text, nullable=True)
    gundem = Column(Text, nullable=True)
    notlar = Column(Text, nullable=True)
    veri_json = Column(Text, nullable=True)                           # türe özgü alanlar (ör. F02 vaka başlıkları)
    durum = Column(String(20), nullable=False, default="Taslak")      # Taslak / Yayınlandı
    olusturan_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    olusturan = relationship("User")
    maddeler = relationship("ToplantiMaddesi", back_populates="tutanak", cascade="all, delete-orphan",
                            order_by="ToplantiMaddesi.sira", lazy="selectin")

    @property
    def veri(self):
        try:
            return json.loads(self.veri_json or "{}") or {}
        except ValueError:
            return {}


class ToplantiMaddesi(Base):
    """Toplantıda görüşülen konu / karar / aksiyon."""
    __tablename__ = "toplanti_maddeleri"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tutanak_id = Column(Integer, ForeignKey("toplanti_tutanaklari.id", ondelete="CASCADE"), nullable=False)
    sira = Column(Integer, nullable=True)
    konu = Column(Unicode(250), nullable=True)                        # başlık / süreç / öneri sahibi
    gorusme = Column(Text, nullable=True)                             # görüşülen / öneri-şikayet / gözden geçirme sonucu
    karar = Column(Text, nullable=True)
    # MSSQL: users → tutanak (SET NULL) → madde (CASCADE) ile ikinci kaskad yolu olmasın diye bu iki FK kaskadsız (1785)
    sorumlu_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sorumlu_metin = Column(Unicode(120), nullable=True)
    termin = Column(Date, nullable=True)
    durum = Column(String(20), nullable=False, default="Açık")
    gerceklesme_tarihi = Column(Date, nullable=True)
    dof_id = Column(Integer, ForeignKey("corrective_actions.id"), nullable=True)

    tutanak = relationship("ToplantiTutanagi", back_populates="maddeler")
    sorumlu = relationship("User")
    dof = relationship("CorrectiveAction")


# ═══════════════════════════════════════════════════════════════════════════
#  Performans ve CAPA (2026) — kpi.debak.com'un DYS'ye taşınmış hali (Y01.8 Performans Değerlendirme, eski Y03 F06)
#  Veri yapısı kpi.debak.com ile aynı (kimlikler korunur): süreç, KPI, ölçüm, NCR (IATF 10.2), NCR atlama.
# ═══════════════════════════════════════════════════════════════════════════
PK_PERIYOTLAR = (("1A", "Aylık"), ("3A", "3 aylık"), ("6A", "6 aylık"), ("1Y", "Yıllık"))
PK_NCR_ASAMALARI = (("kayit", "Açık"), ("kok_neden", "Kök neden"), ("plan", "Planlandı"), ("uygulama", "Gerçekleşti"), ("kapali", "Kapalı"))
PK_NCR_TURLERI = (("kpi-miss", "KPI sapması"), ("customer", "Müşteri şikâyeti"), ("internal", "İç uygunsuzluk"),
                  ("audit", "Tetkik bulgusu"), ("supplier", "Tedarikçi"), ("process", "Proses sapması"))


class PkSurec(Base):
    __tablename__ = "pk_surecler"
    id = Column(String(20), primary_key=True)                 # D01 … Y03 (Y03 F06 süreç kodu)
    ad = Column(Unicode(150), nullable=False)
    baslik = Column(Unicode(250), nullable=True)
    aile = Column(String(5), nullable=True)                   # D / M / Y
    yeni_surec = Column(String(10), nullable=True)            # 11 süreçlik yapıdaki karşılığı
    ham_json = Column(Text, nullable=True)                     # kpi.debak.com arayüzünün nesnesi (birebir)


class PkKpi(Base):
    __tablename__ = "pk_kpiler"
    id = Column(String(40), primary_key=True)                 # D01-01
    surec_id = Column(String(20), nullable=False, index=True)
    sira = Column(String(10), nullable=True)
    prosedur = Column(Unicode(300), nullable=True)
    ad = Column(Unicode(300), nullable=False)
    periyot = Column(String(4), nullable=False, default="1A")  # 1A / 3A / 6A / 1Y
    sorumlu = Column(Unicode(120), nullable=True)
    hedef_metin = Column(Unicode(120), nullable=True)
    birim = Column(Unicode(20), nullable=True)
    yon = Column(String(10), nullable=False, default="higher")  # higher / lower / band
    izleme = Column(Boolean, nullable=False, default=False)     # izlemeye yönelik (hedefsiz)
    hedef = Column(Float, nullable=True)
    sari_esik = Column(Float, nullable=True)
    bant_alt = Column(Float, nullable=True)
    bant_ust = Column(Float, nullable=True)
    oran_olcek = Column(Boolean, nullable=False, default=False)  # 0–1 girilir, % gösterilir
    gecmis_json = Column(Text, nullable=True)                  # {"2024": 97.6, "2025": 95.5}
    yil = Column(Integer, nullable=True)
    aktif = Column(Boolean, nullable=False, default=True)
    ham_json = Column(Text, nullable=True)                     # kpi.debak.com arayüzünün nesnesi (birebir)

    @property
    def gecmis(self):
        try:
            return json.loads(self.gecmis_json or "{}") or {}
        except ValueError:
            return {}


class PkOlcum(Base):
    __tablename__ = "pk_olcumler"
    id = Column(String(60), primary_key=True)
    kpi_id = Column(String(40), nullable=False, index=True)
    donem = Column(String(7), nullable=False)                  # 2026-03 (GG penceresinin son ayı)
    gerceklesen = Column(Float, nullable=True)
    metin = Column(Unicode(250), nullable=True)
    giren = Column(Unicode(120), nullable=True)
    girilme = Column(DateTime, nullable=True)
    ham_json = Column(Text, nullable=True)                     # kpi.debak.com arayüzünün nesnesi (birebir)


class PkNcr(Base):
    """IATF 10.2 uygunsuzluk ve düzeltici faaliyet kaydı (kpi.debak.com NCR)."""
    __tablename__ = "pk_ncr"
    id = Column(String(60), primary_key=True)
    numara = Column(String(30), nullable=False)
    tur = Column(String(20), nullable=False, default="kpi-miss")
    onem = Column(String(10), nullable=True, default="minor")
    baslik = Column(Unicode(300), nullable=False)
    aciklama = Column(Text, nullable=True)
    kpi_id = Column(String(40), nullable=True, index=True)
    donem = Column(String(7), nullable=True)
    surec_id = Column(String(20), nullable=True)
    iatf_madde = Column(String(40), nullable=True)
    tespit_tarihi = Column(Date, nullable=True)
    tespit_eden = Column(Unicode(120), nullable=True)
    sorumlu = Column(Unicode(120), nullable=True)
    termin = Column(Date, nullable=True)
    asama = Column(String(12), nullable=False, default="kayit")
    ekip = Column(Unicode(250), nullable=True)
    koruma = Column(Text, nullable=True)
    koruma_tarihi = Column(Date, nullable=True)
    kok_neden = Column(Text, nullable=True)
    bes_neden_json = Column(Text, nullable=True)               # ["…", "", "", "", ""]
    hata_onleme = Column(Text, nullable=True)
    kys_degisiklik = Column(Boolean, nullable=True, default=False)
    kys_degisiklik_notu = Column(Text, nullable=True)
    risk_guncelleme = Column(Text, nullable=True)
    etkinlik_kaniti = Column(Text, nullable=True)
    etkinlik_tarihi = Column(Date, nullable=True)
    kapanis_tarihi = Column(Date, nullable=True)
    kapatan = Column(Unicode(120), nullable=True)
    aksiyonlar_json = Column(Text, nullable=True)              # [{id, kind, description, owner, dueDate, status, completedAt, evidence, actual}]
    olaylar_json = Column(Text, nullable=True)                 # [{at, by, text}]
    dof_id = Column(Integer, ForeignKey("corrective_actions.id"), nullable=True)   # DYS DÖF modülündeki eşi
    ham_json = Column(Text, nullable=True)                     # kpi.debak.com arayüzünün nesnesi (birebir)

    @property
    def aksiyonlar(self):
        try:
            return json.loads(self.aksiyonlar_json or "[]") or []
        except ValueError:
            return []

    @property
    def olaylar(self):
        try:
            return json.loads(self.olaylar_json or "[]") or []
        except ValueError:
            return []

    @property
    def bes_neden(self):
        try:
            v = json.loads(self.bes_neden_json or "[]") or []
        except ValueError:
            v = []
        return (v + [""] * 5)[:5]


class PkNcrAtlama(Base):
    """Uygunsuz dönem için bilerek NCR açılmadı (kpi.debak.com ncrSkips)."""
    __tablename__ = "pk_ncr_atlamalari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kpi_id = Column(String(40), nullable=False)
    donem = Column(String(7), nullable=False)
    aciklama = Column(Unicode(250), nullable=True)


# ═══════════════════════════════════════════════════════════════════
#  KALİTE — Tedarikçi Uygunsuzlukları (D02 F11) ve Müşteri Şikayetleri (Y01.7 F03) (2026)
#  Alan tanımları (TR / EN başlıklar) kalite_spec.py'de; sık kullanılan alanlar kolonda, diğerleri veri_json'da.
# ═══════════════════════════════════════════════════════════════════
KALITE_DURUMLARI_TU = ("Açık", "Tedarikçi yanıtı bekleniyor", "Değerlendirmede", "Etkinlik doğrulamada", "Kapatıldı", "İptal")
KALITE_DURUMLARI_MS = ("Açık", "Geçici önlem (D3)", "Kök neden analizi (D4)", "Aksiyonlar (D5–D6)", "Etkinlik doğrulamada (D7)", "Kapatıldı", "İptal")
KALITE_ONEM = ("Kritik", "Major", "Minor")


class TedarikciUygunsuzluk(Base):
    __tablename__ = "tedarikci_uygunsuzluklari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    no = Column(String(30), nullable=False, unique=True)                 # TU-2026-001
    tarih = Column(Date, nullable=False)
    tedarikci_id = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    tedarikci_ad = Column(Unicode(200), nullable=True)
    urun_no = Column(Unicode(80), nullable=True)
    urun_adi = Column(Unicode(200), nullable=True)
    onem = Column(String(10), nullable=True)
    hatali_miktar = Column(Float, nullable=True)
    termin_8d = Column(Date, nullable=True)
    durum = Column(Unicode(40), nullable=False, default="Açık")
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturan_id = Column(Integer, nullable=True)
    kapanis_tarihi = Column(Date, nullable=True)
    gonderim_tarihi = Column(DateTime, nullable=True)                    # rapor tedarikçiye gönderildi
    dof_id = Column(Integer, ForeignKey("corrective_actions.id"), nullable=True)   # MSSQL 1785: kademesiz
    veri_json = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    tedarikci = relationship("Supplier")
    sorumlu = relationship("User", foreign_keys=[sorumlu_id])

    @property
    def veri(self):
        try:
            return json.loads(self.veri_json or "{}") or {}
        except ValueError:
            return {}


class MusteriSikayeti(Base):
    __tablename__ = "musteri_sikayetleri"
    id = Column(Integer, primary_key=True, autoincrement=True)
    no = Column(String(30), nullable=False, unique=True)                 # MS-2026-001
    tarih = Column(Date, nullable=False)
    musteri = Column(Unicode(200), nullable=True)
    musteri_ref = Column(Unicode(80), nullable=True)                     # müşterinin şikayet / claim no
    urun_no = Column(Unicode(80), nullable=True)
    urun_adi = Column(Unicode(200), nullable=True)
    onem = Column(String(10), nullable=True)
    hatali_miktar = Column(Float, nullable=True)
    termin_d3 = Column(Date, nullable=True)
    termin_8d = Column(Date, nullable=True)
    durum = Column(Unicode(40), nullable=False, default="Açık")
    sorumlu_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    olusturan_id = Column(Integer, nullable=True)
    kapanis_tarihi = Column(Date, nullable=True)
    gonderim_tarihi = Column(DateTime, nullable=True)                    # 8D raporu müşteriye gönderildi
    dof_id = Column(Integer, ForeignKey("corrective_actions.id"), nullable=True)
    veri_json = Column(Text, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    sorumlu = relationship("User", foreign_keys=[sorumlu_id])

    @property
    def veri(self):
        try:
            return json.loads(self.veri_json or "{}") or {}
        except ValueError:
            return {}


class KaliteGorsel(Base):
    """Tedarikçi uygunsuzluğu / müşteri şikayeti görseli (hatalı parça, referans OK parça, etiket, ambalaj …)."""
    __tablename__ = "kalite_gorselleri"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tur = Column(String(2), nullable=False)                              # TU / MS
    kayit_id = Column(Integer, nullable=False, index=True)
    dosya_adi = Column(Unicode(200), nullable=False)
    dosya_yolu = Column(Unicode(500), nullable=False)
    etiket = Column(Unicode(40), nullable=True)                          # Hatalı (NOK) / Referans (OK) / Etiket …
    aciklama = Column(Unicode(300), nullable=True)
    sira = Column(Integer, nullable=True)
    pdfte = Column(Boolean, nullable=True, default=True)                 # rapora eklensin mi
    yukleyen_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    depo_id = Column(Integer, nullable=True)                            # dosya_depo.id

class BakimEkipman(Base):
    """2026 — D03 bakım ekipmanı (makine, yardımcı ekipman, kalıp). Tür + marka, Bakım Formu ekranında uygun şablonları belirler."""
    __tablename__ = "bakim_ekipmanlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kod = Column(Unicode(40), nullable=False, unique=True)              # K6, AP4, TORNA-B, kalıp kodu …
    ad = Column(Unicode(150), nullable=False)
    tur = Column(Unicode(60), nullable=False, index=True)               # bakim_formlari.json → ekipman_turleri
    marka = Column(Unicode(60), nullable=True)
    model = Column(Unicode(120), nullable=True)
    seri_no = Column(Unicode(80), nullable=True)
    bolum = Column(Unicode(60), nullable=True)                          # Üretim / Kalıphane / Kalite …
    baski_omru = Column(Integer, nullable=True)                         # kalıp: toplam baskı ömrü
    aktif = Column(Boolean, nullable=True, default=True)
    notlar = Column(Unicode(500), nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class BakimKaydi(Base):
    """D03 bakım formu kaydı (periyodik bakım, günlük kontrol, bakım kartı satırı, kalıp bakımı). Maddeler JSON: [{no, grup, madde, durum, olcum, aciklama}]."""
    __tablename__ = "bakim_kayitlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kayit_no = Column(Unicode(30), nullable=True)
    ekipman_id = Column(Integer, nullable=False, index=True)
    form_kod = Column(Unicode(20), nullable=False, index=True)          # D03 F05 …
    form_ad = Column(Unicode(150), nullable=True)
    kategori = Column(Unicode(40), nullable=True)
    tarih = Column(Date, nullable=False)
    bakimci = Column(Unicode(200), nullable=True)
    sure_dk = Column(Integer, nullable=True)
    calisma_saati = Column(Integer, nullable=True)                      # saat sayacı (saatlik bakımlar)
    baski_sayisi = Column(Integer, nullable=True)                       # kalıp
    is_emri = Column(Unicode(60), nullable=True)
    lot_no = Column(Unicode(60), nullable=True)
    urun = Column(Unicode(150), nullable=True)
    sonuc = Column(Unicode(40), nullable=True)                          # Tamamlandı / Tamamlanmadı / Arıza tespit edildi
    maddeler_json = Column(Text, nullable=True)
    uygunsuz_sayisi = Column(Integer, nullable=True)
    degisen_parcalar = Column(Unicode(1000), nullable=True)
    aciklama = Column(Unicode(1000), nullable=True)
    olusturan_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class FmeaSayfa(Base):
    """2026 — FMEA çalışma sayfası (GSI-RD-370 Rev. C02): D-FMEA (D), P-FMEA (P) veya FMEA-MSR (M). Satırlar FmeaSatir."""
    __tablename__ = "fmea_sayfalari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    sayfa_no = Column(Unicode(30), nullable=False, unique=True)         # FS-2026-001
    tip = Column(Unicode(1), nullable=False)                            # D / P / M
    baslik = Column(Unicode(200), nullable=False)
    parca_proses = Column(Unicode(200), nullable=True)
    musteri = Column(Unicode(150), nullable=True)
    ekip = Column(Unicode(300), nullable=True)
    revizyon = Column(Unicode(10), nullable=True)
    tarih = Column(Date, nullable=True)
    aciklama = Column(Unicode(1000), nullable=True)
    olusturan_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class FmeaSatir(Base):
    """FMEA çalışma sayfası satırı. MSR'de F `O` alanında, M `D` alanında tutulur (referans uygulamayla aynı).
    AP / RPN / AP′ saklanmaz, fmea_core ile her seferinde hesaplanır."""
    __tablename__ = "fmea_satirlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    sayfa_id = Column(Integer, nullable=False, index=True)
    sira = Column(Integer, nullable=False, default=0)
    item = Column(Text, nullable=True)       # parça / proses adımı / sistem elemanı
    fn = Column(Text, nullable=True)         # fonksiyon / gereksinim
    eff = Column(Text, nullable=True)        # hata etkisi
    S = Column(Integer, nullable=True)
    mode = Column(Text, nullable=True)       # hata modu
    cause = Column(Text, nullable=True)      # hata nedeni
    prev = Column(Text, nullable=True)       # önleyici aksiyon (MSR: sıklık gerekçesi)
    O = Column(Integer, nullable=True)       # oluşma (MSR: sıklık F)
    det = Column(Text, nullable=True)        # tespit aksiyonu (MSR: mevcut izleme + sistem tepkisi)
    D = Column(Integer, nullable=True)       # tespit (MSR: izleme M)
    act = Column(Text, nullable=True)        # optimizasyon aksiyonu
    resp = Column(Unicode(150), nullable=True)
    date = Column(Unicode(60), nullable=True)
    st = Column(Unicode(30), nullable=True)
    O2 = Column(Integer, nullable=True)
    D2 = Column(Integer, nullable=True)


class ProsesAdimTanim(Base):
    """2026 — Proses adımı kataloğu (Enjeksiyon hazırlama, Enjeksiyon, Çapak alma, Fırın, Kalite kontrol …). FMEA / kontrol planı şablonları adıma bağlanır."""
    __tablename__ = "proses_adim_tanimlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ad = Column(Unicode(120), nullable=False, unique=True)
    sira = Column(Integer, nullable=False, default=0)
    aktif = Column(Boolean, nullable=True, default=True)
    aciklama = Column(Unicode(300), nullable=True)


class UrunProsesi(Base):
    """Ürünün proses akışı: müşteri → ürün → adımlar. Adım eklenince o adımın FMEA'sı ve kontrol planı şablonu otomatik gelir."""
    __tablename__ = "urun_prosesleri"
    id = Column(Integer, primary_key=True, autoincrement=True)
    musteri = Column(Unicode(150), nullable=False, index=True)
    urun_adi = Column(Unicode(200), nullable=False)
    urun_kodu = Column(Unicode(80), nullable=True)
    teknik_resim_id = Column(Integer, nullable=True)                    # teknik_resimler.id (isteğe bağlı)
    fmea_standart = Column(Unicode(8), nullable=True)                   # GSI (Valeo GSI-RD-370) | VDA (AIAG-VDA); boşsa GSI
    aciklama = Column(Unicode(500), nullable=True)
    aktif = Column(Boolean, nullable=True, default=True)
    olusturan_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    valeo_json = Column(Text, nullable=True)                           # Valeo formatı başlıkları: {akis:{no,tarih,rev,baslik}, fmea:{…}, cp:{no,iletisim,ekip,tarihler,onaylar,reaksiyon_tablo}}


class UrunProsesAdimi(Base):
    __tablename__ = "urun_proses_adimlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    urun_proses_id = Column(Integer, nullable=False, index=True)
    adim_tanim_id = Column(Integer, nullable=False)
    sira = Column(Integer, nullable=False, default=0)
    dugum_id = Column(Integer, nullable=True)                           # ürün ağacı düğümü (boşsa ürünün kendisi)
    valeo_json = Column(Text, nullable=True)                           # Valeo akış şeması: {kod, etiket, kutular:[{metin,sekil}], notlar:[…]} ve FMEA sayfa başlığı {fmea_baslik:{…}}


class UrunAgacDugumu(Base):
    """Ürün ağacı (elle girilir): ürün → yarı mamul → hammadde. Operasyonlar (UrunProsesAdimi) düğüme bağlanır; iş akış şeması buradan otomatik çizilir."""
    __tablename__ = "urun_agac_dugumleri"
    id = Column(Integer, primary_key=True, autoincrement=True)
    urun_proses_id = Column(Integer, nullable=False, index=True)
    ust_id = Column(Integer, nullable=True)                             # boşsa kök (ürünün kendisi)
    tur = Column(Unicode(20), nullable=False, default="Yarı Mamul")     # Ürün / Yarı Mamul / Hammadde
    kod = Column(Unicode(80), nullable=True)
    ad = Column(Unicode(200), nullable=False)
    miktar = Column(Unicode(40), nullable=True)
    sira = Column(Integer, nullable=False, default=0)


class PpapRevizyon(Base):
    """Ürünün PPAP dosyasının (iş akışı + FMEA + kontrol planı) revizyon anlık görüntüsü."""
    __tablename__ = "urun_ppap_revizyonlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    urun_proses_id = Column(Integer, nullable=False, index=True)
    rev_no = Column(Integer, nullable=False, default=0)
    tarih = Column(DateTime, default=datetime.now)
    neden = Column(Unicode(500), nullable=True)
    olusturan_id = Column(Integer, nullable=True)
    ozet_hash = Column(Unicode(64), nullable=True)
    veri_json = Column(Text, nullable=True)


class ProsesFmeaSatiri(Base):
    """FMEA satırı (P-FMEA, GSI-RD-370): urun_proses_id boşsa o adım için GENEL (şablon) satır — tüm ürünlerde otomatik görünür;
    doluysa yalnız o ürüne özgü. AP/RPN saklanmaz (fmea_core)."""
    __tablename__ = "proses_fmea_satirlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    adim_tanim_id = Column(Integer, nullable=False, index=True)
    urun_proses_id = Column(Integer, nullable=True, index=True)
    standart = Column(Unicode(8), nullable=True)                        # GSI | VDA (boşsa GSI) — kütüphaneler ayrı yürür
    sira = Column(Integer, nullable=False, default=0)
    fn = Column(Text, nullable=True)
    eff = Column(Text, nullable=True)
    S = Column(Integer, nullable=True)
    mode = Column(Text, nullable=True)
    cause = Column(Text, nullable=True)
    prev = Column(Text, nullable=True)
    O = Column(Integer, nullable=True)
    det = Column(Text, nullable=True)
    D = Column(Integer, nullable=True)
    act = Column(Text, nullable=True)
    O2 = Column(Integer, nullable=True)
    D2 = Column(Integer, nullable=True)
    valeo_json = Column(Text, nullable=True)                           # Valeo ham satır: {req, sinif, S, O, D, rpn, sorumlu, alinan, S2, O2, D2, rpn2} (yazdırmada, satır değişmemişse kullanılır)


class ProsesKpSatiri(Base):
    """Kontrol planı satırı. urun_proses_id boşsa adım için ŞABLON (karakteristik listesi); doluysa ürüne özgü satır
    (toleranslı — ölçü girişi bu satırlara yapılır). kaynak_id: şablondan gelmişse şablon satırı."""
    __tablename__ = "proses_kp_satirlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    adim_tanim_id = Column(Integer, nullable=False, index=True)
    urun_proses_id = Column(Integer, nullable=True, index=True)
    kaynak_id = Column(Integer, nullable=True)
    sira = Column(Integer, nullable=False, default=0)
    karakteristik = Column(Unicode(200), nullable=True)
    tur = Column(Unicode(12), nullable=True)                            # Ölçü / Görsel
    birim = Column(Unicode(20), nullable=True)
    nominal = Column(Float, nullable=True)
    alt_limit = Column(Float, nullable=True)
    ust_limit = Column(Float, nullable=True)
    ozel = Column(Unicode(4), nullable=True)                            # CC / SC
    olcum_araci = Column(Unicode(150), nullable=True)
    numune = Column(Integer, nullable=True)                             # her kontrolde numune adedi
    siklik = Column(Unicode(100), nullable=True)
    yontem = Column(Unicode(300), nullable=True)
    reaksiyon = Column(Unicode(400), nullable=True)
    fmea_satir_id = Column(Integer, nullable=True)                      # ilgili FMEA satırı
    valeo_json = Column(Text, nullable=True)                           # Valeo ham satır: {surec_no, surec_ad, ekipman, no, urun, proses, ozel, sartname, teknik, numune, siklik, kontrol, kayit, reaksiyon}


class OlcumKaydi(Base):
    """Ölçü girişi: yalnız değer girilir; karar (OK / NOK) limitlerden otomatik. Bir kontrol = ortak 'grup' kodu."""
    __tablename__ = "proses_olcum_kayitlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    grup = Column(Unicode(30), nullable=False, index=True)
    urun_proses_id = Column(Integer, nullable=False, index=True)
    adim_id = Column(Integer, nullable=False)                           # urun_proses_adimlari.id
    kp_satir_id = Column(Integer, nullable=False, index=True)
    tarih = Column(DateTime, default=datetime.now)
    vardiya = Column(Unicode(10), nullable=True)
    is_emri = Column(Unicode(40), nullable=True)
    lot_no = Column(Unicode(40), nullable=True)
    numune_no = Column(Integer, nullable=False, default=1)
    deger = Column(Float, nullable=True)
    sonuc = Column(Unicode(4), nullable=False)                          # OK / NOK
    olcen_id = Column(Integer, nullable=True)


TR_DAGITIM = ("Yönetim", "Laboratuvar", "Kalite", "Üretim")              # D04.4 F02 dağıtım sütunları
TR_ETKILER = ("Kontrol Planı", "PFMEA", "Kalıp / Takım", "Mastar / Aparat", "Ölçüm Programı (CMM)", "İş / Kontrol Talimatı",
              "Ambalaj / Etiket", "PPAP / Numune Onayı", "Tedarikçi / Hammadde", "Stok / Yarı Mamul")


class TeknikResim(Base):
    """2026 — müşteri teknik resmi (ürün bazında). Akış: müşteri → ürün → revizyonlar (D04.4 F02 Teknik Resim Takip Listesi yerine)."""
    __tablename__ = "teknik_resimler"
    id = Column(Integer, primary_key=True, autoincrement=True)
    musteri = Column(Unicode(150), nullable=False, index=True)
    urun_adi = Column(Unicode(200), nullable=False)                      # resim adı (ör. MOVING CONTACT PIN)
    urun_kodu = Column(Unicode(80), nullable=True)                       # DEBAK ürün / parça no
    resim_no = Column(Unicode(80), nullable=False)                       # müşteri resim no (ör. A221164_001)
    aciklama = Column(Unicode(500), nullable=True)
    aktif = Column(Boolean, nullable=True, default=True)                 # pasif: ürün devreden çıktı
    olusturan_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    guncelleme_tarihi = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class TeknikResimRevizyon(Base):
    """Teknik resim revizyonu: dosya (manuel yükleme), müşteri revizyon bilgisi, teslim alma, dağıtım ve değişiklik değerlendirmesi."""
    __tablename__ = "teknik_resim_revizyonlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resim_id = Column(Integer, nullable=False, index=True)
    revizyon = Column(Unicode(40), nullable=False)                       # müşteri revizyonu (A, B, C, REV01, _001 …)
    revizyon_tarihi = Column(Date, nullable=True)                        # resimdeki revizyon tarihi
    teslim_alma_tarihi = Column(Date, nullable=True)                     # DEBAK'a ulaştığı tarih
    durum = Column(Unicode(20), nullable=False, default="Geçerli")       # Geçerli / Geçersiz
    degisiklik = Column(Unicode(1000), nullable=True)                    # değişiklik özeti
    dosya_adi = Column(Unicode(260), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    dagitim_json = Column(Text, nullable=True)                    # {bölüm: {tarih, toplandi}}
    degerlendirme_json = Column(Text, nullable=True)              # {tarih, kim, etkiler[], ppap, not}
    gecersiz_tarihi = Column(Date, nullable=True)
    ek_dosyalar_json = Column(Text, nullable=True)                       # [{ad, yol, tur}] — sapma, 3D model, görsel kopya …
    kaynak = Column(Unicode(60), nullable=True)                          # 'Toplu aktarım …' — sistem öncesi resim
    yukleyen_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)
    depo_id = Column(Integer, nullable=True)                            # dosya_depo.id (ana resim dosyası)


TR_EK_TURLERI = ("Sapma onayı (deviation)", "3D model (STEP)", "Görsel kopya", "Geçiş resmi", "Spesifikasyon", "Mastar resmi", "Diğer")


EK_KANIT_TIPLERI = ("Ölçüm Raporu", "Kontrol Planı", "PFMEA", "Proses Akış Şeması", "SPC / MSA", "İş / Kontrol Talimatı", "Kayıt / Form",
                    "Fotoğraf", "Etiket / Ambalaj", "Malzeme Sertifikası", "Ürün Uygunluk Raporu", "Diğer")

class AuditEkKanit(Base):
    """2026 — denetime (özellikle proses / ürün denetimi) eklenen ek kanıt: yüklenen dosya ya da DYS'deki bir doküman bağlantısı.
    İsteğe bağlı olarak bir soruya bağlanır. FK yok (kalite_gorselleri gibi); bütünlük uygulamada korunur."""
    __tablename__ = "audit_ek_kanitlar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(Integer, nullable=False, index=True)
    question_id = Column(Integer, nullable=True)
    kanit_tipi = Column(Unicode(40), nullable=True)                      # EK_KANIT_TIPLERI
    aciklama = Column(Unicode(300), nullable=True)
    dosya_adi = Column(Unicode(260), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    dokuman_id = Column(Integer, nullable=True)                          # DYS dokümanı (kontrol planı, PFMEA …)
    yukleyen_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


def _migrate_mssql_yeni_yapi():
    """SQL Server: yalnızca 11 süreçlik yapı için eklenen boş bırakılabilir kolonları, eksikse ekler.
    Mevcut kolonlara dokunmaz; tekrar çalıştırılması güvenlidir. Eski DYS sürümü bu kolonları görmezden gelir."""
    from sqlalchemy import text
    komutlar = [
        "IF COL_LENGTH('processes','ust_surec_id') IS NULL ALTER TABLE processes ADD ust_surec_id INT NULL",
        "IF COL_LENGTH('processes','aktif') IS NULL ALTER TABLE processes ADD aktif BIT NULL CONSTRAINT DF_processes_aktif DEFAULT 1",
    ] + [f"IF OBJECT_ID('{t}', 'U') IS NOT NULL AND COL_LENGTH('{t}','{c}') IS NULL ALTER TABLE {t} ADD {c} {tip} NULL"
       for t, c, tip in _DENETIM_KOLONLARI_MSSQL]   # tablo henüz yoksa (DBA betiği çalışmadıysa) atla
    try:
        with engine.begin() as conn:
            for k in komutlar:
                conn.execute(text(k))
    except Exception as exc:  # yetki yoksa uygulama açılmaya devam etsin; DBA mssql_yeni_yapi.sql'i çalıştırır
        print(f"[UYARI] Yeni yapı kolonları eklenemedi (mssql_yeni_yapi.sql DBA tarafından uygulanmalı): {exc}", flush=True)

class DogrulamaKarti(Base):
    """2026 — D04 F03 Mastar & Cihaz Doğrulama Kaydı kartı (mastar / cihaz başına). Kalibrasyon listesiyle bağı şimdilik yok (ekipman_id ileride)."""
    __tablename__ = "dogrulama_kartlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    no = Column(Unicode(80), nullable=False, index=True)                # cihaz / mastar no
    ad = Column(Unicode(250), nullable=False)
    tur = Column(Unicode(12), nullable=False, default="Mastar")         # Mastar / Cihaz
    grup = Column(Unicode(80), nullable=True)                           # Kalite / Üretim / Laboratuvar / Cihaz / ürün adı
    urun = Column(Unicode(120), nullable=True)                          # kaynak dosya (ürün / mastar takımı)
    devreye_alma_tarihi = Column(Date, nullable=True)
    devreye_alma_olcusu = Column(Unicode(200), nullable=True)
    dogrulama_ay = Column(Integer, nullable=True)                       # doğrulama sıklığı (ay)
    kalibrasyon_siklik = Column(Unicode(40), nullable=True)
    olcum_araligi = Column(Unicode(100), nullable=True)
    max_sapma = Column(Float, nullable=True)                            # izin verilen sapma / aşınma toleransı (mutlak)
    marka_model = Column(Unicode(200), nullable=True)
    ortam = Column(Unicode(100), nullable=True)
    mastar_takimi = Column(Unicode(300), nullable=True)
    durum = Column(Unicode(60), nullable=True)                          # Aktif / PASİF / açıklama
    aktif = Column(Boolean, nullable=True, default=True)
    kullanim_yeri = Column(Unicode(150), nullable=True)
    yontem = Column(Unicode(1000), nullable=True)                       # doğrulama yöntemi metni
    kaynak = Column(Unicode(300), nullable=True)                        # içe aktarılan dosya / sayfa
    ekipman_id = Column(Integer, nullable=True)                         # calibration_equipment.id (şimdilik kullanılmıyor)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class DogrulamaOturumu(Base):
    __tablename__ = "dogrulama_oturumlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kart_id = Column(Integer, nullable=False, index=True)
    sira = Column(Integer, nullable=False, default=0)
    tarih = Column(DateTime, nullable=True)
    yapan = Column(Unicode(100), nullable=True)
    yapan_id = Column(Integer, nullable=True)
    ortam = Column(Unicode(100), nullable=True)
    cihaz = Column(Unicode(150), nullable=True)                         # doğrulamada kullanılan cihaz (mastar doğrulama)
    karar = Column(Unicode(8), nullable=True)                           # OK / NOK (satırlardan)
    notu = Column(Unicode(500), nullable=True)


class DogrulamaOlcum(Base):
    __tablename__ = "dogrulama_olcumleri"
    id = Column(Integer, primary_key=True, autoincrement=True)
    oturum_id = Column(Integer, nullable=False, index=True)
    sira = Column(Integer, nullable=False, default=0)
    etiket = Column(Unicode(150), nullable=True)                        # Ref. 2 UÇ, İÇ ÇAP 4,998 …
    referans = Column(Float, nullable=True)
    tolerans = Column(Float, nullable=True)
    sonuc = Column(Float, nullable=True)
    sapma = Column(Float, nullable=True)
    karar = Column(Unicode(8), nullable=True)


class DogrulamaGorsel(Base):
    __tablename__ = "dogrulama_gorselleri"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kart_id = Column(Integer, nullable=False, index=True)
    tur = Column(Unicode(12), nullable=False, default="yontem")         # cihaz / yontem
    dosya_adi = Column(Unicode(200), nullable=True)
    dosya_yolu = Column(Unicode(500), nullable=True)
    depo_id = Column(Integer, nullable=True)                            # dosya_depo.id

class YenidenIslemKaydi(Base):
    """2026 — Uygun olmayan ürün & yeniden işlem takip kaydı (eski D06 F06 Excel listesinin yerine)."""
    __tablename__ = "yeniden_islem_kayitlari"
    id = Column(Integer, primary_key=True, autoincrement=True)
    giris_tarihi = Column(Date, nullable=True, index=True)               # karantinaya giriş
    saha = Column(Unicode(80), nullable=True)                            # yerinde / karantina alanı
    statu = Column(Unicode(60), nullable=True, index=True)               # KK Red, İade, Üretim firesi, Müşteri şikayeti …
    urun = Column(Unicode(200), nullable=True, index=True)
    stok_adi = Column(Unicode(250), nullable=True)
    stok_kodu = Column(Unicode(60), nullable=True)
    is_emri = Column(Unicode(60), nullable=True)
    lot_no = Column(Unicode(100), nullable=True)
    miktar = Column(Float, nullable=True)
    uygunsuzluk = Column(Unicode(1000), nullable=True)
    islem_tarihi = Column(Date, nullable=True)
    yapilan_islem = Column(Unicode(1000), nullable=True)
    aksiyon = Column(Unicode(1000), nullable=True)
    sorumlu = Column(Unicode(100), nullable=True)
    termin = Column(Date, nullable=True)
    durum = Column(Unicode(30), nullable=True)                           # Açık / Devam ediyor / Tamamlandı
    saglam_miktar = Column(Float, nullable=True)
    hatali_miktar = Column(Float, nullable=True)
    sure = Column(Float, nullable=True)
    sure_birim = Column(Unicode(10), nullable=True)                      # dk / saat
    operator_sayisi = Column(Float, nullable=True)
    birim_fiyat = Column(Float, nullable=True)
    doviz = Column(Unicode(10), nullable=True)
    parca_birim_maliyet = Column(Float, nullable=True)
    imha_maliyeti = Column(Float, nullable=True)
    geri_kazanim = Column(Float, nullable=True)
    iscilik_maliyeti = Column(Float, nullable=True)
    toplam_maliyet = Column(Float, nullable=True)
    dof_id = Column(Integer, nullable=True)                              # ilgili DÖF kaydı (isteğe bağlı)
    kaynak = Column(Unicode(100), nullable=True)                         # 'import:kayıt#12' / 'elle'
    olusturan_id = Column(Integer, nullable=True)
    olusturma_tarihi = Column(DateTime, default=datetime.now)


class YenidenIslemDF(Base):
    """Yeniden işlem listesindeki 'Düzeltici Faaliyet' sayfası (tarih, ürün, uygunsuzluk, kök neden, planlanan / gerçekleşen aksiyon)."""
    __tablename__ = "yeniden_islem_df"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tarih = Column(Unicode(60), nullable=True)
    tarih_d = Column(Date, nullable=True)
    urun = Column(Unicode(200), nullable=True)
    uygunsuzluk = Column(Unicode(1000), nullable=True)
    acil_onlem = Column(Unicode(1000), nullable=True)
    kok_neden = Column(Unicode(1000), nullable=True)
    planlanan = Column(Unicode(1000), nullable=True)
    sorumlu = Column(Unicode(150), nullable=True)
    hedef_tarih = Column(Date, nullable=True)
    gerceklesen = Column(Unicode(1000), nullable=True)
    gerceklesen_tarih = Column(Date, nullable=True)
    kaynak = Column(Unicode(100), nullable=True)

class DosyaDepo(Base):
    """2026 — yüklenen dosyaların veritabanı kopyası (SHA-256'ya göre tekil). Sertifika, teknik resim, görsel dosyalarının paylaşım olmadan da erişilebilmesi için."""
    __tablename__ = "dosya_depo"
    id = Column(Integer, primary_key=True, autoincrement=True)
    sha256 = Column(String(64), nullable=False, unique=True, index=True)
    ad = Column(Unicode(200), nullable=True)
    mime = Column(String(100), nullable=True)
    boyut = Column(Integer, nullable=False, default=0)
    icerik = Column(LargeBinary, nullable=False)
    olusturma = Column(DateTime, default=datetime.now)



_DENETIM_KOLONLARI = [  # 2026 iç denetim otomasyonu — tablo, kolon, SQLite tipi
    ("urun_prosesleri", "valeo_json", "TEXT"), ("urun_proses_adimlari", "valeo_json", "TEXT"), ("proses_fmea_satirlari", "valeo_json", "TEXT"), ("proses_kp_satirlari", "valeo_json", "TEXT"),
    ("kalibrasyon_sertifikalari", "depo_id", "INTEGER"), ("teknik_resim_revizyonlari", "depo_id", "INTEGER"), ("kalite_gorselleri", "depo_id", "INTEGER"), ("dogrulama_gorselleri", "depo_id", "INTEGER"),
    ("documents", "govde_html", "TEXT"), ("documents", "govde_not", "VARCHAR(300)"),
    ("urun_proses_adimlari", "dugum_id", "INTEGER"), ("urun_prosesleri", "fmea_standart", "VARCHAR(8)"), ("proses_fmea_satirlari", "standart", "VARCHAR(8)"),
    ("internal_audits", "checklist_id", "INTEGER"), ("internal_audits", "ekip_json", "TEXT"), ("internal_audits", "otomatik", "BOOLEAN"),
    ("audit_findings", "answer_id", "INTEGER"),
    ("audit_checklist_questions", "kime", "VARCHAR(200)"), ("audit_checklist_questions", "ipucu", "TEXT"),
    ("audit_answers", "bulgu_derecesi", "VARCHAR(30)"),
    ("audit_program_items", "kalem_adi", "VARCHAR(200)"), ("audit_program_items", "kategori", "VARCHAR(30)"),
    ("audit_program_items", "plan_gunu", "INTEGER"), ("audit_program_items", "denetci_metin", "VARCHAR(200)"),
    ("audit_program_items", "ekip_json", "TEXT"), ("audit_program_items", "checklist_id", "INTEGER"), ("audit_program_items", "notlar", "TEXT"),
] + [("calibration_equipment", c, t) for c, t in (  # 2026 — D04 F06 kalibrasyon listesi alanları
    ("cihaz_kodu", "VARCHAR(60)"), ("cihaz_turu", "VARCHAR(60)"), ("olcum_tipi", "VARCHAR(40)"), ("seri_no", "VARCHAR(100)"),
    ("imalatci", "VARCHAR(100)"), ("kullanici_bolum", "VARCHAR(100)"), ("hassasiyet", "VARCHAR(60)"), ("olcum_araligi", "VARCHAR(100)"),
    ("kullanim_araligi", "VARCHAR(100)"), ("birim", "VARCHAR(30)"), ("ozel_karakteristik", "BOOLEAN"), ("sertifika_no", "VARCHAR(120)"),
    ("dogrulama_periyodu", "VARCHAR(40)"), ("dogrulama_yontemi", "VARCHAR(100)"), ("msa_tipi", "VARCHAR(40)"), ("karar", "VARCHAR(200)"),
    ("kullanima_onay_veren", "VARCHAR(100)"), ("ek_json", "TEXT"), ("kaynak", "VARCHAR(120)"))] + [  # 2026 — kpi.debak.com entegrasyonu
    ("corrective_actions", "dis_kimlik", "VARCHAR(40)"), ("corrective_actions", "aksiyonlar_json", "TEXT"),
    ("teknik_resim_revizyonlari", "ek_dosyalar_json", "TEXT"), ("teknik_resim_revizyonlari", "kaynak", "VARCHAR(60)"),
    ("form_tanimlari", "erisim_json", "TEXT"), ("pk_surecler", "ham_json", "TEXT"), ("pk_kpiler", "ham_json", "TEXT"), ("pk_olcumler", "ham_json", "TEXT"), ("pk_ncr", "ham_json", "TEXT"),
]
_DENETIM_KOLONLARI_MSSQL = [(t, c, {"INTEGER": "INT", "TEXT": "NVARCHAR(MAX)", "BOOLEAN": "BIT"}.get(k, k.replace("VARCHAR", "NVARCHAR")))
                            for t, c, k in _DENETIM_KOLONLARI]


# ── Hafif Şema Migrasyonu (SQLite) ──────────────────────────────────────────
def _migrate_schema():
    """Mevcut SQLite veritabanına eksik kolonları güvenli şekilde ekler."""
    from sqlalchemy import inspect, text

    if _is_mssql:
        _migrate_mssql_yeni_yapi()
        return  # diğer kolonlar için create_all yeterli; SQLite ALTER sözdizimi MSSQL'de uygulanmaz

    inspector = inspect(engine)
    if "documents" not in inspector.get_table_names():
        return

    eklenecek = []
    tablolar = set(inspector.get_table_names())

    # 11 süreçlik yapı (2026): üst süreç ve aktiflik (boş bırakılabilir)
    if "processes" in tablolar:
        proc_kolonlar = {c["name"] for c in inspector.get_columns("processes")}
        if "ust_surec_id" not in proc_kolonlar:
            eklenecek.append("ALTER TABLE processes ADD COLUMN ust_surec_id INTEGER")
        if "aktif" not in proc_kolonlar:
            eklenecek.append("ALTER TABLE processes ADD COLUMN aktif BOOLEAN DEFAULT 1")

    for t, c, tip in _DENETIM_KOLONLARI:
        if t in tablolar and c not in {k["name"] for k in inspector.get_columns(t)}:
            eklenecek.append(f"ALTER TABLE {t} ADD COLUMN {c} {tip}")

    doc_kolonlar = {c["name"] for c in inspector.get_columns("documents")}
    if "icerik_json" not in doc_kolonlar:
        eklenecek.append("ALTER TABLE documents ADD COLUMN icerik_json TEXT")
    if "iptal_nedeni" not in doc_kolonlar:
        eklenecek.append("ALTER TABLE documents ADD COLUMN iptal_nedeni TEXT")
    if "iptal_tarihi" not in doc_kolonlar:
        eklenecek.append("ALTER TABLE documents ADD COLUMN iptal_tarihi DATE")
    if "yerine_gecen_id" not in doc_kolonlar:
        eklenecek.append("ALTER TABLE documents ADD COLUMN yerine_gecen_id INTEGER")
    if "legal_hold" not in doc_kolonlar:
        eklenecek.append("ALTER TABLE documents ADD COLUMN legal_hold BOOLEAN DEFAULT 0")

    if "document_revisions" in tablolar:
        rev_kolonlar = {c["name"] for c in inspector.get_columns("document_revisions")}
        if "onay_durumu" not in rev_kolonlar:
            eklenecek.append("ALTER TABLE document_revisions ADD COLUMN onay_durumu VARCHAR(20)")
        if "icerik_hash" not in rev_kolonlar:
            eklenecek.append("ALTER TABLE document_revisions ADD COLUMN icerik_hash VARCHAR(64)")
        if "degisiklik_kategorisi" not in rev_kolonlar:
            eklenecek.append("ALTER TABLE document_revisions ADD COLUMN degisiklik_kategorisi VARCHAR(20)")
        if "etki_degerlendirmesi" not in rev_kolonlar:
            eklenecek.append("ALTER TABLE document_revisions ADD COLUMN etki_degerlendirmesi TEXT")

    if "users" in tablolar:
        user_kolonlar = {c["name"] for c in inspector.get_columns("users")}
        if "basarisiz_giris_sayisi" not in user_kolonlar:
            eklenecek.append("ALTER TABLE users ADD COLUMN basarisiz_giris_sayisi INTEGER DEFAULT 0")
        if "kilit_tarihi" not in user_kolonlar:
            eklenecek.append("ALTER TABLE users ADD COLUMN kilit_tarihi DATETIME")

    if "ppap_elements" in tablolar:
        ppap_el_kolonlar = {c["name"] for c in inspector.get_columns("ppap_elements")}
        if "ilgili_document_id" not in ppap_el_kolonlar:
            eklenecek.append("ALTER TABLE ppap_elements ADD COLUMN ilgili_document_id INTEGER")

    if "document_approvals" in tablolar:
        appr_kolonlar = {c["name"] for c in inspector.get_columns("document_approvals")}
        if "workflow_no" not in appr_kolonlar:
            eklenecek.append(
                "ALTER TABLE document_approvals ADD COLUMN workflow_no INTEGER DEFAULT 1"
            )

    if "audit_logs" in tablolar:
        al_kolonlar = {c["name"] for c in inspector.get_columns("audit_logs")}
        if "onceki_hash" not in al_kolonlar:
            eklenecek.append("ALTER TABLE audit_logs ADD COLUMN onceki_hash VARCHAR(64)")
        if "kayit_hash" not in al_kolonlar:
            eklenecek.append("ALTER TABLE audit_logs ADD COLUMN kayit_hash VARCHAR(64)")

    if "corrective_actions" in tablolar:
        capa_kolonlar = {c["name"] for c in inspector.get_columns("corrective_actions")}
        if "kok_neden_yontemi" not in capa_kolonlar:
            eklenecek.append("ALTER TABLE corrective_actions ADD COLUMN kok_neden_yontemi VARCHAR(40)")
        for col, sql_type, default in (
            ("d1_team", "TEXT", None),
            ("d2_problem", "TEXT", None),
            ("d3_containment", "TEXT", None),
            ("d4_root_cause", "TEXT", None),
            ("d5_corrective", "TEXT", None),
            ("d6_implement", "TEXT", None),
            ("d7_prevent", "TEXT", None),
            ("d8_congratulate", "TEXT", None),
            ("etkinlik_bekleme_gunu", "INTEGER", "DEFAULT 30"),
            ("yeniden_acildi", "BOOLEAN", "DEFAULT 0"),
        ):
            if col not in capa_kolonlar:
                suffix = f" {default}" if default else ""
                eklenecek.append(f"ALTER TABLE corrective_actions ADD COLUMN {col} {sql_type}{suffix}")
        # Faz B: DÖF → FMEA/CP bağlantısı + müşteri şikayeti alanları
        for col, sql_type, default in (
            ("fmea_guncellendi", "BOOLEAN", "DEFAULT 0"),
            ("cp_guncellendi", "BOOLEAN", "DEFAULT 0"),
            ("ilgili_fmea_id", "INTEGER", None),
            ("ilgili_cp_id", "INTEGER", None),
            ("benzer_analiz", "TEXT", None),
            ("containment_aksiyonu", "TEXT", None),
            ("sikayet_musteri", "VARCHAR(200)", None),
            ("sikayet_parca_no", "VARCHAR(100)", None),
            ("sikayet_miktar", "INTEGER", None),
            ("sikayet_aciliyet", "VARCHAR(20)", None),
            ("garanti_talebi_mi", "BOOLEAN", "DEFAULT 0"),
            ("saha_iade_mi", "BOOLEAN", "DEFAULT 0"),
        ):
            if col not in capa_kolonlar:
                suffix = f" {default}" if default else ""
                eklenecek.append(f"ALTER TABLE corrective_actions ADD COLUMN {col} {sql_type}{suffix}")

    if "emergency_drills" in tablolar:
        drill_kolonlar = {c["name"] for c in inspector.get_columns("emergency_drills")}
        if "katilimci_ids" not in drill_kolonlar:
            eklenecek.append("ALTER TABLE emergency_drills ADD COLUMN katilimci_ids TEXT")

    if "internal_audits" in tablolar:
        ia_kolonlar = {c["name"] for c in inspector.get_columns("internal_audits")}
        if "denetim_tipi" not in ia_kolonlar:
            eklenecek.append(
                "ALTER TABLE internal_audits ADD COLUMN denetim_tipi VARCHAR(30) DEFAULT 'Sistem Denetimi'"
            )
        if "urun_adi" not in ia_kolonlar:
            eklenecek.append("ALTER TABLE internal_audits ADD COLUMN urun_adi VARCHAR(200)")
        # Faz B: Vardiya ve FMEA/CP referansları
        if "vardiya" not in ia_kolonlar:
            eklenecek.append("ALTER TABLE internal_audits ADD COLUMN vardiya VARCHAR(30)")
        if "fmea_referans_id" not in ia_kolonlar:
            eklenecek.append("ALTER TABLE internal_audits ADD COLUMN fmea_referans_id INTEGER")
        if "cp_referans_id" not in ia_kolonlar:
            eklenecek.append("ALTER TABLE internal_audits ADD COLUMN cp_referans_id INTEGER")

    # Faz B: Uygun olmayan ürün — sapma onayı ve yeniden işleme doğrulaması
    if "nonconforming_products" in tablolar:
        nc_kolonlar = {c["name"] for c in inspector.get_columns("nonconforming_products")}
        for col, sql_type, default in (
            ("sapma_onay_gerekli", "BOOLEAN", "DEFAULT 0"),
            ("sapma_onay_tarihi", "DATE", None),
            ("sapma_onay_ref", "VARCHAR(100)", None),
            ("yeniden_islem_dogrulama", "TEXT", None),
            ("dogrulama_tarihi", "DATE", None),
            ("dogrulayan_id", "INTEGER", None),
        ):
            if col not in nc_kolonlar:
                suffix = f" {default}" if default else ""
                eklenecek.append(f"ALTER TABLE nonconforming_products ADD COLUMN {col} {sql_type}{suffix}")

    if "audit_program_items" in tablolar:
        api_kolonlar = {c["name"] for c in inspector.get_columns("audit_program_items")}
        if "denetim_tipi" not in api_kolonlar:
            eklenecek.append(
                "ALTER TABLE audit_program_items ADD COLUMN denetim_tipi VARCHAR(30) DEFAULT 'Sistem Denetimi'"
            )
        if "urun_adi" not in api_kolonlar:
            eklenecek.append("ALTER TABLE audit_program_items ADD COLUMN urun_adi VARCHAR(200)")

    # ── FMEA Handbook 2019 uyumluluk migrasyonu ──────────────────────────
    if "fmeas" in tablolar:
        fmea_kolonlar = {c["name"] for c in inspector.get_columns("fmeas")}
        for col, sql_type, default in (
            ("musteri", "VARCHAR(150)", None),
            ("surec_id", "INTEGER", None),
            ("mevcut_adim", "VARCHAR(30)", "DEFAULT '1-Planlama'"),
            ("baslatma_tarihi", "DATE", None),
            ("tamamlanma_tarihi", "DATE", None),
            ("aciklama", "TEXT", None),
        ):
            if col not in fmea_kolonlar:
                suffix = f" {default}" if default else ""
                eklenecek.append(f"ALTER TABLE fmeas ADD COLUMN {col} {sql_type}{suffix}")

    if "fmea_items" in tablolar:
        fi_kolonlar = {c["name"] for c in inspector.get_columns("fmea_items")}
        for col, sql_type, default in (
            ("onleme_kontrolu", "VARCHAR(300)", None),
            ("tespit_kontrolu", "VARCHAR(300)", None),
            ("ozel_karakteristik", "BOOLEAN", "DEFAULT 0"),
            ("ozel_karakteristik_tip", "VARCHAR(10)", None),
            ("filtre_kodu", "VARCHAR(50)", None),
            ("aksiyon_durumu", "VARCHAR(20)", None),
            ("aksiyon_alinan", "TEXT", None),
            ("aksiyon_tamamlanma_tarihi", "DATE", None),
            ("yeni_siddet", "INTEGER", None),
            ("yeni_olusma", "INTEGER", None),
            ("yeni_tespit", "INTEGER", None),
            ("kontrol_plani_item_id", "INTEGER", None),
        ):
            if col not in fi_kolonlar:
                suffix = f" {default}" if default else ""
                eklenecek.append(f"ALTER TABLE fmea_items ADD COLUMN {col} {sql_type}{suffix}")

    if "calibration_equipment" in tablolar:
        cal_kolonlar = {c["name"] for c in inspector.get_columns("calibration_equipment")}
        if "izin_verilen_hata" not in cal_kolonlar:
            eklenecek.append("ALTER TABLE calibration_equipment ADD COLUMN izin_verilen_hata VARCHAR(150)")
        if "kalibrasyon_sapma" not in cal_kolonlar:
            eklenecek.append("ALTER TABLE calibration_equipment ADD COLUMN kalibrasyon_sapma VARCHAR(150)")

    # Faz C: SPC ölçüm listesi JSON kolonu
    if "spc_records" in tablolar:
        spc_kolonlar = {c["name"] for c in inspector.get_columns("spc_records")}
        if "olcumler" not in spc_kolonlar:
            eklenecek.append("ALTER TABLE spc_records ADD COLUMN olcumler TEXT")

    if eklenecek:
        with engine.begin() as conn:
            for sql in eklenecek:
                conn.execute(text(sql))
        print(f"[OK] Şema migrasyonu uygulandı: {len(eklenecek)} kolon eklendi")


# ── Veritabanını Oluştur ────────────────────────────────────────────────────
def _mssql_strip_fk_cascades():
    """SQL Server multiple cascade paths (1785) için ON DELETE/UPDATE cascade'leri kaldırır."""
    if not _is_mssql:
        return
    from sqlalchemy.schema import ForeignKeyConstraint
    for table in Base.metadata.tables.values():
        for const in table.constraints:
            if isinstance(const, ForeignKeyConstraint):
                const.ondelete = None
                const.onupdate = None


def init_db():
    """Tüm tabloları oluşturur (varsa atlar) ve eksik kolonları migrate eder."""
    _mssql_strip_fk_cascades()
    Base.metadata.create_all(bind=engine)
    _migrate_schema()
    print("[OK] DYS veritabanı tabloları oluşturuldu")

