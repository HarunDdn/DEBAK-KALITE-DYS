"""
DYS — FMEA Modülü (referans uygulama, birebir): D-FMEA / P-FMEA / FMEA-MSR, Valeo GSI-RD-370 Rev. C02.
  /iatf/fmea/modul         kullanıcının sağladığı fmea-module.html AYNEN sunulur (görünüm, kriterler, AP matrisleri, örnekler, yazdırma, logo, inspection başlığı);
                           yalnız tarayıcı localStorage yerine sunucu kaydı (DYS veritabanı) kullanılır — tüm kullanıcılar aynı çalışma sayfasını görür.
  /iatf/fmea/modul/veri    kaydetme API'si (anahtar: fmea_v1 satırlar · fmea_logo · fmea_ins_meta)
Kayıt: SistemAyar tablosunda 'fmea_modul:<anahtar>' (değer + son değişiklik damgası). Eşzamanlı değişiklikte (damga farklı) 409 döner, sessizce ezilmez.
Yetki: görüntüleme herkes; kaydetme "Sadece Görüntüleme" dışındakiler.
Dosya: data/fmea_modul.html (VALEO Restricted kriter metinleri içerir → .gitignore).
"""
import json
import os
from datetime import datetime

from flask import Response, abort, jsonify, request, session, url_for
from flask_wtf.csrf import generate_csrf

from app_runtime import host as _host
_h = _host()
app = _h.app
login_required = _h.login_required
get_db = _h.get_db
log_action = _h.log_action

from models import SistemAyar  # noqa: E402

DOSYA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fmea_modul.html")
ANAHTARLAR = ("fmea_v1", "fmea_logo", "fmea_ins_meta")
ONEK = "fmea_modul:"


def _damga(a):
    return a.guncelleme_tarihi.isoformat() if a and a.guncelleme_tarihi else ""


def _yazabilir():
    return session.get("rol") != "Sadece Görüntüleme"


SHIM = """<script>
(function(){
  var INIT=%(init)s, DAMGA=%(damga)s, YAZ=%(yaz)s, URL=%(url)s, CSRF=%(csrf)s;
  var store={}, bekle={}, zaman={}, k;
  for(k in INIT){store[k]=INIT[k];}
  function bant(m,hata){
    var e=document.getElementById('dys-bant');
    if(!e){e=document.createElement('div');e.id='dys-bant';e.className='noprint';e.style.cssText='position:fixed;right:12px;bottom:12px;z-index:9999;font:12px/1.3 sans-serif;padding:6px 10px;border-radius:6px;background:#fff;border:1px solid #bbb;box-shadow:0 2px 8px rgba(0,0,0,.15)';document.body.appendChild(e);}
    e.textContent=m;e.style.color=hata?'#b91c1c':'#444';e.style.borderColor=hata?'#b91c1c':'#bbb';
  }
  function gonder(key){
    var v=Object.prototype.hasOwnProperty.call(store,key)?store[key]:null;
    var f=new URLSearchParams();f.append('anahtar',key);f.append('damga',DAMGA[key]||'');if(v===null)f.append('sil','1');else f.append('deger',v);f.append('csrf_token',CSRF);
    bant('Kaydediliyor…');
    fetch(URL,{method:'POST',body:f,headers:{'X-CSRFToken':CSRF}}).then(function(r){return r.json().then(function(j){return {s:r.status,j:j};});}).then(function(x){
      if(x.s===200&&x.j.ok){DAMGA[key]=x.j.damga||'';bant('✔ DYS\\'ye kaydedildi');}
      else if(x.s===409){bant('⚠ Başka bir kullanıcı bu sayfayı değiştirdi — kaydedilmedi. Sayfayı yenileyip tekrar deneyin.',true);}
      else{bant('⚠ Kaydedilemedi'+(x.j&&x.j.hata?': '+x.j.hata:''),true);}
    }).catch(function(){bant('⚠ Sunucuya ulaşılamadı',true);});
  }
  function kuyruk(key){clearTimeout(zaman[key]);zaman[key]=setTimeout(function(){gonder(key);},700);}
  var shim={
    getItem:function(key){return Object.prototype.hasOwnProperty.call(store,key)?store[key]:null;},
    setItem:function(key,v){store[key]=String(v);if(YAZ&&%(izinli)s.indexOf(key)>=0)kuyruk(key);},
    removeItem:function(key){delete store[key];if(YAZ&&%(izinli)s.indexOf(key)>=0)kuyruk(key);},
    clear:function(){}, key:function(){return null;}, length:0
  };
  try{Object.defineProperty(window,'localStorage',{value:shim,configurable:true});}catch(e){window.__dysLS=shim;}
  window.addEventListener('DOMContentLoaded',function(){
    var a=document.createElement('a');a.href=%(geri)s;a.textContent='← DYS · FMEA';a.className='noprint';
    a.style.cssText='position:fixed;left:12px;bottom:12px;z-index:9999;font:12px sans-serif;padding:6px 10px;border-radius:6px;background:#fff;border:1px solid #bbb;color:#1f4e78;text-decoration:none';
    document.body.appendChild(a);
    if(!YAZ)bant('Salt okunur: değişiklikler kaydedilmez.',true);
  });
})();
</script>
"""


@app.route("/iatf/fmea/modul")
@login_required
def fmea_modul():
    if not os.path.isfile(DOSYA):
        abort(404)
    html = open(DOSYA, encoding="utf-8").read()
    db = get_db()
    try:
        init, damga = {}, {}
        for k in ANAHTARLAR:
            a = db.get(SistemAyar, ONEK + k)
            if a and a.deger is not None:
                init[k] = a.deger
            damga[k] = _damga(a)
    finally:
        db.close()
    shim = SHIM % {"init": json.dumps(init, ensure_ascii=False).replace("</", "<\\/"), "damga": json.dumps(damga),
                   "yaz": "true" if _yazabilir() else "false", "url": json.dumps(url_for("fmea_modul_veri")),
                   "csrf": json.dumps(generate_csrf()), "izinli": json.dumps(list(ANAHTARLAR)), "geri": json.dumps(url_for("fmea_list"))}
    i = html.find("<script")
    html = html[:i] + shim + html[i:] if i >= 0 else html + shim
    return Response(html, mimetype="text/html")


@app.route("/iatf/fmea/modul/veri", methods=["POST"])
@login_required
def fmea_modul_veri():
    if not _yazabilir():
        abort(403)
    k = request.form.get("anahtar")
    if k not in ANAHTARLAR:
        abort(400)
    db = get_db()
    try:
        a = db.get(SistemAyar, ONEK + k)
        if a and request.form.get("damga", "") != _damga(a):
            return jsonify(ok=False, hata="Başka kullanıcı değiştirdi"), 409
        if request.form.get("sil"):
            if a:
                db.delete(a)
                db.commit()
            return jsonify(ok=True, damga="")
        deger = request.form.get("deger", "")
        if len(deger) > 3_000_000:
            return jsonify(ok=False, hata="Veri çok büyük"), 400
        if k == "fmea_v1":
            try:
                d = json.loads(deger)
                assert isinstance(d, dict) and all(isinstance(d.get(t, []), list) for t in "DPM")
            except Exception:  # noqa: BLE001
                return jsonify(ok=False, hata="Geçersiz FMEA verisi"), 400
        if not a:
            a = SistemAyar(anahtar=ONEK + k)
            db.add(a)
        a.deger = deger
        a.guncelleme_tarihi = datetime.now()
        db.commit()
        if k == "fmea_v1":
            log_action(db, "Güncelleme", detay=f"FMEA Modülü kaydı güncellendi ({sum(len(json.loads(deger).get(t, [])) for t in 'DPM')} satır)")
        return jsonify(ok=True, damga=_damga(a))
    finally:
        db.close()
