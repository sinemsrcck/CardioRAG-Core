<#
Üretime geçiş — AŞAMA 3: güncellenen SQL görünümleri ve kaynak_bul prosedürünün üretime kurulması.

Çalıştırma (proje kökünde; MySQL hizmeti açık; üretimde başka yazan oturum yok):
    # 1) Prova (varsayılan): kontroller, yan etki tahmini ve özgün tanımların kaydı; KURULUM YOK
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama3_sunum.ps1 -Asama1Klasoru <A1> -Asama2Klasoru <A2>
    # 2) Uygulama
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama3_sunum.ps1 -Asama1Klasoru <A1> -Asama2Klasoru <A2> -Uygula
    # Geri alma (bir uygulama klasöründeki özgün tanımlarla)
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama3_sunum.ps1 -Asama1Klasoru <A1> -Asama2Klasoru <A2> -GeriAlKlasoru <A3 uygulama klasörü>

Kurulum yan etkileri (kaynak koddan):
  - python -m src.sunum --kur: kalite_core.sql (kalite_isaretleri / kalite_calistirmalari CREATE TABLE IF NOT EXISTS,
    v_kalite_ozet / v_kalite_bulgu CREATE OR REPLACE VIEW), sunum_core.sql (cikarim_reddedilenler / hata_onlem
    CREATE TABLE IF NOT EXISTS, 10 görünüm CREATE OR REPLACE VIEW), DROP + CREATE PROCEDURE kaynak_bul (prosedüre
    verilmiş EXECUTE yetkileri düşebilir; DEFINER = kurulumu yapan hesap), hata_onlem: eşleme sürümü farklı satırlar
    silinir, ONLEM_ESLEME satırları yazılır (ON DUPLICATE KEY UPDATE).
  - python -m src.sunum_hoca --kur: CREATE DATABASE IF NOT EXISTS kardiyo_sunum, kaynakta v_sunum_taban ve sunumda
    sekiz görünüm CREATE OR REPLACE VIEW, sunumda DROP VIEW IF EXISTS _findings_base; measurements grup sütunu sayısı
    veriden hesaplanır.
  - Kalite tablolarına (kalite_isaretleri / kalite_calistirmalari) ve cikarim_reddedilenler'e veri YAZILMAZ.
  - python -m src.database --init: KULLANILMAZ. Gerekli olup olmadığı salt okunur denetlenir (tablo / sütun / NULL /
    genişlik / kısıt / makaleler sütunları / v_tani_tedavi, v_sonuclar); gerekliyse betik durur.

Güvenlik ve doğrulama:
  - Aşama 1 yedekleri (manifest, SHA-256, salt okunur) ve Aşama 2 sonrası kayıt; ön kontrol: üretim Aşama 2 sonrası
    kayıtla BİREBİR aynı (Aşama 1'in PY_DURUM kontrolü, aynı dosyadan).
  - Prova: kurulum SQL'inde yalnız CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE VIEW / DROP VIEW IF EXISTS; değişecek
    bütün görünümlerin ve kaynak_bul'un sahibi kurulumu yapacak yönetici hesabı (DEFINER korunur; geçici kullanıcı
    sahip olmaz); hata_onlem değişmeyecek (aksi hâlde dur); measurements grup sütunu sayısı değişmeyecek; --init
    gerekmiyor; HEAD ile çalışma kopyası farkı yalnız izole testte doğrulanan nesnelerde. Özgün tanımlar
    (SHOW CREATE VIEW / SHOW CREATE PROCEDURE), EXECUTE yetkileri ve hata_onlem satırları
    geri_alma\ozgun_nesneler.json'a yazılır.
  - Uygulama sonrası: 1362 satırın bütün alanları, 1308 verified, bütün tabloların CHECKSUM'u (hata_onlem, kalite
    tabloları dahil), sekiz klinik görünümün içeriği, şema / yetki kaydı Aşama 2 sonrası ile aynı; tanımı değişebilen
    yalnız izole testte doğrulanan v_bulgu_kaynak, v_sunum_taban ve kaynak_bul (DEFINER, SQL SECURITY, sql_mode aynı);
    kaynak_bul altı terimde eski sonuçlar kaybolmaz, tek parçalı sonuçlar aynı, ek eşleşmeler yalnız 2. / 3. kanıt
    parçasından (yapay yok), toplam ek eşleşme = -BeklenenEkEslesme.
  - Herhangi bir hata ya da fark: otomatik geri alma (özgün görünüm tanımları SHOW CREATE VIEW'dan, kaynak_bul
    SHOW CREATE PROCEDURE metninden ve özgün sql_mode ile, EXECUTE yetkileri, gerekirse hata_onlem satırları),
    ardından Aşama 2 sonrası kayıtla TAM eşitlik (görünüm ve rutin tanımları birebir).
  - Test veritabanlarına yalnız okuma; JSON dosyaları denetlenir; geçici okuma kullanıcısı finally'de silinir.
Çıkış kodu: 0 = geçti; 1 = başarısız (geri alındıysa da 1).
#>
param(
  [Parameter(Mandatory = $true)][string]$Asama1Klasoru,
  [Parameter(Mandatory = $true)][string]$Asama2Klasoru,
  [switch]$Uygula,
  [string]$GeriAlKlasoru,
  [int]$BeklenenSatir = 1362,
  [int]$BeklenenVerified = 1308,
  [int]$BeklenenEkEslesme = 20
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$py  = '.venv\Scripts\python.exe'
$bin = 'C:\Program Files\MySQL\MySQL Server 8.0\bin'
$PROD = @('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
$TEST = @('kardiyo_rag_test', 'kardiyo_rag_test_sunum')
$HOSTS = @('localhost', '127.0.0.1')
$A1SCRIPT = 'scripts\uretim_asama1_yedek.ps1'
$A2SCRIPT = 'scripts\uretim_asama2_enum.ps1'
$stamp = Get-Date -f yyyyMMdd_HHmmss
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 6)
$OKUR_USER = "y3_okur_$suffix"
$mode = if ($GeriAlKlasoru) { 'gerial' } elseif ($Uygula) { 'uygula' } else { 'prova' }

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
function Get-Block([string]$text, [string]$name) { [regex]::Match($text, "(?s)\`$$name = @'\r?\n(.*?)\r?\n'@").Groups[1].Value }
function Save-Head([string]$gitPath, [string]$target) {
  $content = (git show "HEAD:$gitPath") -join "`n"
  if ($LASTEXITCODE -ne 0) { throw "git show HEAD:$gitPath başarısız" }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
  [IO.File]::WriteAllText($target, $content + "`n", [System.Text.UTF8Encoding]::new($false))
}

# --- Gömülü Python (yalnız ASCII). Aşama 1 PY_COMMON / PY_DURUM ve Aşama 2 PY_BAGLANTI çalışma anında okunur. --------
$PY_A3_ORTAK = @'
# Asama 3 ortak: kurulum SQL'inin cozumlenmesi, prosedur govdesi karsilastirmasi, ozgun kayit dosyasi.
from src.database import _split_sql_statements
SRC, SUN = 'kardiyo_tam_metin', 'kardiyo_sunum'
KAYNAK = E['Y3_KAYNAK']                       # ozgun tanimlar ve taban kaydi bu klasorde
ORIG_PATH = os.path.join(KAYNAK, 'geri_alma', 'ozgun_nesneler.json')
VIEW_RE = re.compile(r'^CREATE\s+OR\s+REPLACE\s+VIEW\s+(?:`\{\{(KAYNAK_DB|SUNUM_DB)\}\}`\.)?`?(\w+)`?\s+AS\b', re.I)
DROP_RE = re.compile(r'^DROP\s+VIEW\s+IF\s+EXISTS\s+(?:`\{\{(KAYNAK_DB|SUNUM_DB)\}\}`\.)?`?(\w+)`?\s*$', re.I)
TABLE_RE = re.compile(r'^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?(\w+)`?', re.I)
SCHEMA_OF = {'KAYNAK_DB': SRC, 'SUNUM_DB': SUN, None: SRC}
# Izole entegrasyon testinde dogrulanan degisiklik kumesi: tanimi degisebilecek TEK gorunumler (+ kaynak_bul).
EXPECTED_CHANGED = sorted([(SRC, 'v_bulgu_kaynak'), (SRC, 'v_sunum_taban')])


def sqlnorm(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def mode_set(s):
    """sql_mode kume olarak: ROUTINES.SQL_MODE ile @@sql_mode ayni modlari farkli sirayla gosterebilir.
    Sira onemsiz; eksik / fazla mod yine fark sayilir."""
    return frozenset(x.strip().upper() for x in str(s or '').split(',') if x.strip())


def install_plan(base):
    """{(sema, gorunum): ifade}, silinecek gorunumler, tablolar, taninmayan ifadeler (kurulum sirasiyla)."""
    files = [os.path.join(base, 'kalite_core.sql'), os.path.join(base, 'sunum_core.sql')]
    hoca = os.path.join(base, 'sunum_hoca')
    files += [os.path.join(hoca, n) for n in sorted(os.listdir(hoca)) if n.endswith('.sql')]
    views, drops, tables, unknown = {}, set(), set(), []
    for path in files:
        for st in _split_sql_statements(open(path, encoding='utf-8').read()):
            m, d, t = VIEW_RE.match(st), DROP_RE.match(st), TABLE_RE.match(st)
            if m:
                views[(SCHEMA_OF[m.group(1)], m.group(2))] = sqlnorm(st)
            elif d:
                drops.add((SCHEMA_OF[d.group(1)], d.group(2)))
            elif t:
                tables.add(t.group(1))
            else:
                unknown.append((os.path.basename(path), sqlnorm(st)[:80]))
    return views, drops, tables, unknown


def body_of(sql):
    m = re.search(r'\bBEGIN\b.*\bEND\b', sql or '', re.S)
    return m.group(0) if m else ''


def same_body(stored, source_sql):
    """Saklanan govde kaynak metne esit mi; ROUTINE_DEFINITION'in kacislari cozulmus bicimi de kabul."""
    src = body_of(source_sql)
    return sqlnorm(body_of(stored)) in (sqlnorm(src), sqlnorm(src.replace(BS + BS, BS)))


def admin_user(cur):
    cur.execute('SELECT CURRENT_USER()')
    return str(txt(cur.fetchone()[0]))


def proc_grants(cur):
    cur.execute("SELECT Host, User, Proc_priv FROM mysql.procs_priv WHERE Db = %s AND Routine_name = 'kaynak_bul' "
                "AND Routine_type = 'PROCEDURE' ORDER BY 2, 1", (SRC,))
    return sorted([str(txt(h)), str(txt(u)), str(txt(p))] for h, u, p in cur.fetchall())


def regrant(cur, rows):
    for host, user, privs in rows:
        names = {p.strip().lower() for p in privs.split(',') if p.strip()}
        sql_privs = [x for x, k in (('EXECUTE', 'execute'), ('ALTER ROUTINE', 'alter routine')) if k in names]
        if not sql_privs:
            continue
        extra = ' WITH GRANT OPTION' if 'grant' in names else ''
        cur.execute(f"GRANT {', '.join(sql_privs)} ON PROCEDURE `{SRC}`.`kaynak_bul` TO %s@%s{extra}", (user, host))


def hata_onlem_rows(cur):
    cur.execute(f'SELECT hata_turu, halusinasyon_turu, onlem, kural_yeri, esleme_surumu FROM `{SRC}`.`hata_onlem` ORDER BY 1')
    return {str(txt(r[0])): [str(txt(x)) for x in r[1:]] for r in cur.fetchall()}
'@

$PY_A3_ON = @'
# Prova / on tahmin (yalniz okuma) ve ozgun tanimlarin kaydi.
from src import database as D
from src.sunum import ONLEM_ESLEME, ONLEM_SURUMU, PROCEDURE_SQL as NEW_PROC
from src.sunum_hoca import max_group_count, MIN_GROUPS
new_views, new_drops, new_tables, unknown = install_plan('sql')
head_views, head_drops, _, _ = install_plan(os.path.join(E['Y3_HEAD'], 'sql'))
ok('kurulum SQL: yalniz CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE VIEW / DROP VIEW IF EXISTS', not unknown, unknown[:5])
head_diff = sorted(k for k in new_views if head_views.get(k) != new_views[k])
print(f'BILGI kaynak SQL i HEAD den farkli gorunumler: {head_diff} (commit edildiyse bos olabilir)')
ok('HEAD ile calisma kopyasi farki yalniz izole testte dogrulanan gorunumlerde', set(head_diff) <= set(EXPECTED_CHANGED),
   sorted(set(head_diff) - set(EXPECTED_CHANGED)))
ok('beklenen degisecek gorunumler kurulum SQL inde var', set(EXPECTED_CHANGED) <= set(new_views))
changed = EXPECTED_CHANGED
stop_if_failed('kurulum SQL')

a = connect('admin', SRC)
c = a.cursor()
me = admin_user(c)
existing = {}
for schema in (SRC, SUN):
    c.execute('SELECT TABLE_NAME, DEFINER, SECURITY_TYPE, MD5(VIEW_DEFINITION) FROM information_schema.VIEWS '
              'WHERE TABLE_SCHEMA = %s', (schema,))
    for n, df, st, md in c.fetchall():
        existing[(schema, str(txt(n)))] = (str(txt(df)), str(txt(st)), str(txt(md)))
replace = sorted(k for k in new_views if k in existing)
create = sorted(k for k in new_views if k not in existing)
drop = sorted(k for k in new_drops if k in existing)
bad_owner = [(k, existing[k][0]) for k in replace + drop if existing[k][0] != me]
ok(f'yeniden kurulacak / silinecek gorunumlerin sahibi kurulumu yapacak yonetici ({me})', not bad_owner, bad_owner)
ok('yeniden kurulacak gorunumler SQL SECURITY DEFINER', all(existing[k][1] == 'DEFINER' for k in replace))
c.execute("SELECT DEFINER, SECURITY_TYPE, ROUTINE_DEFINITION, SQL_MODE FROM information_schema.ROUTINES "
          "WHERE ROUTINE_SCHEMA = %s AND ROUTINE_NAME = 'kaynak_bul' AND ROUTINE_TYPE = 'PROCEDURE'", (SRC,))
proc = c.fetchone()
proc = [str(txt(x)) if x is not None else None for x in proc] if proc else None
c.execute('SELECT @@GLOBAL.sql_mode')
gmode = str(txt(c.fetchone()[0]))
ok(f'kaynak_bul var, sahibi {me}, SQL SECURITY DEFINER', bool(proc) and proc[0] == me and proc[1] == 'DEFINER', proc[:2] if proc else None)
ok('uretim kaynak_bul govdesi calisma kopyasindan farkli (kurulacak degisiklik var)', bool(proc) and not same_body(proc[2], NEW_PROC))
ok('kaynak_bul sql_mode = kurulum oturumunun sql_mode u (kume; degismeyecek)', bool(proc) and mode_set(proc[3]) == mode_set(gmode),
   f'{proc[3] if proc else None} / {gmode}')
for t in sorted(new_tables):
    c.execute('SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s', (SRC, t))
    ok(f'tablo zaten var (CREATE TABLE IF NOT EXISTS veri / sema degistirmez): {t}', c.fetchone()[0] == 1)
c.execute('SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s', (SUN,))
ok('kardiyo_sunum veritabani var (CREATE DATABASE IF NOT EXISTS etkisiz)', c.fetchone()[0] == 1)
# --init gerekli mi (salt okunur; init_schema'nin yapacagi her degisiklik icin)
needs = []
schema_tables = re.findall(r'CREATE TABLE IF NOT EXISTS\s+`?(\w+)', open('sql/schema_core.sql', encoding='utf-8').read())
for t in schema_tables:
    if not D.table_exists(c, t):
        needs.append(f'tablo yok: {t}')
for t, cols in D.CORE_TABLE_COLUMNS.items():
    actual = D.get_table_columns(c, t)
    needs += [f'sutun yok: {t}.{x}' for x in cols if actual and x not in actual]
for t, cols in D.CORE_COLUMN_MIGRATIONS.items():
    actual = D.get_table_columns(c, t)
    needs += [f'gecis sutunu yok: {t}.{x}' for x, _ in cols if x not in actual]
for t, cols in D.CORE_NULLABLE_MIGRATIONS.items():
    for x, _ in cols:
        c.execute('SELECT IS_NULLABLE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() '
                  'AND TABLE_NAME = %s AND COLUMN_NAME = %s', (t, x))
        row = c.fetchone()
        if row and str(txt(row[0])) == 'NO':
            needs.append(f'NULL gecisi: {t}.{x}')
for t, cols in D.CORE_WIDEN_MIGRATIONS.items():
    lengths = D._column_max_lengths(c, t)
    needs += [f'genisletme: {t}.{x}' for x, _, minimum in cols if lengths.get(x) is not None and lengths[x] < minimum]
for t, cons in D.CORE_CONSTRAINT_MIGRATIONS.items():
    needs += [f'kisit yok: {t}.{n}' for n, _ in cons if not D._constraint_exists(c, t, n)]
mcols = D.get_table_columns(c, 'makaleler')
needs += [f'makaleler sutunu yok: {x}' for x, _ in D.MAKALELER_NEW_COLUMNS if x not in mcols]
for v in D.CORE_VIEWS:
    if (SRC, v) not in existing:
        needs.append(f'gorunum yok: {v}')
ok('src.database --init GEREKMIYOR (bu betik calistirmaz)', not needs, needs[:10])
# hata_onlem: kurulumdan sonra beklenen icerik = mevcut mu (veri degisikligi olmamali)
current = hata_onlem_rows(c)
after = {k: v for k, v in current.items() if v[3] == ONLEM_SURUMU}
after.update({k: [h, o, kk, ONLEM_SURUMU] for k, (h, o, kk) in ONLEM_ESLEME.items()})
diff = sorted(k for k in set(current) | set(after) if current.get(k) != after.get(k))
ok(f'hata_onlem kurulumla degismeyecek ({len(current)} satir, {ONLEM_SURUMU})', not diff, diff[:10])
# measurements grup sutunu sayisi
n_groups = max(MIN_GROUPS, max_group_count(a, SRC))
c.execute('SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s '
          'AND COLUMN_NAME LIKE %s', (SUN, 'measurements', 'group' + BS + '_%' + BS + '_value'))
installed = c.fetchone()[0]
ok('measurements grup sutunu sayisi degismeyecek', installed == n_groups, f'kurulu {installed}, kurulumda {n_groups}')
stop_if_failed('on kontrol')
# Ozgun tanimlar (geri alma icin)
originals = []
for schema, name in replace + drop:
    c.execute(f'SHOW CREATE VIEW `{schema}`.`{name}`')
    stmt = str(txt(c.fetchone()[1]))
    ok(f'ozgun tanim okundu: {schema}.{name}', stmt.startswith('CREATE ALGORITHM='), stmt[:40])
    originals.append({'sema': schema, 'ad': name, 'create': stmt, 'definer': existing[(schema, name)][0],
                      'guvenlik': existing[(schema, name)][1], 'md5': existing[(schema, name)][2]})
c.execute('SHOW CREATE PROCEDURE `kardiyo_tam_metin`.`kaynak_bul`')
show_proc = str(txt(c.fetchone()[2]))     # mysqldump'in kullandigi, yeniden calistirilabilir ozgun metin (geri alma)
ok('kaynak_bul ozgun tanimi okundu (SHOW CREATE PROCEDURE)',
   show_proc.startswith('CREATE DEFINER=') and 'PROCEDURE `kaynak_bul`' in show_proc and same_body(proc[2], show_proc),
   show_proc[:60])
grants = proc_grants(c)
a.rollback(); a.close()
stop_if_failed('ozgun tanimlar')
if E['Y3_MOD'] != 'gerial':
    os.makedirs(os.path.dirname(ORIG_PATH), exist_ok=True)
    write_new(ORIG_PATH, {'yonetici': me, 'gorunumler': originals, 'kaynagi_degisen': changed,
                          'yeniden_kurulacak': replace, 'olusturulacak': create, 'silinecek': drop,
                          'kaynagi_head_den_farkli': head_diff,
                          'kaynak_bul': {'definer': proc[0], 'guvenlik': proc[1], 'sql_mode': proc[3],
                                         'tanim': proc[2], 'show_create': show_proc},
                          'kaynak_bul_yetkileri': grants, 'hata_onlem': current})
print(f'\nPLAN: yeniden kurulacak {len(replace)} gorunum, olusturulacak {create}, silinecek {drop}, '
      f'kaynagi degisen {changed}, kaynak_bul yeniden kurulur ({len(grants)} EXECUTE yetkisi geri verilir); '
      f'veri degisikligi: yok (hata_onlem ayni, kalite tablolarina yazim yok)')
print('SONUC:', 'ON KONTROL TAMAM' if not fail else fail)
sys.exit(1 if fail else 0)
'@

$PY_A3_YETKI = @'
# kaynak_bul yeniden kurulduktan sonra: ozgun EXECUTE yetkileri ve sahibi.
ORIG = json.load(open(ORIG_PATH, encoding='utf-8'))
a = connect('admin', SRC, readonly=False)
c = a.cursor()
ok('yonetici hesabi ozgun kayittaki ile ayni', admin_user(c) == ORIG['yonetici'])
stop_if_failed('yonetici')
regrant(c, ORIG['kaynak_bul_yetkileri'])
got = proc_grants(c)
ok('kaynak_bul EXECUTE yetkileri ozgun ile ayni', got == sorted(ORIG['kaynak_bul_yetkileri']), got)
c.execute("SELECT DEFINER, SECURITY_TYPE, SQL_MODE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s "
          "AND ROUTINE_NAME = 'kaynak_bul' AND ROUTINE_TYPE = 'PROCEDURE'", (SRC,))
r = [str(txt(x)) for x in (c.fetchone() or ('', '', ''))]
k = ORIG['kaynak_bul']
ok('kaynak_bul sahibi / SQL SECURITY / sql_mode (kume) ozgun ile ayni',
   r[:2] == [k['definer'], k['guvenlik']] and mode_set(r[2]) == mode_set(k['sql_mode']), r)
a.close()
sys.exit(1 if fail else 0)
'@

$PY_A3_SONRA = @'
# Uygulama sonrasi: Asama 2 sonrasi kayitla karsilastirma; yalniz beklenen nesne degisiklikleri.
from src.sunum import PROCEDURE_SQL as NEW_PROC
ORIG = json.load(open(ORIG_PATH, encoding='utf-8'))
OUT = E['Y3_WORK']
CHANGED = {tuple(x) for x in ORIG['kaynagi_degisen']}
CREATED = {tuple(x) for x in ORIG['olusturulacak']}
DROPPED = {tuple(x) for x in ORIG['silinecek']}
ME = ORIG['yonetici']
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
# Klinik veri
kb0, kb1 = before['klinik_bulgular'], state['klinik_bulgular']
changed_rows = [i for i in set(kb0['satir_ozetleri']) | set(kb1['satir_ozetleri'])
                if kb0['satir_ozetleri'].get(i) != kb1['satir_ozetleri'].get(i)]
ok('klinik_bulgular butun satirlarin butun alanlari ayni', not changed_rows and kb1['satir_sayisi'] == kb0['satir_sayisi'],
   f"{kb1['satir_sayisi']} satir; farkli {len(changed_rows)}")
ok('verified pilot kayitlari ayni', kb0['verified_pilot'] == kb1['verified_pilot'], kb1['verified_pilot_sayisi'])
ok('klinik_bulgular sutunlari ve pilot durumu ayni', kb0['sutunlar'] == kb1['sutunlar'] and kb0['pilot_durumu_sql'] == kb1['pilot_durumu_sql'])
ok('sekiz klinik gorunumun satir sayisi ve icerigi birebir ayni', before['sunum_gorunumleri'] == state['sunum_gorunumleri'],
   {v: x[0] for v, x in state['sunum_gorunumleri'].items()})
ok('test veritabanlari ayni', before['test_parmak_izi'] == state['test_parmak_izi'])
# Parmak izi: butun tablolar ayni; gorunum tanimlarinda yalniz beklenenler
fp0, fp1 = before['uretim_parmak_izi'], state['uretim_parmak_izi']
for s in PROD:
    t0, t1 = fp0[s]['tablolar'], fp1[s]['tablolar']
    ok(f'{s}: butun tablolarin CHECKSUM ve satir sayisi ayni (hata_onlem, kalite tablolari dahil)', t0 == t1,
       [n for n in set(t0) | set(t1) if t0.get(n) != t1.get(n)][:5])
    v0, v1 = fp0[s]['gorunumler'], fp1[s]['gorunumler']
    unexpected = sorted(n for n in set(v0) | set(v1) if v0.get(n) != v1.get(n) and (s, n) not in CHANGED | CREATED | DROPPED)
    ok(f'{s}: yalniz kaynak SQL i degisen / yeni / silinen gorunumlerin tanimi degisti', not unexpected, unexpected)
    ok(f'{s}: olusturulacak gorunumler var, silinecekler yok',
       all(n in v1 for ss, n in CREATED if ss == s) and all(n not in v1 for ss, n in DROPPED if ss == s))
# Sema / nesne / yetki kaydi
for key in ('sunucu', 'kullanicilar', 'procs_priv', 'tables_priv'):
    ok(f'{key} ayni (EXECUTE yetkileri dahil)', bmeta[key] == meta[key])
ok('nesne sahipleri ayni', set(bmeta['sahipler']) | {ME} >= set(meta['sahipler']) and all(meta['sahipler'].values()), meta['sahipler'])
for s in PROD:
    d0, d1 = bmeta['veritabanlari'][s], meta['veritabanlari'][s]
    for kind in ('tablolar', 'tetikleyiciler', 'olaylar'):
        ok(f'{s}: {kind} ayni', d0[kind] == d1[kind])
    for n in sorted(set(d0['gorunumler']) | set(d1['gorunumler'])):
        a0, a1 = d0['gorunumler'].get(n), d1['gorunumler'].get(n)
        if (s, n) in DROPPED:
            ok(f'{s}.{n} silindi', a1 is None)
        elif (s, n) in CREATED:
            ok(f'{s}.{n} olusturuldu (sahibi {ME}, DEFINER)', bool(a1) and a1['definer'] == ME and a1['guvenlik'] == 'DEFINER')
        elif (s, n) in CHANGED:
            ok(f'{s}.{n}: tanim guncellendi, sahibi / SQL SECURITY / CHECK ayni',
               bool(a1) and (a0['definer'], a0['guvenlik'], a0['check']) == (a1['definer'], a1['guvenlik'], a1['check']))
        elif a0 != a1:
            ok(f'{s}.{n} degismedi', False, 'beklenmeyen gorunum degisikligi')
    for k in sorted(set(d0['rutinler']) | set(d1['rutinler'])):
        r0, r1 = d0['rutinler'].get(k), d1['rutinler'].get(k)
        if s == SRC and k == 'PROCEDURE kaynak_bul':
            ok('kaynak_bul: yeni govde (calisma kopyasi), sahibi / SQL SECURITY / sql_mode (kume) ayni',
               bool(r1) and same_body(r1['tanim'], NEW_PROC) and
               (r0['definer'], r0['guvenlik']) == (r1['definer'], r1['guvenlik']) and
               mode_set(r0['sql_mode']) == mode_set(r1['sql_mode']), f"{r0['sql_mode']} / {r1['sql_mode'] if r1 else None}")
        elif r0 != r1:
            ok(f'{s} rutin degismedi: {k}', False)
# kaynak_bul: eski sonuclar korunur, ek eslesmeler yalniz 2. / 3. kanit parcasindan
COLL = 'utf8mb4_0900_ai_ci'
OLD = ('mudahale', 'tani', 'hasta_grubu', 'sonuc', 'kanit_cumle')
like = lambda expr: f'({expr}) COLLATE {COLL} LIKE %s COLLATE {COLL}'
part = lambda i: (f"CASE WHEN JSON_VALID(b.taslak_notu) THEN CASE WHEN JSON_TYPE(JSON_EXTRACT(b.taslak_notu, "
                  f"'$.kanit_parcalari[{i}].metin')) = 'STRING' THEN JSON_UNQUOTE(JSON_EXTRACT(b.taslak_notu, "
                  f"'$.kanit_parcalari[{i}].metin')) END END")
pattern = lambda term: '%' + term.strip(' ').replace(BS, BS + BS).replace('%', BS + '%').replace('_', BS + '_') + '%'
p = connect('okur', SRC)
pc = p.cursor()
pc.execute('SELECT bulgu_id FROM v_bulgu_kaynak WHERE kanit_parcalari_metin IS NOT NULL')
multi = {r[0] for r in pc.fetchall()}
extra_total = 0
for term in TERMS:
    A, B = set(before['kaynak_bul'][term]), set(state['kaynak_bul'][term])
    lost, new = sorted(A - B), sorted(B - A)
    extra_total += len(new)
    ok(f'kaynak_bul {term!r}: kaybolan kayit yok', not lost, f'A {len(A)}, B {len(B)}; kayip {lost[:10]}')
    ok(f'kaynak_bul {term!r}: tek parcali sonuclar ayni', (A - multi) == (B - multi))
    examined, bad = set(), []
    if new:
        d = pattern(term)
        pc.execute(f"SELECT k.bulgu_id, {', '.join(like('k.' + x) for x in OLD)}, {like('k.kanit_parcalari_metin')}, "
                   f"CASE WHEN JSON_VALID(b.taslak_notu) THEN JSON_LENGTH(JSON_EXTRACT(b.taslak_notu, '$.kanit_parcalari')) END, "
                   f"{like(part(0))}, {like(part(1))}, {like(part(2))}, ({part(0)}) = b.kanit_cumle "
                   f"FROM v_bulgu_kaynak AS k JOIN klinik_bulgular AS b ON b.id = k.bulgu_id "
                   f"WHERE k.bulgu_id IN ({','.join(['%s'] * len(new))}) ORDER BY k.bulgu_id",
                   (d,) * (len(OLD) + 4) + tuple(new))
        for r in pc.fetchall():
            i, olds, concat, nparts, p0, p1, p2, first_same = r[0], r[1:6], r[6], r[7], r[8], r[9], r[10], r[11]
            examined.add(i)
            problems = [x for x, cond in (('cok parcali degil', i not in multi), ('eski sutunda da eslesiyor', any(bool(v) for v in olds)),
                                          (f'parca sayisi {nparts}', not (nparts and nparts <= 3)), ('ilk parcada eslesiyor', bool(p0)),
                                          ('yapay ya da parcasiz eslesme', not (bool(p1) or bool(p2))),
                                          ('ilk parca kanit_cumle degil', not first_same)) if cond]
            if problems:
                bad.append((i, problems))
    ok(f'kaynak_bul {term!r}: incelenen yeni kayitlar = B - A (tamami)', examined == set(new), f'{len(examined)} / {len(new)}')
    ok(f'kaynak_bul {term!r}: ek eslesmeler yalniz 2. / 3. kanit parcasindan (yapay yok)', not bad, bad[:5])
p.rollback(); p.close()
ok(f"kaynak_bul toplam ek eslesme = {E['Y3_BEKLENEN_EK']}", extra_total == int(E['Y3_BEKLENEN_EK']), extra_total)
print('\nSONUC:', 'SON KONTROL TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_A3_GERIAL = @'
# Geri alma: ozgun gorunum tanimlari (SHOW CREATE VIEW), yeni olusanlar silinir, kaynak_bul HEAD metni,
# EXECUTE yetkileri, gerekirse hata_onlem satirlari. Ardindan taban kayitla tam karsilastirma ayrica yapilir.
ORIG = json.load(open(ORIG_PATH, encoding='utf-8'))
a = connect('admin', SRC, readonly=False)
c = a.cursor()
ok('yonetici hesabi ozgun kayittaki ile ayni (DEFINER korunur)', admin_user(c) == ORIG['yonetici'])
stop_if_failed('yonetici')
c.execute('SET SESSION lock_wait_timeout = 30')
for v in ORIG['gorunumler']:
    stmt = v['create']
    if not stmt.startswith('CREATE ALGORITHM='):
        ok(f"ozgun tanim bicimi {v['sema']}.{v['ad']}", False)
        continue
    c.execute(f"USE `{v['sema']}`")
    c.execute('CREATE OR REPLACE ' + stmt[len('CREATE '):])
c.execute(f'USE `{SRC}`')
for schema, name in ORIG['olusturulacak']:
    c.execute(f'DROP VIEW IF EXISTS `{schema}`.`{name}`')
c.execute('SET SESSION sql_mode = %s', (ORIG['kaynak_bul']['sql_mode'],))
c.execute('DROP PROCEDURE IF EXISTS kaynak_bul')
c.execute(ORIG['kaynak_bul']['show_create'])          # ozgun metin (SHOW CREATE PROCEDURE), sahibi ayni yonetici
regrant(c, ORIG['kaynak_bul_yetkileri'])
current = hata_onlem_rows(c)
a.rollback()                                          # okuma transaction'ini kapat
if current != ORIG['hata_onlem']:
    a.start_transaction()
    c.execute(f'DELETE FROM `{SRC}`.`hata_onlem`')
    c.executemany(f'INSERT INTO `{SRC}`.`hata_onlem` (hata_turu, halusinasyon_turu, onlem, kural_yeri, esleme_surumu) '
                  'VALUES (%s, %s, %s, %s, %s)', [(k, *v) for k, v in sorted(ORIG['hata_onlem'].items())])
    a.commit()
    print('BILGI hata_onlem ozgun satirlara donduruldu')
# Dogrulama
for v in ORIG['gorunumler']:
    c.execute('SELECT DEFINER, SECURITY_TYPE, MD5(VIEW_DEFINITION) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s '
              'AND TABLE_NAME = %s', (v['sema'], v['ad']))
    r = [str(txt(x)) for x in (c.fetchone() or ('', '', ''))]
    ok(f"geri alma: {v['sema']}.{v['ad']} ozgun tanim / sahip", r == [v['definer'], v['guvenlik'], v['md5']])
for schema, name in ORIG['olusturulacak']:
    c.execute('SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s', (schema, name))
    ok(f'geri alma: yeni gorunum silindi {schema}.{name}', c.fetchone()[0] == 0)
c.execute("SELECT DEFINER, SECURITY_TYPE, SQL_MODE, ROUTINE_DEFINITION FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s "
          "AND ROUTINE_NAME = 'kaynak_bul' AND ROUTINE_TYPE = 'PROCEDURE'", (SRC,))
r = [str(txt(x)) for x in (c.fetchone() or ('', '', '', ''))]
k = ORIG['kaynak_bul']
ok('geri alma: kaynak_bul ozgun tanim (birebir), sahip, SQL SECURITY, sql_mode (kume)',
   [r[0], r[1], r[3]] == [k['definer'], k['guvenlik'], k['tanim']] and mode_set(r[2]) == mode_set(k['sql_mode']),
   f"{r[2]} / {k['sql_mode']}")
ok('geri alma: kaynak_bul EXECUTE yetkileri ozgun ile ayni', proc_grants(c) == sorted(ORIG['kaynak_bul_yetkileri']))
ok('geri alma: hata_onlem ozgun ile ayni', hata_onlem_rows(c) == ORIG['hata_onlem'])
a.close()
print('\nSONUC:', 'GERI ALMA TAMAM (taban kayitla tam karsilastirma ayrica yapilir)' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

# =================================================================================================
$saved = @{}
foreach ($n in 'MYSQL_PWD', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE', 'MYSQL_SUNUM_DATABASE', 'MYSQL_HOST', 'MYSQL_PORT',
               'Y1_HOST', 'Y1_PORT', 'Y1_ADMIN_USER', 'Y1_ADMIN_PWD', 'Y1_OKUR_USER', 'Y1_OKUR_PWD', 'Y1_GERI_USER',
               'Y1_GERI_PWD', 'Y1_GERI_DBS', 'Y1_BEKLENEN_SATIR', 'Y1_BEKLENEN_VERIFIED', 'Y3_KAYNAK', 'Y3_WORK', 'Y3_HEAD',
               'Y3_MOD', 'Y3_BEKLENEN_EK', 'PYTHONIOENCODING') {
  $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$usersCreated = $false; $jsonBefore = $null; $work = $null; $installStarted = $false
$exitCode = 1
try {
  [Environment]::SetEnvironmentVariable('Y1_GERI_PWD', $null, 'Process')
  if ($GeriAlKlasoru -and $Uygula) { Add-Result 'parametreler' $false '-Uygula ve -GeriAlKlasoru birlikte kullanılamaz' }
  Write-Host "MOD: $mode" -ForegroundColor Cyan

  # 0) Aşama 1 yedekleri, Aşama 2 kayıtları, ön koşullar
  $svc = Get-Service MYSQL80 -ErrorAction SilentlyContinue
  Add-Result 'MySQL hizmeti çalışıyor' ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
  $srcDb = if ($dotenv.MYSQL_DATABASE) { $dotenv.MYSQL_DATABASE } else { '' }
  $sunDb = if ($dotenv.MYSQL_SUNUM_DATABASE) { $dotenv.MYSQL_SUNUM_DATABASE } else { 'kardiyo_sunum' }
  Add-Result '.env hedefleri üretim: kardiyo_tam_metin / kardiyo_sunum' ($srcDb -eq 'kardiyo_tam_metin' -and $sunDb -eq 'kardiyo_sunum') "$srcDb / $sunDb"
  $a1 = (Resolve-Path -LiteralPath $Asama1Klasoru).Path
  $a2 = (Resolve-Path -LiteralPath $Asama2Klasoru).Path
  $manifest = Get-Content -LiteralPath (Join-Path $a1 'yedek\manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $report = Get-Content -LiteralPath (Join-Path $a1 'geri_yukleme\temizleme_raporu.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($db in $PROD) {
    $m = $manifest.$db
    if (-not $m) { Add-Result "manifest: $db" $false 'kayıt yok' }
    $file = Join-Path $a1 "yedek\$($m.dosya)"
    $item = Get-Item -LiteralPath $file
    $sha = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
    $okm = ($item.Length -eq [int64]$m.boyut) -and ($sha -eq $m.sha256) -and $item.IsReadOnly -and
           ($report.$db.kaynak_sha256 -and ($report.$db.kaynak_sha256.ToUpper() -eq $sha))
    Add-Result "Aşama 1 yedeği doğrulandı: $db" $okm "$($item.Length) bayt"
  }
  foreach ($f in 'enum_alter_yapildi.json', 'enum_sonucu.json', 'sonra_durum.json', 'sonra_nesneler_ve_yetkiler.json') {
    if (-not (Test-Path -LiteralPath (Join-Path $a2 $f))) { Add-Result 'Aşama 2 kaydı mevcut' $false $f }
  }
  Add-Result 'Aşama 2 uygulama kayıtları mevcut' $true $a2
  foreach ($s in $A1SCRIPT, $A2SCRIPT) { if (-not (Test-Path -LiteralPath $s)) { Add-Result 'önceki aşama betiği' $false $s } }
  $a1raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $A1SCRIPT).Path)
  $a2raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $A2SCRIPT).Path)
  $A1_COMMON = Get-Block $a1raw 'PY_COMMON'; $A1_DURUM = Get-Block $a1raw 'PY_DURUM'; $BAGLANTI = Get-Block $a2raw 'PY_BAGLANTI'
  $marker = "`no = connect('okur')"
  $hits = ([regex]::Matches($A1_DURUM, [regex]::Escape($marker))).Count
  Add-Result 'Aşama 1 PY_COMMON / PY_DURUM ve Aşama 2 PY_BAGLANTI okundu' ($A1_COMMON -and $A1_DURUM -and $BAGLANTI -and $hits -eq 1) "işaret $hits"
  $A1_DEFS = $A1_DURUM.Substring(0, $A1_DURUM.IndexOf($marker))
  $COMMON = $A1_COMMON + "`n" + $BAGLANTI + "`n"
  $jsonBefore = Get-JsonHashes
  Add-Result 'JSON dosyalarının SHA-256 kaydı' ($jsonBefore.Count -gt 0) "$($jsonBefore.Count) dosya"
  $work = "$a1`_asama3_${mode}_$stamp"
  Add-Result 'çalışma klasörü yeni' (-not (Test-Path -LiteralPath $work)) $work
  New-Item -ItemType Directory -Path $work | Out-Null

  # Taban kayıt (Aşama 2 sonrası) ve HEAD kaynakları (geri alma ve değişiklik listesi)
  if ($mode -eq 'gerial') {
    $kaynak = (Resolve-Path -LiteralPath $GeriAlKlasoru).Path
    foreach ($f in 'taban\mevcut_durum.json', 'taban\nesneler_ve_yetkiler.json', 'geri_alma\ozgun_nesneler.json') {
      if (-not (Test-Path -LiteralPath (Join-Path $kaynak $f))) { Add-Result 'geri alma kaydı mevcut' $false $f }
    }
  } else {
    $kaynak = $work
    New-Item -ItemType Directory -Path "$work\taban" | Out-Null
    Copy-Item -LiteralPath (Join-Path $a2 'sonra_durum.json') -Destination "$work\taban\mevcut_durum.json"
    Copy-Item -LiteralPath (Join-Path $a2 'sonra_nesneler_ve_yetkiler.json') -Destination "$work\taban\nesneler_ve_yetkiler.json"
    $same = ((Get-FileHash "$work\taban\mevcut_durum.json").Hash -eq (Get-FileHash (Join-Path $a2 'sonra_durum.json')).Hash) -and
            ((Get-FileHash "$work\taban\nesneler_ve_yetkiler.json").Hash -eq (Get-FileHash (Join-Path $a2 'sonra_nesneler_ve_yetkiler.json')).Hash)
    Add-Result 'taban kayıt: Aşama 2 sonrası durum kopyalandı' $same "$work\taban"
  }
  $head = "$work\head"
  Save-Head 'src/sunum.py' "$head\sunum.py"
  Save-Head 'sql/kalite_core.sql' "$head\sql\kalite_core.sql"
  Save-Head 'sql/sunum_core.sql' "$head\sql\sunum_core.sql"
  $hocaFiles = @(git ls-tree --name-only HEAD sql/sunum_hoca/ | Where-Object { $_ -like '*.sql' })
  if ($LASTEXITCODE -ne 0 -or -not $hocaFiles) { throw 'git ls-tree sql/sunum_hoca başarısız' }
  foreach ($h in $hocaFiles) { Save-Head $h "$head\sql\sunum_hoca\$(Split-Path -Leaf $h)" }
  Add-Result 'HEAD kaynakları kaydedildi (değişiklik listesi denetimi ve kayıt)' $true "$($hocaFiles.Count + 3) dosya"

  # 1) Geçici okuma kullanıcısı (Aşama 1 / 2 ile aynı yetkiler)
  $exist = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "SELECT CONCAT(User,'@',Host) FROM mysql.user WHERE User = '$OKUR_USER'")
  Add-Result 'geçici kullanıcı önceden yok' (-not $exist) "$exist"
  $sql = ''
  foreach ($h in $HOSTS) {
    $sql += "CREATE USER '$OKUR_USER'@'$h' IDENTIFIED BY '$okurPwd';`n"
    foreach ($p in $PROD + $TEST) { $sql += "GRANT SELECT, SHOW VIEW ON ``$(Esc $p)``.* TO '$OKUR_USER'@'$h';`n" }
    $sql += "GRANT EXECUTE ON PROCEDURE ``kardiyo_tam_metin``.``kaynak_bul`` TO '$OKUR_USER'@'$h';`n"
  }
  $usersCreated = $true
  Invoke-My 'mysql.exe' $adminUser $adminPwd @() $sql | Out-Null
  $sql = $null
  Add-Result 'geçici okuma kullanıcısı oluşturuldu (nesne sahibi olmaz)' $true $OKUR_USER

  $env:Y1_HOST = $dbHost; $env:Y1_PORT = $dbPort
  $env:Y1_ADMIN_USER = $adminUser; $env:Y1_ADMIN_PWD = $adminPwd
  $env:Y1_OKUR_USER = $OKUR_USER; $env:Y1_OKUR_PWD = $okurPwd
  $env:Y1_GERI_USER = $OKUR_USER; $env:Y1_GERI_DBS = '{}'
  $env:Y1_BEKLENEN_SATIR = "$BeklenenSatir"; $env:Y1_BEKLENEN_VERIFIED = "$BeklenenVerified"
  $env:Y3_KAYNAK = $kaynak; $env:Y3_WORK = $work; $env:Y3_HEAD = $head; $env:Y3_MOD = $mode
  $env:Y3_BEKLENEN_EK = "$BeklenenEkEslesme"; $env:PYTHONIOENCODING = 'utf-8'
  $FULL_CHECK = $COMMON + $A1_DURUM
  $ORTAK = $COMMON + $PY_A3_ORTAK + "`n"
  $taban = Join-Path $kaynak 'taban'

  if ($mode -eq 'gerial') {
    Add-Result 'geri alma (özgün görünümler, kaynak_bul, yetkiler, hata_onlem)' ((Invoke-Py ($ORTAK + $PY_A3_GERIAL)) -eq 0)
    Add-Result 'üretim Aşama 2 sonrası kayıtla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $taban)) -eq 0)
    $exitCode = 0
  } else {
    # 2) Ön kontrol: üretim Aşama 2 sonrası kayıtla BİREBİR aynı
    Add-Result 'ön kontrol: üretim Aşama 2 sonrası kayıtla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $taban)) -eq 0)
    # 3) Yan etki tahmini ve özgün tanımların kaydı (yalnız okuma)
    Add-Result 'ön tahmin: DEFINER, hata_onlem, --init, measurements, geri alma metni; özgün tanımlar kaydedildi' ((Invoke-Py ($ORTAK + $PY_A3_ON)) -eq 0)
    if ($mode -eq 'prova') {
      Add-Info 'PROVA: kurulum yapılmadı' $true "özgün tanımlar: $work\geri_alma\ozgun_nesneler.json"
      $exitCode = 0
    } else {
      # 4) Kurulum: yalnız yönetici hesabıyla (nesne sahibi korunur), .env üretim hedefleri
      $env:MYSQL_USER = $adminUser; $env:MYSQL_PASSWORD = $adminPwd; $env:MYSQL_HOST = $dbHost; $env:MYSQL_PORT = $dbPort
      $env:MYSQL_DATABASE = 'kardiyo_tam_metin'; $env:MYSQL_SUNUM_DATABASE = 'kardiyo_sunum'
      $installStarted = $true
      $ok1 = $false; $ok2 = $false; $ok3 = $false; $ok4 = $false
      & $py -m src.sunum --kur | Out-Host; $ok1 = ($LASTEXITCODE -eq 0)
      Add-Info 'kurulum: src.sunum --kur' $ok1
      if ($ok1) { & $py -m src.sunum_hoca --kur | Out-Host; $ok2 = ($LASTEXITCODE -eq 0); Add-Info 'kurulum: src.sunum_hoca --kur' $ok2 }
      foreach ($n in 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE', 'MYSQL_SUNUM_DATABASE', 'MYSQL_HOST', 'MYSQL_PORT') {
        [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process')
      }
      if ($ok2) { $ok3 = ((Invoke-Py ($ORTAK + $PY_A3_YETKI)) -eq 0); Add-Info 'kaynak_bul EXECUTE yetkileri ve sahibi korundu' $ok3 }
      if ($ok3) {
        $ok4 = ((Invoke-Py ($COMMON + $A1_DEFS + "`n" + $PY_A3_ORTAK + "`n" + $PY_A3_SONRA) @('sonra', $taban)) -eq 0)
        Add-Info 'son kontrol (veri, 8 görünüm, nesneler, yetkiler, kaynak_bul)' $ok4
      }
      if (-not ($ok1 -and $ok2 -and $ok3 -and $ok4)) {
        Add-Info 'beklenmeyen hata / fark' $false 'otomatik geri alma başlıyor'
        $g1 = Invoke-Py ($ORTAK + $PY_A3_GERIAL)
        $g2 = Invoke-Py $FULL_CHECK @('sonra', $taban)
        Add-Info 'otomatik geri alma ve Aşama 2 sonrası kayıtla tam eşitlik' ($g1 -eq 0 -and $g2 -eq 0) $(if ($g1 -eq 0 -and $g2 -eq 0) { 'üretim Aşama 2 sonrası duruma döndü' } else { "GERİ ALINAMADI: $work\geri_alma\ozgun_nesneler.json ve Aşama 1 yedeği ile elle geri dönüş gerekir" })
        throw 'Aşama 3 kurulumu geri alındı ya da geri alma gerekli'
      }
      Add-Result 'Aşama 3 kurulumu ve son kontrol' $true
      $exitCode = 0
    }
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
