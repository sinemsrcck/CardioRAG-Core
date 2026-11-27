"""RAG adaylarının sunum tabloları: ayrı veritabanında (varsayılan kardiyo_rag_sunum) dört FİZİKSEL tablo.

    aday_bulgular          güncel RAG adaylarının tam klinik içeriği (klinik_bulgular sütunları + kaynak / durum alanları);
                           review_status her zaman 'pending' (CHECK). İnsan onayı değildir.
    clinical_outcomes      \\
    measurements            > manuel hoca view'larının temel sütunları + doğrulama / insan onayı / kaynak aday alanları
    measurements_by_group  /
    diseases, diagnostics, symptoms, treatments, general_clinical_findings
                           manuel hoca view'larının temel sütunları (aynı ad, sıra ve SELECT'ten türeyen veri tipi) + yalnız
                           zorunlu teknik alanlar: id, sira, kaynak_aday_hashleri (aday_bulgular'a bağ; doğrulama / insan
                           onayı / tekrar durumu oradadır), icerik_anahtari, yukleme_kimligi.

Kaynaklar (yalnız OKUMA, kardiyo_tam_metin):
  - güncel adaylar ve durumlar: v_rag_dogrulama_durumu (belge başına en son deterministik parti, aday dosyası yolu ve
    SHA-256'sı; deterministik / semantik sonuç; insan durumu). Aday dosyası SHA-256 ve aday kimlikleri birebir eşleşmeli.
  - mevcut kayıtlarla tekrar durumu: src.rag_aktarim.classify_against_existing (yeni_aday / kesin_tekrar / olasi_tekrar).
  - satır içeriği: src.pilot_clinical.klinik_satiri (import_records ile aynı eşleme ve satir_hash).
  - kategorilendirme ve sütunlar: mevcut SQL dosyaları (sql/sunum_core.sql v_pilot_bulgular, sql/sunum_hoca/00_ortak.sql
    v_sunum_taban, 10_measurements.sql, 11_clinical_outcomes.sql) çalışma anında okunur; sayılan ve doğrulanan
    değişikliklerle (kaynak tablo aday_bulgular, verified -> pending, manuel kalite işaretleri bağlanmaz) iç içe
    SELECT'e çevrilir. VIEW oluşturulmaz; manuel veri kopyalanmaz.

Yazım: hedef veritabanında tek transaction'da dört tablonun içeriği yenilenir. Commit ÖNCESİ denetim (aday sayısı = güncel
aday sayısı, hepsi pending, kimlikler eşleşiyor, içerik anahtarları tekil); hata -> ROLLBACK, önceki başarılı yükleme
kalır. İçerik anahtarı (icerik_anahtari) yükleme zamanı / kimliği / id / sıra gibi değişken alanları içermez; yeni
içerik mevcutla aynıysa hiçbir şey yazılmaz. Üretim veritabanlarına yazılmaz; tablo / veritabanı silinmez.

    python -m src.rag_sunum_tablolari yukle            # prova: hesaplar, yazar, ROLLBACK
    python -m src.rag_sunum_tablolari yukle --yaz [--ozet <dosya.json>] [--tablolar diseases,symptoms,...]
--tablolar: yalnız bu tablolar yazılır (diğerlerine dokunulmaz). aday_bulgular listede değilse mevcut içeriği güncel
adaylarla birebir aynı olmalı (değilse durulur). Bir tablonun yeni içeriği mevcutla aynıysa o tabloya dokunulmaz.
Hedef: MYSQL_RAG_SUNUM_DATABASE (varsayılan kardiyo_rag_sunum); bağlantı bilgileri .env MYSQL_*.
"""
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

KAYNAK = 'kardiyo_tam_metin'
VARSAYILAN_HEDEF = 'kardiyo_rag_sunum'
URETIM = ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
IDENT = re.compile(r'^[A-Za-z0-9_]{1,64}$')
ROOT = Path(__file__).resolve().parents[1]
SQL_PILOT = ROOT / 'sql' / 'sunum_core.sql'
SQL_TABAN = ROOT / 'sql' / 'sunum_hoca' / '00_ortak.sql'
SQL_MEAS = ROOT / 'sql' / 'sunum_hoca' / '10_measurements.sql'
SQL_CO = ROOT / 'sql' / 'sunum_hoca' / '11_clinical_outcomes.sql'
HOCA = ROOT / 'sql' / 'sunum_hoca'
TABLOLAR_TAM = ('clinical_outcomes', 'measurements', 'measurements_by_group')
# Diğer manuel view'lar: (dosya, _kaynak_idler'in eklendiği satır sonu, eklenen ifade)
YALIN_SQL = {
    'treatments': ('12_treatments.sql', '    MIN(f.page) AS page\nFROM ', 'GROUP_CONCAT(f.bulgu_id ORDER BY f.bulgu_id)'),
    'general_clinical_findings': ('13_general_clinical_findings.sql', '    f.page\nFROM ', 'CAST(f.bulgu_id AS CHAR)'),
    'diseases': ('20_diseases.sql', '    MIN(f.page) AS page\nFROM ', 'GROUP_CONCAT(f.bulgu_id ORDER BY f.bulgu_id)'),
    'diagnostics': ('21_diagnostics.sql', '    MIN(f.page) AS page\nFROM ', 'GROUP_CONCAT(f.bulgu_id ORDER BY f.bulgu_id)'),
    'symptoms': ('22_symptoms.sql', '    f.page\nFROM ', 'CAST(f.bulgu_id AS CHAR)'),
}
TABLOLAR_YALIN = ('diseases', 'diagnostics', 'symptoms', 'treatments', 'general_clinical_findings')
TABLOLAR = TABLOLAR_TAM + TABLOLAR_YALIN
YALIN_EK = (('sira', 'INT NOT NULL'), ('kaynak_aday_hashleri', 'JSON NOT NULL'), ('icerik_anahtari', 'CHAR(64) NOT NULL'),
            ('yukleme_kimligi', 'VARCHAR(40) NOT NULL'))
DETERMINISTIK = ('basarisiz', 'belirsiz', 'gecti')              # öncelik: en kötü önce
KONTROL_EDILMEDI = 'kontrol_edilmedi'
KARISIK = 'karisik'
ADAY_EK = (('aday_dosyasi', 'VARCHAR(512) NULL'), ('aday_dosyasi_sha256', 'CHAR(64) NULL'),
           ('deterministik_parti', 'VARCHAR(40) NULL'), ('deterministik_dogrulama', 'VARCHAR(16) NULL'),
           ('semantik_dogrulama', 'VARCHAR(32) NULL'), ('insan_onayi', 'VARCHAR(32) NULL'),
           ('uretim_tekrar_durumu', 'VARCHAR(16) NULL'), ('icerik_anahtari', 'CHAR(64) NULL'),
           ('yukleme_kimligi', 'VARCHAR(40) NULL'), ('yukleme_zamani', 'DATETIME NULL'))
CIKTI_EK = (('sira', 'INT NOT NULL'), ('belge_kimligi', 'CHAR(64) NULL'),
            ('deterministik_dogrulama', 'VARCHAR(16) NOT NULL'), ('deterministik_dagilim', 'VARCHAR(255) NOT NULL'),
            ('semantik_dogrulama', 'VARCHAR(32) NOT NULL'), ('insan_onayi', 'VARCHAR(32) NOT NULL'),
            ('uretim_tekrar_durumu', 'VARCHAR(16) NOT NULL'), ('kaynak_aday_sayisi', 'INT NOT NULL'),
            ('kaynak_aday_hashleri', 'JSON NOT NULL'), ('kaynak_adaylar', 'JSON NOT NULL'),
            ('aday_dosyasi_sha256', 'CHAR(64) NULL'), ('deterministik_parti', 'VARCHAR(40) NULL'),
            ('icerik_anahtari', 'CHAR(64) NOT NULL'), ('yukleme_kimligi', 'VARCHAR(40) NOT NULL'),
            ('yukleme_zamani', 'DATETIME NOT NULL'))
DEGISKEN = ('id', 'sira', 'icerik_anahtari', 'yukleme_kimligi', 'yukleme_zamani')   # içerik anahtarına girmez


class SunumTabloHatasi(RuntimeError):
    pass


def hedef_adi():
    name = os.getenv('MYSQL_RAG_SUNUM_DATABASE', VARSAYILAN_HEDEF)
    if not IDENT.match(name) or name in URETIM:
        raise SunumTabloHatasi(f'geçersiz hedef veritabanı: {name!r} (üretim veritabanı olamaz)')
    return name


def baglanti(hedef):
    import mysql.connector
    from src.database import CHARSET, COLLATION, get_db_config
    cfg = dict(get_db_config(), database=hedef)
    return mysql.connector.connect(**cfg, charset=CHARSET, collation=COLLATION, use_unicode=True, autocommit=False)


def _txt(v):
    return v.decode('utf-8') if isinstance(v, (bytes, bytearray)) else v


def icerik_anahtari(row):
    """Değişken alanlar (DEGISKEN) dışındaki bütün alanların SHA-256'sı."""
    data = {k: v for k, v in row.items() if k not in DEGISKEN}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# SQL dönüşümü (mevcut sunum SQL'i -> iç içe SELECT)
# ---------------------------------------------------------------------------

VIEW_RE = re.compile(r'^CREATE\s+OR\s+REPLACE\s+VIEW\s+(?:`\{\{(?:KAYNAK_DB|SUNUM_DB)\}\}`\.)?`?(\w+)`?\s+AS\s+', re.I)


def _govde(path, name):
    from src.database import _split_sql_statements
    for st in _split_sql_statements(path.read_text(encoding='utf-8')):
        m = VIEW_RE.match(st)
        if m and m.group(1) == name:
            return st[m.end():]
    raise SunumTabloHatasi(f'{path.name}: {name} tanımı bulunamadı')


def _degistir(text, pairs, where):
    for old, new, count in pairs:
        found = text.count(old)
        if found != count:
            raise SunumTabloHatasi(f'{where}: SQL dönüşümü {old[:60]!r} {found} kez bulundu (beklenen {count}); '
                                   f'kaynak SQL değişmiş, dönüşüm güncellenmeli')
        text = text.replace(old, new)
    return text


def _artik_denetim(text, where):
    bad = [x for x in ('{{', 'v_sunum_taban', 'v_pilot_bulgular', "'verified'") if x in text]
    if re.search(r'(?<![`.\w])klinik_bulgular\b', text):
        bad.append('klinik_bulgular')
    if bad:
        raise SunumTabloHatasi(f'{where}: dönüştürülmüş SQL\'de beklenmeyen başvuru: {bad}')
    return text


def pilot_sql(hedef):
    q = f'`{KAYNAK}`'
    body = _degistir(_govde(SQL_PILOT, 'v_pilot_bulgular'), [
        ('FROM klinik_bulgular AS x', f'FROM `{hedef}`.`aday_bulgular` AS x', 1),
        ('LEFT JOIN pdf_belgeleri AS p', f'LEFT JOIN {q}.`pdf_belgeleri` AS p', 1),
        ('LEFT JOIN makaleler AS m', f'LEFT JOIN {q}.`makaleler` AS m', 1),
        # Manuel data_quality işaretleri RAG adaylarına bağlanmaz (id'ler ayrı tablodan; çakışma olmasın).
        ('LEFT JOIN v_kalite_bulgu AS k ON k.klinik_bulgu_id = b.id',
         'LEFT JOIN (SELECT CAST(NULL AS SIGNED) AS hata_sayisi, CAST(NULL AS SIGNED) AS uyari_sayisi, '
         'CAST(NULL AS SIGNED) AS bilgi_sayisi, CAST(NULL AS CHAR) AS kontroller) AS k ON 1 = 0', 1),
    ], 'v_pilot_bulgular')
    return _artik_denetim(body, 'v_pilot_bulgular')


def taban_sql(hedef):
    pilot = pilot_sql(hedef)
    body = _degistir(_govde(SQL_TABAN, 'v_sunum_taban'), [
        ('`{{KAYNAK_DB}}`.v_pilot_bulgular AS b', f'({pilot}) AS b', 1),
        ('`{{KAYNAK_DB}}`.v_pilot_bulgular AS r', f'({pilot}) AS r', 1),
        ('`{{KAYNAK_DB}}`.klinik_bulgular AS k', f'`{hedef}`.`aday_bulgular` AS k', 1),
        ("r.review_status = 'verified'", "r.review_status = 'pending'", 1),
        ("WHERE b.review_status = 'verified'", "WHERE b.review_status = 'pending'", 1),
    ], 'v_sunum_taban')
    return _artik_denetim(body, 'v_sunum_taban')


def tablo_sql(name, hedef, n_groups):
    """Hoca view'ının SELECT'i; kaynak aday id'leri teknik sütun _kaynak_idler olarak eklenir."""
    from src.sunum_hoca import measurement_columns
    taban = f'({taban_sql(hedef)})'
    if name == 'clinical_outcomes':
        body = _degistir(_govde(SQL_CO, 'clinical_outcomes'), [
            ('`{{KAYNAK_DB}}`.`v_sunum_taban` AS f', f'{taban} AS f', 1),
            ('`{{KAYNAK_DB}}`.`v_sunum_taban` AS lp', f'{taban} AS lp', 1),
            ('    f.source,\n    f.page\nFROM ', "    f.source,\n    f.page,\n    CONCAT_WS(',', f.bulgu_id, lp.bulgu_id) AS _kaynak_idler\nFROM ", 1),
        ], name)
    elif name == 'measurements_by_group':
        body = _degistir(_govde(SQL_MEAS, 'measurements_by_group'), [
            ('`{{KAYNAK_DB}}`.`v_sunum_taban` AS f', f'{taban} AS f', 1),
            ('    f.page\nFROM ', '    f.page,\n    CAST(f.bulgu_id AS CHAR) AS _kaynak_idler\nFROM ', 1),
        ], name)
    elif name == 'measurements':
        body = _degistir(_govde(SQL_MEAS, 'measurements'), [
            ('`{{KAYNAK_DB}}`.`v_sunum_taban` AS f', f'{taban} AS f', 1),
            ('{{MEASUREMENT_GROUP_COLUMNS}}', measurement_columns(n_groups), 1),
            ('    MAX(x.page) AS page\nFROM (', '    MAX(x.page) AS page,\n    GROUP_CONCAT(x.bulgu_id ORDER BY x.bulgu_id) '
                                             'AS _kaynak_idler\nFROM (', 1),
        ], name)
    elif name in YALIN_SQL:
        fname, tail, expr = YALIN_SQL[name]
        body = _degistir(_govde(HOCA / fname, name), [
            ('`{{KAYNAK_DB}}`.`v_sunum_taban` AS f', f'{taban} AS f', 1),
            (tail, tail.replace('\nFROM ', f',\n    {expr} AS _kaynak_idler\nFROM '), 1),
        ], name)
    else:
        raise SunumTabloHatasi(name)
    return _artik_denetim(body, name)


def grup_sayisi_sql(hedef):
    return (f"SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) AS n FROM ({taban_sql(hedef)}) AS f "
            f"WHERE f.kanit_turu = 'tablo' AND f.hasta_grubu IS NOT NULL AND COALESCE(f.sutun_rolu, '') <> 'toplam' "
            f"GROUP BY f.row_key) AS g")


def temel_sutunlar(name, n_groups):
    from src.sunum_hoca import EXPECTED_COLUMNS, expected_measurement_columns
    return expected_measurement_columns(n_groups) if name == 'measurements' else list(EXPECTED_COLUMNS[name])


# ---------------------------------------------------------------------------
# Adaylar (yalnız okuma) ve durum birleştirme
# ---------------------------------------------------------------------------

def guncel_adaylar(cur):
    """v_rag_dogrulama_durumu (güncel parti) -> {satir_hash: durum sözlüğü}."""
    cur.execute(f'SELECT satir_hash, belge_kimligi, aday_dosyasi, aday_dosyasi_sha256, deterministik_parti, '
                f'otomatik_deterministik, semantik_karar, insan_durumu FROM `{KAYNAK}`.`v_rag_dogrulama_durumu`')
    keys = ('satir_hash', 'belge', 'aday_dosyasi', 'aday_dosyasi_sha256', 'parti', 'deterministik', 'semantik', 'insan')
    return {r[0]: dict(zip(keys, r)) for r in ([_txt(v) for v in row] for row in cur.fetchall())}


def mevcut_kayitlar(cur, belge):
    from src.rag_aktarim import SQL_COLUMNS
    cur.execute(f"SELECT {', '.join(SQL_COLUMNS)} FROM `{KAYNAK}`.`klinik_bulgular` WHERE document_id = %s "
                f"AND bilgi_turu IS NOT NULL ORDER BY id", (belge,))
    return [dict(zip(SQL_COLUMNS, [_txt(v) for v in row])) for row in cur.fetchall()]


def insan_etiketi(insan):
    return 'ayni_kayit_verified' if insan == 'verified' else 'yok'


def aday_satirlari(view, existing_of, now, yukleme):
    """Güncel adaylar -> aday_bulgular satırları (aday dosyası sırasıyla). Bağ denetimi: dosya SHA-256 ve kimlikler."""
    from src.pilot_clinical import klinik_satiri
    from src.rag_aktarim import classify_against_existing
    files = {}
    for h, v in view.items():
        files.setdefault((v['belge'], v['aday_dosyasi'], v['aday_dosyasi_sha256'], v['parti']), set()).add(h)
    belgeler = Counter(k[0] for k in files)
    if any(n > 1 for n in belgeler.values()):
        raise SunumTabloHatasi(f'belge başına birden çok güncel aday dosyası: {[b for b, n in belgeler.items() if n > 1]}')
    rows = []
    for (belge, path, sha, parti), hashes in sorted(files.items(), key=lambda kv: (kv[0][1] or '', kv[0][0])):
        p = Path(path) if Path(path).is_absolute() else ROOT / path
        if not p.is_file():
            raise SunumTabloHatasi(f'aday dosyası yok: {path}')
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise SunumTabloHatasi(f'aday dosyası SQL kaydındaki SHA-256 ile aynı değil: {path}')
        data = json.loads(p.read_text(encoding='utf-8'))
        if data.get('document_id') != belge:
            raise SunumTabloHatasi(f'aday dosyasının belgesi farklı: {path}')
        existing = existing_of(belge)
        got = set()
        for cand in data['adaylar']:
            payload = klinik_satiri({'document_id': belge}, cand, review_status='pending', cikarma_yontemi='rag')
            h = payload['satir_hash']
            got.add(h)
            kind, _ = classify_against_existing(cand, existing)
            v = view.get(h) or {}
            payload.update({'aday_dosyasi': path, 'aday_dosyasi_sha256': sha, 'deterministik_parti': parti,
                            'deterministik_dogrulama': v.get('deterministik'),
                            'semantik_dogrulama': v.get('semantik') or KONTROL_EDILMEDI,
                            'insan_onayi': insan_etiketi(v.get('insan')),
                            'uretim_tekrar_durumu': {'kesin': 'kesin_tekrar', 'olasi': 'olasi_tekrar'}.get(kind, 'yeni_aday')})
            payload['icerik_anahtari'] = icerik_anahtari(payload)
            payload.update({'yukleme_kimligi': yukleme, 'yukleme_zamani': now})
            rows.append(payload)
        if got != hashes:
            raise SunumTabloHatasi(f'aday kimlikleri SQL güncel partisiyle eşleşmiyor ({path}): yalnız dosyada '
                                   f'{len(got - hashes)}, yalnız SQL\'de {len(hashes - got)}')
    return rows


def _birlesik(values):
    s = sorted(set(values))
    return s[0] if len(s) == 1 else KARISIK


def durum_ozeti(sources):
    """Kaynak adaylar (aday_bulgular satırları) -> satır durum alanları. Hiçbir kaynak bilgisi kaybolmaz."""
    det = [s['deterministik_dogrulama'] for s in sources]
    worst = next((d for d in DETERMINISTIK if d in det), None)
    if worst is None or any(d not in DETERMINISTIK for d in det):
        raise SunumTabloHatasi(f'deterministik sonuç eksik / bilinmeyen: {det}')
    return {'belge_kimligi': _birlesik(s['document_id'] for s in sources),
            'deterministik_dogrulama': worst,
            'deterministik_dagilim': ', '.join(f'{k}:{n}' for k, n in sorted(Counter(det).items())),
            'semantik_dogrulama': _birlesik(s['semantik_dogrulama'] for s in sources),
            'insan_onayi': _birlesik(s['insan_onayi'] for s in sources),
            'uretim_tekrar_durumu': _birlesik(s['uretim_tekrar_durumu'] for s in sources),
            'kaynak_aday_sayisi': len(sources),
            'kaynak_aday_hashleri': json.dumps([s['satir_hash'] for s in sources]),
            'kaynak_adaylar': json.dumps([{k: s[k] for k in ('satir_hash', 'deterministik_dogrulama',
                                                             'semantik_dogrulama', 'insan_onayi', 'uretim_tekrar_durumu')}
                                          for s in sources], ensure_ascii=False),
            'aday_dosyasi_sha256': _birlesik(s['aday_dosyasi_sha256'] for s in sources),
            'deterministik_parti': _birlesik(s['deterministik_parti'] for s in sources)}


def cikti_satirlari(names, results, by_id, now, yukleme, tam=True):
    """SELECT sonucu (+ _kaynak_idler) -> tablo satırları. tam: durum ve kaynak aday alanları (ilk üç tablo);
    değilse yalnız zorunlu teknik alanlar (kaynak_aday_hashleri; durumlar aday_bulgular'da)."""
    rows = []
    for sira, r in enumerate(results, 1):
        rec = dict(zip(names, [_txt(v) for v in r]))
        ids = [int(x) for x in str(rec.pop('_kaynak_idler') or '').split(',') if x]
        if not ids or any(i not in by_id for i in ids):
            raise SunumTabloHatasi(f'satırın kaynak adayı bulunamadı: {ids}')
        if tam:
            rec.update(durum_ozeti([by_id[i] for i in ids]))
        else:
            rec['kaynak_aday_hashleri'] = json.dumps([by_id[i]['satir_hash'] for i in ids])
        rec['icerik_anahtari'] = icerik_anahtari(rec)
        rec.update({'sira': sira, 'yukleme_kimligi': yukleme})
        if tam:
            rec['yukleme_zamani'] = now
        rows.append(rec)
    return rows


# ---------------------------------------------------------------------------
# Şema (yalnız oluşturma / denetim; silme yok)
# ---------------------------------------------------------------------------

def _sutunlar(cur, db, table):
    cur.execute('SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s '
                'ORDER BY ORDINAL_POSITION', (db, table))
    return [_txt(r[0]) for r in cur.fetchall()]


def sema_hazirla(cur, hedef, n_groups, tablolar=TABLOLAR):
    """İstenen tablolar yoksa oluşturur; varsa sütunları denetler. Hiçbir tablo silinmez / değiştirilmez.
    Dönüş: group sayısı."""
    klinik = _sutunlar(cur, KAYNAK, 'klinik_bulgular')
    want = klinik + [c for c, _ in ADAY_EK]
    have = _sutunlar(cur, hedef, 'aday_bulgular')
    if not have and 'aday_bulgular' not in tablolar:
        raise SunumTabloHatasi('aday_bulgular yok; önce bütün tablolarla yükleme yapılmalı')
    if not have:
        cur.execute(f'CREATE TABLE `{hedef}`.`aday_bulgular` LIKE `{KAYNAK}`.`klinik_bulgular`')
        cur.execute(f'ALTER TABLE `{hedef}`.`aday_bulgular` ' + ', '.join(f'ADD COLUMN `{c}` {t}' for c, t in ADAY_EK)
                    + ', ADD UNIQUE KEY uq_aday_icerik (icerik_anahtari), '
                      "ADD CONSTRAINT ck_aday_bulgular_pending CHECK (review_status = 'pending')")
    elif have != want:
        raise SunumTabloHatasi('aday_bulgular sütunları beklenenle aynı değil (kaynak şema değişmiş olabilir); '
                               'tablo değiştirilmedi')
    have = _sutunlar(cur, hedef, 'measurements')
    if have:
        n_groups = sum(1 for c in have if re.fullmatch(r'group_\d+', c))
    for name in [t for t in TABLOLAR_YALIN if t in tablolar]:
        base = temel_sutunlar(name, n_groups)
        want = ['id'] + base + [c for c, _ in YALIN_EK]
        have = _sutunlar(cur, hedef, name)
        if not have:
            # Temel sütunların veri tipleri manuel view ile aynı SELECT'ten türetilir (LIMIT 0: satır yazılmaz).
            cur.execute(f'CREATE TABLE `{hedef}`.`{name}` ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin AS '
                        f'SELECT {", ".join(f"q.`{c}`" for c in base)} FROM ({tablo_sql(name, hedef, n_groups)}) AS q LIMIT 0')
            cur.execute(f'ALTER TABLE `{hedef}`.`{name}` ADD COLUMN `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT FIRST, '
                        + ', '.join(f'ADD COLUMN `{c}` {t}' for c, t in YALIN_EK)
                        + f', ADD PRIMARY KEY (`id`), ADD UNIQUE KEY uq_{name}_icerik (`icerik_anahtari`)')
        elif have != want:
            raise SunumTabloHatasi(f'{name} sütunları beklenenle aynı değil; tablo değiştirilmedi')
    for name in [t for t in TABLOLAR_TAM if t in tablolar]:
        base = temel_sutunlar(name, n_groups)
        want = ['id'] + base + [c for c, _ in CIKTI_EK]
        have = _sutunlar(cur, hedef, name)
        if not have:
            cols = ['`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT'] + [
                f'`{c}` {"INT NULL" if c == "group_order" else "TEXT NULL"}' for c in base] + [
                f'`{c}` {t}' for c, t in CIKTI_EK]
            cur.execute(f'CREATE TABLE `{hedef}`.`{name}` ({", ".join(cols)}, PRIMARY KEY (`id`), '
                        f'UNIQUE KEY uq_{name}_icerik (`icerik_anahtari`)) '
                        f'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin')
        elif have != want:
            raise SunumTabloHatasi(f'{name} sütunları beklenenle aynı değil; tablo değiştirilmedi')
    return n_groups


def python_grup_sayisi(rows):
    """measurements group_N sayısı (tablo oluştururken): tablo satırı başına Total dışı hasta grubu kaydı."""
    from src.sunum_hoca import MIN_GROUPS
    count = Counter()
    for r in rows:
        note = json.loads(r['taslak_notu']) if r.get('taslak_notu') else {}
        if r.get('kanit_turu') == 'tablo' and r.get('hasta_grubu') and note.get('sutun_rolu') != 'toplam' \
                and note.get('tablo_no') is not None:
            count[(r['document_id'], note.get('tablo_no'), note.get('satir_no'))] += 1
    return max([MIN_GROUPS] + list(count.values()))


# ---------------------------------------------------------------------------
# Yükleme
# ---------------------------------------------------------------------------

def _anahtarlar(cur, hedef, table):
    cur.execute(f'SELECT icerik_anahtari FROM `{hedef}`.`{table}`')
    return sorted(_txt(r[0]) for r in cur.fetchall())


def _ekle(cur, hedef, table, rows):
    if not rows:
        return
    cols = list(dict.fromkeys(c for r in rows for c in r))     # aday satırlarında isteğe bağlı alanlar değişebilir
    cur.executemany(f"INSERT INTO `{hedef}`.`{table}` ({', '.join(f'`{c}`' for c in cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))})", [tuple(r.get(c) for c in cols) for r in rows])


def yukle(conn, *, yaz=False, tablolar=None):
    """tablolar: yazılacak tablolar (varsayılan: aday_bulgular + bütün sunum tabloları). Listede olmayan tabloya
    dokunulmaz; yeni içeriği mevcutla aynı olan tablo da yazılmaz (id / sıra / yükleme alanları korunur)."""
    hedef = hedef_adi()
    hepsi = ('aday_bulgular',) + TABLOLAR
    tablolar = tuple(hepsi if not tablolar else tablolar)
    if set(tablolar) - set(hepsi):
        raise SunumTabloHatasi(f'bilinmeyen tablo: {sorted(set(tablolar) - set(hepsi))}')
    cur = conn.cursor()
    try:
        cur.execute('SELECT DATABASE()')
        db = _txt(cur.fetchone()[0])
        if db != hedef or db in URETIM:
            raise SunumTabloHatasi(f'bağlantı veritabanı {db!r}; beklenen hedef {hedef!r} (üretim olamaz)')
        now = datetime.now().replace(microsecond=0)
        view = guncel_adaylar(cur)
        if not view:
            raise SunumTabloHatasi('v_rag_dogrulama_durumu boş: güncel aday yok')
        digest = hashlib.sha256('|'.join(sorted({f"{v['aday_dosyasi_sha256']}:{v['parti']}" for v in view.values()}))
                                .encode()).hexdigest()[:16]
        yukleme = f'{now:%Y%m%d%H%M%S}-{digest}'
        cache = {}
        adaylar = aday_satirlari(view, lambda b: cache.setdefault(b, mevcut_kayitlar(cur, b)), now, yukleme)
        conn.rollback()
        n_groups = sema_hazirla(cur, hedef, python_grup_sayisi(adaylar), tablolar)   # DDL: yalnız eksik tablolar
        conn.rollback()
        conn.start_transaction()
        sunum = [t for t in TABLOLAR if t in tablolar]
        old = {t: _anahtarlar(cur, hedef, t) for t in ['aday_bulgular'] + sunum}
        new = {'aday_bulgular': sorted(r['icerik_anahtari'] for r in adaylar)}
        yazilan = []
        if new['aday_bulgular'] != old['aday_bulgular']:
            if 'aday_bulgular' not in tablolar:
                raise SunumTabloHatasi('aday_bulgular güncel adaylarla aynı değil ve yazılacak tablolar arasında yok; '
                                       'hiçbir şey yazılmadı')
            cur.execute(f'DELETE FROM `{hedef}`.`aday_bulgular`')
            _ekle(cur, hedef, 'aday_bulgular', adaylar)
            yazilan.append('aday_bulgular')
        cur.execute(f'SELECT id, satir_hash, icerik_anahtari FROM `{hedef}`.`aday_bulgular`')
        stored = [(int(i), _txt(h), _txt(k)) for i, h, k in cur.fetchall()]
        by_hash = {r['satir_hash']: r for r in adaylar}
        by_id = {i: by_hash[h] for i, h, _ in stored if h in by_hash}
        if 'measurements' in sunum:
            cur.execute(grup_sayisi_sql(hedef))
            need = int(cur.fetchone()[0])
            if need > n_groups:
                raise SunumTabloHatasi(f'measurements {need} grup gerektiriyor, tabloda {n_groups} group sütunu var; '
                                       f'tablo değiştirilmedi')
        for name in sunum:
            cur.execute(tablo_sql(name, hedef, n_groups))
            names = [d[0] for d in cur.description]
            if [c for c in names if c != '_kaynak_idler'] != temel_sutunlar(name, n_groups):
                raise SunumTabloHatasi(f'{name}: SELECT sütunları manuel yapıyla aynı değil')
            rows = cikti_satirlari(names, cur.fetchall(), by_id, now, yukleme, tam=name in TABLOLAR_TAM)
            new[name] = sorted(r['icerik_anahtari'] for r in rows)
            if new[name] != old[name]:
                cur.execute(f'DELETE FROM `{hedef}`.`{name}`')
                _ekle(cur, hedef, name, rows)
                yazilan.append(name)
        cur.execute(f"SELECT COUNT(*), SUM(review_status <> 'pending'), COUNT(DISTINCT satir_hash) "
                    f"FROM `{hedef}`.`aday_bulgular`")
        n, not_pending, distinct = (int(x or 0) for x in cur.fetchone())
        problems = [x for x, bad in (
            (f'aday sayısı {n}, güncel aday {len(view)}', n != len(view) or distinct != n),
            ('pending olmayan aday', not_pending != 0),
            ('aday kimlikleri / içerikleri güncel adaylarla aynı değil',
             {h for _, h, _ in stored} != set(view) or sorted(k for _, _, k in stored) != new['aday_bulgular']),
            ('tekil olmayan içerik anahtarı', any(len(set(v)) != len(v) for v in new.values())),
            ('istenmeyen tabloya yazım', bool(set(yazilan) - set(tablolar)))) if bad]
        if problems:
            raise SunumTabloHatasi('commit öncesi denetim: ' + '; '.join(problems) + '; ROLLBACK')
        ozet = {'hedef': hedef, 'yukleme_kimligi': yukleme, 'group_sayisi': n_groups, 'istenen': list(tablolar),
                'yazilan': yazilan, 'degismeyen': [t for t in new if t not in yazilan],
                'satir': {t: len(v) for t, v in new.items()},
                'deterministik': dict(Counter(r['deterministik_dogrulama'] for r in adaylar)),
                'semantik': dict(Counter(r['semantik_dogrulama'] for r in adaylar)),
                'insan_onayi': dict(Counter(r['insan_onayi'] for r in adaylar)),
                'uretim_tekrar_durumu': dict(Counter(r['uretim_tekrar_durumu'] for r in adaylar))}
        if not yazilan:
            conn.rollback()
            return {**ozet, 'sonuc': 'icerik_ayni', 'kalici': False}
        if yaz:
            conn.commit()
        else:
            conn.rollback()
        return {**ozet, 'sonuc': 'yazildi' if yaz else 'prova', 'kalici': bool(yaz)}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def main(argv=None):
    import argparse
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog='python -m src.rag_sunum_tablolari')
    sub = p.add_subparsers(dest='komut', required=True)
    y = sub.add_parser('yukle', help='fiziksel tabloları güncel RAG adaylarından yeniler (varsayılan prova)')
    y.add_argument('--yaz', action='store_true')
    y.add_argument('--ozet', help='özet JSON dosyası (yeni dosya)')
    y.add_argument('--tablolar', help='virgülle ayrılmış tablo listesi (varsayılan: hepsi); diğerlerine dokunulmaz')
    args = p.parse_args(argv)
    conn = baglanti(hedef_adi())
    try:
        result = yukle(conn, yaz=args.yaz, tablolar=[t.strip() for t in args.tablolar.split(',') if t.strip()] if args.tablolar else None)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    if args.ozet:
        with open(args.ozet, 'x', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1, default=str)


if __name__ == '__main__':
    main()
