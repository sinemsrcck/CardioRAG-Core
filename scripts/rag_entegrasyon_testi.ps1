<#
RAG SQL aktarım adaptörü — izole test veritabanında entegrasyon testi.

Çalıştırma (proje kökünde; MySQL hizmeti açık olmalı):
    powershell -ExecutionPolicy Bypass -File scripts\rag_entegrasyon_testi.ps1 [-TestVeritabanlariniSil]
        [-DegerlendirmeKlasoru <klasör> [-AktarimDisi <aktarim_disi.json>] [-SemantikSonuc <sonuclar.json> [-HashEsleme <hash_esleme.json>]]]
  -DegerlendirmeKlasoru : scripts/rag_onbellek_degerlendirme.py çıktısı (önbellekten, 0 API). Taslaklar bu klasördeki
                          yeni hat kayıtlarından, mevcut satırlar İZOLE TEST KOPYASINDAN okunarak test içinde kurulur
                          (backups\rag_test\<zaman>\taslak). Aynı klasördeki *.dogrulama.json otomatik deterministik
                          (ve -SemantikSonuc ile semantik) sonuçları YALNIZ test veritabanında kalite_isaretleri'ne
                          ('rag-' sürümleri) yazılır; sql/rag_dogrulama_core.sql görünümleri YALNIZ test veritabanında
                          kurulup denetlenir. Verilmezse eski altı taslak (data/review/rag, 741 aday) kullanılır.
  -AktarimDisi          : aktarım dışı listesi; bu kayıtlar aday olmaz, onaylanmışsa aktar() durur.
  -HashEsleme           : yalnız metaveri düzeltmesiyle hash'i değişen kayıtların eski -> yeni eşlemesi; eski
                          semantik sonuçlar değişmeden yeni hash'e bağlanır (eski hash ayrintida kalır).

Güvenlik:
  - Üretim veritabanları (kardiyo_tam_metin, kardiyo_sunum, kardiyo_clinical_data) yalnız OKUNUR:
    ayrı kullanıcı 'kardiyo_rag_okur' (yalnız SELECT, SHOW VIEW ve kaynak_bul için EXECUTE) + salt okunur transaction.
  - Test işlemleri ayrı kullanıcı 'kardiyo_rag_test' ile: yetkisi YALNIZ kardiyo_rag_test ve kardiyo_rag_test_sunum
    üzerinde; üretimde hiçbir yetkisi yok (betik bunu SHOW GRANTS ve reddedilen okuma denemesiyle doğrular).
  - Yönetici hesabı (.env MYSQL_USER) yalnız: test veritabanlarını ve iki test kullanıcısını oluşturma / silme.
  - Taslak dosyaları yalnız okunur (SHA-256 önce/sonra karşılaştırılır). Onaylar YALNIZ bellekteki test kopyasında
    verilir ve yalnız izole test veritabanına yazılır.
  - Üretim kopyasındaki şema sapması (klinik_bulgular.bilgi_turu / baglam VARCHAR) yalnız test kopyasında,
    test kullanıcısıyla ve SQL kurulumundan önce schema_core.sql'deki ENUM tanımına getirilir: önce küme dışı değer
    ve strict SQL modu kontrolü, sonra bütün satırların bütün alanlarının önce/sonra karşılaştırması.
    Üretim veritabanlarına bu düzeltme uygulanmaz.
  - Her kritik adımdan sonra çıkış kodu denetlenir; kurulum / izolasyon / entegrasyon testi başarısızsa RAG aktarım
    testine geçilmez. Üretim parmak izinin son kontrolü, test kullanıcılarının silinmesi ve ortam değişkenlerinin
    geri yüklenmesi finally içinde her durumda yapılır.
Çıkış kodu: 0 = bütün adımlar geçti; 1 = en az bir adım başarısız (ayrıntı özet tablosunda).
#>
param([switch]$TestVeritabanlariniSil, [string]$DegerlendirmeKlasoru, [string]$AktarimDisi, [string]$SemantikSonuc,
      [string]$HashEsleme,
      [int]$BeklenenAday = 741)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$py   = '.venv\Scripts\python.exe'
$bin  = 'C:\Program Files\MySQL\MySQL Server 8.0\bin'
$TEST_SRC = 'kardiyo_rag_test'; $TEST_SUNUM = 'kardiyo_rag_test_sunum'
$TEST_USER = 'kardiyo_rag_test'; $OKUR_USER = 'kardiyo_rag_okur'
$HOSTS = @('localhost', '127.0.0.1')
$DRAFTS = @(
  'data/review/rag/pubM-Atrial Fibrillation_rag_taslak_20261126-130859.json',
  'data/review/rag/pubM-Clinical Updates in Coronary Artery Disease A comprehensive review_rag_taslak_20261126-130940.json',
  'data/review/rag/TK-cardiovascular36-2-1_rag_taslak_20261126-130954.json',
  'data/review/rag/TK-cardiovascular37-2-1_rag_taslak_20261126-131007.json',
  'data/review/rag/who-J-Coll-Physicians-Surg-Pak-2020-30-9-906-911-eng_rag_taslak_20261126-131018.json',
  'data/review/rag/who-Out-of-hospital cardiac arrest in countries of the Gulf Cooperation_rag_taslak_20261126-131034.json'
)
# Salt okunur kaynak dosyalar (SHA-256 önce / sonra)
$SOURCES = $DRAFTS
if ($DegerlendirmeKlasoru) {
  if (-not (Test-Path -LiteralPath $DegerlendirmeKlasoru)) { throw "klasör yok: $DegerlendirmeKlasoru" }
  $SOURCES = @(Get-ChildItem -LiteralPath $DegerlendirmeKlasoru -Filter '*.json' | Sort-Object Name |
               ForEach-Object { Resolve-Path -LiteralPath $_.FullName -Relative })
}
foreach ($f in @($AktarimDisi, $SemantikSonuc, $HashEsleme) | Where-Object { $_ }) {
  if (-not (Test-Path -LiteralPath $f)) { throw "dosya yok: $f" }
  $SOURCES += $f
}
$stamp = Get-Date -f yyyyMMdd-HHmmss
$work  = "backups\rag_test\$stamp"

# --- .env (yönetici bağlantısı) ---------------------------------------------------------------
$dotenv = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $dotenv[$Matches[1]] = $Matches[2].Trim() }
$adminUser = if ($dotenv.MYSQL_USER) { $dotenv.MYSQL_USER } else { 'root' }
$adminPwd  = $dotenv.MYSQL_PASSWORD
$dbHost    = if ($dotenv.MYSQL_HOST) { $dotenv.MYSQL_HOST } else { 'localhost' }
$dbPort    = if ($dotenv.MYSQL_PORT) { $dotenv.MYSQL_PORT } else { '3306' }
function New-Secret { [guid]::NewGuid().ToString('N') + 'Aa1!' }
$testPwd = New-Secret; $okurPwd = New-Secret

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string]$step, [bool]$ok, [string]$info = '') {
  $results.Add([pscustomobject]@{ Adim = $step; Sonuc = $(if ($ok) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = $info })
  $color = if ($ok) { 'Green' } else { 'Red' }
  Write-Host ("[{0}] {1} {2}" -f $(if ($ok) { 'GECTI' } else { 'HATA ' }), $step, $info) -ForegroundColor $color
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
  # Python çıktısı doğrudan ekrana; fonksiyonun tek dönüş değeri çıkış kodudur
  ($PY_COMMON + "`n" + $code) | & $py - @arguments | Out-Host
  return [int]$LASTEXITCODE
}

# --- Gömülü Python (yalnız ASCII) ---------------------------------------------------------------
$PY_COMMON = @'
import json, os, sys, hashlib
PROD = ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
TEST_SRC, TEST_SUNUM, TEST_USER = 'kardiyo_rag_test', 'kardiyo_rag_test_sunum', 'kardiyo_rag_test'
E = os.environ
if (E.get('MYSQL_DATABASE'), E.get('MYSQL_SUNUM_DATABASE'), E.get('MYSQL_USER')) != (TEST_SRC, TEST_SUNUM, TEST_USER):
    sys.exit('GUVENLIK: ortam test veritabani / test kullanicisina yonlendirilmemis; durdu')
import mysql.connector
from src.database import get_connection, get_db_config
VIEWS = ('measurements', 'measurements_by_group', 'clinical_outcomes', 'treatments', 'general_clinical_findings',
         'diseases', 'diagnostics', 'symptoms')


def test_conn(readonly=False):
    c = get_connection()
    cur = c.cursor()
    cur.execute('SELECT DATABASE(), CURRENT_USER()')
    db, user = cur.fetchone()
    cur.close()
    c.rollback()
    if db != TEST_SRC or not str(user).startswith(TEST_USER + '@'):
        c.close()
        sys.exit(f'GUVENLIK: baglanti {db!r} / {user!r}; durdu')
    if readonly:
        c.start_transaction(readonly=True)
    return c


def prod_conn(database=None):
    cfg = dict(get_db_config())
    cfg.update(user=E['RAG_OKUR_USER'], password=E['RAG_OKUR_PASSWORD'], database=database)
    c = mysql.connector.connect(**cfg, charset='utf8mb4', use_unicode=True, autocommit=False)
    c.start_transaction(readonly=True)
    return c


def view_hash(cur, db, view):
    cur.execute(f'SELECT * FROM `{db}`.`{view}`')
    rows = sorted(json.dumps(list(r), default=str, ensure_ascii=False) for r in cur.fetchall())
    return len(rows), hashlib.sha256('\n'.join(rows).encode()).hexdigest()
'@

$PY_FINGERPRINT = @'
mode, path = sys.argv[1], sys.argv[2]
c = prod_conn()
cur = c.cursor()
out = {}
for s in PROD:
    cur.execute('SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s ORDER BY 1', (s,))
    tabs = {}
    for t, kind in cur.fetchall():
        if kind != 'BASE TABLE':
            continue
        inner = c.cursor()
        inner.execute(f'CHECKSUM TABLE `{s}`.`{t}`')
        chk = inner.fetchone()[1]
        inner.execute(f'SELECT COUNT(*) FROM `{s}`.`{t}`')
        tabs[t] = f'{chk}/{inner.fetchone()[0]}'
        inner.close()
    cur.execute('SELECT TABLE_NAME, MD5(VIEW_DEFINITION) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=%s ORDER BY 1', (s,))
    views = dict(cur.fetchall())
    cur.execute("SELECT ROUTINE_NAME, MD5(CONCAT_WS('|', CREATED, LAST_ALTERED)) FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA=%s ORDER BY 1", (s,))
    out[s] = {'tablolar': tabs, 'gorunumler': views, 'rutinler': dict(cur.fetchall())}
c.rollback()
c.close()
if mode == 'once':
    with open(path, 'x', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('uretim parmak izi:', {s: len(v['tablolar']) for s, v in out.items()}, 'tablo')
else:
    before = json.load(open(path, encoding='utf-8'))
    diff = [(s, k, n) for s in PROD for k in ('tablolar', 'gorunumler', 'rutinler')
            for n in set(before[s][k]) | set(out[s][k]) if before[s][k].get(n) != out[s][k].get(n)]
    print('URETIM VERITABANLARI DEGISMEDI' if not diff else f'FARK VAR: {diff[:20]}')
    sys.exit(1 if diff else 0)
'@

$PY_ISOLATION = @'
problems = []
t = test_conn(readonly=True)
tc = t.cursor()
for s in (TEST_SRC, TEST_SUNUM):
    for p in PROD:
        tc.execute('SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA=%s AND VIEW_DEFINITION LIKE %s',
                   (s, f'%{p}%'))
        problems += [f'{s}.{r[0]} -> {p}' for r in tc.fetchall()]
        tc.execute('SELECT ROUTINE_NAME FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=%s AND ROUTINE_DEFINITION LIKE %s',
                   (s, f'%{p}%'))
        problems += [f'{s}.{r[0]}() -> {p}' for r in tc.fetchall()]
tc.execute('SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=%s', (TEST_SUNUM,))
nview = tc.fetchone()[0]
tc.execute('SELECT review_status, COUNT(*) FROM klinik_bulgular WHERE bilgi_turu IS NOT NULL GROUP BY 1')
test_dist = dict(tc.fetchall())
tc.execute('SHOW GRANTS')
grants = [r[0] for r in tc.fetchall()]
bad_grants = [g for g in grants if not g.startswith('GRANT USAGE ON *.*')
              and not any(n in g.replace('\\_', '_') for n in (f'`{TEST_SRC}`.*', f'`{TEST_SUNUM}`.*'))]
denied = False
try:
    tc.execute('SELECT 1 FROM `kardiyo_tam_metin`.`klinik_bulgular` LIMIT 1')
    tc.fetchall()
except mysql.connector.Error as exc:
    denied = exc.errno in (1142, 1044)
t.rollback(); t.close()
p = prod_conn('kardiyo_tam_metin')
pc = p.cursor()
pc.execute('SELECT review_status, COUNT(*) FROM klinik_bulgular WHERE bilgi_turu IS NOT NULL GROUP BY 1')
prod_dist = dict(pc.fetchall())
p.rollback(); p.close()
print('test gorunum sayisi:', nview, '| test pilot:', test_dist, '| uretim pilot:', prod_dist)
print('test kullanicisi yetkileri:', grants)
ok = not problems and nview >= len(VIEWS) and test_dist == prod_dist and not bad_grants and denied
if not ok:
    sys.exit(f'IZOLASYON HATASI: uretime basvuran nesne {problems[:5]} | sayi esit {test_dist == prod_dist} | '
             f'test disi yetki {bad_grants} | uretim okumasi reddedildi {denied}')
print('IZOLASYON TAMAM: test nesneleri uretime basvurmuyor, test kullanicisi uretime erisemiyor, kopya sayilari ayni')
'@

$PY_ENUM = @'
# Uretimden kopyalanan semadaki sapma (bilgi_turu / baglam VARCHAR) YALNIZ test kopyasinda
# schema_core.sql'deki ENUM tanimina getirilir. Uretime, src/database.py'ye ve testlere dokunulmaz.
import re
from collections import Counter
from src.database import CORE_COLUMN_MIGRATIONS
from src.pilot_clinical import TYPES, CONTEXTS

TABLE = 'klinik_bulgular'
SETS = {'bilgi_turu': TYPES, 'baglam': CONTEXTS}
QT = f'`{TEST_SRC}`.`{TABLE}`'


def norm(s):
    return re.sub(r'\(\s+', '(', re.sub(r'\s+', ' ', s)).strip()


def stop(msg):
    sys.exit('SEMA DUZELTMESI DURDU: ' + msg)


# 1) Tanim: schema_core.sql'den birebir; CORE_COLUMN_MIGRATIONS ve TYPES / CONTEXTS ile tutarli olmali
text = open('sql/schema_core.sql', encoding='utf-8').read()
start = text.index('CREATE TABLE IF NOT EXISTS klinik_bulgular')
block = text[start:text.find('CREATE TABLE', start + 10)]
migr = {c: norm(d) for c, d in CORE_COLUMN_MIGRATIONS[TABLE]}
DEFS, VALUES = {}, {}
for col, allowed in SETS.items():
    m = re.search(r'\n\s*' + col + r"\s+(ENUM\([^)]*\)\s+NULL)\s*,", block)
    if not m:
        stop(f'schema_core.sql icinde {col} ENUM tanimi bulunamadi')
    DEFS[col] = norm(m.group(1))
    VALUES[col] = re.findall(r"'([^']*)'", DEFS[col])
    if DEFS[col] != migr.get(col):
        stop(f'{col}: schema_core.sql ve CORE_COLUMN_MIGRATIONS tanimlari farkli: {DEFS[col]!r} / {migr.get(col)!r}')
    if len(VALUES[col]) != len(set(VALUES[col])) or set(VALUES[col]) != set(allowed):
        stop(f'{col}: ENUM kumesi pilot_clinical kumesiyle ayni degil')
    print(f'tanim ({col}): {DEFS[col]}')
WANT_TYPE = {c: 'enum(' + ','.join(f"'{v}'" for v in VALUES[c]) + ')' for c in SETS}

# 2) Baglanti: yalniz test veritabani + sinirli test kullanicisi; uretimde yetki yok
c = test_conn()
cur = c.cursor()
cur.execute('SHOW GRANTS')
grants = [r[0] for r in cur.fetchall()]
bad = [g for g in grants if not g.startswith('GRANT USAGE ON *.*')
       and not any(n in g.replace('\\_', '_') for n in (f'`{TEST_SRC}`.*', f'`{TEST_SUNUM}`.*'))]
if bad:
    stop(f'test kullanicisinin test disi yetkisi var: {bad}')
try:
    cur.execute('SELECT 1 FROM `kardiyo_tam_metin`.`klinik_bulgular` LIMIT 1')
    cur.fetchall()
    stop('test kullanicisi uretimi okuyabiliyor')
except mysql.connector.Error as exc:
    if exc.errno not in (1142, 1044):
        raise

# 3) Strict SQL modu (kume disi deger ALTER'i hata ile durdurur, sessizce bosaltmaz)
cur.execute('SELECT @@SESSION.sql_mode, @@GLOBAL.sql_mode')
smode, gmode = cur.fetchone()
smode, gmode = str(smode), str(gmode)
print('sql_mode (oturum):', smode)
if not ({'STRICT_TRANS_TABLES', 'STRICT_ALL_TABLES'} & set(smode.split(','))):
    stop(f'strict SQL modu etkin degil: {smode!r}')
cur.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s', (TEST_SRC, TABLE))
engine = cur.fetchone()
if not engine or str(engine[0]).lower() != 'innodb':
    stop(f'{TABLE} InnoDB degil: {engine}')

COLQ = ('SELECT COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, '
        'CHARACTER_SET_NAME, COLLATION_NAME, EXTRA, COLUMN_KEY, COLUMN_COMMENT FROM information_schema.COLUMNS '
        'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION')
IDXQ = ('SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS '
        'WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY 1, 2')


def txt(v):
    return v.decode('utf-8') if isinstance(v, (bytes, bytearray)) else v


def meta():
    cur.execute(COLQ, (TEST_SRC, TABLE))
    cols = {txt(r[0]): tuple(txt(x) for x in r[1:]) for r in cur.fetchall()}
    cur.execute(IDXQ, (TEST_SRC, TABLE))
    return cols, [tuple(txt(x) for x in r) for r in cur.fetchall()]


def val(v):
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode('utf-8')
        except UnicodeDecodeError:
            return 'hex:' + v.hex()
    return v


def snapshot():
    """Butun satirlarin butun alanlari (NULL = None), sutun bazinda NULL sayilari."""
    cur.execute(f'SELECT * FROM {QT} ORDER BY id')
    names = [d[0] for d in cur.description]
    rows, nulls = {}, Counter()
    for r in cur.fetchall():
        rec = dict(zip(names, (val(v) for v in r)))
        if rec['id'] in rows:
            stop(f'yinelenen id {rec["id"]}')
        rows[rec['id']] = json.dumps(rec, default=str, ensure_ascii=False, sort_keys=True)
        nulls.update(k for k, v in rec.items() if v is None)
    c.rollback()
    return names, rows, nulls


cols0, idx0 = meta()
todo = []
for col in SETS:
    info = cols0.get(col)
    if not info:
        stop(f'{col} sutunu yok')
    _, dtype, ctype, nullable = info[0], info[1], info[2], info[3]
    print(f'once  {col}: {ctype} / NULL {nullable} / {info[6]}')
    if ctype == WANT_TYPE[col] and nullable == 'YES':
        print(f'{col} zaten schema_core.sql ile ayni; degistirilmeyecek')
        continue
    if dtype not in ('varchar', 'char') or nullable != 'YES':
        stop(f'{col}: beklenmeyen kaynak tipi {ctype} / NULL {nullable}; yalniz VARCHAR NULL donusturulur')
    todo.append(col)

# 4) Kume disi deger kontrolu (HEX ile birebir: buyuk/kucuk harf ve sondaki bosluk farki da yakalanir)
for col in todo:
    cur.execute(f'SELECT HEX(`{col}`), COUNT(*) FROM {QT} WHERE `{col}` IS NOT NULL GROUP BY HEX(`{col}`)')
    dist = {bytes.fromhex(h if isinstance(h, str) else h.decode()).decode('utf-8'): n for h, n in cur.fetchall()}
    outside = {v: n for v, n in dist.items() if v not in VALUES[col]}
    print(f'deger dagilimi {col}:', dict(sorted(dist.items())))
    if outside:
        stop(f'{col}: kume disi degerler {outside}; donusum yapilmadi')
c.rollback()

if not todo:
    c.close()
    print('SEMA DUZELTMESI GEREKMEDI: iki sutun zaten ENUM')
    sys.exit(0)

names0, rows0, nulls0 = snapshot()
print(f'once: {len(rows0)} satir, {len(names0)} sutun, NULL bilgi_turu {nulls0["bilgi_turu"]}, baglam {nulls0["baglam"]}')

# 5) ALTER: yalniz kardiyo_rag_test (tam nitelikli ad) ve yalniz test kullanicisi
cur.execute('SELECT DATABASE(), CURRENT_USER()')
db, user = cur.fetchone()
if db != TEST_SRC or not str(user).startswith(TEST_USER + '@'):
    stop(f'ALTER oncesi baglanti {db!r} / {user!r}')
stmt = f'ALTER TABLE {QT} ' + ', '.join(f'MODIFY COLUMN `{col}` {DEFS[col]}' for col in todo)
print('calistiriliyor:', stmt)
try:
    cur.execute(stmt)
except mysql.connector.Error as exc:
    stop(f'ALTER hatasi (veri degismedi): {exc}')

# 6) Sonra: sema ve butun satirlarin butun alanlari
cols1, idx1 = meta()
names1, rows1, nulls1 = snapshot()
problems = []
if names1 != names0:
    problems.append('sutun listesi/sirasi degisti')
if len(rows1) != len(rows0):
    problems.append(f'satir sayisi {len(rows0)} -> {len(rows1)}')
diff_rows = [i for i in set(rows0) | set(rows1) if rows0.get(i) != rows1.get(i)]
if diff_rows:
    problems.append(f'{len(diff_rows)} satirin icerigi degisti (or. id {sorted(diff_rows)[:5]})')
if nulls1 != nulls0:
    problems.append(f'NULL sayilari degisti: {dict(nulls0)} -> {dict(nulls1)}')
if idx1 != idx0:
    problems.append('indeksler degisti')
for name in set(cols0) | set(cols1):
    if name in todo:
        a, b = cols0[name], cols1.get(name)
        if not b or b[1] != 'enum' or b[2] != WANT_TYPE[name] or b[3] != 'YES' or b[0] != a[0] or b[4] is not None:
            problems.append(f'{name}: sonuc tanimi beklenen degil {b}')
        else:
            print(f'sonra {name}: {b[2]} / NULL {b[3]} / {b[6]}')
    elif cols0.get(name) != cols1.get(name):
        problems.append(f'diger sutun degisti: {name}')
c.close()
if problems:
    stop('; '.join(problems))
print(f'SEMA DUZELTMESI TAMAM (yalniz {TEST_SRC}): {", ".join(todo)} -> ENUM; {len(rows1)} satirin butun alanlari, '
      f'NULL sayilari ve satir sayisi ayni')
'@

$PY_RAG = @'
import copy
from collections import Counter, defaultdict
from src import rag_aktarim as A
from src.pilot_clinical import pdf_pages
from src.rag_klinik_cikarim import grouped_rows, categories_of, measurement_key
from src.sunum_hoca import max_group_count

GROUPED = {'measurements', 'treatments', 'diseases', 'diagnostics'}
TERMS = ('HbA1c', 'Aspirin', 'troponin', 'stroke', 'ablation', 'eosinophil')
fail = []


def check(name, ok, info=''):
    print(('TAMAM ' if ok else 'HATA  ') + name + (f' | {info}' if info else ''))
    if not ok:
        fail.append(name)


def proc_ids(conn, term):
    cur = conn.cursor()
    cur.callproc('kaynak_bul', (term,))
    ids = []
    for res in cur.stored_results():
        cols = [d[0] for d in res.description]
        ids += [dict(zip(cols, r))['bulgu_id'] for r in res.fetchall()]
    cur.close()
    return sorted(ids)


def ph(values):
    return ','.join(['%s'] * len(values))


# kaynak_bul karsilastirmasi. Testte yeni prosedur (+ kanit_parcalari_metin). Uretimdeki prosedurun surumu
# varsayilmaz, sonuc sutunlarindan (SELECT k.*) belirlenir: yeni ise guncel uretim = guncel test (A = B); eski ise
# A = eski kosul. Her iki durumda: eski kosul iki veritabaninda ayni (A_eski = A2), eski kosul sonuclari korunur
# (A2 - B bos) ve eski kosulun otesindeki her eslesme (B - A2) yalniz 2. / 3. kanit parcasindan gelir.
KB_OLD_COLS = ('mudahale', 'tani', 'hasta_grubu', 'sonuc', 'kanit_cumle')
KB_COLL = 'utf8mb4_0900_ai_ci'
BS = chr(92)


def kb_pattern(term):
    """kaynak_bul icindeki desenle ayni: TRIM, \\ % _ kacisli, iki yanda %."""
    return '%' + term.strip(' ').replace(BS, BS + BS).replace('%', BS + '%').replace('_', BS + '_') + '%'


def kb_like(expr):
    return f'({expr}) COLLATE {KB_COLL} LIKE %s COLLATE {KB_COLL}'


def kb_part(i):
    return (f"CASE WHEN JSON_VALID(b.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(b.taslak_notu, "
            f"'$.kanit_parcalari[{i}].metin')) = 'STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(b.taslak_notu, "
            f"'$.kanit_parcalari[{i}].metin')) END END")


def kb_rows(cur, ids):
    """id -> (satir_hash, review_status); cur'un veritabanindaki klinik_bulgular."""
    if not ids:
        return {}
    cur.execute(f'SELECT id, satir_hash, review_status FROM klinik_bulgular WHERE id IN ({ph(ids)})', tuple(sorted(ids)))
    return {i: tuple(v.decode('utf-8') if isinstance(v, (bytes, bytearray)) else v for v in rest)
            for i, *rest in cur.fetchall()}


def proc_result(conn, term):
    """(bulgu_id kumesi, sonuc sutunlari) - kaynak_bul."""
    cur = conn.cursor()
    cur.callproc('kaynak_bul', (term,))
    ids, cols = set(), set()
    for res in cur.stored_results():
        names = [x[0] for x in res.description]
        cols |= set(names)
        ids |= {dict(zip(names, r))['bulgu_id'] for r in res.fetchall()}
    cur.close()
    return ids, cols


def kb_old(cur, d):
    """Eski arama kosulu (5 sutun) - cur'un veritabanindaki v_bulgu_kaynak."""
    cur.execute('SELECT bulgu_id FROM v_bulgu_kaynak WHERE ' + ' OR '.join(kb_like(c) for c in KB_OLD_COLS),
                (d,) * len(KB_OLD_COLS))
    return {r[0] for r in cur.fetchall()}


def kb_compare(pc, tc, p, t, multi_ids, term):
    d = kb_pattern(term)
    (A, a_cols), (B, b_cols) = proc_result(p, term), proc_result(t, term)
    A_old, A2 = kb_old(pc, d), kb_old(tc, d)
    prod_new = 'kanit_parcalari_metin' in a_cols
    check(f'kaynak_bul {term!r}: test proseduru yeni (kanit_parcalari_metin)', 'kanit_parcalari_metin' in b_cols)
    check(f'kaynak_bul {term!r}: eski kosul uretim = test (A_eski = A2)', A_old == A2,
          f'A_eski {len(A_old)}, A2 {len(A2)}; fark {sorted(A_old ^ A2)[:10]}')
    if prod_new:
        check(f'kaynak_bul {term!r}: guncel uretim = guncel test (A = B; uretim proseduru yeni)', A == B,
              f'A {len(A)}, B {len(B)}; A-B {sorted(A - B)[:10]}, B-A {sorted(B - A)[:10]}')
    else:
        check(f'kaynak_bul {term!r}: uretim proseduru eski: A = eski kosul (A = A_eski)', A == A_old,
              f'A {len(A)}, A_eski {len(A_old)}; fark {sorted(A ^ A_old)[:10]}')
    lost, new = sorted(A2 - B), sorted(B - A2)
    check(f'kaynak_bul {term!r}: kaybolan kayit yok (A - B bos)', not (A - B), f'{sorted(A - B)[:20]}')
    check(f'kaynak_bul {term!r}: eski kosul sonuclari korunuyor (A2 - B bos)', not lost, f'{len(lost)} {lost[:20]}')
    check(f'kaynak_bul {term!r}: tek parcali sonuclar eski kosulla ayni', (A2 - multi_ids) == (B - multi_ids),
          f'yalniz eski kosul {sorted((A2 - multi_ids) - (B - multi_ids))[:10]}, '
          f'yalniz test {sorted((B - multi_ids) - (A2 - multi_ids))[:10]}')
    print(f'  kaynak_bul {term!r}: uretim proseduru {"yeni" if prod_new else "eski"}; A {len(A)}, B {len(B)}, '
          f'A2 {len(A2)}, ek (B - A2) {len(new)}')
    bad, examined = [], set()
    if new:
        cols = ', '.join(kb_like(f'k.{c}') for c in KB_OLD_COLS)
        tc.execute(f"SELECT k.bulgu_id, {cols}, {kb_like('k.kanit_parcalari_metin')}, "
                   f"CASE WHEN JSON_VALID(b.taslak_notu) THEN JSON_LENGTH(JSON_EXTRACT(b.taslak_notu, '$.kanit_parcalari')) END, "
                   f"{kb_like(kb_part(0))}, {kb_like(kb_part(1))}, {kb_like(kb_part(2))}, ({kb_part(0)}) = b.kanit_cumle "
                   f"FROM v_bulgu_kaynak AS k JOIN klinik_bulgular AS b ON b.id = k.bulgu_id "
                   f"WHERE k.bulgu_id IN ({ph(new)}) ORDER BY k.bulgu_id",
                   (d,) * (len(KB_OLD_COLS) + 4) + tuple(new))
        for r in tc.fetchall():
            i, olds, concat, nparts, p0, p1, p2, first_same = r[0], r[1:6], r[6], r[7], r[8], r[9], r[10], r[11]
            examined.add(i)
            problems = []
            if i not in multi_ids:
                problems.append('cok parcali degil')
            if any(bool(x) for x in olds):
                problems.append('eski sutunda da eslesiyor')
            if not (nparts and nparts <= 3):
                problems.append(f'parca sayisi {nparts}')
            if bool(p0):
                problems.append('ilk parcada eslesiyor')
            if not (bool(p1) or bool(p2)):
                problems.append('yapay (birlestirmeden) eslesme' if bool(concat) else 'hicbir parcada eslesme yok')
            if not first_same:
                problems.append('ilk parca kanit_cumle degil')
            if problems:
                bad.append((i, problems))
    check(f'kaynak_bul {term!r}: incelenen ek kayitlar = B - A2 (tamami)', examined == set(new),
          f'yeni {len(new)}, incelenen {len(examined)}; eksik {sorted(set(new) - examined)[:10]}, '
          f'fazla {sorted(examined - set(new))[:10]}')
    check(f'kaynak_bul {term!r}: yeni eslesmeler yalniz 2. / 3. kanit parcasindan (yapay yok)', not bad,
          f'yeni {len(new)}; sorunlu {bad[:5]}')
    diff = set(lost) | set(new) | (A ^ B)
    rp, rt = kb_rows(pc, diff), kb_rows(tc, diff)
    check(f'kaynak_bul {term!r}: farkli kayitlarin id / satir_hash / review_status iki veritabaninda ayni',
          set(rp) == set(rt) == diff and rp == rt, f'{len(diff)} kayit')


# 0) Yalniz adi verilen alti taslak; onaylar YALNIZ bellekteki test kopyasinda
paths = json.loads(E['RAG_TASLAKLAR'])
expected_total = int(E['RAG_BEKLENEN_ADAY'])
haric = A.load_haric(E.get('RAG_AKTARIM_DISI') or None)
plan, total, listed = [], 0, 0
for path in paths:
    d = json.load(open(path, encoding='utf-8'))
    total += len(d['adaylar'])
    if haric:
        listed += sum(A.satir_hash(d['document_id'], r) in haric for r in d['adaylar'])
    check(f'taslak tamamen pending: {os.path.basename(path)[:45]}', all(r['review_status'] == 'pending' for r in d['adaylar']))
    doc, pages = pdf_pages(f"data/pdfs/{d['pdf']}")
    check(f"taslak PDF ile eslesiyor: {d['pdf'][:35]}", d['document_id'] == doc['document_id'])
    recs = copy.deepcopy(d['adaylar'])
    keep = [i for i, r in enumerate(recs) if r['kanit_turu'] == 'metin' and not r.get('kanit_parcalari')][:2]
    for i, r in enumerate(recs):
        r['review_status'] = 'pending' if i in keep else 'verified'
    plan.append((d['pdf'], doc, pages, recs, keep))
check(f'toplam pending aday {expected_total}', total == expected_total, str(total))
if haric:
    check('aktarim disi kayitlar aday degil', listed == 0, f'{len(haric)} kayit listede; adaylarda {listed}')
if fail:
    sys.exit('taslak dogrulamasi basarisiz; aktarim testi yapilmadi')

# 1) RAG oncesi: pilot gorunumleri uretimle ayni; kaynak_bul eski sonuclari koruyor, yeni sonuclar yalniz
#    cok parcali kanitin 2. / 3. parcasindan (iki taraf da salt okunur)
p, t = prod_conn('kardiyo_tam_metin'), test_conn(readonly=True)
pc, tc = p.cursor(), t.cursor()
for v in VIEWS:
    a, b = view_hash(pc, 'kardiyo_sunum', v), view_hash(tc, TEST_SUNUM, v)
    check(f'pilot gorunumu degismedi: {v}', a == b, f'uretim {a[0]}, test {b[0]} satir')
tc.execute('SELECT bulgu_id FROM v_bulgu_kaynak WHERE kanit_parcalari_metin IS NOT NULL')
multi_ids = {r[0] for r in tc.fetchall()}
print(f'  test v_bulgu_kaynak cok parcali kayit: {len(multi_ids)}')
for term in TERMS:
    kb_compare(pc, tc, p, t, multi_ids, term)
before_views = {v: view_hash(tc, TEST_SUNUM, v)[0] for v in VIEWS}
p.rollback(); p.close(); t.rollback(); t.close()
c = test_conn()
fp0 = A.table_fingerprint(A.KorumaliBaglanti(c))
c.rollback(); c.close()

# 2) Prova -> ROLLBACK
for pdf, doc, pages, recs, _ in plan:
    c = test_conn()
    try:
        res = A.aktar(A.KorumaliBaglanti(c, yazma=True, commit_izni=False), doc, pages, recs, yaz=False, haric=haric)
        want = sum(r['review_status'] == 'verified' for r in recs)
        check(f'prova {pdf[:35]}', res['kalici'] is False and res['yazilan'] == want,
              f"yazilan {res['yazilan']}, onaysiz atlanan {res['atlanan_onaysiz']}")
    finally:
        c.rollback(); c.close()
c = test_conn()
fp1 = A.table_fingerprint(A.KorumaliBaglanti(c))
c.rollback(); c.close()
check('prova sonrasi klinik_bulgular birebir ayni (ROLLBACK)', fp1 == fp0, f'{len(fp0)} satir')
if fail:
    sys.exit('prova basarisiz; kalici test aktarimi yapilmadi')

# 3) Izole test veritabanina kalici aktarim (yalniz test kopyasindaki verified kayitlar)
new = []
for pdf, doc, pages, recs, _ in plan:
    c = test_conn()
    try:
        res = A.aktar(A.KorumaliBaglanti(c, yazma=True, commit_izni=True), doc, pages, recs, yaz=True, haric=haric)
        new += [(pdf, doc, r) for r in recs if r['review_status'] == 'verified']
        print(f'  {pdf[:45]}: {res["yazilan"]} kayit (izole test veritabani)')
    finally:
        c.close()
c = test_conn(readonly=True)
g = A.KorumaliBaglanti(c)
fp2 = A.table_fingerprint(g)
check('mevcut butun satirlarin butun alanlari ayni', all(fp2.get(i) == h for i, h in fp0.items()))
new_ids = sorted(set(fp2) - set(fp0))
check('yeni satir = onaylanan test kaydi', len(new_ids) == len(new), f'{len(new_ids)} / {len(new)}')
cur = g.cursor()
cur.execute(f'SELECT id, satir_hash, cikarma_yontemi, review_status FROM klinik_bulgular WHERE id IN ({ph(new_ids)})',
            tuple(new_ids))
rows = {h: (i, m, s) for i, h, m, s in cur.fetchall()}
check('yeni satirlar rag / verified', all(m == 'rag' and s == 'verified' for _, m, s in rows.values()))
pending = [A.satir_hash(doc['document_id'], recs[i]) for _, doc, _, recs, keep in plan for i in keep]
cur.execute(f'SELECT COUNT(*) FROM klinik_bulgular WHERE satir_hash IN ({ph(pending)})', tuple(pending))
check('pending kayitlar aktarilmadi', cur.fetchone()[0] == 0, f'{len(pending)} pending')
idmap = {rows[A.satir_hash(doc['document_id'], r)][0]: (pdf, {**r, 'taslak_notu': json.loads(r['taslak_notu'])})
         for pdf, doc, r in new}

# 4) Grup kurali ve bolum bilgisi
cur.execute(f'SELECT bulgu_id, satir_grup_karsilastirmasi, bolum_oneki, evidence FROM v_sunum_taban WHERE bulgu_id IN ({ph(new_ids)})',
            tuple(new_ids))
taban = {r[0]: r[1:] for r in cur.fetchall()}
by_pdf = defaultdict(list)
for i, (pdf, r) in idmap.items():
    by_pdf[pdf].append((i, r))
group_bad, section_bad, paed, adult = [], [], 0, 0
for pdf, items in by_pdf.items():
    py = grouped_rows([r for _, r in items])
    for i, r in items:
        flag, prefix, evidence = taban[i]
        if r['kanit_turu'] == 'tablo' and bool(flag) != (measurement_key(r) in py):
            group_bad.append(i)
        note = r['taslak_notu']
        if note.get('bolum_goster'):
            if prefix != note.get('alt_bolum') or note['alt_bolum'] not in (evidence or ''):
                section_bad.append(i)
            paed += note['alt_bolum'] == 'Paediatric cases'
            adult += note['alt_bolum'] == 'Adult cases'
check('Python ve SQL grup karsilastirmasi ayni', not group_bad, f'uyumsuz {len(group_bad)}')
check('bolum bilgisi gorunumde (bolum_oneki + evidence)', not section_bad, f'uyumsuz {len(section_bad)}')
check('Adult / Paediatric bolumleri gorunuyor', paed > 0 and adult > 0, f'Adult {adult}, Paediatric {paed}')

# 5) Sekiz gorunum: artis = beklenen
expected = Counter()
for pdf, items in by_pdf.items():
    py = grouped_rows([r for _, r in items])
    keys = defaultdict(set)
    for i, r in items:
        for cat in categories_of(r, py):
            keys[cat].add(A.sql_row_key(r) if cat in GROUPED and A.sql_row_key(r) else ('k', i))
    for cat, k in keys.items():
        expected[cat] += len(k)
vc = c.cursor()
for view in VIEWS:
    delta = view_hash(vc, TEST_SUNUM, view)[0] - before_views[view]
    check(f'gorunum {view}', delta == expected[view], f'artis {delta}, beklenen {expected[view]}')
vc.execute('SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME LIKE %s',
           (TEST_SUNUM, 'measurements', 'group\\_%\\_value'))
installed, needed = vc.fetchone()[0], max_group_count(c, TEST_SRC)
check('measurements grup sutunu yeterli', installed >= needed, f'kurulu {installed}, gereken {needed}')

# 6) Cok parcali kanit: kaynak_bul ikinci parcadan buluyor
multi = [(i, r) for i, (_, r) in idmap.items() if r.get('kanit_parcalari')]
missed = [i for i, r in multi if i not in proc_ids(c, r['kanit_parcalari'][1]['metin'][:60].strip())]
check('kaynak_bul cok parcali kayitlari ikinci parcadan buluyor', bool(multi) and not missed,
      f'{len(multi)} kayit, bulunamayan {len(missed)}')
c.rollback(); c.close()
print('\nSONUC:', 'BUTUN KONTROLLER GECTI' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_TASLAK = @'
# Degerlendirme klasorundeki (onbellekten, 0 API) yeni hat kayitlarindan taslak; mevcut satirlar IZOLE TEST KOPYASINDAN
# okunur (uretim kopyasi ile ayni icerik). Butun adaylar pending; dosyalar yalniz backups/rag_test altina yazilir.
from collections import Counter
from pathlib import Path
from src import rag_aktarim as A
from src.pilot_clinical import pdf_pages

src_dir, out_dir, manifest = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
haric = A.load_haric(E.get('RAG_AKTARIM_DISI') or None)
fail = []


def check(name, ok, info=''):
    print(('TAMAM ' if ok else 'HATA  ') + name + (f' | {info}' if info else ''))
    if not ok:
        fail.append(name)


files = sorted(f for f in src_dir.glob('*.json') if f.name != 'ozet.json' and f.name.count('.') == 1)
check('alti makale kaydi', len(files) == 6, f'{len(files)}')
out_dir.mkdir(parents=True, exist_ok=True)
paths, total, summary = [], 0, Counter()
for f in files:
    d = json.load(open(f, encoding='utf-8'))
    meta, _ = A.load_run(d['calistirma'])
    doc, pages = pdf_pages(f"data/pdfs/{meta['pdf']}")
    check(f"PDF ayni: {meta['pdf'][:35]}", doc['document_id'] == meta['document_id'])
    c = test_conn(readonly=True)
    existing = A.existing_rows(A.KorumaliBaglanti(c), doc['document_id'])
    c.rollback(); c.close()
    draft = A.build_draft(d['kayitlar'], pages, meta, existing, haric=haric)
    adaylar = [A.serializable(r) for r in draft['adaylar']]
    reasons = Counter(r['asama'] for r in draft['inceleme'])
    old = Counter(d['taslak_sayilari'].get('inceleme_nedenleri', {}))
    # mevcut satirlarla karsilastirma disinda her sey degerlendirmedeki taslakla ayni olmali
    check(f"{meta['pdf'][:35]}: aday + kesin + olasi + grup = degerlendirme adayi",
          len(adaylar) + len(draft['kesin_tekrar']) + reasons['olasi_tekrar'] + reasons['grup_uyumu'] - old['grup_uyumu']
          == d['taslak_sayilari']['pending_aday'],
          f"aday {len(adaylar)}, kesin {len(draft['kesin_tekrar'])}, olasi {reasons['olasi_tekrar']}, "
          f"degerlendirme {d['taslak_sayilari']['pending_aday']}, mevcut satir {len(existing)}")
    check(f"{meta['pdf'][:35]}: esleme / aktarim_disi incelemesi ayni",
          (reasons['esleme'], reasons['aktarim_disi']) == (old['esleme'], old['aktarim_disi']), f'{dict(reasons)}')
    check(f"{meta['pdf'][:35]}: butun adaylar pending", all(r['review_status'] == 'pending' for r in adaylar))
    path = out_dir / f"{Path(meta['pdf']).stem}.taslak.json"
    with open(path, 'x', encoding='utf-8') as handle:
        json.dump({'pdf': meta['pdf'], 'document_id': doc['document_id'], 'calistirma': d['calistirma'],
                   'sayilar': draft['sayilar'], 'adaylar': adaylar, 'inceleme': draft['inceleme'],
                   'kesin_tekrar': draft['kesin_tekrar']}, handle, ensure_ascii=False, indent=2, default=str)
    paths.append(str(path)); total += len(adaylar)
    summary.update({'aday': len(adaylar), 'kesin_tekrar': len(draft['kesin_tekrar']), **reasons})
print('  taslak ozeti:', dict(summary))
json.dump({'taslaklar': paths, 'toplam_aday': total, 'ozet': dict(summary)}, open(manifest, 'x', encoding='utf-8'))
sys.exit(1 if fail else 0)
'@

$PY_DOGRULAMA = @'
# Otomatik dogrulama sonuclari -> kalite_isaretleri ('rag-' surumleri) ve iki yeni gorunum; YALNIZ test veritabani.
import subprocess
import tempfile
from pathlib import Path
from src import rag_aktarim as A
from src import rag_dogrulama as V

rapor = sys.argv[1]
semantik = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != '-' else None
esleme_yolu = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != '-' else None
links = json.load(open(esleme_yolu, encoding='utf-8'))['eslesme'] if esleme_yolu else None
fail = []


def check(name, ok, info=''):
    print(('TAMAM ' if ok else 'HATA  ') + name + (f' | {info}' if info else ''))
    if not ok:
        fail.append(name)


def rows_hash(cur, sql):
    cur.execute(sql)
    rows = sorted(json.dumps(list(r), default=str, ensure_ascii=False) for r in cur.fetchall())
    return len(rows), hashlib.sha256('\n'.join(rows).encode()).hexdigest()


def columns(cur):
    cur.execute('SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA IN (%s, %s) AND TABLE_NAME NOT IN (%s, %s) ORDER BY 1, 2, 4',
                (TEST_SRC, TEST_SUNUM, 'v_rag_dogrulama_durumu', 'v_rag_ek_bilgi'))
    return cur.fetchall()


def snapshot():
    c = test_conn(readonly=True)
    cur = c.cursor()
    out = {'kalite_calistirmalari': rows_hash(cur, 'SELECT * FROM kalite_calistirmalari'),
           'data_quality_isaretleri': rows_hash(cur, "SELECT * FROM kalite_isaretleri WHERE kontrol_surumu NOT LIKE 'rag-%'"),
           'v_kalite_ozet': view_hash(cur, TEST_SRC, 'v_kalite_ozet'),
           'v_kalite_bulgu': view_hash(cur, TEST_SRC, 'v_kalite_bulgu'),
           'klinik_bulgular': A.table_fingerprint(A.KorumaliBaglanti(c)),
           'sutunlar': columns(cur)}
    for v in VIEWS:
        out['sunum.' + v] = view_hash(cur, TEST_SUNUM, v)
    c.rollback(); c.close()
    return out


def rag_rows():
    c = test_conn(readonly=True)
    cur = c.cursor()
    cur.execute("SELECT kontrol_surumu, COUNT(*), SUM(klinik_bulgu_id IS NOT NULL) FROM kalite_isaretleri "
                "WHERE kontrol_surumu LIKE 'rag-%' GROUP BY 1")
    out = {v: (int(n), int(linked or 0)) for v, n, linked in cur.fetchall()}
    c.rollback(); c.close()
    return out


KAYIT_DIR = tempfile.mkdtemp(prefix='rag_kalite_')
KAYITLAR = []


def kalite_yaz(write):
    cmd = [sys.executable, '-m', 'src.rag_dogrulama', 'kalite-yaz', '--rapor', rapor]
    cmd += ['--semantik', semantik] if semantik else []
    cmd += ['--hash-esleme', esleme_yolu] if esleme_yolu else []
    if write:
        KAYITLAR.append(os.path.join(KAYIT_DIR, f'kalite_parti_{len(KAYITLAR) + 1}.json'))
        cmd += ['--yaz', '--kayit', KAYITLAR[-1]]
    return subprocess.run(cmd).returncode


def kalite_geri_al(kayit):
    return subprocess.run([sys.executable, '-m', 'src.rag_dogrulama', 'kalite-geri-al', '--kayit', kayit, '--yaz']).returncode


# Beklenen satirlar (Python tarafi)
files = sorted(Path(rapor).glob('*.dogrulama.json'))
expected = {}
for f in files:
    for v, rows in V.kalite_satirlari(json.load(open(f, encoding='utf-8'))).items():
        expected[v] = expected.get(v, 0) + len(rows)
if semantik:
    sem = json.load(open(semantik, encoding='utf-8'))['sonuclar']
    for v, rows in V.kalite_satirlari({'kayitlar': []}, sem, hash_map=links).items():
        if rows:
            expected[v] = expected.get(v, 0) + len(rows)
check('dogrulama raporlari bulundu', len(files) > 0, f'{len(files)} dosya, beklenen satir {expected}')
if fail:
    sys.exit(1)

before = snapshot()
check('oncesinde rag- kalite satiri yok', not rag_rows())

# 1) Gorunum dosyasi (yalniz CREATE OR REPLACE VIEW) test veritabanina
sql = open('sql/rag_dogrulama_core.sql', encoding='utf-8').read()
body = '\n'.join(l for l in sql.splitlines() if not l.lstrip().startswith('--'))
stmts = [s.strip() for s in body.split(';') if s.strip()]
check('gorunum dosyasi yalniz iki CREATE OR REPLACE VIEW', len(stmts) == 2 and
      all(s.startswith('CREATE OR REPLACE VIEW v_rag_') for s in stmts))
c = test_conn()
cur = c.cursor()
for s in stmts:
    cur.execute(s)
c.commit(); c.close()

# 2) Prova (ROLLBACK), sonra kalici yazim, sonra tekrar (yeniden calistirilabilir)
check('kalite-yaz prova cikis kodu 0', kalite_yaz(False) == 0)
check('prova sonrasi rag- kalite satiri yok (ROLLBACK)', not rag_rows())
check('kalite-yaz --yaz cikis kodu 0', kalite_yaz(True) == 0)
got = rag_rows()
check('yazilan satir = beklenen (surum bazinda)', {v: n for v, (n, _) in got.items()} == expected, f'{got}')
check('rag- satirlari klinik_bulgu_id ile baglanmadi (satir_hash ile)', all(l == 0 for _, l in got.values()))
check('ayni girdiyle ikinci --yaz reddedildi (mukerrer parti yok)', kalite_yaz(True) != 0)
check('reddedilen yazimdan sonra satirlar ayni', rag_rows() == got, f'{rag_rows()}')
check('ilk parti geri alindi (kalite-geri-al --yaz)', kalite_geri_al(KAYITLAR[0]) == 0)
check('geri alma sonrasi rag- satiri yok', not rag_rows(), f'{rag_rows()}')
check('geri alma sonrasi yeniden --yaz cikis kodu 0', kalite_yaz(True) == 0)
check('yeniden yazimda satir sayisi = beklenen', rag_rows() == got, f'{rag_rows()}')

# 3) Mevcut tablolar / gorunumler / sutunlar degismedi; verified anlami ayni
after = snapshot()
for k in before:
    check(f'degismedi: {k}', before[k] == after[k])

# 4) Uc durum ayri sutunlarda
c = test_conn(readonly=True)
cur = c.cursor()
cur.execute('SELECT otomatik_deterministik, semantik_karar, insan_durumu, insan_onayli, COUNT(*) '
            'FROM v_rag_dogrulama_durumu GROUP BY 1, 2, 3, 4')
combos = cur.fetchall()
for combo in combos:
    print('  durum:', combo)
det_values = {r[0] for r in combos}
check('otomatik_deterministik degerleri', det_values <= {'gecti', 'belirsiz', 'basarisiz', None}, f'{det_values}')
check('otomatik gecti insan onayi sayilmiyor (aktarilmamis kayitlarda insan_durumu NULL)',
      any(r[0] == 'gecti' and r[2] is None for r in combos))
check('insan_onayli yalniz review_status=verified', all((r[3] == 1) == (r[2] == 'verified') for r in combos if r[2] is not None))
if semantik:
    check('semantik karar ayri sutunda gorunuyor', any(r[1] is not None for r in combos))
cur.execute('SELECT COUNT(*), COUNT(DISTINCT satir_hash) FROM v_rag_dogrulama_durumu')
n, distinct = cur.fetchone()
check('v_rag_dogrulama_durumu satir_hash basina bir satir', n == distinct, f'{n}')
cur.execute("SELECT COUNT(*) FROM v_rag_dogrulama_durumu AS d JOIN klinik_bulgular AS b ON b.satir_hash = d.satir_hash "
            "WHERE NOT (d.insan_durumu <=> b.review_status)")
check('insan_durumu = klinik_bulgular.review_status', cur.fetchone()[0] == 0)
if links:
    new = [v['yeni_satir_hash'] for v in links.values()]
    cur.execute(f"SELECT d.satir_hash, d.semantik_karar, d.otomatik_deterministik, d.klinik_bulgu_id, "
                f"JSON_UNQUOTE(JSON_EXTRACT(q.ayrinti, '$.eski_satir_hash')) FROM v_rag_dogrulama_durumu AS d "
                f"JOIN kalite_isaretleri AS q ON q.satir_hash = d.satir_hash AND q.kontrol_kodu = 'rag_sem_ozet' "
                f"WHERE d.satir_hash IN ({', '.join(['%s'] * len(new))})", tuple(new))
    got_links = {r[0]: r[1:] for r in cur.fetchall()}
    want = {v['yeni_satir_hash']: (v['semantik_karar'], old) for old, v in links.items()}
    check('hash eslemesi: eski semantik sonuc yeni hash ile bagli (eski hash ayrintida)',
          set(got_links) == set(want) and all(got_links[h][0] == want[h][0] and got_links[h][3] == want[h][1]
                                              and got_links[h][1] is not None for h in want), f'{got_links}')
    cur.execute(f"SELECT COUNT(*) FROM kalite_isaretleri WHERE kontrol_surumu LIKE 'rag-%' AND satir_hash IN "
                f"({', '.join(['%s'] * len(links))})", tuple(links))
    check('eski hashlerle bagsiz semantik satir kalmadi', cur.fetchone()[0] == 0)

# 5) v_rag_ek_bilgi: SQL JSON cikarimi = Python json cikarimi (aktarilan rag kayitlari)
cur.execute("SELECT id, taslak_notu FROM klinik_bulgular WHERE cikarma_yontemi = 'rag'")
notes = {i: json.loads(n) for i, n in cur.fetchall()}


def get(note, path):
    for part in path.split('.'):
        note = note.get(part) if isinstance(note, dict) else None
    return None if note is None else str(note)


def lod(note):
    return get(note, 'kanit_duzeyi') or get(note, 'tablo_iliskisi.kanit_duzeyi')


SPEC = {'calisma_kimligi': 'kaynak_calisma.etiket', 'calisma_adi': 'kaynak_calisma.ad',
        'alt_grup': 'alt_grup.etiket', 'tedavi_kolu': 'kol.etiket', 'tedavi_kolu_rolu': 'kol.rol',
        'olcu_turu': 'olcu', 'istatistik_turu': 'istatistik', 'oneri_sinifi': 'oneri_sinifi',
        'oneri_metni': 'tablo_iliskisi.oneri_metni', 'tahmin_modeli': 'tahmin_modeli', 'cikarim_modeli': 'model'}
cols = list(SPEC) + ['kanit_duzeyi']
cur.execute(f"SELECT bulgu_id, {', '.join(cols)} FROM v_rag_ek_bilgi")
view = {r[0]: dict(zip(cols, r[1:])) for r in cur.fetchall()}
check('v_rag_ek_bilgi = aktarilan rag kayitlari', set(view) == set(notes), f'{len(view)} / {len(notes)}')
for col in cols:
    want = {i: (lod(n) if col == 'kanit_duzeyi' else get(n, SPEC[col])) for i, n in notes.items()}
    have = {i: (None if v[col] is None else str(v[col])) for i, v in view.items()}
    bad = [i for i in want if want[i] != have.get(i)]
    check(f'v_rag_ek_bilgi.{col} = taslak_notu', not bad, f'dolu {sum(v is not None for v in want.values())}, uyumsuz {len(bad)}')
cur.execute("SELECT COUNT(*) FROM v_rag_ek_bilgi WHERE tahmin_modeli IS NOT NULL AND tahmin_modeli = cikarim_modeli")
check('tahmin_modeli cikarim modelini ezmiyor', cur.fetchone()[0] == 0)
c.rollback(); c.close()
print('\nSONUC:', 'BUTUN KONTROLLER GECTI' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

# =================================================================================================
$saved = @{}
foreach ($n in 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE', 'MYSQL_SUNUM_DATABASE', 'MYSQL_PWD',
               'RAG_OKUR_USER', 'RAG_OKUR_PASSWORD', 'RAG_TASLAKLAR', 'RAG_BEKLENEN_ADAY', 'RAG_AKTARIM_DISI',
               'PYTHONIOENCODING') {
  $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$fingerprint = $null; $usersCreated = $false; $dbsCreated = $false; $draftHashBefore = @{}
$exitCode = 1
try {
  # 0) Ön koşullar ve taslakların salt okunur kontrolü
  $svc = Get-Service MYSQL80 -ErrorAction SilentlyContinue
  Add-Result 'MySQL hizmeti çalışıyor' ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
  foreach ($d in $SOURCES) {
    if (-not (Test-Path -LiteralPath $d)) { Add-Result 'kaynak dosya mevcut' $false $d }
    $draftHashBefore[$d] = (Get-FileHash -LiteralPath $d -Algorithm SHA256).Hash
  }
  if ($DegerlendirmeKlasoru) {
    Add-Result 'değerlendirme klasörü: 6 makale + 6 doğrulama raporu + özet' ($draftHashBefore.Count -ge 13) "$($draftHashBefore.Count) dosya"
  } else {
    Add-Result 'altı taslak mevcut' ($draftHashBefore.Count -eq 6) "$($draftHashBefore.Count) dosya"
  }
  New-Item -ItemType Directory -Force $work | Out-Null

  # 1) Yönetici: test veritabanları ve kullanıcıları (önceden varsa dur; üretim veritabanlarına dokunulmaz)
  $exist = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ('$TEST_SRC','$TEST_SUNUM') UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$TEST_USER','$OKUR_USER')")
  Add-Result 'test veritabanı / kullanıcı önceden yok' (-not $exist) "$exist"
  $hasProc = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='kardiyo_tam_metin' AND ROUTINE_NAME='kaynak_bul'")
  $sql = "CREATE DATABASE ``$TEST_SRC`` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;`nCREATE DATABASE ``$TEST_SUNUM`` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;`n"
  foreach ($h in $HOSTS) {
    $sql += "CREATE USER '$TEST_USER'@'$h' IDENTIFIED BY '$testPwd';`n"
    $sql += "GRANT ALL PRIVILEGES ON ``kardiyo\_rag\_test``.* TO '$TEST_USER'@'$h';`n"
    $sql += "GRANT ALL PRIVILEGES ON ``kardiyo\_rag\_test\_sunum``.* TO '$TEST_USER'@'$h';`n"
    $sql += "CREATE USER '$OKUR_USER'@'$h' IDENTIFIED BY '$okurPwd';`n"
    foreach ($p in 'kardiyo\_tam\_metin', 'kardiyo\_sunum', 'kardiyo\_clinical\_data') {
      $sql += "GRANT SELECT, SHOW VIEW ON ``$p``.* TO '$OKUR_USER'@'$h';`n"
    }
    if ([int]$hasProc -gt 0) { $sql += "GRANT EXECUTE ON PROCEDURE ``kardiyo_tam_metin``.``kaynak_bul`` TO '$OKUR_USER'@'$h';`n" }
  }
  $dbsCreated = $true; $usersCreated = $true
  Invoke-My 'mysql.exe' $adminUser $adminPwd @() $sql | Out-Null      # parolalar komut satırında değil, stdin'de
  Add-Result 'izole test veritabanları ve kullanıcılar oluşturuldu' $true "$TEST_SRC, $TEST_SUNUM"

  # Bundan sonra Python araçları test kullanıcısıyla test veritabanına bağlanır (.env'den öncelikli)
  $env:MYSQL_USER = $TEST_USER; $env:MYSQL_PASSWORD = $testPwd
  $env:MYSQL_DATABASE = $TEST_SRC; $env:MYSQL_SUNUM_DATABASE = $TEST_SUNUM
  $env:RAG_OKUR_USER = $OKUR_USER; $env:RAG_OKUR_PASSWORD = $okurPwd
  $env:RAG_TASLAKLAR = ($DRAFTS | ConvertTo-Json -Compress); $env:PYTHONIOENCODING = 'utf-8'
  $env:RAG_BEKLENEN_ADAY = "$BeklenenAday"; $env:RAG_AKTARIM_DISI = $(if ($AktarimDisi) { $AktarimDisi } else { '' })

  # 2) Üretim parmak izi (önce) — salt okunur kullanıcı
  $fingerprint = "$work\uretim_parmak_izi.json"
  Add-Result 'üretim parmak izi (önce)' ((Invoke-Py $PY_FINGERPRINT @('once', $fingerprint)) -eq 0)

  # 3) Üretimden yalnız temel tablolar — salt okunur kullanıcı; görünümler kopyalanmaz
  $views = Invoke-My 'mysql.exe' $OKUR_USER $okurPwd @('-N', '-B', '-e',
    "SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA='kardiyo_tam_metin'")
  $ignore = @($views | Where-Object { $_ } | ForEach-Object { "--ignore-table=kardiyo_tam_metin.$_" })
  $dump = "$work\kaynak_tablolar.sql"
  Invoke-My 'mysqldump.exe' $OKUR_USER $okurPwd (@('--single-transaction', '--skip-triggers', '--no-tablespaces',
    '--set-gtid-purged=OFF', '--column-statistics=0', '--default-character-set=utf8mb4') + $ignore +
    @("--result-file=$dump", 'kardiyo_tam_metin')) | Out-Null
  $leak = Select-String -LiteralPath $dump -Pattern '^USE |CREATE DATABASE|`kardiyo_(tam_metin|sunum|clinical_data)`' | Select-Object -First 1
  Add-Result 'döküm yalnız tablo içeriyor, üretim adına başvurmuyor' (-not $leak) "$leak"
  Invoke-My 'mysql.exe' $TEST_USER $testPwd @('--default-character-set=utf8mb4', "--database=$TEST_SRC", '-e',
    "SOURCE $((Resolve-Path -LiteralPath $dump).Path -replace '\\','/')") | Out-Null
  Add-Result 'kopya test kullanıcısıyla test veritabanına yüklendi' $true

  Add-Result 'test bağlantısı doğrulandı' ((Invoke-Py "test_conn().close(); print('baglanti: test kullanicisi / kardiyo_rag_test')") -eq 0)

  # 3b) Şema sapması düzeltmesi — YALNIZ test kopyasında, test kullanıcısıyla, SQL kurulumundan önce:
  #     bilgi_turu / baglam VARCHAR -> schema_core.sql'deki ENUM (küme dışı değer / strict mod / satır karşılaştırması)
  Add-Result 'şema düzeltmesi (yalnız test kopyası): bilgi_turu / baglam -> ENUM' ((Invoke-Py $PY_ENUM) -eq 0)

  # 4) Test veritabanında şema / görünüm / prosedür kurulumu (test kullanıcısı)
  foreach ($m in 'src.database --init', 'src.sunum --kur', 'src.sunum_hoca --kur') {
    & $py -m ($m -split ' ')
    Add-Result "kurulum: $m" ($LASTEXITCODE -eq 0)
  }

  # 5) İzolasyon ve yetki doğrulaması
  Add-Result 'izolasyon ve yetki kontrolü' ((Invoke-Py $PY_ISOLATION) -eq 0)

  # 6) Mevcut entegrasyon testleri (kopya üzerinde, RAG aktarımından önce)
  foreach ($tname in 'test_database_schema', 'test_pilot_clinical', 'test_pilot_gosterim', 'test_sunum',
                     'test_sunum_hoca', 'test_data_quality') {
    if ((Invoke-Py "test_conn().close()") -ne 0) { Add-Result "yönlendirme ($tname öncesi)" $false }
    Write-Host "=== evaluation.$tname" -ForegroundColor Cyan
    & $py -m "evaluation.$tname"
    Add-Result "entegrasyon testi: $tname" ($LASTEXITCODE -eq 0)
  }

  # 6b) Yeni hat taslakları: mevcut satırlar izole test kopyasından (yalnız -DegerlendirmeKlasoru)
  if ($DegerlendirmeKlasoru) {
    $manifest = "$work\taslak_manifest.json"
    Add-Result 'yeni hat taslakları (test kopyasına karşı, hepsi pending)' ((Invoke-Py $PY_TASLAK @($DegerlendirmeKlasoru, "$work\taslak", $manifest)) -eq 0)
    $m = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $env:RAG_TASLAKLAR = (@($m.taslaklar) | ConvertTo-Json -Compress); $env:RAG_BEKLENEN_ADAY = "$($m.toplam_aday)"
  }

  # 7) RAG aktarım entegrasyon testi (yalnız önceki bütün adımlar geçtiyse buraya gelinir)
  Add-Result 'RAG aktarım entegrasyon testi' ((Invoke-Py $PY_RAG) -eq 0)

  # 8) Otomatik doğrulama durumları ve yeni görünümler (yalnız -DegerlendirmeKlasoru; yalnız test veritabanı)
  if ($DegerlendirmeKlasoru) {
    $semArg = $(if ($SemantikSonuc) { $SemantikSonuc } else { '-' })
    $mapArg = $(if ($HashEsleme) { $HashEsleme } else { '-' })
    Add-Result 'RAG doğrulama durumu / ek bilgi görünümleri' ((Invoke-Py $PY_DOGRULAMA @($DegerlendirmeKlasoru, $semArg, $mapArg)) -eq 0)
  }
  $exitCode = 0
}
catch {
  Write-Host "DURDURULDU: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
  # Üretim parmak izi (sonra) — her durumda
  if ($fingerprint -and (Test-Path -LiteralPath $fingerprint)) {
    $code = Invoke-Py $PY_FINGERPRINT @('sonra', $fingerprint)
    $results.Add([pscustomobject]@{ Adim = 'üretim parmak izi (sonra)'; Sonuc = $(if ($code -eq 0) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = '' })
    if ($code -ne 0) { $exitCode = 1 }
  }
  # Taslaklar değişmedi mi
  $changed = @($draftHashBefore.Keys | Where-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash -ne $draftHashBefore[$_] })
  if ($draftHashBefore.Count) {
    $results.Add([pscustomobject]@{ Adim = 'kaynak dosyalar değişmedi'; Sonuc = $(if (-not $changed) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "$changed" })
    if ($changed) { $exitCode = 1 }
  }
  # Test kullanıcıları her durumda silinir; test veritabanları yalnız -TestVeritabanlariniSil ile
  try {
    $cleanup = ''
    if ($usersCreated) { foreach ($h in $HOSTS) { $cleanup += "DROP USER IF EXISTS '$TEST_USER'@'$h', '$OKUR_USER'@'$h';`n" } }
    if ($dbsCreated -and $TestVeritabanlariniSil) { $cleanup += "DROP DATABASE IF EXISTS ``$TEST_SRC``;`nDROP DATABASE IF EXISTS ``$TEST_SUNUM``;`n" }
    if ($cleanup) { Invoke-My 'mysql.exe' $adminUser $adminPwd @() $cleanup | Out-Null }
  } catch { Write-Host "Temizlik uyarısı: $($_.Exception.Message)" -ForegroundColor Yellow; $exitCode = 1 }
  # Ortam değişkenleri geri yüklenir
  foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
  $testPwd = $null; $okurPwd = $null; $adminPwd = $null
  Write-Host "`n=== ÖZET" -ForegroundColor Cyan
  $results | Format-Table -AutoSize
  Write-Host $(if ($exitCode -eq 0) { 'SONUÇ: BÜTÜN ADIMLAR GEÇTİ' } else { 'SONUÇ: EN AZ BİR ADIM BAŞARISIZ (yukarıdaki tabloya bakın)' }) `
    -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Red' })
  if (-not $TestVeritabanlariniSil -and $dbsCreated) {
    Write-Host "Test veritabanları inceleme için bırakıldı: $TEST_SRC, $TEST_SUNUM" -ForegroundColor Yellow
    Write-Host "Silmek için: & `"$bin\mysql.exe`" -u $adminUser -p -e `"DROP DATABASE $TEST_SRC; DROP DATABASE $TEST_SUNUM`"" -ForegroundColor Yellow
  }
}
exit $exitCode
