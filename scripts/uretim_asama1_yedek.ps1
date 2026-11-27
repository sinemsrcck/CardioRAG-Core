<#
Üretime geçiş — AŞAMA 1: güvenli yedek, izole geri yükleme doğrulaması ve mevcut durum kaydı.
Üretimde hiçbir tablo, kayıt, görünüm, prosedür veya yetki DEĞİŞTİRİLMEZ.

Çalıştırma (proje kökünde; MySQL hizmeti açık olmalı):
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama1_yedek.ps1 [-BeklenenSatir 1362] [-BeklenenVerified 1308] [-GeriYuklemeyiBirak]

Ne yapar:
  1. Mevcut durum (yalnız okuma): klinik_bulgular'ın bütün satırlarının bütün alanları (satır başına SHA-256),
     verified pilot kayıtları (bilgi_turu dolu + review_status = 'verified'), sekiz sunum görünümünün satır sayısı ve
     içerik parmak izi, eski kaynak_bul sonuçları, şema / nesne tanımları / DEFINER'lar / kullanıcılar ve yetkiler.
  2. Üç üretim veritabanının tam dökümü (mysqldump --single-transaction --routines --triggers --events).
  3. İzole geri yükleme doğrulaması: dökümlerin TEMİZLENMİŞ KOPYASI (USE / CREATE DATABASE / olay yok; şema
     nitelikleri geri yükleme veritabanlarına çevrilir; DEFINER kaldırılır; üretim ve test adı kalmaz) ayrı bir geri
     yükleme kullanıcısıyla, yalnız bu betiğin oluşturduğu geçici veritabanlarına yüklenir; tablolar (CHECKSUM + satır),
     görünümler (satır + içerik), rutinler ve tetikleyiciler üretimle karşılaştırılır.

Güvenlik:
  - Yönetici hesabı (.env MYSQL_USER) yalnız: mysqldump (okuma), salt okunur transaction içinde şema / yetki okuma,
    geçici kullanıcıları ve geçici geri yükleme veritabanlarını oluşturma / silme.
  - Geçici okuma kullanıcısı: üretim ve test veritabanlarında yalnız SELECT, SHOW VIEW; kaynak_bul için EXECUTE.
  - Geçici geri yükleme kullanıcısı: YALNIZ üç geçici geri yükleme veritabanında (açık liste, ALL değil) yetkili;
    üretimde ve test veritabanlarında hiçbir yetkisi yok (betik SHOW GRANTS ve reddedilen okuma denemesiyle doğrular).
  - Temizlenmiş kopyada üretim / test adı, USE, CREATE DATABASE, DEFINER veya olay (EVENT) kalırsa geri yükleme yapılmaz.
  - finally: üretim parmak izi, şema / nesne / yetki kaydı, klinik satırlar ve sekiz görünüm önce ile aynı mı;
    test veritabanları ve JSON dosyaları (data/review, data/clinical) değişmedi mi; geçici veritabanları ve kullanıcılar
    silinir; ortam değişkenleri geri yüklenir.
  - Mevcut iki test veritabanına yalnız okuma (CHECKSUM, görünüm tanımı) yapılır.
Çıktılar: backups\uretim_gecisi\<zaman>\ (yedek\*.sql salt okunur, manifest, mevcut_durum.json,
nesneler_ve_yetkiler.json, geri_yukleme\temizleme_raporu.json).
Çıkış kodu: 0 = bütün adımlar geçti; 1 = en az bir adım başarısız.
#>
param(
  [int]$BeklenenSatir = 1362,
  [int]$BeklenenVerified = 1308,
  [switch]$GeriYuklemeyiBirak
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$py  = '.venv\Scripts\python.exe'
$bin = 'C:\Program Files\MySQL\MySQL Server 8.0\bin'
$PROD = @('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
$TEST = @('kardiyo_rag_test', 'kardiyo_rag_test_sunum')
$HOSTS = @('localhost', '127.0.0.1')
$stamp = Get-Date -f yyyyMMdd_HHmmss
$work  = Join-Path (Get-Location) "backups\uretim_gecisi\$stamp"
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 6)
$OKUR_USER = "y1_okur_$suffix"
$GERI_USER = "y1_geri_$suffix"
# Geri yükleme veritabanları: adlarında üretim / test adı geçmez (temizleme denetimi bu adları da arar)
$GERI_DBS = [ordered]@{
  'kardiyo_tam_metin'     = "y1geri_${stamp}_tm"
  'kardiyo_clinical_data' = "y1geri_${stamp}_cd"
  'kardiyo_sunum'         = "y1geri_${stamp}_sn"
}
$GERI_RE = '^y1geri_\d{8}_\d{6}_(tm|cd|sn)$'

# --- .env (yönetici bağlantısı) ---------------------------------------------------------------
$dotenv = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $dotenv[$Matches[1]] = $Matches[2].Trim() }
$adminUser = if ($dotenv.MYSQL_USER) { $dotenv.MYSQL_USER } else { 'root' }
$adminPwd  = $dotenv.MYSQL_PASSWORD
$dbHost    = if ($dotenv.MYSQL_HOST) { $dotenv.MYSQL_HOST } else { 'localhost' }
$dbPort    = if ($dotenv.MYSQL_PORT) { $dotenv.MYSQL_PORT } else { '3306' }
function New-Secret { [guid]::NewGuid().ToString('N') + 'Aa1!' }
$okurPwd = New-Secret; $geriPwd = New-Secret

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string]$step, [bool]$ok, [string]$info = '') {
  $results.Add([pscustomobject]@{ Adim = $step; Sonuc = $(if ($ok) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = $info })
  Write-Host ("[{0}] {1} {2}" -f $(if ($ok) { 'GECTI' } else { 'HATA ' }), $step, $info) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
  if (-not $ok) { throw "Adım başarısız: $step" }
}
function Invoke-My([string]$exe, [string]$user, [string]$pwd, [string[]]$arguments, [string]$stdin) {
  $old = $env:MYSQL_PWD; $env:MYSQL_PWD = $pwd
  try {
    $common = @("--user=$user", "--host=$dbHost", "--port=$dbPort")
    if ($stdin) { $out = $stdin | & "$bin\$exe" @common @arguments } else { $out = & "$bin\$exe" @common @arguments }
    if ($LASTEXITCODE -ne 0) { throw "$exe başarısız (çıkış kodu $LASTEXITCODE)" }
    return $out
  } finally { $env:MYSQL_PWD = $old }
}
function Invoke-Py([string]$code, [string[]]$arguments) {
  ($PY_COMMON + "`n" + $code) | & $py - @arguments | Out-Host
  return [int]$LASTEXITCODE
}
function Get-JsonHashes {
  $files = @()
  foreach ($d in 'data\review', 'data\clinical') {
    if (Test-Path -LiteralPath $d) { $files += Get-ChildItem -LiteralPath $d -Recurse -File -Filter *.json }
  }
  $h = @{}
  foreach ($f in $files) { $h[$f.FullName] = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash }
  return $h
}
function Esc([string]$name) { $name -replace '_', '\_' }

# --- Gömülü Python (yalnız ASCII) ---------------------------------------------------------------
$PY_COMMON = @'
import json, os, re, sys, hashlib
from collections import Counter
import mysql.connector
E = os.environ
PROD = ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
TEST = ('kardiyo_rag_test', 'kardiyo_rag_test_sunum')
GERI = json.loads(E['Y1_GERI_DBS'])          # uretim adi -> gecici geri yukleme veritabani
TEMP_USERS = (E['Y1_OKUR_USER'], E['Y1_GERI_USER'])
VIEWS8 = ('measurements', 'measurements_by_group', 'clinical_outcomes', 'treatments', 'general_clinical_findings',
          'diseases', 'diagnostics', 'symptoms')
TERMS = ('HbA1c', 'Aspirin', 'troponin', 'stroke', 'eosinophil', 'ablation')
BS = chr(92)
NAMES_RE = re.compile(r'\b(' + '|'.join(PROD + TEST) + r')\b', re.I)
fail = []


def ok(name, cond, info=''):
    print(('TAMAM ' if cond else 'HATA  ') + name + (f' | {info}' if info != '' else ''))
    if not cond:
        fail.append(name)


def stop_if_failed(stage):
    if fail:
        print(f'\nDUR {stage}: {len(fail)} kontrol basarisiz: {fail}')
        sys.exit(1)


def txt(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode('utf-8')
    if isinstance(v, set):
        return ','.join(sorted(v))
    return v


def connect(kind, db=None, readonly=True):
    user, pwd = {'admin': (E['Y1_ADMIN_USER'], E['Y1_ADMIN_PWD']), 'okur': (E['Y1_OKUR_USER'], E['Y1_OKUR_PWD']),
                 'geri': (E['Y1_GERI_USER'], E['Y1_GERI_PWD'])}[kind]
    c = mysql.connector.connect(host=E['Y1_HOST'], port=int(E['Y1_PORT']), user=user, password=pwd, database=db,
                                charset='utf8mb4', use_unicode=True, autocommit=False)
    if readonly:
        c.start_transaction(readonly=True)
    return c


def whoami(cur, kind_user, db):
    cur.execute('SELECT DATABASE(), CURRENT_USER()')
    d, u = map(txt, cur.fetchone())
    return d == db and str(u).startswith(kind_user + '@'), f'{d} / {u}'


def norm(v):
    if isinstance(v, (bytes, bytearray)):
        return 'hex:' + bytes(v).hex()
    if isinstance(v, set):
        return sorted(v)
    return v


def row_hash(row):
    return hashlib.sha256(json.dumps([norm(v) for v in row], ensure_ascii=False, default=str).encode()).hexdigest()


def view_hash(cur, db, view):
    cur.execute(f'SELECT * FROM `{db}`.`{view}`')
    rows = sorted(json.dumps([norm(v) for v in r], ensure_ascii=False, default=str) for r in cur.fetchall())
    return [len(rows), hashlib.sha256('\n'.join(rows).encode()).hexdigest()]


def base_tables(cur, db):
    cur.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY 1", (db,))
    rows = [(txt(a), txt(b)) for a, b in cur.fetchall()]
    return [t for t, k in rows if k == 'BASE TABLE'], [t for t, k in rows if k == 'VIEW']


def table_sums(cur, db):
    out = {}
    for t in base_tables(cur, db)[0]:
        cur.execute(f'CHECKSUM TABLE `{db}`.`{t}`')
        chk = cur.fetchone()[1]
        cur.execute(f'SELECT COUNT(*) FROM `{db}`.`{t}`')
        n = cur.fetchone()[0]
        if chk is None:
            ok(f'checksum gecerli: {db}.{t}', False, 'NULL')
        out[t] = f'{chk}/{n}'
    return out


def write_new(path, obj):
    with open(path, 'x', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
'@

$PY_YETKI = @'
# Gecici kullanicilarin yetkileri: okur yalniz okuma; geri yalniz gecici geri yukleme veritabanlari.
o = connect('okur')
oc = o.cursor()
oc.execute('SHOW GRANTS')
og = [txt(r[0]) for r in oc.fetchall()]
ok('okuma kullanicisi yalniz USAGE / SELECT, SHOW VIEW / EXECUTE',
   bool(og) and all(re.match(r'GRANT (USAGE ON \*\.\*|SELECT, SHOW VIEW ON |EXECUTE ON PROCEDURE )', g) for g in og), og)
o.rollback(); o.close()
g = connect('geri')
gc = g.cursor()
gc.execute('SHOW GRANTS')
gg = [txt(r[0]) for r in gc.fetchall()]
allowed = [f'`{n}`.*' for n in GERI.values()]
bad = [x for x in gg if not x.startswith('GRANT USAGE ON *.*') and not any(a in x.replace(BS + '_', '_') for a in allowed)]
ok('geri yukleme kullanicisi yalniz gecici geri yukleme veritabanlarinda yetkili', bool(gg) and not bad, bad or gg)
ok('geri yukleme kullanicisinda ALL / GRANT OPTION / global yetki yok',
   not any(('ALL PRIVILEGES' in x) or ('GRANT OPTION' in x) for x in gg))
for db in PROD + TEST:
    try:
        gc.execute(f'SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s', (db,))
        visible = bool(gc.fetchall())
        ok(f'geri yukleme kullanicisi {db} veritabanini goremiyor', not visible)
    except mysql.connector.Error as exc:
        ok(f'geri yukleme kullanicisi {db} sorgusu', False, str(exc))
try:
    gc.execute('SELECT 1 FROM `kardiyo_tam_metin`.`klinik_bulgular` LIMIT 1')
    gc.fetchall()
    ok('geri yukleme kullanicisinin uretim okumasi reddedildi', False, 'okuyabildi')
except mysql.connector.Error as exc:
    ok('geri yukleme kullanicisinin uretim okumasi reddedildi', exc.errno in (1142, 1044), exc.errno)
g.rollback(); g.close()
sys.exit(1 if fail else 0)
'@

$PY_DURUM = @'
# Mevcut durum. once: kaydet; sonra: once ile birebir karsilastir (her durumda yalniz okuma).
mode, work = sys.argv[1], sys.argv[2]
EXP_ROWS, EXP_VERIFIED = int(E['Y1_BEKLENEN_SATIR']), int(E['Y1_BEKLENEN_VERIFIED'])
STATE = os.path.join(work, 'mevcut_durum.json')
META = os.path.join(work, 'nesneler_ve_yetkiler.json')
FORBIDDEN = re.compile(r'\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|RENAME|GRANT|REVOKE|CALL|PREPARE|EXECUTE|'
                       r'DEALLOCATE|LOAD|HANDLER|LOCK|UNLOCK|COMMIT|ROLLBACK|START|SAVEPOINT|INTO|OUTFILE|DUMPFILE|'
                       r'FLUSH|KILL|SHUTDOWN|INSTALL|UNINSTALL)\b|\bREPLACE\b(?!\s*\()|\bFOR\s+UPDATE\b', re.I)


def scan(definition):
    """Tanimin TAMAMI taranir (dize / yorum ayiklanmaz): yanlis RET olabilir, yanlis KABUL olmaz."""
    return sorted({m.group(0).upper() for m in FORBIDDEN.finditer(definition or '')}), \
        re.search(r'\bSELECT\b', definition or '', re.I) is not None


def admin_meta():
    a = connect('admin')
    c = a.cursor()
    meta = {'sunucu': {}, 'veritabanlari': {}, 'kullanicilar': {}, 'procs_priv': [], 'tables_priv': []}
    c.execute('SELECT @@version, @@GLOBAL.sql_mode, @@GLOBAL.event_scheduler, @@GLOBAL.log_bin')
    meta['sunucu'] = dict(zip(('surum', 'sql_mode', 'event_scheduler', 'log_bin'), map(lambda v: str(txt(v)), c.fetchone())))
    for s in PROD:
        d = {'tablolar': {}, 'gorunumler': {}, 'rutinler': {}, 'tetikleyiciler': {}, 'olaylar': {}}
        tables, _ = base_tables(c, s)
        for t in tables:
            c.execute(f'SHOW CREATE TABLE `{s}`.`{t}`')
            d['tablolar'][t] = txt(c.fetchone()[1])
        c.execute('SELECT TABLE_NAME, VIEW_DEFINITION, DEFINER, SECURITY_TYPE, CHECK_OPTION FROM information_schema.VIEWS '
                  'WHERE TABLE_SCHEMA = %s ORDER BY 1', (s,))
        for n, vd, df, st, co in c.fetchall():
            d['gorunumler'][txt(n)] = {'tanim': txt(vd), 'definer': txt(df), 'guvenlik': txt(st), 'check': txt(co)}
        c.execute('SELECT ROUTINE_TYPE, ROUTINE_NAME, DEFINER, SECURITY_TYPE, SQL_MODE, ROUTINE_DEFINITION, CREATED, '
                  'LAST_ALTERED FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s ORDER BY 1, 2', (s,))
        for rt, n, df, st, sm, rd, cr, la in c.fetchall():
            d['rutinler'][f'{txt(rt)} {txt(n)}'] = {'definer': txt(df), 'guvenlik': txt(st), 'sql_mode': txt(sm),
                                                   'tanim': txt(rd), 'olusturma': str(cr), 'degisiklik': str(la)}
        c.execute('SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, DEFINER, ACTION_STATEMENT '
                  'FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s ORDER BY 1', (s,))
        for n, tb, tm, ev, df, st in c.fetchall():
            d['tetikleyiciler'][txt(n)] = {'tablo': txt(tb), 'zaman': txt(tm), 'olay': txt(ev), 'definer': txt(df),
                                           'tanim': txt(st)}
        c.execute('SELECT EVENT_NAME, DEFINER, STATUS, EVENT_DEFINITION FROM information_schema.EVENTS '
                  'WHERE EVENT_SCHEMA = %s ORDER BY 1', (s,))
        for n, df, stt, ed in c.fetchall():
            d['olaylar'][txt(n)] = {'definer': txt(df), 'durum': txt(stt), 'tanim': txt(ed)}
        meta['veritabanlari'][s] = d
    c.execute('SELECT User, Host, account_locked, plugin FROM mysql.user ORDER BY 1, 2')
    users = [tuple(map(txt, r)) for r in c.fetchall()]
    for u, h, locked, plugin in users:
        if u in TEMP_USERS:
            continue
        c.execute('SHOW GRANTS FOR %s@%s', (u, h))
        meta['kullanicilar'][f'{u}@{h}'] = {'kilitli': locked, 'plugin': plugin,
                                           'yetkiler': sorted(txt(r[0]) for r in c.fetchall())}
    c.execute('SELECT Host, Db, User, Routine_name, Routine_type, Proc_priv FROM mysql.procs_priv ORDER BY 1, 2, 3, 4')
    meta['procs_priv'] = [[str(txt(v)) for v in r] for r in c.fetchall() if txt(r[2]) not in TEMP_USERS]
    c.execute('SELECT Host, Db, User, Table_name, Table_priv, Column_priv FROM mysql.tables_priv ORDER BY 1, 2, 3, 4')
    meta['tables_priv'] = [[str(txt(v)) for v in r] for r in c.fetchall() if txt(r[2]) not in TEMP_USERS]
    owners = sorted({v['definer'] for d in meta['veritabanlari'].values()
                     for kind in ('gorunumler', 'rutinler', 'tetikleyiciler', 'olaylar') for v in d[kind].values()})
    meta['sahipler'] = {o: (o in meta['kullanicilar']) for o in owners}
    a.rollback(); a.close()
    return meta


def fingerprints(cur, dbs):
    out = {}
    for s in dbs:
        cur.execute('SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s', (s,))
        if not cur.fetchone()[0]:
            out[s] = None
            continue
        cur.execute('SELECT TABLE_NAME, MD5(VIEW_DEFINITION) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s ORDER BY 1', (s,))
        views = {txt(a): txt(b) for a, b in cur.fetchall()}
        out[s] = {'tablolar': table_sums(cur, s), 'gorunumler': views}
    return out


def clinical(cur):
    cur.execute("SELECT COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLLATION_NAME "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'kardiyo_tam_metin' AND TABLE_NAME = 'klinik_bulgular' "
                "ORDER BY ORDINAL_POSITION")
    columns = [[str(txt(v)) if v is not None else None for v in r] for r in cur.fetchall()]
    cur.execute('SELECT * FROM `kardiyo_tam_metin`.`klinik_bulgular` ORDER BY id')
    names = [d[0] for d in cur.description]
    rows, verified, status = {}, {}, Counter()
    for r in cur.fetchall():
        rec = dict(zip(names, r))
        key = str(rec['id'])
        if key in rows:
            ok(f'klinik_bulgular id tekil: {key}', False)
        h = row_hash(r)
        rows[key] = h
        status[str(txt(rec.get('review_status')))] += 1
        if rec.get('bilgi_turu') is not None and txt(rec.get('review_status')) == 'verified':
            verified[key] = [txt(rec.get('satir_hash')), h]
    cur.execute("SELECT COUNT(*), MIN(id), MAX(id), COALESCE(SUM(CRC32(CONCAT_WS('|', id, satir_hash, review_status))), 0) "
                "FROM `kardiyo_tam_metin`.`klinik_bulgular` WHERE bilgi_turu IS NOT NULL")
    pilot = [str(v) for v in cur.fetchone()]
    digest = lambda obj: hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
    return {'sutunlar': columns, 'satir_sayisi': len(rows), 'review_status': dict(status), 'satir_ozetleri': rows,
            'tum_satirlar_ozeti': digest(rows), 'verified_pilot_sayisi': len(verified), 'verified_pilot': verified,
            'verified_pilot_ozeti': digest(verified), 'pilot_durumu_sql': pilot}


def views8(cur):
    cur.execute('SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s', ('kardiyo_sunum',))
    present = {txt(r[0]) for r in cur.fetchall()}
    ok('sekiz sunum gorunumu mevcut', set(VIEWS8) <= present, sorted(set(VIEWS8) - present))
    return {v: view_hash(cur, 'kardiyo_sunum', v) for v in VIEWS8 if v in present}


def source_search(meta):
    r = meta['veritabanlari']['kardiyo_tam_metin']['rutinler'].get('PROCEDURE kaynak_bul')
    if not r:
        print('BILGI kardiyo_tam_metin.kaynak_bul yok; eski sonuclar kaydedilmedi')
        return None
    for sample in ("BEGIN SELECT 1; DELETE FROM t; END", "BEGIN SET x = '" + BS + "'; SELECT 1; INSERT INTO t VALUES (1); END",
                   "BEGIN SELECT 'a' INTO OUTFILE '/x'; END", "BEGIN REPLACE INTO t VALUES (1); END"):
        ok(f'tarayici yazma ornegini reddeder {sample[:30]!r}', bool(scan(sample)[0]))
    bad, has_select = scan(r['tanim'])
    ok('uretim kaynak_bul yalniz okuma (tanimin tamami tarandi)', bool(r['tanim']) and not bad and has_select,
       f'yasak {bad}; SELECT {has_select}')
    stop_if_failed('kaynak_bul salt okunur dogrulanamadi; cagrilmadi')
    p = connect('okur', 'kardiyo_tam_metin')
    out = {}
    for term in TERMS:
        cur = p.cursor()
        cur.callproc('kaynak_bul', (term,))
        ids = set()
        for res in cur.stored_results():
            cols = [d[0] for d in res.description]
            ids |= {dict(zip(cols, x))['bulgu_id'] for x in res.fetchall()}
        cur.close()
        out[term] = sorted(ids)
    p.rollback(); p.close()
    return out


o = connect('okur')
oc = o.cursor()
good, info = whoami(oc, E['Y1_OKUR_USER'], None)
ok('okuma baglantisi gecici okuma kullanicisi', good, info)
stop_if_failed('baglanti')
meta = admin_meta()
state = {'uretim_parmak_izi': fingerprints(oc, PROD), 'test_parmak_izi': fingerprints(oc, TEST),
         'klinik_bulgular': clinical(oc), 'sunum_gorunumleri': views8(oc)}
o.rollback(); o.close()
state['kaynak_bul'] = source_search(meta)
stop_if_failed('durum okunamadi')

if mode == 'once':
    k = state['klinik_bulgular']
    ok(f'klinik_bulgular satir sayisi = {EXP_ROWS}', k['satir_sayisi'] == EXP_ROWS, k['satir_sayisi'])
    ok(f'verified pilot kayit (bilgi_turu dolu, verified) = {EXP_VERIFIED}', k['verified_pilot_sayisi'] == EXP_VERIFIED,
       k['verified_pilot_sayisi'])
    missing_owner = [o for o, exists in meta['sahipler'].items() if not exists]
    print(f"BILGI review_status dagilimi: {k['review_status']}; sahibi olmayan uretim nesnesi: {missing_owner or 'yok'}")
    print(f"BILGI sunum gorunumleri: { {v: x[0] for v, x in state['sunum_gorunumleri'].items()} }")
    stop_if_failed('beklenen sayilar tutmuyor; kayit yazilmadi')
    write_new(STATE, state)
    write_new(META, meta)
    print(f'MEVCUT DURUM KAYDEDILDI: {STATE}, {META}')
else:
    before, before_meta = json.load(open(STATE, encoding='utf-8')), json.load(open(META, encoding='utf-8'))
    state = json.loads(json.dumps(state, default=str))
    meta = json.loads(json.dumps(meta, default=str))
    for key in ('uretim_parmak_izi', 'test_parmak_izi', 'sunum_gorunumleri', 'kaynak_bul'):
        diff = [n for n in set(before[key] or {}) | set(state[key] or {}) if (before[key] or {}).get(n) != (state[key] or {}).get(n)]
        ok(f'{key} once ile ayni', not diff, diff[:10])
    kb0, kb1 = before['klinik_bulgular'], state['klinik_bulgular']
    changed = [i for i in set(kb0['satir_ozetleri']) | set(kb1['satir_ozetleri'])
               if kb0['satir_ozetleri'].get(i) != kb1['satir_ozetleri'].get(i)]
    ok('klinik_bulgular butun satirlarin butun alanlari ayni', not changed and kb0['satir_sayisi'] == kb1['satir_sayisi'],
       f'{len(changed)} farkli, ornek {sorted(changed)[:5]}')
    ok('verified pilot kayitlari ayni', kb0['verified_pilot'] == kb1['verified_pilot'], kb1['verified_pilot_sayisi'])
    ok('klinik_bulgular sutunlari ayni', kb0['sutunlar'] == kb1['sutunlar'])
    for key in ('sunucu', 'veritabanlari', 'kullanicilar', 'procs_priv', 'tables_priv', 'sahipler'):
        ok(f'sema / nesne / yetki kaydi ayni: {key}', before_meta[key] == meta[key])
print('\nSONUC:', 'TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_TEMIZLE = @'
# Dokumlerin geri yukleme icin TEMIZLENMIS KOPYASI. Orijinal dokumler degismez. Izolasyon dogrulanamazsa dur.
work = sys.argv[1]
src_dir, dst_dir = os.path.join(work, 'yedek'), os.path.join(work, 'geri_yukleme')
QUAL = re.compile(r'`(' + '|'.join(PROD) + r')`(?=\s*\.)')
DEFINER = re.compile(r"DEFINER\s*=\s*(?:`[^`]*`|'[^']*'|[^\s@]+)@(?:`[^`]*`|'[^']*'|[^\s*]+)\s*", re.I)
INSERT = re.compile(r'INSERT INTO `[^`.]+` (?:\(|VALUES)')
report = {}
for db in PROD:
    src, dst = os.path.join(src_dir, f'{db}.sql'), os.path.join(dst_dir, f'{db}.sql')
    in_block, completed, problems, counts = False, False, [], Counter()
    with open(src, encoding='utf-8', newline='') as fi, open(dst, 'x', encoding='utf-8', newline='') as fo:
        for n, line in enumerate(fi, 1):
            s = line.lstrip()
            up = s.upper()
            if up.startswith('DELIMITER'):
                parts = s.split()
                in_block = len(parts) > 1 and parts[1] != ';'
                fo.write(line)
                continue
            if not in_block and s.startswith('-- '):
                completed = completed or s.startswith('-- Dump completed')
                continue                     # yorum satiri: geri yuklemede gereksiz, uretim adlari icerir
            if not in_block and s.startswith('INSERT INTO'):
                if not INSERT.match(s):
                    problems.append((n, 'nitelikli ya da beklenmeyen INSERT', s[:120]))
                counts['insert'] += 1
                fo.write(line)               # veri satiri: degistirilmez
                continue
            if re.match(r'USE\b', up) or 'CREATE DATABASE' in up or 'CREATE SCHEMA' in up:
                problems.append((n, 'USE / CREATE DATABASE', s[:120]))
            if re.search(r'\bEVENT\s+`', s, re.I) or re.search(r'\bCREATE\s+EVENT\b', s, re.I):
                problems.append((n, 'olay (EVENT) tanimi: izole geri yukleme yapilmaz', s[:120]))
            counts['tablo'] += bool(re.match(r'CREATE TABLE `', s))
            counts['gorunum'] += bool(re.search(r'/\*!50001 VIEW `', s))
            counts['prosedur'] += bool(re.search(r'\bPROCEDURE\s+`', s) and 'CREATE' in up)
            counts['fonksiyon'] += bool(re.search(r'\bFUNCTION\s+`', s) and 'CREATE' in up)
            counts['tetikleyici'] += bool(re.search(r'\bTRIGGER\s+`', s) and 'CREATE' in up)
            new = DEFINER.sub('', QUAL.sub(lambda m: '`' + GERI[m.group(1)] + '`', line))
            if re.search(r'DEFINER\s*=', new, re.I):
                problems.append((n, 'DEFINER kaldirilamadi', new.strip()[:120]))
            hit = NAMES_RE.search(new)
            if hit:
                problems.append((n, f'uretim / test adi kaldi: {hit.group(0)}', new.strip()[:120]))
            fo.write(new)
    if not completed:
        problems.append((0, 'dokum tamamlanmamis (-- Dump completed yok)', ''))
    report[db] = {'hedef': GERI[db], 'sayilar': dict(counts), 'sorunlar': problems[:50],
                  'kaynak_sha256': hashlib.sha256(open(src, 'rb').read()).hexdigest(),
                  'temiz_sha256': hashlib.sha256(open(dst, 'rb').read()).hexdigest()}
    ok(f'{db}: temizlenmis kopya izole (USE / CREATE DATABASE / olay / DEFINER / uretim-test adi yok)', not problems,
       f"{dict(counts)}; sorun {len(problems)} {problems[:3]}")
write_new(os.path.join(dst_dir, 'temizleme_raporu.json'), report)
sys.exit(1 if fail else 0)
'@

$PY_GERI = @'
# Izole geri yukleme dogrulamasi: uretim (okuma kullanicisi) ile gecici veritabanlari (geri yukleme kullanicisi).
work = sys.argv[1]
meta = json.load(open(os.path.join(work, 'nesneler_ve_yetkiler.json'), encoding='utf-8'))
o = connect('okur')
oc = o.cursor()
for prod, geri in GERI.items():
    g = connect('geri', geri)
    gc = g.cursor()
    good, info = whoami(gc, E['Y1_GERI_USER'], geri)
    ok(f'{geri}: baglanti gecici geri yukleme kullanicisi', good, info)
    stop_if_failed('geri yukleme baglantisi')
    pt, pv = base_tables(oc, prod)
    gt, gv = base_tables(gc, geri)
    ok(f'{prod} -> {geri}: tablo listesi ayni', pt == gt, f'yalniz uretim {sorted(set(pt) - set(gt))}, yalniz geri {sorted(set(gt) - set(pt))}')
    ok(f'{prod} -> {geri}: gorunum listesi ayni', pv == gv, f'yalniz uretim {sorted(set(pv) - set(gv))}, yalniz geri {sorted(set(gv) - set(pv))}')
    ps, gs = table_sums(oc, prod), table_sums(gc, geri)
    diff = [t for t in set(ps) | set(gs) if ps.get(t) != gs.get(t)]
    ok(f'{prod} -> {geri}: butun tablolarin CHECKSUM ve satir sayisi ayni', not diff,
       f'{len(ps)} tablo; farkli {[(t, ps.get(t), gs.get(t)) for t in diff[:5]]}')
    vdiff = []
    for v in sorted(set(pv) & set(gv)):
        a, b = view_hash(oc, prod, v), view_hash(gc, geri, v)
        if a != b:
            vdiff.append((v, a[0], b[0]))
    ok(f'{prod} -> {geri}: butun gorunumlerin satir sayisi ve icerigi ayni', not vdiff, f'{len(pv)} gorunum; farkli {vdiff[:5]}')
    gc.execute('SELECT ROUTINE_TYPE, ROUTINE_NAME, DEFINER, ROUTINE_DEFINITION FROM information_schema.ROUTINES '
               'WHERE ROUTINE_SCHEMA = %s', (geri,))
    groutines = {f'{txt(a)} {txt(b)}': (txt(c), txt(d)) for a, b, c, d in gc.fetchall()}
    ok(f'{prod} -> {geri}: rutin listesi ayni', set(groutines) == set(meta['veritabanlari'][prod]['rutinler']),
       f"uretim {sorted(meta['veritabanlari'][prod]['rutinler'])}, geri {sorted(groutines)}")
    gc.execute('SELECT TRIGGER_NAME, DEFINER, ACTION_STATEMENT FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s', (geri,))
    gtriggers = {txt(a): (txt(b), txt(c)) for a, b, c in gc.fetchall()}
    ok(f'{prod} -> {geri}: tetikleyici listesi ayni', set(gtriggers) == set(meta['veritabanlari'][prod]['tetikleyiciler']))
    gc.execute('SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s', (geri,))
    ok(f'{geri}: olay (EVENT) yok', gc.fetchone()[0] == 0)
    gc.execute('SELECT TABLE_NAME, DEFINER, VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s', (geri,))
    gviews = {txt(a): (txt(b), txt(c)) for a, b, c in gc.fetchall()}
    leaks = [(k, NAMES_RE.search(d or '').group(0)) for k, (_, d) in {**gviews, **groutines, **gtriggers}.items()
             if NAMES_RE.search(d or '')]
    ok(f'{geri}: gorunum / rutin / tetikleyici tanimlari uretim ve test veritabanlarina basvurmuyor', not leaks, leaks[:5])
    owners = {df for df, _ in list(gviews.values()) + list(groutines.values()) + list(gtriggers.values())}
    ok(f'{geri}: nesnelerin sahibi yalniz gecici geri yukleme kullanicisi',
       all(str(x).startswith(E['Y1_GERI_USER'] + '@') for x in owners), sorted(owners))
    g.rollback(); g.close()
o.rollback(); o.close()
print('\nSONUC:', 'GERI YUKLEME DOGRULANDI' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

# =================================================================================================
$saved = @{}
foreach ($n in 'MYSQL_PWD', 'Y1_HOST', 'Y1_PORT', 'Y1_ADMIN_USER', 'Y1_ADMIN_PWD', 'Y1_OKUR_USER', 'Y1_OKUR_PWD',
               'Y1_GERI_USER', 'Y1_GERI_PWD', 'Y1_GERI_DBS', 'Y1_BEKLENEN_SATIR', 'Y1_BEKLENEN_VERIFIED', 'PYTHONIOENCODING') {
  $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$usersCreated = $false; $dbsCreated = $false; $stateSaved = $false; $jsonBefore = $null
$exitCode = 1
try {
  # 0) Ön koşullar
  $svc = Get-Service MYSQL80 -ErrorAction SilentlyContinue
  Add-Result 'MySQL hizmeti çalışıyor' ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
  Add-Result 'çalışma klasörü yeni' (-not (Test-Path -LiteralPath $work)) $work
  foreach ($g in $GERI_DBS.Values) { if ($g -notmatch $GERI_RE) { Add-Result 'geri yükleme veritabanı adı biçimi' $false $g } }
  $names = ($GERI_DBS.Values | ForEach-Object { "'$_'" }) -join ','
  $exist = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ($names) UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$OKUR_USER','$GERI_USER')")
  Add-Result 'geçici veritabanı / kullanıcı önceden yok' (-not $exist) "$exist"
  $prodCount = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ('kardiyo_tam_metin','kardiyo_sunum','kardiyo_clinical_data')")
  Add-Result 'üç üretim veritabanı mevcut' ([int]$prodCount -eq 3) "$prodCount"
  $jsonBefore = Get-JsonHashes
  Add-Result 'JSON dosyalarının SHA-256 kaydı (data\review, data\clinical)' ($jsonBefore.Count -gt 0) "$($jsonBefore.Count) dosya"
  New-Item -ItemType Directory -Path "$work\yedek", "$work\geri_yukleme" | Out-Null

  # 1) Geçici kullanıcılar ve geçici geri yükleme veritabanları (yönetici; parolalar stdin'de)
  $hasProc = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='kardiyo_tam_metin' AND ROUTINE_NAME='kaynak_bul' AND ROUTINE_TYPE='PROCEDURE'")
  $sql = ''
  foreach ($g in $GERI_DBS.Values) { $sql += "CREATE DATABASE ``$g`` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;`n" }
  $restorePrivs = 'SELECT, INSERT, CREATE, DROP, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, EXECUTE, TRIGGER, LOCK TABLES'
  foreach ($h in $HOSTS) {
    $sql += "CREATE USER '$OKUR_USER'@'$h' IDENTIFIED BY '$okurPwd';`n"
    foreach ($p in $PROD + $TEST) { $sql += "GRANT SELECT, SHOW VIEW ON ``$(Esc $p)``.* TO '$OKUR_USER'@'$h';`n" }
    if ([int]$hasProc -gt 0) { $sql += "GRANT EXECUTE ON PROCEDURE ``kardiyo_tam_metin``.``kaynak_bul`` TO '$OKUR_USER'@'$h';`n" }
    $sql += "CREATE USER '$GERI_USER'@'$h' IDENTIFIED BY '$geriPwd';`n"
    foreach ($g in $GERI_DBS.Values) { $sql += "GRANT $restorePrivs ON ``$(Esc $g)``.* TO '$GERI_USER'@'$h';`n" }
  }
  $dbsCreated = $true; $usersCreated = $true
  Invoke-My 'mysql.exe' $adminUser $adminPwd @() $sql | Out-Null
  $sql = $null
  Add-Result 'geçici kullanıcılar ve geri yükleme veritabanları oluşturuldu' $true "$OKUR_USER, $GERI_USER; $($GERI_DBS.Values -join ', ')"

  $env:Y1_HOST = $dbHost; $env:Y1_PORT = $dbPort
  $env:Y1_ADMIN_USER = $adminUser; $env:Y1_ADMIN_PWD = $adminPwd
  $env:Y1_OKUR_USER = $OKUR_USER; $env:Y1_OKUR_PWD = $okurPwd
  $env:Y1_GERI_USER = $GERI_USER; $env:Y1_GERI_PWD = $geriPwd
  $env:Y1_GERI_DBS = ($GERI_DBS | ConvertTo-Json -Compress)
  $env:Y1_BEKLENEN_SATIR = "$BeklenenSatir"; $env:Y1_BEKLENEN_VERIFIED = "$BeklenenVerified"
  $env:PYTHONIOENCODING = 'utf-8'

  # 2) Yetki izolasyonu
  Add-Result 'geçici kullanıcı yetkileri (okuma / yalnız geri yükleme veritabanları)' ((Invoke-Py $PY_YETKI) -eq 0)

  # 3) Mevcut durum kaydı (yalnız okuma)
  Add-Result 'mevcut durum kaydedildi (klinik satırlar, verified, 8 görünüm, nesneler, yetkiler)' ((Invoke-Py $PY_DURUM @('once', $work)) -eq 0)
  $stateSaved = $true

  # 4) Tam yedek (yönetici; mysqldump yalnız okur)
  $manifest = [ordered]@{}
  foreach ($db in $PROD) {
    $f = "$work\yedek\$db.sql"
    Invoke-My 'mysqldump.exe' $adminUser $adminPwd @('--single-transaction', '--routines', '--triggers', '--events',
      '--hex-blob', '--set-gtid-purged=OFF', '--column-statistics=0', '--no-tablespaces', '--default-character-set=utf8mb4',
      "--result-file=$f", $db) | Out-Null
    $item = Get-Item -LiteralPath $f
    Add-Result "yedek alındı: $db" ($item.Length -gt 0) "$([math]::Round($item.Length / 1KB)) KB"
    $manifest[$db] = [ordered]@{ dosya = $item.Name; boyut = $item.Length; sha256 = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash }
  }
  [IO.File]::WriteAllText("$work\yedek\manifest.json", ($manifest | ConvertTo-Json -Depth 4), [System.Text.UTF8Encoding]::new($false))
  Get-ChildItem -LiteralPath "$work\yedek" -File | ForEach-Object { $_.IsReadOnly = $true }
  Add-Result 'yedek dosyaları salt okunur, manifest yazıldı' $true "$work\yedek"

  # 5) Geri yükleme için temizlenmiş kopya ve izolasyon denetimi
  Add-Result 'temizlenmiş kopya izole (USE / CREATE DATABASE / olay / DEFINER / üretim-test adı yok)' ((Invoke-Py $PY_TEMIZLE @($work)) -eq 0)

  # 6) İzole geri yükleme: yalnız geri yükleme kullanıcısıyla, yalnız geçici veritabanlarına
  foreach ($db in $GERI_DBS.Keys) {
    $target = $GERI_DBS[$db]
    $file = ((Resolve-Path -LiteralPath "$work\geri_yukleme\$db.sql").Path -replace '\\', '/')
    Invoke-My 'mysql.exe' $GERI_USER $geriPwd @('--default-character-set=utf8mb4', "--database=$target", '-e', "SOURCE $file") | Out-Null
    Add-Result "geri yüklendi: $db -> $target" $true
  }

  # 7) Geri yükleme doğrulaması
  Add-Result 'geri yükleme doğrulandı (tablolar, görünümler, rutinler, tetikleyiciler, izolasyon)' ((Invoke-Py $PY_GERI @($work)) -eq 0)
  $exitCode = 0
}
catch {
  Write-Host "DURDURULDU: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
  # Üretim, şema / yetki, klinik satırlar, 8 görünüm ve test veritabanları önce ile aynı mı (geçici kullanıcılar silinmeden)
  if ($stateSaved -and (Test-Path -LiteralPath "$work\mevcut_durum.json")) {
    $code = Invoke-Py $PY_DURUM @('sonra', $work)
    $results.Add([pscustomobject]@{ Adim = 'üretim / yetkiler / klinik satırlar / görünümler / test veritabanları değişmedi'; Sonuc = $(if ($code -eq 0) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = '' })
    if ($code -ne 0) { $exitCode = 1 }
  }
  if ($jsonBefore) {
    $jsonAfter = Get-JsonHashes
    $changed = @($jsonBefore.Keys | Where-Object { $jsonAfter[$_] -ne $jsonBefore[$_] }) + @($jsonAfter.Keys | Where-Object { -not $jsonBefore.ContainsKey($_) })
    $results.Add([pscustomobject]@{ Adim = 'JSON dosyaları değişmedi'; Sonuc = $(if (-not $changed) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "$($changed | Select-Object -First 5)" })
    if ($changed) { $exitCode = 1 }
  }
  # Geçici geri yükleme veritabanları (yalnız bu betiğin oluşturdukları, ad biçimi denetlenir) ve kullanıcılar
  try {
    $cleanup = ''
    if ($dbsCreated -and -not $GeriYuklemeyiBirak) {
      foreach ($g in $GERI_DBS.Values) { if ($g -match $GERI_RE) { $cleanup += "DROP DATABASE IF EXISTS ``$g``;`n" } }
    }
    if ($usersCreated) { foreach ($h in $HOSTS) { $cleanup += "DROP USER IF EXISTS '$OKUR_USER'@'$h', '$GERI_USER'@'$h';`n" } }
    if ($cleanup) { Invoke-My 'mysql.exe' $adminUser $adminPwd @() $cleanup | Out-Null }
    if ($usersCreated -or $dbsCreated) {
      $left = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
        "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ($names) UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$OKUR_USER','$GERI_USER')")
      $results.Add([pscustomobject]@{ Adim = 'geçici kullanıcı / veritabanı temizliği'; Sonuc = $(if (-not $left -or $GeriYuklemeyiBirak) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "kalan: $(if ($left) { $left -join ', ' } else { 'yok' })" })
      if ($left -and -not $GeriYuklemeyiBirak) { $exitCode = 1 }
    }
  } catch { Write-Host "TEMİZLİK UYARISI: $($_.Exception.Message)" -ForegroundColor Red; $exitCode = 1 }
  foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
  $okurPwd = $null; $geriPwd = $null; $adminPwd = $null
  Write-Host "`n=== ÖZET" -ForegroundColor Cyan
  $results | Format-Table -AutoSize
  Write-Host $(if ($exitCode -eq 0) { "SONUÇ: AŞAMA 1 TAMAM. Yedek ve kayıtlar: $work" } else { 'SONUÇ: EN AZ BİR ADIM BAŞARISIZ (yukarıdaki tabloya bakın)' }) `
    -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Red' })
}
exit $exitCode
