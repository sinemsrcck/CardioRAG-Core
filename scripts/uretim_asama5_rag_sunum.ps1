<#
Üretime geçiş — AŞAMA 5: RAG sunum tabloları. Ayrı veritabanı kardiyo_rag_sunum'da dört FİZİKSEL tablo
(aday_bulgular, clinical_outcomes, measurements, measurements_by_group) güncel RAG adaylarından src.rag_sunum_tablolari
ile doldurulur. Üç üretim veritabanına (kardiyo_tam_metin, kardiyo_sunum, kardiyo_clinical_data) YAZILMAZ; yükleme
kullanıcısının üretimde yalnız SELECT yetkisi vardır.

Çalıştırma (proje kökünde; MySQL hizmeti açık; önce taze Aşama 1):
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama1_yedek.ps1
    # Prova (varsayılan): yalnız geçici prova veritabanında (rsprova_<zaman>) yükleme + tekrar (içerik aynı) + kontroller
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama5_rag_sunum.ps1 -Asama1Klasoru <A1>
    # Uygulama (prova aynı çalıştırmada yeniden yapılır ve geçmelidir)
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama5_rag_sunum.ps1 -Asama1Klasoru <A1> -Uygula
    # Mevcut kardiyo_rag_sunum'a yalnız belirli tabloları ekleme (diğer tablolara dokunulmaz; önce / sonra CHECKSUM):
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama5_rag_sunum.ps1 -Asama1Klasoru <A1> `
        -Tablolar diseases,diagnostics,symptoms,treatments,general_clinical_findings [-Uygula]
      Provada bütün tablolar prova veritabanına yüklenir; korunacak mevcut tabloların içerik anahtarları hedefle birebir
      aynı olmalı (yükleme mevcut tablolarla tutarlı); sonra yalnız istenen tablolarla yükleme 'icerik_ayni' olmalı.
    # Geri alma: yalnız bu aşamanın OLUŞTURDUĞU kardiyo_rag_sunum (uygulama kaydında onceden_vardi = false) silinir
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama5_rag_sunum.ps1 -Asama1Klasoru <A1> -GeriAlKlasoru <A5 uygulama klasörü>

Güvenlik (Aşama 1 / 4 yaklaşımı yeniden kullanılır; PY_COMMON / PY_DURUM Aşama 1 betiğinden okunur):
  - Aşama 1 yedeği (manifest SHA-256, boyut, salt okunur, izole geri yükleme raporu) doğrulanır; üretim Aşama 1 kaydıyla
    BİREBİR aynı olmalı (önce ve sonra; 1362 satır, 1308 verified, sekiz hoca view'ı, bütün tablolar, view ve rutin
    tanımları, kullanıcılar / yetkiler).
  - Yükleme kök / yönetici hesabıyla DEĞİL, geçici kullanıcıyla: üretimde yalnız SELECT; yalnız hedef / prova
    veritabanında SELECT, INSERT, DELETE, CREATE, ALTER, INDEX, REFERENCES (DROP yok). Yönetici yalnız veritabanı ve
    geçici kullanıcı oluşturur / siler.
  - Yükleme tek transaction; hata -> ROLLBACK, önceki başarılı yükleme kalır. İkinci yükleme 'icerik_ayni' olmalı.
  - Hata durumunda hiçbir veritabanı silinmez. Önceden var olan kardiyo_rag_sunum yalnız dört tabloyu içeriyorsa
    kullanılır; başka nesne varsa durulur. Prova veritabanı yalnız bu çalıştırmanın oluşturduğu ad biçimindeyse silinir.
Çıkış kodu: 0 = geçti; 1 = başarısız.
#>
param(
  [Parameter(Mandatory = $true)][string]$Asama1Klasoru,
  [switch]$Uygula,
  [string]$GeriAlKlasoru,
  [string]$Hedef = 'kardiyo_rag_sunum',
  [string[]]$Tablolar = @(),
  [int]$BeklenenSatir = 1362,
  [int]$BeklenenVerified = 1308
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
$TABLES = @('aday_bulgular', 'clinical_outcomes', 'measurements', 'measurements_by_group', 'diseases', 'diagnostics',
            'symptoms', 'treatments', 'general_clinical_findings')
$stamp = Get-Date -f yyyyMMdd_HHmmss
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 6)
$OKUR_USER = "y5_okur_$suffix"
$YUKLE_USER = "y5_yukle_$suffix"
$PROVA_DB = "rsprova_$stamp"
$PROVA_RE = '^rsprova_\d{8}_\d{6}$'
$mode = if ($GeriAlKlasoru) { 'gerial' } elseif ($Uygula) { 'uygula' } else { 'prova' }

$dotenv = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $dotenv[$Matches[1]] = $Matches[2].Trim() }
$adminUser = if ($dotenv.MYSQL_USER) { $dotenv.MYSQL_USER } else { 'root' }
$adminPwd  = $dotenv.MYSQL_PASSWORD
$dbHost    = if ($dotenv.MYSQL_HOST) { $dotenv.MYSQL_HOST } else { 'localhost' }
$dbPort    = if ($dotenv.MYSQL_PORT) { $dotenv.MYSQL_PORT } else { '3306' }
$okurPwd  = [guid]::NewGuid().ToString('N') + 'Aa1!'
$yuklePwd = [guid]::NewGuid().ToString('N') + 'Aa1!'

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
  $code | & $py - @arguments | Out-Host
  return [int]$LASTEXITCODE
}
function Get-JsonHashes {
  $files = @()
  foreach ($d in 'data\review', 'data\clinical', 'evaluation\rag_onbellek_degerlendirme') {
    if (Test-Path -LiteralPath $d) { $files += Get-ChildItem -LiteralPath $d -Recurse -File -Filter *.json }
  }
  $h = @{}
  foreach ($f in $files) { $h[$f.FullName] = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash }
  return $h
}
function Esc([string]$name) { $name -replace '_', '\_' }
function Get-Block([string]$text, [string]$name) { [regex]::Match($text, "(?s)\`$$name = @'\r?\n(.*?)\r?\n'@").Groups[1].Value }
function Get-Tables([string]$db) {
  $rows = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '$db' ORDER BY 1")
  return @($rows | Where-Object { $_ })
}
function Get-Checksums([string]$db, [string[]]$tables) {
  if (-not $tables) { return @() }
  $list = ($tables | ForEach-Object { "``$db``.``$_``" }) -join ', '
  return @(Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "CHECKSUM TABLE $list"))
}
function Invoke-Yukle([string]$db, [string]$ozet, [string[]]$tables) {
  $env:MYSQL_USER = $YUKLE_USER; $env:MYSQL_PASSWORD = $yuklePwd; $env:MYSQL_HOST = $dbHost; $env:MYSQL_PORT = $dbPort
  $env:MYSQL_RAG_SUNUM_DATABASE = $db
  try {
    $yArgs = @('-m', 'src.rag_sunum_tablolari', 'yukle', '--yaz', '--ozet', $ozet)
    if ($tables) { $yArgs += @('--tablolar', ($tables -join ',')) }
    & $py @yArgs | Out-Host
    $code = $LASTEXITCODE
  } finally {
    foreach ($n in 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_RAG_SUNUM_DATABASE') {
      [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process')
    }
  }
  if ($code -ne 0) { return $null }
  return (Get-Content -LiteralPath $ozet -Raw -Encoding UTF8 | ConvertFrom-Json)
}

# --- Gömülü Python (yalnız ASCII). Aşama 1 PY_COMMON / PY_DURUM çalışma anında okunur. ----------------------------------
$PY_A5_YETKI = @'
# Yukleme kullanicisi: uretimde yalniz SELECT; DROP yok; yazma yetkisi yalniz hedef / prova veritabaninda.
g = connect('geri')
gc = g.cursor()
gc.execute('SHOW GRANTS')
grants = [txt(r[0]) for r in gc.fetchall()]
g.rollback(); g.close()
allowed = set(sys.argv[1].split(','))
prod_lines = [x for x in grants if any(p in x.replace(BS + '_', '_') for p in PROD)]
ok('yukleme kullanicisi uretimde yalniz SELECT', bool(prod_lines) and all(re.match(r'GRANT SELECT ON ', x) for x in prod_lines), prod_lines)
other = [x for x in grants if x not in prod_lines and not x.startswith('GRANT USAGE ON *.*')]
ok('yazma yetkisi yalniz hedef / prova veritabaninda', all(any(f'`{d}`.*' in x.replace(BS + '_', '_') for d in allowed) for x in other), other)
ok('yukleme kullanicisinda DROP / ALL / GRANT OPTION yok', not any(('DROP' in x) or ('ALL PRIVILEGES' in x) or ('GRANT OPTION' in x) for x in grants))
sys.exit(1 if fail else 0)
'@

$PY_A5_KONTROL = @'
# Hedef / prova veritabani kontrolu (yukleme kullanicisi, yalniz okuma).
db = sys.argv[1]
g = connect('geri', db)
c = g.cursor()
c.execute('SELECT satir_hash FROM `kardiyo_tam_metin`.`v_rag_dogrulama_durumu`')
view = {txt(r[0]) for r in c.fetchall()}
c.execute("SELECT COUNT(*), COALESCE(SUM(review_status <> 'pending'), 0), COUNT(DISTINCT satir_hash), "
          "COUNT(DISTINCT icerik_anahtari) FROM aday_bulgular")
n, notp, dh, di = (int(x) for x in c.fetchone())
c.execute('SELECT satir_hash FROM aday_bulgular')
adaylar = {txt(r[0]) for r in c.fetchall()}
ok(f'aday_bulgular: {n} aday = guncel aday ({len(view)}); hepsi pending; tekil', n == len(view) == dh == di and notp == 0 and adaylar == view)
DET = {'gecti', 'belirsiz', 'basarisiz'}
SEM = {'destekleniyor', 'kismen_destekleniyor', 'kaynakla_celisiyor', 'yetersiz_kanit', 'belirsiz', 'kontrol_edilmedi', 'karisik'}
INSAN = {'yok', 'ayni_kayit_verified', 'karisik'}
c.execute('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s', (db,))
present = {txt(r[0]) for r in c.fetchall()}
for t in [x for x in ('diseases', 'diagnostics', 'symptoms', 'treatments', 'general_clinical_findings') if x in present]:
    c.execute(f'SELECT COUNT(*), COUNT(DISTINCT icerik_anahtari) FROM `{t}`')
    m, md = (int(x) for x in c.fetchone())
    c.execute(f'SELECT kaynak_aday_hashleri FROM `{t}`')
    bad = [r for r in c.fetchall() if not json.loads(txt(r[0])) or not set(json.loads(txt(r[0]))) <= adaylar]
    ok(f'{t}: {m} satir; icerik anahtari tekil; kaynak aday hashleri aday_bulgular da', m == md and not bad, bad[:2])
for t in ('clinical_outcomes', 'measurements', 'measurements_by_group'):
    c.execute(f'SELECT COUNT(*), COUNT(DISTINCT icerik_anahtari) FROM `{t}`')
    m, md = (int(x) for x in c.fetchone())
    c.execute(f'SELECT deterministik_dogrulama, semantik_dogrulama, insan_onayi, kaynak_aday_hashleri, kaynak_aday_sayisi FROM `{t}`')
    rows = c.fetchall()
    bad = [r for r in rows if txt(r[0]) not in DET or txt(r[1]) not in SEM or txt(r[2]) not in INSAN
           or not set(json.loads(txt(r[3]))) <= adaylar or len(json.loads(txt(r[3]))) != int(r[4])]
    ok(f'{t}: {m} satir; icerik anahtari tekil; durumlar gecerli; kaynak aday hashleri aday_bulgular da', m > 0 and m == md and not bad, bad[:2])
c.execute('SELECT deterministik_dogrulama, semantik_dogrulama, insan_onayi, uretim_tekrar_durumu, COUNT(*) FROM clinical_outcomes '
          'GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4')
for r in c.fetchall():
    print('  clinical_outcomes (deterministik, semantik, insan, tekrar, sayi):', [txt(x) for x in r])
g.rollback(); g.close()
print('\nSONUC:', 'KONTROL TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_A5_KARSILASTIR = @'
# Prova (butun tablolar) ile hedefin korunacak tablolari: icerik anahtarlari birebir ayni mi (yukleme kullanicisi, okuma).
prova, hedef, tables = sys.argv[1], sys.argv[2], [t for t in sys.argv[3].split(',') if t]
g = connect('geri')
c = g.cursor()
for t in tables:
    keys = []
    for db in (prova, hedef):
        c.execute(f'SELECT icerik_anahtari FROM `{db}`.`{t}`')
        keys.append(sorted(txt(r[0]) for r in c.fetchall()))
    ok(f'{t}: prova icerigi = mevcut hedef icerigi ({len(keys[1])} satir; tablo degismeyecek)', keys[0] == keys[1])
g.rollback(); g.close()
sys.exit(1 if fail else 0)
'@

# =================================================================================================
$saved = @{}
foreach ($n in 'MYSQL_PWD', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE', 'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_RAG_SUNUM_DATABASE',
               'Y1_HOST', 'Y1_PORT', 'Y1_ADMIN_USER', 'Y1_ADMIN_PWD', 'Y1_OKUR_USER', 'Y1_OKUR_PWD', 'Y1_GERI_USER',
               'Y1_GERI_PWD', 'Y1_GERI_DBS', 'Y1_BEKLENEN_SATIR', 'Y1_BEKLENEN_VERIFIED', 'PYTHONIOENCODING') {
  $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$usersCreated = $false; $provaCreated = $false; $jsonBefore = $null; $work = $null; $protected = @()
$exitCode = 1
try {
  if ($GeriAlKlasoru -and $Uygula) { Add-Result 'parametreler' $false '-Uygula ve -GeriAlKlasoru birlikte kullanılamaz' }
  if ($Hedef -notmatch '^[A-Za-z0-9_]{1,64}$' -or $PROD -contains $Hedef -or $TEST -contains $Hedef) { Add-Result 'hedef veritabanı adı' $false $Hedef }
  Write-Host "MOD: $mode | hedef: $Hedef" -ForegroundColor Cyan

  # 0) Ön koşullar
  $svc = Get-Service MYSQL80 -ErrorAction SilentlyContinue
  Add-Result 'MySQL hizmeti çalışıyor' ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
  Add-Result '.env kaynak veritabanı kardiyo_tam_metin' ($dotenv.MYSQL_DATABASE -eq 'kardiyo_tam_metin') "$($dotenv.MYSQL_DATABASE)"
  $a1 = (Resolve-Path -LiteralPath $Asama1Klasoru).Path
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
  $a1raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $A1SCRIPT).Path)
  $A1_COMMON = Get-Block $a1raw 'PY_COMMON'; $A1_DURUM = Get-Block $a1raw 'PY_DURUM'
  Add-Result 'Aşama 1 PY_COMMON / PY_DURUM okundu' ([bool]$A1_COMMON -and [bool]$A1_DURUM)
  $COMMON = $A1_COMMON + "`n"
  $FULL_CHECK = $COMMON + $A1_DURUM
  $jsonBefore = Get-JsonHashes
  $work = "$a1`_asama5_${mode}_$stamp"
  Add-Result 'çalışma klasörü yeni' (-not (Test-Path -LiteralPath $work)) $work
  New-Item -ItemType Directory -Path $work | Out-Null

  # 1) Geçici kullanıcılar (okuma; yükleme) ve prova veritabanı
  $exist = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '$PROVA_DB' UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$OKUR_USER','$YUKLE_USER')")
  Add-Result 'geçici veritabanı / kullanıcı önceden yok' (-not $exist) "$exist"
  $targetExisted = [bool](Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '$Hedef'"))
  $Tablolar = @($Tablolar | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  $unknown = @($Tablolar | Where-Object { $TABLES -notcontains $_ })
  if ($unknown) { Add-Result 'tablo listesi' $false "bilinmeyen: $($unknown -join ', ')" }
  if ($Tablolar -and $mode -ne 'gerial') {
    Add-Result "tablo ekleme: $Hedef önceden var ve aday_bulgular içeriyor" ($targetExisted -and ((Get-Tables $Hedef) -contains 'aday_bulgular')) "$Hedef"
    $existing = @(Get-Tables $Hedef)
    $protected = @($existing | Where-Object { $Tablolar -notcontains $_ })
    $already = @($Tablolar | Where-Object { $existing -contains $_ })
    Write-Host "Yazılacak: $($Tablolar -join ', ') | korunacak (dokunulmaz): $($protected -join ', ')$(if ($already) { " | hedefte zaten var: $($already -join ', ')" })" -ForegroundColor Cyan
  }
  $writePrivs = 'SELECT, INSERT, DELETE, CREATE, ALTER, INDEX, REFERENCES'
  $sql = ''
  if ($mode -ne 'gerial') { $sql += "CREATE DATABASE ``$PROVA_DB`` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;`n" }
  foreach ($h in $HOSTS) {
    $sql += "CREATE USER '$OKUR_USER'@'$h' IDENTIFIED BY '$okurPwd';`n"
    foreach ($p in $PROD + $TEST) { $sql += "GRANT SELECT, SHOW VIEW ON ``$(Esc $p)``.* TO '$OKUR_USER'@'$h';`n" }
    $sql += "GRANT EXECUTE ON PROCEDURE ``kardiyo_tam_metin``.``kaynak_bul`` TO '$OKUR_USER'@'$h';`n"
    $sql += "CREATE USER '$YUKLE_USER'@'$h' IDENTIFIED BY '$yuklePwd';`n"
    $sql += "GRANT SELECT ON ``kardiyo\_tam\_metin``.* TO '$YUKLE_USER'@'$h';`n"
    if ($mode -ne 'gerial') { $sql += "GRANT $writePrivs ON ``$(Esc $PROVA_DB)``.* TO '$YUKLE_USER'@'$h';`n" }
    if ($mode -eq 'uygula') { $sql += "GRANT $writePrivs ON ``$(Esc $Hedef)``.* TO '$YUKLE_USER'@'$h';`n" }
    elseif ($mode -eq 'prova' -and $Tablolar -and $targetExisted) { $sql += "GRANT SELECT ON ``$(Esc $Hedef)``.* TO '$YUKLE_USER'@'$h';`n" }
  }
  $usersCreated = $true; $provaCreated = ($mode -ne 'gerial')
  Invoke-My 'mysql.exe' $adminUser $adminPwd @() $sql | Out-Null
  $sql = $null
  Add-Result 'geçici kullanıcılar ve prova veritabanı oluşturuldu' $true "$OKUR_USER, $YUKLE_USER$(if ($provaCreated) { ", $PROVA_DB" })"

  $env:Y1_HOST = $dbHost; $env:Y1_PORT = $dbPort
  $env:Y1_ADMIN_USER = $adminUser; $env:Y1_ADMIN_PWD = $adminPwd
  $env:Y1_OKUR_USER = $OKUR_USER; $env:Y1_OKUR_PWD = $okurPwd
  $env:Y1_GERI_USER = $YUKLE_USER; $env:Y1_GERI_PWD = $yuklePwd; $env:Y1_GERI_DBS = '{}'
  $env:Y1_BEKLENEN_SATIR = "$BeklenenSatir"; $env:Y1_BEKLENEN_VERIFIED = "$BeklenenVerified"
  $env:PYTHONIOENCODING = 'utf-8'

  # 2) Üretim = Aşama 1 kaydı
  Add-Result 'ön kontrol: üretim Aşama 1 kaydıyla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)

  if ($mode -eq 'gerial') {
    $kayitPath = Join-Path (Resolve-Path -LiteralPath $GeriAlKlasoru).Path 'kayit.json'
    $kayit = Get-Content -LiteralPath $kayitPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Add-Result 'uygulama kaydı bu hedefe ait' ($kayit.veritabani -eq $Hedef) "$($kayit.veritabani)"
    Add-Result 'hedef bu aşamada oluşturulmuştu (önceden yoktu)' (-not $kayit.onceden_vardi) 'önceden var olan veritabanı silinmez'
    $tables = Get-Tables $Hedef
    Add-Result 'hedefte yalnız Aşama 5 tabloları var' (-not ($tables | Where-Object { $TABLES -notcontains $_ })) "$($tables -join ', ')"
    Invoke-My 'mysql.exe' $adminUser $adminPwd @('-e', "DROP DATABASE ``$Hedef``;") | Out-Null
    Add-Result "geri alma: $Hedef silindi" (-not (Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e', "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '$Hedef'")))
    Add-Result 'geri alma sonrası üretim Aşama 1 kaydıyla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)
    $exitCode = 0
  } else {
    $allowed = $(if ($mode -eq 'uygula') { "$PROVA_DB,$Hedef" } elseif ($Tablolar -and $targetExisted) { "$PROVA_DB,$Hedef" } else { $PROVA_DB })
    Add-Result 'yükleme kullanıcısı yetkileri (üretimde yalnız SELECT; DROP yok)' ((Invoke-Py ($COMMON + $PY_A5_YETKI) @($allowed)) -eq 0)

    # 3) İzole prova: prova veritabanına yükleme, tekrar yükleme (içerik aynı), kontrol, üretim değişmedi
    $o1 = Invoke-Yukle $PROVA_DB "$work\prova_ozet_1.json"
    Add-Result 'prova: yükleme' ($o1 -and $o1.sonuc -eq 'yazildi') "$(if ($o1) { $o1.satir | ConvertTo-Json -Compress })"
    $o2 = Invoke-Yukle $PROVA_DB "$work\prova_ozet_2.json"
    Add-Result 'prova: tekrar yükleme mükerrer oluşturmadı (içerik aynı)' ($o2 -and $o2.sonuc -eq 'icerik_ayni')
    Add-Result 'prova: tablo kontrolleri' ((Invoke-Py ($COMMON + $PY_A5_KONTROL) @($PROVA_DB)) -eq 0)
    if ($Tablolar) {
      Add-Result 'prova: korunacak tabloların içeriği hedefle birebir aynı' ((Invoke-Py ($COMMON + $PY_A5_KARSILASTIR) @($PROVA_DB, $Hedef, ($protected -join ','))) -eq 0)
      $o3 = Invoke-Yukle $PROVA_DB "$work\prova_ozet_3.json" $Tablolar
      Add-Result 'prova: yalnız istenen tablolarla yükleme (içerik aynı)' ($o3 -and $o3.sonuc -eq 'icerik_ayni')
    }
    Add-Result 'prova sonrası üretim Aşama 1 kaydıyla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)

    if ($mode -eq 'prova') {
      Add-Result 'PROVA: hedef veritabanına dokunulmadı' $true "$work\prova_ozet_1.json"
      $exitCode = 0
    } else {
      # 4) Uygulama
      if ($targetExisted) {
        $tables = Get-Tables $Hedef
        Add-Result "önceden var olan $Hedef yalnız Aşama 5 tablolarını içeriyor" (-not ($tables | Where-Object { $TABLES -notcontains $_ })) "$($tables -join ', ')"
      } else {
        Invoke-My 'mysql.exe' $adminUser $adminPwd @('-e', "CREATE DATABASE ``$Hedef`` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;") | Out-Null
      }
      [IO.File]::WriteAllText("$work\kayit.json", ([ordered]@{ veritabani = $Hedef; onceden_vardi = $targetExisted; zaman = $stamp;
                                                          tablolar = @($Tablolar); korunan = @($protected) } | ConvertTo-Json),
                              [System.Text.UTF8Encoding]::new($false))
      Add-Result "hedef veritabanı hazır ($(if ($targetExisted) { 'önceden vardı' } else { 'oluşturuldu' }))" $true $Hedef
      $before = Get-Checksums $Hedef $protected
      $u1 = Invoke-Yukle $Hedef "$work\ozet_1.json" $Tablolar
      Add-Result 'uygulama: yükleme (tek transaction; hata -> önceki yükleme korunur)' ($u1 -and $u1.sonuc -in @('yazildi', 'icerik_ayni')) "$(if ($u1) { "$($u1.sonuc); yazılan: $($u1.yazilan -join ', '); $($u1.satir | ConvertTo-Json -Compress)" })"
      if ($Tablolar) {
        Add-Result 'uygulama: yalnız istenen tablolar yazıldı' (-not (@($u1.yazilan) | Where-Object { $Tablolar -notcontains $_ })) "$($u1.yazilan -join ', ')"
        $after = Get-Checksums $Hedef $protected
        Add-Result "uygulama: korunan tablolar değişmedi (CHECKSUM: $($protected -join ', '))" ((($before -join '|') -eq ($after -join '|')) -and $before.Count -eq $protected.Count)
      }
      $u2 = Invoke-Yukle $Hedef "$work\ozet_2.json" $Tablolar
      Add-Result 'uygulama: tekrar yükleme mükerrer oluşturmadı (içerik aynı)' ($u2 -and $u2.sonuc -eq 'icerik_ayni')
      Add-Result 'uygulama: tablo kontrolleri' ((Invoke-Py ($COMMON + $PY_A5_KONTROL) @($Hedef)) -eq 0)
      Add-Result 'uygulama sonrası üretim Aşama 1 kaydıyla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)
      $exitCode = 0
    }
  }
}
catch {
  Write-Host "DURDURULDU: $($_.Exception.Message)" -ForegroundColor Red
  Write-Host 'Not: hiçbir veritabanı silinmedi; yükleme hatası transaction içinde geri alındı (önceki yükleme korunur).' -ForegroundColor Yellow
}
finally {
  if ($jsonBefore) {
    $jsonAfter = Get-JsonHashes
    $changed = @($jsonBefore.Keys | Where-Object { $jsonAfter[$_] -ne $jsonBefore[$_] }) + @($jsonAfter.Keys | Where-Object { -not $jsonBefore.ContainsKey($_) })
    $results.Add([pscustomobject]@{ Adim = 'JSON dosyaları değişmedi'; Sonuc = $(if (-not $changed) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "$($changed | Select-Object -First 5)" })
    if ($changed) { $exitCode = 1 }
  }
  try {
    $cleanup = ''
    if ($provaCreated -and $PROVA_DB -match $PROVA_RE) { $cleanup += "DROP DATABASE IF EXISTS ``$PROVA_DB``;`n" }
    if ($usersCreated) { foreach ($h in $HOSTS) { $cleanup += "DROP USER IF EXISTS '$OKUR_USER'@'$h', '$YUKLE_USER'@'$h';`n" } }
    if ($cleanup) { Invoke-My 'mysql.exe' $adminUser $adminPwd @() $cleanup | Out-Null }
    if ($usersCreated) {
      $left = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
        "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = '$PROVA_DB' UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$OKUR_USER','$YUKLE_USER')")
      $results.Add([pscustomobject]@{ Adim = 'geçici kullanıcı / prova veritabanı temizliği'; Sonuc = $(if (-not $left) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "kalan: $(if ($left) { $left -join ', ' } else { 'yok' })" })
      if ($left) { $exitCode = 1 }
    }
  } catch { Write-Host "TEMİZLİK UYARISI: $($_.Exception.Message)" -ForegroundColor Red; $exitCode = 1 }
  foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
  $okurPwd = $null; $yuklePwd = $null; $adminPwd = $null
  Write-Host "`n=== ÖZET ($mode)" -ForegroundColor Cyan
  $results | Format-Table -AutoSize
  Write-Host $(if ($exitCode -eq 0) { "SONUÇ: TAMAM ($mode). Kayıtlar: $work" } else { 'SONUÇ: BAŞARISIZ (yukarıdaki tabloya bakın)' }) `
    -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Red' })
}
exit $exitCode
