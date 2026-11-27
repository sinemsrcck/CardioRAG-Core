<#
Üretime geçiş — AŞAMA 2: kardiyo_tam_metin.klinik_bulgular.bilgi_turu / baglam  VARCHAR -> ENUM (schema_core.sql).
Hedef YALNIZ bu iki sütun. Diğer üretim ve test veritabanlarına, JSON dosyalarına dokunulmaz.

Çalıştırma (proje kökünde; MySQL hizmeti açık; başka yazan oturum yok):
    # 1) Prova (varsayılan): bütün kontroller, ALTER YOK
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama2_enum.ps1 -Asama1Klasoru backups\uretim_gecisi\20261126_182820
    # 2) Uygulama
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama2_enum.ps1 -Asama1Klasoru backups\uretim_gecisi\20261126_182820 -Uygula
    # Geri alma (ENUM -> Aşama 1'deki VARCHAR tanımları; yalnız satırlar Aşama 1 ile aynıysa)
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama2_enum.ps1 -Asama1Klasoru backups\uretim_gecisi\20261126_182820 -GeriAl

Akış:
  0. Aşama 1 kayıtları: yedek manifesti (dosya, boyut, SHA-256, salt okunur), temizleme raporuyla çapraz kontrol,
     mevcut_durum.json, nesneler_ve_yetkiler.json; JSON dosyalarının SHA-256'sı.
  1. Ön kontrol: Aşama 1 betiğinin PY_DURUM 'sonra' kontrolü (aynı algoritma, aynı dosyadan okunur) üretimin Aşama 1
     kaydıyla BİREBİR aynı olduğunu doğrular (1362 satırın bütün alanları, 1308 verified, 8 görünüm, kaynak_bul,
     şema / nesne / yetki kaydı, test veritabanları). Fark varsa dur (yedek artık güncel değildir).
  2. ENUM adımı (izole entegrasyon testindeki PY_ENUM mekanizması, hedef kardiyo_tam_metin): tanım schema_core.sql'den
     (CORE_COLUMN_MIGRATIONS ve TYPES / CONTEXTS ile tutarlı), strict SQL modu, InnoDB, başka oturum yok, küme dışı değer
     yok (HEX ile birebir), satırlar Aşama 1 özetleriyle aynı; tek ALTER TABLE (iki MODIFY COLUMN); sonra bütün satırların
     bütün alanları, NULL sayıları, sütun listesi, diğer sütunlar ve indeksler önce ile aynı.
  3. Son kontrol (yalnız -Uygula): Aşama 1 kaydıyla karşılaştırma; izin verilen TEK fark iki sütunun tipi
     (SHOW CREATE TABLE'da yalnız bu iki satır, sütun bilgisinde COLUMN_TYPE / harmanlama, klinik_bulgular CHECKSUM'u).
Geri dönüş (DDL örtük commit yapar; InnoDB'de ALTER TABLE atomiktir: hata verirse tablo değişmez):
  - ALTER başarılı ama sonraki kontrol başarısızsa: otomatik geri alma. Aşama 1'deki SHOW CREATE TABLE'dan okunan özgün
    VARCHAR tanımlarıyla tek ALTER (ENUM -> VARCHAR veri kaybettirmez), satır karşılaştırması, ardından Aşama 1
    kaydıyla TAM eşitlik (DDL ve CHECKSUM dahil). Satırlar Aşama 1'den farklıysa otomatik geri alma yapılmaz.
  - Son çare: Aşama 1 yedeği (backups\...\yedek\kardiyo_tam_metin.sql); betik bunu otomatik yüklemez.
Güvenlik: yönetici hesabı yalnız şema okuma, geçici okuma kullanıcısını oluşturma / silme ve tek ALTER için; geçici okuma
kullanıcısı yalnız SELECT, SHOW VIEW (+ kaynak_bul EXECUTE). finally: geçici kullanıcı silinir, JSON dosyaları denetlenir.
Çıkış kodu: 0 = geçti; 1 = başarısız (geri alındıysa da 1).
#>
param(
  [Parameter(Mandatory = $true)][string]$Asama1Klasoru,
  [switch]$Uygula,
  [switch]$GeriAl,
  [int]$BeklenenSatir = 1362,
  [int]$BeklenenVerified = 1308
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$py  = '.venv\Scripts\python.exe'
$bin = 'C:\Program Files\MySQL\MySQL Server 8.0\bin'
$PROD = @('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
$TEST = @('kardiyo_rag_test', 'kardiyo_rag_test_sunum')
$HOSTS = @('localhost', '127.0.0.1')
$A1SCRIPT = 'scripts\uretim_asama1_yedek.ps1'
$stamp = Get-Date -f yyyyMMdd_HHmmss
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 6)
$OKUR_USER = "y2_okur_$suffix"
$mode = if ($GeriAl) { 'gerial' } elseif ($Uygula) { 'uygula' } else { 'prova' }

$dotenv = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $dotenv[$Matches[1]] = $Matches[2].Trim() }
$adminUser = if ($dotenv.MYSQL_USER) { $dotenv.MYSQL_USER } else { 'root' }
$adminPwd  = $dotenv.MYSQL_PASSWORD
$dbHost    = if ($dotenv.MYSQL_HOST) { $dotenv.MYSQL_HOST } else { 'localhost' }
$dbPort    = if ($dotenv.MYSQL_PORT) { $dotenv.MYSQL_PORT } else { '3306' }
$okurPwd = [guid]::NewGuid().ToString('N') + 'Aa1!'

$results = [System.Collections.Generic.List[object]]::new()
function Add-Result([string]$step, [bool]$ok, [string]$info = '') {
  $results.Add([pscustomobject]@{ Adim = $step; Sonuc = $(if ($ok) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = $info })
  Write-Host ("[{0}] {1} {2}" -f $(if ($ok) { 'GECTI' } else { 'HATA ' }), $step, $info) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
  if (-not $ok) { throw "Adım başarısız: $step" }
}
function Add-Info([string]$step, [bool]$ok, [string]$info = '') {
  $results.Add([pscustomobject]@{ Adim = $step; Sonuc = $(if ($ok) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = $info })
  Write-Host ("[{0}] {1} {2}" -f $(if ($ok) { 'GECTI' } else { 'HATA ' }), $step, $info) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
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
  $code | & $py - @arguments | Out-Host
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

# --- Gömülü Python (yalnız ASCII). Aşama 1'in PY_COMMON ve PY_DURUM blokları çalışma anında okunur. -------------
$PY_BAGLANTI = @'
# Asama 2'de geri yukleme kullanicisi YOK. Asama 1 connect() uc hesabin ortam degiskenlerini tek sozlukte birlikte
# okur (Y1_GERI_PWD yoksa KeyError). Bu tanim Asama 1 PY_COMMON'dan hemen sonra gelir ve onu gecersiz kilar:
# yalniz admin / okur, degiskenler gerektiginde okunur; 'geri' istenirse acik hata. Diger davranis ayni.
def connect(kind, db=None, readonly=True):
    if kind == 'admin':
        user, pwd = E['Y1_ADMIN_USER'], E['Y1_ADMIN_PWD']
    elif kind == 'okur':
        user, pwd = E['Y1_OKUR_USER'], E['Y1_OKUR_PWD']
    else:
        raise RuntimeError(f'Asama 2 baglanti turu desteklenmiyor: {kind!r} (geri yukleme kullanicisi yok)')
    c = mysql.connector.connect(host=E['Y1_HOST'], port=int(E['Y1_PORT']), user=user, password=pwd, database=db,
                                charset='utf8mb4', use_unicode=True, autocommit=False)
    if readonly:
        c.start_transaction(readonly=True)
    return c
'@

$PY_TANIM = @'
# Asama 2 ortak: ENUM tanimi (schema_core.sql), sutun / indeks bilgisi, satir anlik goruntusu.
from src.database import CORE_COLUMN_MIGRATIONS
from src.pilot_clinical import TYPES, CONTEXTS
SCHEMA, TABLE = 'kardiyo_tam_metin', 'klinik_bulgular'
QT = f'`{SCHEMA}`.`{TABLE}`'
SETS = {'bilgi_turu': TYPES, 'baglam': CONTEXTS}
A1 = E['Y2_A1']
A1_STATE = json.load(open(os.path.join(A1, 'mevcut_durum.json'), encoding='utf-8'))
A1_META = json.load(open(os.path.join(A1, 'nesneler_ve_yetkiler.json'), encoding='utf-8'))


def norm_def(s):
    return re.sub(r'\(\s+', '(', re.sub(r'\s+', ' ', s)).strip()


def enum_definitions():
    text = open('sql/schema_core.sql', encoding='utf-8').read()
    start = text.index('CREATE TABLE IF NOT EXISTS klinik_bulgular')
    block = text[start:text.find('CREATE TABLE', start + 10)]
    migr = {c: norm_def(d) for c, d in CORE_COLUMN_MIGRATIONS[TABLE]}
    defs, values = {}, {}
    for col, allowed in SETS.items():
        m = re.search(r'\n\s*' + col + r"\s+(ENUM\([^)]*\)\s+NULL)\s*,", block)
        if not m:
            ok(f'{col}: schema_core.sql ENUM tanimi bulundu', False)
            continue
        defs[col] = norm_def(m.group(1))
        values[col] = re.findall(r"'([^']*)'", defs[col])
        ok(f'{col}: schema_core.sql = CORE_COLUMN_MIGRATIONS', defs[col] == migr.get(col), defs[col])
        ok(f'{col}: ENUM kumesi = pilot_clinical kumesi', len(values[col]) == len(set(values[col]))
           and set(values[col]) == set(allowed))
    stop_if_failed('ENUM tanimi')
    want = {c: 'enum(' + ','.join(f"'{v}'" for v in values[c]) + ')' for c in SETS}
    return defs, values, want


def original_definitions():
    """Asama 1 SHOW CREATE TABLE'daki ozgun iki sutun tanimi (geri alma icin)."""
    ddl = A1_META['veritabanlari'][SCHEMA]['tablolar'][TABLE]
    out = {}
    for col in SETS:
        m = re.search(r'^\s*`' + col + r'`\s+(.+?),?\s*$', ddl, re.M)
        d = m.group(1).rstrip(',').strip() if m else ''
        good = re.fullmatch(r'(varchar|char)\(\d+\)( CHARACTER SET \w+)?( COLLATE \w+)? DEFAULT NULL', d) is not None
        ok(f'{col}: Asama 1 ozgun tanimi okundu (VARCHAR NULL)', good, d)
        out[col] = d
    stop_if_failed('ozgun tanim')
    return out


COLQ = ('SELECT COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, '
        'CHARACTER_SET_NAME, COLLATION_NAME, EXTRA, COLUMN_KEY, COLUMN_COMMENT FROM information_schema.COLUMNS '
        'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION')
IDXQ = ('SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS '
        'WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY 1, 2')


def table_meta(cur):
    cur.execute(COLQ, (SCHEMA, TABLE))
    cols = {txt(r[0]): tuple(txt(x) for x in r[1:]) for r in cur.fetchall()}
    cur.execute(IDXQ, (SCHEMA, TABLE))
    return cols, [tuple(txt(x) for x in r) for r in cur.fetchall()]


def val(v):
    if isinstance(v, (bytes, bytearray)):
        try:
            return bytes(v).decode('utf-8')
        except UnicodeDecodeError:
            return 'hex:' + bytes(v).hex()
    return v


def snapshot(conn, cur):
    """Butun satirlarin butun alanlari (NULL = None), sutun bazinda NULL sayilari, Asama 1 satir ozetleri."""
    cur.execute(f'SELECT * FROM {QT} ORDER BY id')
    names = [d[0] for d in cur.description]
    rows, nulls, a1 = {}, Counter(), {}
    for r in cur.fetchall():
        rec = dict(zip(names, (val(v) for v in r)))
        if rec['id'] in rows:
            ok(f'yinelenen id {rec["id"]}', False)
        rows[rec['id']] = json.dumps(rec, default=str, ensure_ascii=False, sort_keys=True)
        nulls.update(k for k, v in rec.items() if v is None)
        a1[str(rec['id'])] = row_hash(r)
    conn.rollback()
    return names, rows, nulls, a1


def admin_session(conn, cur):
    cur.execute('SET SESSION lock_wait_timeout = 30')
    cur.execute('SELECT DATABASE(), CURRENT_USER(), @@SESSION.sql_mode')
    db, user, smode = map(txt, cur.fetchone())
    ok('yonetici baglantisi kardiyo_tam_metin', db == SCHEMA and str(user).startswith(E['Y1_ADMIN_USER'] + '@'), f'{db} / {user}')
    ok('strict SQL modu etkin', bool({'STRICT_TRANS_TABLES', 'STRICT_ALL_TABLES'} & set(str(smode).split(','))), smode)
    cur.execute('SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s', (SCHEMA, TABLE))
    eng = cur.fetchone()
    ok(f'{TABLE} InnoDB', bool(eng) and str(txt(eng[0])).lower() == 'innodb', eng)
    cur.execute("SELECT ID, USER, HOST, DB, COMMAND FROM information_schema.PROCESSLIST WHERE ID <> CONNECTION_ID() "
                "AND DB IN (%s, %s, %s) AND USER NOT IN ('event_scheduler', 'system user')", PROD)
    others = [tuple(map(txt, r)) for r in cur.fetchall()]
    ok('uretim veritabanlarinda baska oturum yok', not others, others[:5])
    conn.rollback()
    stop_if_failed('yonetici oturumu')


def rows_equal_asama1(a1rows, label):
    want = A1_STATE['klinik_bulgular']['satir_ozetleri']
    diff = [i for i in set(want) | set(a1rows) if want.get(i) != a1rows.get(i)]
    ok(f'{label}: butun satirlarin butun alanlari Asama 1 ile ayni', not diff and len(a1rows) == len(want),
       f'{len(a1rows)} satir; farkli {len(diff)} ornek {sorted(diff)[:5]}')
'@

$PY_ENUM2 = @'
# Uretim ENUM donusumu (izole testteki PY_ENUM mekanizmasi). argv: prova | uygula, cikti klasoru
mode, out = sys.argv[1], sys.argv[2]
DEFS, VALUES, WANT = enum_definitions()
c = connect('admin', SCHEMA, readonly=False)
cur = c.cursor()
admin_session(c, cur)
cols0, idx0 = table_meta(cur)
todo = []
for col in SETS:
    info = cols0.get(col)
    if not info:
        ok(f'{col} sutunu var', False)
        continue
    dtype, ctype, nullable = info[1], info[2], info[3]
    print(f'once  {col}: {ctype} / NULL {nullable} / {info[6]}')
    if ctype == WANT[col] and nullable == 'YES':
        ok(f'{col}: beklenmeyen durum - zaten ENUM (Asama 1 kaydi VARCHAR idi)', False)
    elif dtype in ('varchar', 'char') and nullable == 'YES':
        todo.append(col)
    else:
        ok(f'{col}: beklenmeyen kaynak tipi', False, f'{ctype} / NULL {nullable}')
ok('donusturulecek sutunlar tam olarak bilgi_turu ve baglam', todo == list(SETS), todo)
stop_if_failed('kaynak tipler')
# Kume disi deger (HEX ile birebir: buyuk / kucuk harf ve sondaki bosluk da yakalanir)
for col in todo:
    cur.execute(f'SELECT HEX(`{col}`), COUNT(*) FROM {QT} WHERE `{col}` IS NOT NULL GROUP BY HEX(`{col}`)')
    dist = {bytes.fromhex(txt(h)).decode('utf-8'): n for h, n in cur.fetchall()}
    outside = {v: n for v, n in dist.items() if v not in VALUES[col]}
    print(f'deger dagilimi {col}:', dict(sorted(dist.items())))
    ok(f'{col}: kume disi deger yok', not outside, outside)
c.rollback()
stop_if_failed('kume disi deger; donusum yapilmadi')
names0, rows0, nulls0, a10 = snapshot(c, cur)
rows_equal_asama1(a10, 'ALTER oncesi')
verified0 = sum(1 for i, r in A1_STATE['klinik_bulgular']['verified_pilot'].items() if a10.get(i) == r[1])
ok('ALTER oncesi: verified pilot kayitlari Asama 1 ile ayni',
   verified0 == len(A1_STATE['klinik_bulgular']['verified_pilot']) == int(E['Y1_BEKLENEN_VERIFIED']), verified0)
print(f'once: {len(rows0)} satir, {len(names0)} sutun, NULL bilgi_turu {nulls0["bilgi_turu"]}, baglam {nulls0["baglam"]}')
stop_if_failed('ALTER oncesi satir kontrolu')
stmt = f'ALTER TABLE {QT} ' + ', '.join(f'MODIFY COLUMN `{col}` {DEFS[col]}' for col in todo)
if mode == 'prova':
    write_new(os.path.join(out, 'enum_prova.json'), {'ifade': stmt, 'satir': len(rows0), 'null': dict(nulls0)})
    print('PROVA: ALTER CALISTIRILMADI. Uygulamada calisacak tek ifade:\n  ' + stmt)
    c.close()
    sys.exit(0)
cur.execute('SELECT DATABASE(), CURRENT_USER()')
d, u = map(txt, cur.fetchone())
if d != SCHEMA or not str(u).startswith(E['Y1_ADMIN_USER'] + '@'):
    ok('ALTER oncesi baglanti', False, f'{d} / {u}')
    stop_if_failed('baglanti')
print('calistiriliyor:', stmt)
try:
    cur.execute(stmt)                    # ortuk commit; InnoDB atomik DDL: hata verirse tablo degismez
except mysql.connector.Error as exc:
    ok('ALTER', False, f'hata (tablo degismedi): {exc}')
    stop_if_failed('ALTER')
write_new(os.path.join(out, 'enum_alter_yapildi.json'), {'ifade': stmt})
cols1, idx1 = table_meta(cur)
names1, rows1, nulls1, a11 = snapshot(c, cur)
ok('sutun listesi ve sirasi ayni', names1 == names0)
ok('satir sayisi ayni', len(rows1) == len(rows0), f'{len(rows0)} -> {len(rows1)}')
diff_rows = [i for i in set(rows0) | set(rows1) if rows0.get(i) != rows1.get(i)]
ok('butun satirlarin butun alanlari ayni', not diff_rows, f'{len(diff_rows)} farkli, ornek {sorted(diff_rows)[:5]}')
ok('NULL sayilari ayni', nulls1 == nulls0)
ok('indeksler ayni', idx1 == idx0)
for name in set(cols0) | set(cols1):
    a, b = cols0.get(name), cols1.get(name)
    if name in todo:
        good = bool(b) and b[1] == 'enum' and b[2] == WANT[name] and b[3] == 'YES' and b[0] == a[0] and b[4] is None
        ok(f'{name}: ENUM, ayni konum, NULL olabilir, varsayilan NULL', good, f'{a[2]} -> {b[2] if b else None}; '
           f'harmanlama {a[6]} -> {b[6] if b else None}')
    elif a != b:
        ok(f'diger sutun degismedi: {name}', False, f'{a} -> {b}')
c.close()
write_new(os.path.join(out, 'enum_sonucu.json'), {'ifade': stmt, 'satir': len(rows1), 'null': dict(nulls1),
                                                 'once': {k: cols0[k] for k in todo}, 'sonra': {k: cols1[k] for k in todo}})
print('\nSONUC:', 'ENUM DONUSUMU TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_SONRA = @'
# Son kontrol: Asama 1 kaydiyla karsilastirma; izin verilen TEK fark iki sutunun tipi.
OUT = E['Y2_WORK']
DEFS, VALUES, WANT = enum_definitions()
before, bmeta = json.load(open(STATE, encoding='utf-8')), json.load(open(META, encoding='utf-8'))
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
state, meta = json.loads(json.dumps(state, default=str)), json.loads(json.dumps(meta, default=str))
write_new(os.path.join(OUT, 'sonra_durum.json'), state)
write_new(os.path.join(OUT, 'sonra_nesneler_ve_yetkiler.json'), meta)
kb0, kb1 = before['klinik_bulgular'], state['klinik_bulgular']
changed = [i for i in set(kb0['satir_ozetleri']) | set(kb1['satir_ozetleri'])
           if kb0['satir_ozetleri'].get(i) != kb1['satir_ozetleri'].get(i)]
ok('klinik_bulgular butun satirlarin butun alanlari Asama 1 ile ayni', not changed and kb1['satir_sayisi'] == kb0['satir_sayisi'],
   f"{kb1['satir_sayisi']} satir; farkli {len(changed)}")
ok('verified pilot kayitlari Asama 1 ile ayni', kb0['verified_pilot'] == kb1['verified_pilot'], kb1['verified_pilot_sayisi'])
ok('pilot durumu (sayi, id araligi, parmak izi) ayni', kb0['pilot_durumu_sql'] == kb1['pilot_durumu_sql'])
ok('review_status dagilimi ayni', kb0['review_status'] == kb1['review_status'], kb1['review_status'])
c0, c1 = {r[0]: r for r in kb0['sutunlar']}, {r[0]: r for r in kb1['sutunlar']}
ok('sutun listesi ve sirasi ayni', [r[0] for r in kb0['sutunlar']] == [r[0] for r in kb1['sutunlar']])
other = []
for name, a in c0.items():
    b = c1.get(name)
    if name in SETS:
        good = bool(b) and b[1] == a[1] and b[2] == WANT[name] and b[3] == 'YES' and b[4] is None
        ok(f'{name}: ENUM (schema_core.sql), ayni konum, NULL olabilir', good,
           f'{a[2]} -> {b[2] if b else None}; harmanlama {a[5]} -> {b[5] if b else None}')
    elif a != b:
        other.append((name, a, b))
ok('diger sutunlar ayni', not other, other[:5])
fp0, fp1 = before['uretim_parmak_izi'], state['uretim_parmak_izi']
t0 = fp0['kardiyo_tam_metin']['tablolar'].pop(TABLE, None)
t1 = fp1['kardiyo_tam_metin']['tablolar'].pop(TABLE, None)
ok('klinik_bulgular satir sayisi (parmak izi) ayni', bool(t0) and bool(t1) and t0.split('/')[1] == t1.split('/')[1], f'{t0} -> {t1}')
ok('diger uretim tablolari, gorunum tanimlari ve rutinler ayni', fp0 == fp1)
for key in ('test_parmak_izi', 'sunum_gorunumleri', 'kaynak_bul'):
    ok(f'{key} Asama 1 ile ayni', before[key] == state[key])
d0 = bmeta['veritabanlari'][SCHEMA]['tablolar'].pop(TABLE)
d1 = meta['veritabanlari'][SCHEMA]['tablolar'].pop(TABLE)
l0, l1 = [x.strip() for x in d0.splitlines()], [x.strip() for x in d1.splitlines()]
target = lambda line: any(line.startswith(f'`{c}` ') for c in SETS)
ok('klinik_bulgular DDL: iki sutun satiri disinda ayni', [x for x in l0 if not target(x)] == [x for x in l1 if not target(x)])
for col in SETS:
    line = next((x for x in l1 if x.startswith(f'`{col}` ')), '')
    ok(f'DDL {col}: ENUM (schema_core.sql), DEFAULT NULL', line.startswith(f'`{col}` {WANT[col]}') and 'DEFAULT NULL' in line, line)
for key in ('sunucu', 'veritabanlari', 'kullanicilar', 'procs_priv', 'tables_priv', 'sahipler'):
    ok(f'sema / nesne / yetki kaydi Asama 1 ile ayni: {key}', bmeta[key] == meta[key])
print('\nSONUC:', 'SON KONTROL TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_GERIAL = @'
# Geri alma: ENUM -> Asama 1'deki ozgun VARCHAR tanimlari. Yalniz satirlar Asama 1 ile birebir ayniysa. argv: cikti klasoru
out = sys.argv[1]
DEFS, VALUES, WANT = enum_definitions()
ORIG = original_definitions()
c = connect('admin', SCHEMA, readonly=False)
cur = c.cursor()
admin_session(c, cur)
cols0, idx0 = table_meta(cur)
for col in SETS:
    info = cols0.get(col)
    ok(f'{col}: su an ENUM (geri alinacak)', bool(info) and info[2] == WANT[col], info[2] if info else None)
stop_if_failed('geri alinacak durum degil; hicbir sey yapilmadi')
names0, rows0, nulls0, a10 = snapshot(c, cur)
rows_equal_asama1(a10, 'geri alma oncesi')
stop_if_failed('satirlar Asama 1 ile ayni degil; otomatik geri alma yapilmadi (Asama 1 yedeginden geri yukleme gerekir)')
stmt = f'ALTER TABLE {QT} ' + ', '.join(f'MODIFY COLUMN `{col}` {ORIG[col]}' for col in SETS)
print('calistiriliyor:', stmt)
try:
    cur.execute(stmt)
except mysql.connector.Error as exc:
    ok('geri alma ALTER', False, f'hata (tablo degismedi): {exc}')
    stop_if_failed('geri alma ALTER')
write_new(os.path.join(out, 'geri_alma_yapildi.json'), {'ifade': stmt})
cols1, idx1 = table_meta(cur)
names1, rows1, nulls1, a11 = snapshot(c, cur)
ok('geri alma: sutun listesi ayni', names1 == names0)
ok('geri alma: butun satirlarin butun alanlari ayni', rows1 == rows0, f'{len(rows1)} satir')
ok('geri alma: NULL sayilari ayni', nulls1 == nulls0)
ok('geri alma: indeksler ayni', idx1 == idx0)
for name in set(cols0) | set(cols1):
    if name not in SETS and cols0.get(name) != cols1.get(name):
        ok(f'geri alma: diger sutun degismedi: {name}', False)
for col in SETS:
    ok(f'geri alma: {col} ozgun tipe dondu', cols1[col][1] in ('varchar', 'char') and ORIG[col].startswith(cols1[col][2]),
       f'{cols1[col][2]} / {ORIG[col]}')
c.close()
print('\nSONUC:', 'GERI ALMA ALTER TAMAM (Asama 1 ile tam karsilastirma ayrica yapilir)' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

# =================================================================================================
$saved = @{}
foreach ($n in 'MYSQL_PWD', 'Y1_HOST', 'Y1_PORT', 'Y1_ADMIN_USER', 'Y1_ADMIN_PWD', 'Y1_OKUR_USER', 'Y1_OKUR_PWD',
               'Y1_GERI_USER', 'Y1_GERI_PWD', 'Y1_GERI_DBS', 'Y1_BEKLENEN_SATIR', 'Y1_BEKLENEN_VERIFIED', 'Y2_A1', 'Y2_WORK',
               'PYTHONIOENCODING') {
  $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$usersCreated = $false; $jsonBefore = $null; $work = $null
$exitCode = 1
try {
  [Environment]::SetEnvironmentVariable('Y1_GERI_PWD', $null, 'Process')
  if ($Uygula -and $GeriAl) { Add-Result 'parametreler' $false '-Uygula ve -GeriAl birlikte kullanılamaz' }
  Write-Host "MOD: $mode" -ForegroundColor Cyan

  # 0) Aşama 1 kayıtları ve ön koşullar
  $svc = Get-Service MYSQL80 -ErrorAction SilentlyContinue
  Add-Result 'MySQL hizmeti çalışıyor' ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
  $a1 = (Resolve-Path -LiteralPath $Asama1Klasoru).Path
  foreach ($f in 'yedek\manifest.json', 'mevcut_durum.json', 'nesneler_ve_yetkiler.json', 'geri_yukleme\temizleme_raporu.json') {
    if (-not (Test-Path -LiteralPath (Join-Path $a1 $f))) { Add-Result 'Aşama 1 kaydı mevcut' $false $f }
  }
  $manifest = Get-Content -LiteralPath (Join-Path $a1 'yedek\manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $report = Get-Content -LiteralPath (Join-Path $a1 'geri_yukleme\temizleme_raporu.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($db in $PROD) {
    $m = $manifest.$db
    if (-not $m) { Add-Result "manifest: $db" $false 'kayıt yok' }
    $file = Join-Path $a1 "yedek\$($m.dosya)"
    if (-not (Test-Path -LiteralPath $file)) { Add-Result "yedek dosyası: $db" $false $file }
    $item = Get-Item -LiteralPath $file
    $sha = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $okm = ($item.Length -eq [int64]$m.boyut) -and ($sha -eq $m.sha256) -and $item.IsReadOnly -and
           ($report.$db.kaynak_sha256 -and ($report.$db.kaynak_sha256.ToUpper() -eq $sha))
    Add-Result "yedek doğrulandı: $db (boyut, SHA-256, salt okunur, temizleme raporu)" $okm "$($item.Length) bayt"
  }
  if (-not (Test-Path -LiteralPath $A1SCRIPT)) { Add-Result 'Aşama 1 betiği' $false $A1SCRIPT }
  $a1raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $A1SCRIPT).Path)
  $A1_COMMON = [regex]::Match($a1raw, "(?s)\`$PY_COMMON = @'\r?\n(.*?)\r?\n'@").Groups[1].Value
  $A1_DURUM  = [regex]::Match($a1raw, "(?s)\`$PY_DURUM = @'\r?\n(.*?)\r?\n'@").Groups[1].Value
  $marker = "`no = connect('okur')"
  $hits = ([regex]::Matches($A1_DURUM, [regex]::Escape($marker))).Count
  Add-Result "Aşama 1 PY_COMMON / PY_DURUM okundu (SHA-256 $((Get-FileHash -LiteralPath $A1SCRIPT -Algorithm SHA256).Hash.Substring(0, 12)))" `
    ($A1_COMMON -and $A1_DURUM -and $hits -eq 1) "işaret $hits"
  $A1_DEFS = $A1_DURUM.Substring(0, $A1_DURUM.IndexOf($marker))
  $A1_COMMON = $A1_COMMON + "`n" + $PY_BAGLANTI     # bütün modlarda Aşama 1 connect() yerine admin / okur bağlantısı
  $jsonBefore = Get-JsonHashes
  Add-Result 'JSON dosyalarının SHA-256 kaydı' ($jsonBefore.Count -gt 0) "$($jsonBefore.Count) dosya"
  $work = "$a1`_asama2_${mode}_$stamp"
  Add-Result 'çalışma klasörü yeni' (-not (Test-Path -LiteralPath $work)) $work
  New-Item -ItemType Directory -Path $work | Out-Null

  # 1) Geçici okuma kullanıcısı (Aşama 1 ile aynı yetkiler)
  $exist = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "SELECT CONCAT(User,'@',Host) FROM mysql.user WHERE User = '$OKUR_USER'")
  Add-Result 'geçici kullanıcı önceden yok' (-not $exist) "$exist"
  $hasProc = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='kardiyo_tam_metin' AND ROUTINE_NAME='kaynak_bul' AND ROUTINE_TYPE='PROCEDURE'")
  $sql = ''
  foreach ($h in $HOSTS) {
    $sql += "CREATE USER '$OKUR_USER'@'$h' IDENTIFIED BY '$okurPwd';`n"
    foreach ($p in $PROD + $TEST) { $sql += "GRANT SELECT, SHOW VIEW ON ``$(Esc $p)``.* TO '$OKUR_USER'@'$h';`n" }
    if ([int]$hasProc -gt 0) { $sql += "GRANT EXECUTE ON PROCEDURE ``kardiyo_tam_metin``.``kaynak_bul`` TO '$OKUR_USER'@'$h';`n" }
  }
  $usersCreated = $true
  Invoke-My 'mysql.exe' $adminUser $adminPwd @() $sql | Out-Null
  $sql = $null
  Add-Result 'geçici okuma kullanıcısı oluşturuldu' $true $OKUR_USER

  $env:Y1_HOST = $dbHost; $env:Y1_PORT = $dbPort
  $env:Y1_ADMIN_USER = $adminUser; $env:Y1_ADMIN_PWD = $adminPwd
  $env:Y1_OKUR_USER = $OKUR_USER; $env:Y1_OKUR_PWD = $okurPwd
  # Bu aşamada geri yükleme kullanıcısı yok: Aşama 1 PY_COMMON yalnız geçici kullanıcı adları (yetki karşılaştırmasında
  # hariç tutulur) ve geri yükleme veritabanı listesi için okur; parola tanımlanmaz (connect: PY_BAGLANTI).
  $env:Y1_GERI_USER = $OKUR_USER; $env:Y1_GERI_DBS = '{}'
  $env:Y1_BEKLENEN_SATIR = "$BeklenenSatir"; $env:Y1_BEKLENEN_VERIFIED = "$BeklenenVerified"
  $env:Y2_A1 = $a1; $env:Y2_WORK = $work; $env:PYTHONIOENCODING = 'utf-8'
  $FULL_CHECK = $A1_COMMON + "`n" + $A1_DURUM
  $TANIM = $A1_COMMON + "`n" + $PY_TANIM + "`n"

  if ($mode -eq 'gerial') {
    # Geri alma: satırlar Aşama 1 ile aynıysa özgün VARCHAR tanımları; sonra Aşama 1 ile TAM eşitlik
    Add-Result 'geri alma ALTER (ENUM -> Aşama 1 VARCHAR)' ((Invoke-Py ($TANIM + $PY_GERIAL) @($work)) -eq 0)
    Add-Result 'üretim Aşama 1 kaydıyla birebir aynı (DDL ve CHECKSUM dahil)' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)
    $exitCode = 0
  } else {
    # 2) Ön kontrol: üretim Aşama 1 kaydıyla BİREBİR aynı (yedek güncel)
    Add-Result 'ön kontrol: üretim Aşama 1 kaydıyla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)
    # 3) ENUM adımı (prova: ALTER yok)
    $enumCode = Invoke-Py ($TANIM + $PY_ENUM2) @($mode, $work)
    $altered = Test-Path -LiteralPath (Join-Path $work 'enum_alter_yapildi.json')
    $postCode = 0
    if ($mode -eq 'uygula' -and $altered -and $enumCode -eq 0) {
      # 4) Son kontrol: Aşama 1 ile karşılaştırma, izin verilen tek fark iki sütunun tipi
      $postCode = Invoke-Py ($A1_COMMON + "`n" + $A1_DEFS + "`n" + $PY_TANIM + "`n" + $PY_SONRA) @('sonra', $a1)
    }
    if ($altered -and ($enumCode -ne 0 -or $postCode -ne 0)) {
      Add-Info 'ENUM sonrası kontrol' $false 'beklenmeyen fark; otomatik geri alma başlıyor'
      $g1 = Invoke-Py ($TANIM + $PY_GERIAL) @($work)
      $g2 = if ($g1 -eq 0) { Invoke-Py $FULL_CHECK @('sonra', $a1) } else { 1 }
      Add-Info 'otomatik geri alma ve Aşama 1 ile tam eşitlik' ($g1 -eq 0 -and $g2 -eq 0) $(if ($g1 -eq 0 -and $g2 -eq 0) { 'üretim Aşama 1 durumuna döndü' } else { "GERİ ALINAMADI: Aşama 1 yedeğinden geri yükleme gerekir ($a1\yedek\kardiyo_tam_metin.sql)" })
      throw 'ENUM dönüşümü geri alındı ya da geri alma gerekli'
    }
    Add-Result $(if ($mode -eq 'prova') { 'ENUM prova (ALTER yok)' } else { 'ENUM dönüşümü ve son kontrol' }) ($enumCode -eq 0 -and $postCode -eq 0)
    $exitCode = 0
  }
}
catch {
  Write-Host "DURDURULDU: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
  if ($jsonBefore) {
    $jsonAfter = Get-JsonHashes
    $changed = @($jsonBefore.Keys | Where-Object { $jsonAfter[$_] -ne $jsonBefore[$_] }) + @($jsonAfter.Keys | Where-Object { -not $jsonBefore.ContainsKey($_) })
    $results.Add([pscustomobject]@{ Adim = 'JSON dosyaları değişmedi'; Sonuc = $(if (-not $changed) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "$($changed | Select-Object -First 5)" })
    if ($changed) { $exitCode = 1 }
  }
  if ($usersCreated) {
    try {
      $drop = ($HOSTS | ForEach-Object { "DROP USER IF EXISTS '$OKUR_USER'@'$_';" }) -join "`n"
      Invoke-My 'mysql.exe' $adminUser $adminPwd @() $drop | Out-Null
      $left = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "SELECT COUNT(*) FROM mysql.user WHERE User = '$OKUR_USER'")
      $results.Add([pscustomobject]@{ Adim = 'geçici kullanıcı silindi'; Sonuc = $(if ([int]$left -eq 0) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "kalan $left" })
      if ([int]$left -ne 0) { $exitCode = 1 }
    } catch { Write-Host "TEMİZLİK UYARISI: $($_.Exception.Message)" -ForegroundColor Red; $exitCode = 1 }
  }
  foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
  $okurPwd = $null; $adminPwd = $null
  Write-Host "`n=== ÖZET ($mode)" -ForegroundColor Cyan
  $results | Format-Table -AutoSize
  Write-Host $(if ($exitCode -eq 0) { "SONUÇ: TAMAM ($mode). Kayıtlar: $work" } else { 'SONUÇ: BAŞARISIZ (yukarıdaki tabloya bakın)' }) `
    -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Red' })
}
exit $exitCode
