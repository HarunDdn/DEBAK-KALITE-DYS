"""
DYS — Ortak Yardımcı Fonksiyonlar
=================================
Dosya yükleme sertleştirme, versiyonlu depolama, hash, onay adımı üretimi ve
önizleme/indirme mantığı gibi tekrarlayan işleri tek yerde toplar.
"""

import os
import hashlib
from datetime import datetime

from werkzeug.utils import secure_filename

from config import Config, connect_upload_share


# ── Dosya Yükleme Güvenliği ─────────────────────────────────────────────────
def file_ext(filename):
    """Uzantıyı (noktasız, küçük harf) döndürür."""
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def allowed_file(filename):
    """Yalnızca beyaz listedeki uzantılara izin verir."""
    return file_ext(filename) in Config.ALLOWED_UPLOAD_EXTENSIONS


def is_unsafe_inline(filename):
    """Tarayıcıda satır içi gösterilmesi XSS riski taşıyan tipler."""
    return file_ext(filename) in Config.UNSAFE_INLINE_EXTENSIONS


# ── Versiyonlu Depolama ─────────────────────────────────────────────────────
def document_storage_dir(doc_id, revizyon_no):
    """uploads/documents/<doc_id>/rev<revizyon_no>/ yolunu döndürür."""
    return os.path.join(Config.UPLOAD_FOLDER, "documents", str(doc_id), f"rev{revizyon_no}")


def save_document_file(file, doc_id, revizyon_no):
    """
    Yüklenen dosyayı versiyonlu klasöre kaydeder.
    Aynı ada sahip farklı revizyonların birbirini ezmesini önler.
    Döndürür: (dosya_adi, dosya_yolu)
    """
    original = secure_filename(file.filename) or "dosya"
    target_dir = document_storage_dir(doc_id, revizyon_no)
    os.makedirs(target_dir, exist_ok=True)
    dosya_yolu = os.path.join(target_dir, original)
    file.save(dosya_yolu)
    return original, dosya_yolu


def save_generic_file(file, *subdirs):
    """Genel amaçlı yükleme (eğitim sertifikası, PPAP vb.). (ad, yol) döndürür."""
    original = secure_filename(file.filename) or "dosya"
    target_dir = os.path.join(Config.UPLOAD_FOLDER, *[str(s) for s in subdirs])
    os.makedirs(target_dir, exist_ok=True)
    dosya_yolu = os.path.join(target_dir, original)
    file.save(dosya_yolu)
    return original, dosya_yolu


def file_sha256(path):
    """Dosyanın SHA-256 özetini döndürür (yoksa None)."""
    if not path or not os.path.isfile(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def yeni_yapi_esle(yol, kok):
    """``...\\YENİ YAPI\\<göreli>`` yolunu ``kok\\<göreli>`` yapar (kök klasör adı işaret alınır). Eşleşmezse None."""
    if not yol or not kok:
        return None
    kok = kok.replace("/", "\\").rstrip("\\")
    p = yol.replace("/", "\\").rstrip("\\") + "\\"
    marker = "\\" + kok.rsplit("\\", 1)[-1] + "\\"
    idx = p.find(marker)
    if idx < 0:
        return None
    return (kok + "\\" + p[idx + len(marker):]).rstrip("\\")


def resolve_file_path(dosya_yolu, dosya_adi):
    """Mutlak/göreli dosya yolunu UPLOAD_FOLDER'a göre çözer.

    Eski yerel ``...\\uploads\\...`` yolları UNC (DYS_UPLOAD_FOLDER) altına
    yeniden eşlenir; böylece migrasyon sonrası / öncesi kırık yollar toparlanır.
    """
    upload_root = os.path.normpath(Config.UPLOAD_FOLDER)

    def _under_upload(rel_or_name: str) -> str:
        return os.path.normpath(os.path.join(upload_root, rel_or_name))

    def _isfile(path: str) -> bool:
        if not path:
            return False

        def _exists(p: str) -> bool:
            if os.path.isfile(p):
                return True
            if p.startswith("\\\\") and not p.startswith("\\\\?\\"):
                return os.path.isfile("\\\\?\\UNC\\" + p.lstrip("\\"))
            return False

        try:
            if _exists(path):
                return True
            if path.startswith("\\\\"):
                connect_upload_share()
                return _exists(path)
        except OSError:
            return False
        return False

    if dosya_yolu:
        p = os.path.normpath(dosya_yolu)
        if _isfile(p):
            return p

        # ...\YENİ YAPI\... yolu başka sürücü/sunucudan kaldıysa bu ortamdaki köke eşle
        cand = yeni_yapi_esle(p, Config.YENI_YAPI_KLASORU)
        if cand and _isfile(cand):
            return cand

        # uploads/documents/... göreli parçayı çıkar
        lowered = p.replace("/", "\\").lower()
        marker = "\\uploads\\"
        if marker in lowered:
            rel = p.replace("/", "\\")[lowered.find(marker) + len(marker) :]
            cand = _under_upload(rel)
            if _isfile(cand):
                return cand

        # documents/<id>/revN/... zaten UPLOAD_FOLDER altında tutuluyorsa
        if "documents\\" in lowered or "documents/" in dosya_yolu.replace("\\", "/").lower():
            idx = lowered.find("documents\\")
            if idx < 0:
                idx = dosya_yolu.replace("/", "\\").lower().find("documents\\")
            if idx >= 0:
                rel = p.replace("/", "\\")[idx:]
                cand = _under_upload(rel)
                if _isfile(cand):
                    return cand

        if not os.path.isabs(p):
            cand = _under_upload(p)
            if _isfile(cand):
                return cand

    if dosya_adi:
        cand = _under_upload(dosya_adi)
        if _isfile(cand):
            return cand

    # Son çare: eski mutlak yol (yoksa bile) veya uploads + ad
    if dosya_yolu and os.path.isabs(dosya_yolu):
        return os.path.normpath(dosya_yolu)
    return _under_upload(dosya_adi or "")


def document_dir_path(dosya_yolu, dosya_adi=None):
    """Dokümanın bulunduğu klasör adresi (UNC/yerel). Disk erişimi yok.

    Kayıtlı yol ...\\YENİ YAPI\\... ise kullanıcıya gösterilen köke eşlenir
    (DYS_YENI_YAPI_GORUNEN_KLASOR, yoksa DYS_YENI_YAPI_KLASORU).
    """
    raw = (dosya_yolu or "").strip()
    if not raw:
        return ""
    p = raw.replace("/", "\\").rstrip("\\")
    p = yeni_yapi_esle(p, Config.YENI_YAPI_GORUNEN_KLASOR or Config.YENI_YAPI_KLASORU) or p
    if p.upper().startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    name = p.rsplit("\\", 1)[-1]
    adi = (dosya_adi or "").replace("/", "\\").rsplit("\\", 1)[-1]
    if name and (
        (adi and name.lower() == adi.lower())
        or ("." in name and not name.startswith("."))
    ):
        parent = p[: -len(name)].rstrip("\\")
        return parent
    return p


# ── Onay İş Akışı ───────────────────────────────────────────────────────────
def create_approval_steps(db, doc):
    """Bir doküman için Hazırlama/Kontrol/Onay adımlarını oluşturur."""
    from models import DocumentApproval
    from sqlalchemy import func
    # Aynı doküman için yeni bir onay çevrimi başlat: mevcut en yüksek workflow_no + 1
    mevcut = db.query(func.max(DocumentApproval.workflow_no)).filter_by(
        document_id=doc.id
    ).scalar()
    yeni_wf = (mevcut or 0) + 1
    for adim in ("Hazırlama", "Kontrol", "Onay"):
        approval = DocumentApproval(
            document_id=doc.id,
            onay_adimi=adim,
            durum="Bekliyor",
            tarih=datetime.now(),
            workflow_no=yeni_wf,
        )
        if adim == "Hazırlama" and doc.hazirlayan_id:
            approval.kullanici_id = doc.hazirlayan_id
        elif adim == "Kontrol" and doc.kontrol_eden_id:
            approval.kullanici_id = doc.kontrol_eden_id
        elif adim == "Onay" and doc.onaylayan_id:
            approval.kullanici_id = doc.onaylayan_id
        db.add(approval)


# ── Önizleme MIME Haritası ──────────────────────────────────────────────────
PREVIEW_MIME_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".txt": "text/plain", ".csv": "text/csv",
    ".json": "application/json",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

PREVIEWABLE_EXTS = {
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "bmp",
    "xlsx", "xls", "csv", "txt", "json", "docx",
}


def _debak_harf(tip_kisaltmasi):
    """PR/TL/FR… kısaltmasından şirket harfini (P/T/F…) bulur."""
    from models import TIP_KISALTMALARI, DEBAK_TIP_HARFLERI
    for tip, kisa in TIP_KISALTMALARI.items():
        if kisa == tip_kisaltmasi:
            return DEBAK_TIP_HARFLERI.get(tip)
    return None


def next_document_number(db, surec, tip_kisaltmasi):
    """Belirli süreç+tip için bir sonraki doküman numarasını üretir.
    DYS_NUMARA_FORMATI=debak → 'D04 P03' (şirket biçimi); aksi halde eski 'D04-PR-001' biçimi."""
    from models import Document
    from config import Config
    harf = _debak_harf(tip_kisaltmasi) if Config.NUMARA_FORMATI == "debak" else None
    if harf:
        prefix = f"{surec.kod} {harf}"
        mevcut = db.query(Document).filter(Document.dokuman_no.like(f"{prefix}%")).all()
        en_yuksek = 0
        for d in mevcut:
            kalan = d.dokuman_no[len(prefix):].strip()
            if kalan.isdigit():
                en_yuksek = max(en_yuksek, int(kalan))
        return f"{prefix}{en_yuksek + 1:02d}"
    prefix = f"{surec.kod}-{tip_kisaltmasi}-"
    mevcut = db.query(Document).filter(Document.dokuman_no.like(f"{prefix}%")).all()
    en_yuksek = 0
    for d in mevcut:
        try:
            n = int(d.dokuman_no.rsplit("-", 1)[1])
            en_yuksek = max(en_yuksek, n)
        except (ValueError, IndexError):
            continue
    return f"{prefix}{en_yuksek + 1:03d}"


def next_sequence_no(db, model, field, prefix):
    """DÖF-2026-001 gibi yıllık sıra numarası üretir."""
    yil = datetime.now().year
    tam_prefix = f"{prefix}-{yil}-"
    mevcut = db.query(model).filter(getattr(model, field).like(f"{tam_prefix}%")).all()
    en_yuksek = 0
    for kayit in mevcut:
        try:
            n = int(getattr(kayit, field).rsplit("-", 1)[1])
            en_yuksek = max(en_yuksek, n)
        except (ValueError, IndexError):
            continue
    return f"{tam_prefix}{en_yuksek + 1:03d}"
