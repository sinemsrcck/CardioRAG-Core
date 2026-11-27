<#
Üretime geçiş — AŞAMA 4: RAG doğrulama görünümleri (sql/rag_dogrulama_core.sql) ve otomatik doğrulama sonuçlarının
kalite_isaretleri'ne tek parti olarak eklenmesi. Aday kayıtları SQL'e YAZILMAZ (tam içerik JSON aday dosyalarında kalır);
klinik_bulgular'a, review_status'a ve hoca görünümlerine dokunulmaz; verified oluşturulmaz.

Ön koşul sırası (proje kökünde; MySQL hizmeti açık; üretimde başka yazan oturum yok):
    # 0) test veritabanları / kullanıcıları silinmiş olmalı (Aşama 1 kaydı ile aynı durum)
    # 1) taze yedek + izole geri yükleme doğrulaması + mevcut durum kaydı
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama1_yedek.ps1
    # 2) Prova (varsayılan): yalnız izole prova veritabanında kurulum + parti + karşılaştırma + geri alma provası
    powershell -ExecutionPolicy Bypass -File scripts\uretim_asama4_rag.ps1 -Asama1Klasoru <A1> -DegerlendirmeKlasoru <D>
        [-SemantikSonuc <sonuclar.json> [-HashEsleme <hash_esleme.json>]]
    # 3) Uygulama (prova aynı çalıştırmada yeniden yapılır ve geçmelidir)
    ... aynı parametreler + -Uygula
    # Geri alma (bir uygulama klasörünün kayıtlarıyla; yalnız o geçişte eklenen parti ve görünümler)
    ... -Asama1Klasoru <A1> -DegerlendirmeKlasoru <D> -GeriAlKlasoru <A4 uygulama klasörü>
    # SONRAKİ PARTİLER (-SonrakiParti): iki görünüm kurulu olmalı ve YENİDEN KURULMAZ; önceki partiler korunur
    # (yalnız ekleme); aynı girdi dosyası (rapor / semantik sonuç SHA-256) ikinci kez yazılmaz. Kısmi semantik devamı:
    # yalnız yeni sonuç dosyası (-SemantikSonuc, -DegerlendirmeKlasoru olmadan); önceki kararlar silinmez, görünüm
    # aday başına en son tamamlanmış kararı gösterir.
    ... -Asama1Klasoru <A1> [-DegerlendirmeKlasoru <rapor klasörü>] [-SemantikSonuc <sonuclar.json>] -SonrakiParti [-Uygula]

Yeniden kullanılan yaklaşım (uretim_asama1_yedek.ps1 / uretim_asama2_enum.ps1 / uretim_asama3_sunum.ps1):
  - Aşama 1 PY_COMMON, PY_YETKI, PY_DURUM (durum / parmak izi / nesne ve yetki kaydı) ve PY_TEMIZLE (dökümün izole
    temizlenmiş kopyası) çalışma anında Aşama 1 betiğinden okunur; Aşama 1 yedeği manifest SHA-256, boyut, salt okunur ve
    izole geri yükleme raporuyla doğrulanır; yedek 24 saatten eski olamaz (src.rag_dogrulama.yedek_dogrula).
  - Ön kontrol: üretim Aşama 1 kaydıyla BİREBİR aynı (yedek güncel).
  - Prova: kök / yönetici hesabıyla DEĞİL; yalnız bu betiğin oluşturduğu y4prova_<zaman>_tm veritabanında yetkili geçici
    prova kullanıcısıyla (üretimi göremez; PY_YETKI doğrular). Yönetici yalnız geçici kullanıcı / veritabanı oluşturur ve
    siler. Provada: görünüm kurulumu, kalite partisi (önce ROLLBACK provası, sonra kalıcı), bütün tablolar ve görünümler
    üretimle karşılaştırılır (v_kalite_ozet / v_kalite_bulgu / sunum görünümleri dahil), yeni görünümün içeriği raporlarla
    karşılaştırılır, sonra geri alma provası prova veritabanını üretimle birebir aynı duruma döndürmelidir.
  - Uygulama: görünümler yönetici hesabıyla (DEFINER diğer görünümlerle aynı), parti `python -m src.rag_dogrulama
    kalite-yaz --uretim --yedek --kayit --yaz` ile (açık izin, doğrulanmış yedek, hedef veritabanı denetimi, tek
    transaction, commit ÖNCESİ önce / sonra eşitlik; geri alma kaydı commit'ten önce yazılır). Üretimde ROLLBACK'li
    prova yapılmaz (prova izole veritabanındadır).
  - Son kontrol (geçici okuma kullanıcısı): klinik_bulgular'ın bütün satırlarının bütün alanları, verified kayıtlar,
    sekiz sunum görünümü, kaynak_bul sonuçları, test veritabanları, şema / yetki kaydı Aşama 1 ile aynı; değişen yalnız
    kalite_isaretleri (eklenen = parti kaydı, diğer satırlar satır izi ile aynı) ve iki yeni görünüm.
  - Hata ya da fark: otomatik geri alma (yalnız bu partinin satırları, id + satır izi kayıtla birebir; yalnız bu geçişte
    oluşturulan iki görünüm) ve Aşama 1 kaydıyla eşitlik denetimi.
  - Hiçbir JSON (data/review, data/clinical, değerlendirme / semantik girdi dosyaları) değişmez; geçici kullanıcılar ve
    prova veritabanı finally'de silinir.
Çıkış kodu: 0 = geçti; 1 = başarısız (geri alındıysa da 1).
#>
param(
  [Parameter(Mandatory = $true)][string]$Asama1Klasoru,
  [string]$DegerlendirmeKlasoru,
  [string]$SemantikSonuc,
  [string]$HashEsleme,
  [switch]$Uygula,
  [switch]$SonrakiParti,
  [string]$GeriAlKlasoru,
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
$stamp = Get-Date -f yyyyMMdd_HHmmss
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 6)
$OKUR_USER = "y4_okur_$suffix"
$PROVA_USER = "y4_prova_$suffix"
# Prova veritabanları: adlarında üretim / test adı geçmez. Yalnız _tm oluşturulur; diğer adlar temizleme eşlemesi içindir.
$PROVA_DBS = [ordered]@{
  'kardiyo_tam_metin'     = "y4prova_${stamp}_tm"
  'kardiyo_clinical_data' = "y4prova_${stamp}_cd"
  'kardiyo_sunum'         = "y4prova_${stamp}_sn"
}
$PROVA_RE = '^y4prova_\d{8}_\d{6}_(tm|cd|sn)$'
$mode = if ($GeriAlKlasoru) { 'gerial' } elseif ($Uygula) { 'uygula' } else { 'prova' }

$dotenv = @{}
Get-Content .env | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $dotenv[$Matches[1]] = $Matches[2].Trim() }
$adminUser = if ($dotenv.MYSQL_USER) { $dotenv.MYSQL_USER } else { 'root' }
$adminPwd  = $dotenv.MYSQL_PASSWORD
$dbHost    = if ($dotenv.MYSQL_HOST) { $dotenv.MYSQL_HOST } else { 'localhost' }
$dbPort    = if ($dotenv.MYSQL_PORT) { $dotenv.MYSQL_PORT } else { '3306' }
$okurPwd  = [guid]::NewGuid().ToString('N') + 'Aa1!'
$provaPwd = [guid]::NewGuid().ToString('N') + 'Aa1!'

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
function Get-JsonHashes([string[]]$extra) {
  $files = @()
  foreach ($d in @('data\review', 'data\clinical') + $extra) {
    if (-not $d) { continue }
    if (Test-Path -LiteralPath $d -PathType Container) { $files += Get-ChildItem -LiteralPath $d -Recurse -File -Filter *.json }
    elseif (Test-Path -LiteralPath $d -PathType Leaf) { $files += Get-Item -LiteralPath $d }
  }
  $h = @{}
  foreach ($f in $files) { $h[$f.FullName] = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash }
  return $h
}
function Esc([string]$name) { $name -replace '_', '\_' }
function Get-Block([string]$text, [string]$name) { [regex]::Match($text, "(?s)\`$$name = @'\r?\n(.*?)\r?\n'@").Groups[1].Value }

# --- Gömülü Python (yalnız ASCII). Aşama 1 PY_COMMON / PY_YETKI / PY_DURUM / PY_TEMIZLE çalışma anında okunur. -----------
$PY_A4_ORTAK = @'
# Asama 4 ortak
from src import rag_dogrulama as V
from src.database import _split_sql_statements
SRC = 'kardiyo_tam_metin'
WORK = E['Y4_WORK']
KAYNAK = E['Y4_KAYNAK']                      # geri alma kayitlari (uygulama klasoru)
ON_PATH = os.path.join(KAYNAK, 'geri_alma', 'on_kayit.json')
KP_PATH = os.path.join(KAYNAK, 'geri_alma', 'kalite_parti.json')
VIEW_SQL = 'sql/rag_dogrulama_core.sql'
DURUM_SQL = 'sql/kontrol/rag_durum_sorgusu.sql'
NEW_VIEWS = ('v_rag_dogrulama_durumu', 'v_rag_ek_bilgi')
PROVA = GERI['kardiyo_tam_metin']
SONRAKI = bool(E.get('Y4_SONRAKI'))           # sonraki parti: gorunumler kurulu, yeniden kurulmaz
VIEW_COLS = 'satir_hash, belge_kimligi, otomatik_deterministik, semantik_karar, deterministik_parti, semantik_parti'


def view_statements():
    sts = _split_sql_statements(open(VIEW_SQL, encoding='utf-8').read())
    names = [(re.match(r'CREATE OR REPLACE VIEW (\w+) AS\b', s) or [None, None])[1] for s in sts]
    return sts, names


def inputs():
    reports = [E['Y4_RAPOR']] if E.get('Y4_RAPOR') else []
    return V.kalite_gruplari(reports, E.get('Y4_SEMANTIK') or None, E.get('Y4_ESLEME') or None)


def view_state(cur):
    """{satir_hash: [belge, deterministik, semantik, deterministik_parti, semantik_parti]}"""
    return {r[0]: list(r[1:]) for r in rows_of(cur, f'SELECT {VIEW_COLS} FROM v_rag_dogrulama_durumu')}


def check_view(after, before, groups, parti):
    """Beklenen gorunum: partideki belgelerin adaylari = bu partinin adaylari (deterministik ozet, parti); diger
    belgeler aynen; partideki semantik kararlar ayni satir_hash li adaya bu partiyle bagli; diger semantik kararlar
    (onceki partiler) aynen. Donus: sorun listesi."""
    det = {r['satir_hash']: (r['ayrinti']['belge_kimligi'], r['ayrinti']['genel_sonuc'])
           for r in groups.get(V.SURUM, []) if r['kontrol_kodu'] == 'rag_det_ozet'}
    docs = {b for b, _ in det.values()}
    sem = {r['satir_hash']: r['ayrinti']['karar'] for v, rows in groups.items() if v != V.SURUM for r in rows}
    want = {h for h, v in before.items() if v[0] not in docs} | set(det)
    problems = [] if set(after) == want else [('aday kumesi', len(after), len(want), sorted(set(after) ^ want)[:3])]
    for h, a in after.items():
        b = before.get(h)
        if h in det:
            if [a[0], a[1], a[3]] != [det[h][0], det[h][1], parti]:
                problems.append(('deterministik', h[:12], a))
        elif b is None or [a[0], a[1], a[3]] != [b[0], b[1], b[3]]:
            problems.append(('degismemeli', h[:12], a, b))
        if h in sem:
            if [a[2], a[4]] != [sem[h], parti]:
                problems.append(('semantik', h[:12], a))
        elif b is not None and [a[2], a[4]] != [b[2], b[4]]:
            problems.append(('onceki semantik karar kayboldu / degisti', h[:12], a, b))
    return problems


def current_and_semantic(groups):
    """(guncel aday hash kumesi, {hash: semantik karar} yalniz guncel adaylar icin)."""
    cur = {r['satir_hash'] for r in groups[V.SURUM] if r['kontrol_kodu'] == 'rag_det_ozet'}
    sem = {r['satir_hash']: r['ayrinti']['karar'] for v, rows in groups.items() if v != V.SURUM for r in rows
           if r['satir_hash'] in cur}
    return cur, sem


def rows_of(cur, sql, params=()):
    cur.execute(sql, params)
    return [tuple(txt(v) for v in r) for r in cur.fetchall()]
'@

$PY_A4_YEDEK = @'
# Dogrulanmis guncel yedek (manifest SHA-256 / boyut / salt okunur / izole geri yukleme raporu / son 24 saat)
import json, sys
from src import rag_dogrulama as V
info = V.yedek_dogrula(sys.argv[1])
print('TAMAM yedek dogrulandi', json.dumps(info, ensure_ascii=False, default=str))
'@

$PY_A4_PROVA = @'
# Izole prova: gecici prova veritabaninda (uretim kopyasi) kurulum, parti, karsilastirma ve geri alma provasi.
o = connect('okur', SRC)
oc = o.cursor()
g = connect('geri', PROVA, readonly=False)
gc = g.cursor()
good, info = whoami(gc, E['Y1_GERI_USER'], PROVA)
ok('prova baglantisi: gecici prova kullanicisi / prova veritabani (yonetici degil)', good, info)
stop_if_failed('prova baglantisi')
try:
    gc.execute('SELECT 1 FROM `kardiyo_tam_metin`.`klinik_bulgular` LIMIT 1')
    gc.fetchall()
    ok('prova kullanicisi uretimi okuyamaz', False, 'okuyabildi')
except mysql.connector.Error as exc:
    ok('prova kullanicisi uretimi okuyamaz', exc.errno in (1142, 1044), exc.errno)
g.rollback()
pt, pv = base_tables(oc, SRC)
gt, gv = base_tables(gc, PROVA)
prod_sums, prova_sums = table_sums(oc, SRC), table_sums(gc, PROVA)
ok('prova = uretim: tablo ve gorunum listesi', (pt, pv) == (gt, gv))
ok('prova = uretim: butun tablolarin CHECKSUM ve satir sayisi', prod_sums == prova_sums,
   [t for t in set(prod_sums) | set(prova_sums) if prod_sums.get(t) != prova_sums.get(t)][:5])
if SONRAKI:
    ok('sonraki parti: iki gorunum uretimde kurulu (yeniden kurulmaz)', set(NEW_VIEWS) <= set(pv))
else:
    ok('yeni gorunumler uretimde yok', not set(NEW_VIEWS) & set(pv))
prod_views = {v: view_hash(oc, SRC, v) for v in pv}
prova_views = {v: view_hash(gc, PROVA, v) for v in gv}
ok('prova = uretim: butun gorunumlerin satir sayisi ve icerigi', prod_views == prova_views,
   [v for v in pv if prod_views.get(v) != prova_views.get(v)][:5])
prod_rows = clinical(oc)['satir_ozetleri']
o.rollback(); o.close()
g.rollback()
stop_if_failed('prova kopyasi')


def prova_rows():
    gc.execute('SELECT * FROM klinik_bulgular ORDER BY id')
    return {str(dict(zip([d[0] for d in gc.description], r))['id']): row_hash(r) for r in gc.fetchall()}


# 1) Gorunumler (ilk geciste kurulur; sonraki partide kurulu olanlar kullanilir)
sts, names = view_statements()
ok('gorunum dosyasi: yalniz iki CREATE OR REPLACE VIEW', names == list(NEW_VIEWS), names)
stop_if_failed('gorunum dosyasi')
if not SONRAKI:
    for s in sts:
        gc.execute(s)
    g.commit()
before_view = view_state(gc)
before_izi = V._kalite_izi(gc)
g.rollback()
# 2) Kalite partisi: once ROLLBACK provasi, sonra kalici
groups, parti, kaynaklar = inputs()
exp = {v: len(r) for v, r in groups.items()}
current, want_sem = current_and_semantic(groups) if V.SURUM in groups else (set(), {})
print(f"BILGI parti {parti}; satir {exp}; partideki aday {len(current)}; semantik (partideki aday) {len(want_sem)}; "
      f"tamamlanmamis semantik (yazilmaz) {kaynaklar['semantik_yazilmayan_tamamlanmamis']}; "
      f"guncel aday disi semantik (gecmis olarak yazilir, gorunumde yok) {kaynaklar['semantik_guncel_aday_disi']}")
dry = V.kalite_yaz(g, groups, parti, yaz=False)
ok('kalite-yaz ROLLBACK provasi: eklenen = beklenen', len(dry['eklenen']) == sum(exp.values()), dry['surumler'])
ok('ROLLBACK provasi sonrasi kalite satirlari ayni (parti yok)', V._kalite_izi(gc) == before_izi)
g.rollback()
kayit = V.kalite_yaz(g, groups, parti, yaz=True)
ok('kalite-yaz kalici (yalniz prova veritabani)', kayit['kalici'] and len(kayit['eklenen']) == sum(exp.values()))
g.rollback()
# 3) Karsilastirma
sums1 = table_sums(gc, PROVA)
strip = lambda d: {k: v for k, v in d.items() if k != 'kalite_isaretleri'}
ok('kalite_isaretleri disindaki butun tablolar uretimle ayni', strip(sums1) == strip(prod_sums))
got = rows_of(gc, 'SELECT kontrol_surumu, COUNT(*), SUM(klinik_bulgu_id IS NOT NULL) FROM kalite_isaretleri '
                  'WHERE grup_id = %s GROUP BY 1', (parti,))
ok('yazilan = beklenen (surum bazinda, bu parti); klinik_bulgu_id NULL',
   {v: int(n) for v, n, _ in got} == exp and all(int(l) == 0 for _, _, l in got), got)
after_izi = V._kalite_izi(gc)
ok('onceki kalite satirlari (onceki partiler dahil) satir izi ile ayni',
   {i: h for i, h in after_izi.items() if str(i) not in kayit['eklenen']} == before_izi)
views1 = {v: view_hash(gc, PROVA, v) for v in gv}
ok('mevcut butun gorunumlerin icerigi ayni (v_kalite_ozet, v_kalite_bulgu, v_pilot_bulgular, v_sunum_taban dahil)',
   views1 == prova_views, [v for v in gv if views1[v] != prova_views[v]])
ok('klinik_bulgular butun satirlarin butun alanlari uretimle ayni', prova_rows() == prod_rows)
n, nd = rows_of(gc, 'SELECT COUNT(*), COUNT(DISTINCT satir_hash) FROM v_rag_dogrulama_durumu')[0]
problems = check_view(view_state(gc), before_view, groups, parti)
ok('v_rag_dogrulama_durumu: satir_hash basina bir satir; partideki belgeler = bu partinin adaylari; diger belgeler ve '
   'onceki semantik kararlar aynen', int(n) == int(nd) and not problems, problems[:5])
bad = rows_of(gc, "SELECT COUNT(*) FROM v_rag_dogrulama_durumu WHERE NOT ((insan_onayli <=> 1) = (insan_durumu <=> 'verified'))")[0][0]
ok('insan_onayli yalniz klinik_bulgular.review_status = verified', int(bad) == 0)
trace = set(rows_of(gc, 'SELECT DISTINCT aday_dosyasi, aday_dosyasi_sha256, deterministik_parti FROM v_rag_dogrulama_durumu '
                       'WHERE deterministik_parti = %s', (parti,)))
ok('aday dosyasi / SHA-256 / parti izlenebilir',
   trace == {(k['aday_dosyasi'], k['aday_dosyasi_sha256'], parti) for k in kaynaklar['raporlar']}, sorted(trace)[:3])
dagilim = rows_of(gc, 'SELECT otomatik_deterministik, semantik_karar, insan_durumu, COUNT(*) FROM v_rag_dogrulama_durumu '
                      'GROUP BY 1, 2, 3 ORDER BY 1, 2, 3')
for r in dagilim:
    print('  durum (deterministik, semantik, insan, sayi):', r)
for s in _split_sql_statements(open(DURUM_SQL, encoding='utf-8').read()):
    gc.execute(s)
    gc.fetchall()
guv = rows_of(gc, "SELECT COALESCE(SUM(klinik_bulgu_id IS NOT NULL), 0), COALESCE(SUM(grup_id = ''), 0) "
                  "FROM kalite_isaretleri WHERE kontrol_surumu LIKE %s", ('rag-%',))[0]
ok('sql/kontrol/rag_durum_sorgusu.sql calisti; bagli / partisiz otomatik satir yok', [int(x) for x in guv] == [0, 0], guv)
g.rollback()
# 4) Geri alma provasi: yalniz bu parti ve bu iki gorunum
res = V.kalite_geri_al(g, kayit, yaz=True)
ok('geri alma provasi: yalniz parti satirlari silindi', res['silinen'] == len(kayit['eklenen']), res)
if not SONRAKI:
    for v in NEW_VIEWS:
        gc.execute(f'DROP VIEW IF EXISTS `{v}`')
g.rollback()
ok('geri alma provasi: gorunum durumu partiden onceki ile ayni', view_state(gc) == before_view)
ok('geri alma provasi: prova veritabani uretimle birebir (tablolar)', table_sums(gc, PROVA) == prod_sums)
ok('geri alma provasi: gorunum listesi uretimle ayni', base_tables(gc, PROVA)[1] == pv)
g.rollback(); g.close()
write_new(os.path.join(WORK, 'prova_sonucu.json'),
          {'parti': parti, 'satir': exp, 'guncel_aday': len(current), 'semantik_guncel': len(want_sem),
           'kaynaklar': kaynaklar, 'durum_dagilimi': dagilim, 'kontroller_basarisiz': fail})
print('\nSONUC:', 'PROVA TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_A4_ON = @'
# Uygulama oncesi (yalniz okuma): yeni gorunumler yok, rag- satiri yok; kalite satir izi ve yonetici kaydi.
o = connect('okur', SRC)
oc = o.cursor()
good, info = whoami(oc, E['Y1_OKUR_USER'], SRC)
ok('okuma baglantisi gecici okuma kullanicisi', good, info)
stop_if_failed('baglanti')
found = rows_of(oc, 'SELECT TABLE_SCHEMA, TABLE_NAME FROM information_schema.TABLES WHERE TABLE_NAME IN (%s, %s) '
                    'AND TABLE_SCHEMA IN (%s, %s, %s)', NEW_VIEWS + PROD)
if SONRAKI:
    found = rows_of(oc, 'SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (%s, %s)',
                    (SRC,) + NEW_VIEWS)
    ok('sonraki parti: iki gorunum kurulu (yeniden kurulmaz)', len(found) == 2, found)
    view_before = view_state(oc)
else:
    ok('yeni gorunumler uretim veritabanlarinda yok', not found, found)
    n = rows_of(oc, 'SELECT COUNT(*) FROM kalite_isaretleri WHERE kontrol_surumu LIKE %s', ('rag-%',))[0][0]
    ok("uretimde rag- kalite satiri yok (ilk gecis; geri alma yalniz bu partiyi hedefler)", int(n) == 0, n)
    view_before = {}
izi = V._kalite_izi(oc)
o.rollback(); o.close()
a = connect('admin')
c = a.cursor()
c.execute('SELECT CURRENT_USER()')
me = str(txt(c.fetchone()[0]))
a.rollback(); a.close()
stop_if_failed('on kontrol')
os.makedirs(os.path.dirname(ON_PATH), exist_ok=True)
write_new(ON_PATH, {'yonetici': me, 'sonraki_parti': SONRAKI,
                    'olusturulacak_gorunumler': [] if SONRAKI else list(NEW_VIEWS),
                    'kalite_izi_once': {str(k): v for k, v in izi.items()}, 'gorunum_once': view_before})
print('SONUC: ON KONTROL TAMAM')
sys.exit(0)
'@

$PY_A4_SONRA = @'
# Uygulama / geri alma sonrasi: Asama 1 kaydiyla karsilastirma (yalniz okuma). Degisebilen: uygulamada yalniz
# kalite_isaretleri (eklenen = parti kaydi) ve iki yeni gorunum; geri almada hicbir sey.
MOD = E['Y4_MOD']
ON = json.load(open(ON_PATH, encoding='utf-8'))
K = json.load(open(KP_PATH, encoding='utf-8')) if MOD == 'uygula' else None
NORM = lambda s: re.sub(r' AUTO_INCREMENT=\d+', '', s or '')
before, bmeta = json.load(open(STATE, encoding='utf-8')), json.load(open(META, encoding='utf-8'))
o = connect('okur', SRC)
oc = o.cursor()
good, info = whoami(oc, E['Y1_OKUR_USER'], SRC)
ok('okuma baglantisi gecici okuma kullanicisi', good, info)
stop_if_failed('baglanti')
meta = admin_meta()
state = {'uretim_parmak_izi': fingerprints(oc, PROD), 'test_parmak_izi': fingerprints(oc, TEST),
         'klinik_bulgular': clinical(oc), 'sunum_gorunumleri': views8(oc)}
izi = V._kalite_izi(oc)
view_rows = rows_of(oc, 'SELECT satir_hash, insan_durumu, insan_onayli, aday_dosyasi_sha256, deterministik_parti '
                        'FROM v_rag_dogrulama_durumu') if MOD == 'uygula' else []
view_after = view_state(oc) if MOD == 'uygula' else None
o.rollback(); o.close()
state['kaynak_bul'] = source_search(meta)
state, meta = json.loads(json.dumps(state, default=str)), json.loads(json.dumps(meta, default=str))
write_new(os.path.join(WORK, f'sonra_durum_{MOD}.json'), state)
kb0, kb1 = before['klinik_bulgular'], state['klinik_bulgular']
changed = [i for i in set(kb0['satir_ozetleri']) | set(kb1['satir_ozetleri']) if kb0['satir_ozetleri'].get(i) != kb1['satir_ozetleri'].get(i)]
ok('klinik_bulgular butun satirlarin butun alanlari ayni', not changed and kb0['satir_sayisi'] == kb1['satir_sayisi'],
   f"{kb1['satir_sayisi']} satir; farkli {len(changed)}")
ok('verified pilot kayitlari ayni', kb0['verified_pilot'] == kb1['verified_pilot'], kb1['verified_pilot_sayisi'])
ok('klinik_bulgular sutunlari ve review_status dagilimi ayni', kb0['sutunlar'] == kb1['sutunlar'] and kb0['review_status'] == kb1['review_status'])
ok('sekiz sunum gorunumunun satir sayisi ve icerigi ayni', before['sunum_gorunumleri'] == state['sunum_gorunumleri'])
ok('kaynak_bul sonuclari ayni', before['kaynak_bul'] == state['kaynak_bul'])
ok('test veritabanlari ayni', before['test_parmak_izi'] == state['test_parmak_izi'])
for s in PROD:
    t0, t1 = before['uretim_parmak_izi'][s]['tablolar'], state['uretim_parmak_izi'][s]['tablolar']
    allowed = {'kalite_isaretleri'} if (s == SRC and MOD == 'uygula') else set()
    diff = sorted(n for n in set(t0) | set(t1) if t0.get(n) != t1.get(n))
    ok(f'{s}: tablolarin CHECKSUM / satir sayisi ayni (degisebilen: {sorted(allowed) or "yok"})', set(diff) <= allowed, diff)
    v0, v1 = before['uretim_parmak_izi'][s]['gorunumler'], state['uretim_parmak_izi'][s]['gorunumler']
    new = set(ON['olusturulacak_gorunumler']) if (s == SRC and MOD == 'uygula') else set()
    ok(f'{s}: mevcut gorunum tanimlari ayni; eklenen yalniz {sorted(new) or "yok"}',
       set(v1) == set(v0) | new and all(v1[n] == v0[n] for n in v0), sorted(set(v1) ^ set(v0)))
for key in ('sunucu', 'kullanicilar', 'procs_priv', 'tables_priv'):
    ok(f'{key} ayni', bmeta[key] == meta[key])
ok('nesne sahipleri: yeni sahip yok (yalniz yonetici), hepsi mevcut',
   set(meta['sahipler']) <= set(bmeta['sahipler']) | {ON['yonetici']} and all(meta['sahipler'].values()), meta['sahipler'])
for s in PROD:
    d0, d1 = bmeta['veritabanlari'][s], meta['veritabanlari'][s]
    ok(f'{s}: tablo tanimlari ayni (AUTO_INCREMENT haric)',
       {k: NORM(v) for k, v in d0['tablolar'].items()} == {k: NORM(v) for k, v in d1['tablolar'].items()})
    for kind in ('rutinler', 'tetikleyiciler', 'olaylar'):
        ok(f'{s}: {kind} ayni', d0[kind] == d1[kind])
    new = set(ON['olusturulacak_gorunumler']) if (s == SRC and MOD == 'uygula') else set()
    ok(f'{s}: mevcut gorunumler (tanim / sahip / SQL SECURITY) ayni',
       {k: v for k, v in d1['gorunumler'].items() if k not in new} == d0['gorunumler'] and new <= set(d1['gorunumler']))
    for n in sorted(new):
        x = d1['gorunumler'][n]
        ok(f'{s}.{n} olusturuldu (sahibi {ON["yonetici"]}, DEFINER)', x['definer'] == ON['yonetici'] and x['guvenlik'] == 'DEFINER')
once = {int(k): v for k, v in ON['kalite_izi_once'].items()}
if MOD == 'uygula':
    added = {int(k): v for k, v in K['eklenen'].items()}
    ok('kalite_isaretleri: onceki satirlar ayni (satir izi), eklenenler = parti kaydi', not (set(added) & set(once)) and izi == {**once, **added},
       f'once {len(once)}, eklenen {len(added)}, simdi {len(izi)}')
    groups, _, kaynaklar = inputs()
    problems = check_view(view_after, ON['gorunum_once'], groups, K['parti'])
    ok('v_rag_dogrulama_durumu: partideki belgeler = bu partinin adaylari; diger belgeler ve onceki semantik kararlar aynen',
       not problems and len(view_rows) == len(view_after), problems[:5])
    ok('v_rag_dogrulama_durumu: bu partinin aday dosyasi SHA-256 = rapor kaynaklari',
       {r[3] for r in view_rows if r[4] == K['parti']} == {k['aday_dosyasi_sha256'] for k in kaynaklar['raporlar']})
    ok('insan_onayli yalniz klinik_bulgular.review_status = verified',
       all((str(r[2]) == '1') == (r[1] == 'verified') for r in view_rows))
else:
    ok('kalite_isaretleri: Asama 4 oncesi ile birebir (satir izi)', izi == once, f'once {len(once)}, simdi {len(izi)}')
print('\nSONUC:', 'SON KONTROL TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

$PY_A4_GERIAL = @'
# Geri alma: yalniz bu gecisin partisi (id + satir izi kayitla birebir) ve bu gecisin olusturdugu iki gorunum.
ON = json.load(open(ON_PATH, encoding='utf-8'))
a = connect('admin', SRC, readonly=False)
c = a.cursor()
c.execute('SELECT CURRENT_USER()')
ok('yonetici hesabi on kayittaki ile ayni', str(txt(c.fetchone()[0])) == ON['yonetici'])
stop_if_failed('yonetici')
a.rollback()
if os.path.exists(KP_PATH):
    K = json.load(open(KP_PATH, encoding='utf-8'))
    c.execute('SELECT COUNT(*) FROM kalite_isaretleri WHERE grup_id = %s', (K['parti'],))
    n = c.fetchone()[0]
    a.rollback()
    if n == 0:
        print(f"BILGI parti {K['parti']} uretimde yok (commit edilmemis); silinecek satir yok")
    else:
        res = V.kalite_geri_al(a, K, yaz=True, uretim=True)
        ok(f"kalite partisi geri alindi: {res['silinen']} satir", res['silinen'] == len(K['eklenen']), res)
else:
    c.execute('SELECT COUNT(*) FROM kalite_isaretleri WHERE kontrol_surumu LIKE %s', ('rag-%',))
    ok('parti kaydi yok ve uretimde rag- satiri yok', c.fetchone()[0] == 0)
    a.rollback()
for v in ON['olusturulacak_gorunumler']:
    c.execute('SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s', (SRC, v))
    if c.fetchone()[0]:
        c.execute(f'DROP VIEW `{SRC}`.`{v}`')          # on kontrolde yoktu: yalniz bu gecis olusturdu
c.execute('SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN (%s, %s)',
          (SRC, *ON['olusturulacak_gorunumler']))
ok('bu gecisin gorunumleri silindi', c.fetchone()[0] == 0)
a.rollback(); a.close()
print('\nSONUC:', 'GERI ALMA TAMAM' if not fail else f'{len(fail)} KONTROL BASARISIZ: {fail}')
sys.exit(1 if fail else 0)
'@

# =================================================================================================
$saved = @{}
foreach ($n in 'MYSQL_PWD', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE', 'MYSQL_HOST', 'MYSQL_PORT',
               'Y1_HOST', 'Y1_PORT', 'Y1_ADMIN_USER', 'Y1_ADMIN_PWD', 'Y1_OKUR_USER', 'Y1_OKUR_PWD', 'Y1_GERI_USER',
               'Y1_GERI_PWD', 'Y1_GERI_DBS', 'Y1_BEKLENEN_SATIR', 'Y1_BEKLENEN_VERIFIED', 'Y4_WORK', 'Y4_KAYNAK',
               'Y4_RAPOR', 'Y4_SEMANTIK', 'Y4_ESLEME', 'Y4_MOD', 'Y4_YEDEK_DIR', 'Y4_SONRAKI', 'PYTHONIOENCODING') {
  $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process')
}
$usersCreated = $false; $dbCreated = $false; $jsonBefore = $null; $work = $null
$exitCode = 1
try {
  if ($GeriAlKlasoru -and $Uygula) { Add-Result 'parametreler' $false '-Uygula ve -GeriAlKlasoru birlikte kullanılamaz' }
  if ($HashEsleme -and -not $SemantikSonuc) { Add-Result 'parametreler' $false '-HashEsleme yalnız -SemantikSonuc ile' }
  if (-not $DegerlendirmeKlasoru -and -not $SemantikSonuc) { Add-Result 'parametreler' $false '-DegerlendirmeKlasoru ya da -SemantikSonuc gerekli' }
  if (-not $DegerlendirmeKlasoru -and -not $SonrakiParti) { Add-Result 'parametreler' $false 'yalnız semantik parti yalnız -SonrakiParti ile' }
  Write-Host "PARTİ: $(if ($SonrakiParti) { 'sonraki (görünümler yeniden kurulmaz)' } else { 'ilk geçiş' })" -ForegroundColor Cyan
  Write-Host "MOD: $mode" -ForegroundColor Cyan

  # 0) Ön koşullar: hizmet, .env hedefi, Aşama 1 yedeği ve kayıtları, girdiler, önceki aşama blokları
  $svc = Get-Service MYSQL80 -ErrorAction SilentlyContinue
  Add-Result 'MySQL hizmeti çalışıyor' ($svc -and $svc.Status -eq 'Running') "$($svc.Status)"
  Add-Result '.env hedefi üretim kaynak veritabanı (kardiyo_tam_metin)' ($dotenv.MYSQL_DATABASE -eq 'kardiyo_tam_metin') "$($dotenv.MYSQL_DATABASE)"
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
  foreach ($f in 'mevcut_durum.json', 'nesneler_ve_yetkiler.json') {
    if (-not (Test-Path -LiteralPath (Join-Path $a1 $f))) { Add-Result 'Aşama 1 durum kaydı mevcut' $false $f }
  }
  $deg = if ($DegerlendirmeKlasoru) { (Resolve-Path -LiteralPath $DegerlendirmeKlasoru -Relative) } else { '' }
  $inputs = @($deg | Where-Object { $_ })
  foreach ($f in @($SemantikSonuc, $HashEsleme) | Where-Object { $_ }) {
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Add-Result 'girdi dosyası mevcut' $false $f }
    $inputs += $f
  }
  if ($deg) {
    $reports = @(Get-ChildItem -LiteralPath $deg -Filter '*.dogrulama.json')
    Add-Result 'doğrulama raporları mevcut' ($reports.Count -gt 0) "$($reports.Count) rapor"
  }
  if (-not (Test-Path -LiteralPath $A1SCRIPT)) { Add-Result 'Aşama 1 betiği' $false $A1SCRIPT }
  $a1raw = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $A1SCRIPT).Path)
  $A1_COMMON = Get-Block $a1raw 'PY_COMMON'; $A1_YETKI = Get-Block $a1raw 'PY_YETKI'
  $A1_DURUM = Get-Block $a1raw 'PY_DURUM'; $A1_TEMIZLE = Get-Block $a1raw 'PY_TEMIZLE'
  $marker = "`no = connect('okur')"
  $srcLine = "src_dir, dst_dir = os.path.join(work, 'yedek'), os.path.join(work, 'geri_yukleme')"
  $hits = ([regex]::Matches($A1_DURUM, [regex]::Escape($marker))).Count
  $tHits = ([regex]::Matches($A1_TEMIZLE, [regex]::Escape($srcLine))).Count
  Add-Result 'Aşama 1 PY_COMMON / PY_YETKI / PY_DURUM / PY_TEMIZLE okundu' ($A1_COMMON -and $A1_YETKI -and $A1_DURUM -and $hits -eq 1 -and $tHits -eq 1) "işaret $hits / $tHits"
  $A1_DEFS = $A1_DURUM.Substring(0, $A1_DURUM.IndexOf($marker))
  # Temizleme: aynı kod; kaynak Aşama 1 yedeği, hedef bu çalışmanın prova_yukleme klasörü (prova veritabanı adları)
  $TEMIZLE = $A1_TEMIZLE.Replace($srcLine, "src_dir, dst_dir = E['Y4_YEDEK_DIR'], os.path.join(work, 'prova_yukleme')")
  $COMMON = $A1_COMMON + "`n"
  $jsonBefore = Get-JsonHashes $inputs
  Add-Result 'JSON dosyalarının SHA-256 kaydı (data\review, data\clinical, girdiler)' ($jsonBefore.Count -gt 0) "$($jsonBefore.Count) dosya"
  $work = "$a1`_asama4_${mode}_$stamp"
  Add-Result 'çalışma klasörü yeni' (-not (Test-Path -LiteralPath $work)) $work
  New-Item -ItemType Directory -Path $work | Out-Null
  if ($mode -ne 'gerial') { New-Item -ItemType Directory -Path "$work\prova_yukleme" | Out-Null }
  $kaynak = if ($mode -eq 'gerial') { (Resolve-Path -LiteralPath $GeriAlKlasoru).Path } else { $work }
  if ($mode -eq 'gerial' -and -not (Test-Path -LiteralPath (Join-Path $kaynak 'geri_alma\on_kayit.json'))) {
    Add-Result 'geri alma kaydı mevcut' $false "$kaynak\geri_alma\on_kayit.json"
  }

  # 1) Geçici kullanıcılar (okuma; provada ayrıca yalnız prova veritabanında yetkili prova kullanıcısı) ve prova veritabanı
  foreach ($g in $PROVA_DBS.Values) { if ($g -notmatch $PROVA_RE) { Add-Result 'prova veritabanı adı biçimi' $false $g } }
  $names = ($PROVA_DBS.Values | ForEach-Object { "'$_'" }) -join ','
  $exist = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
    "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ($names) UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$OKUR_USER','$PROVA_USER')")
  Add-Result 'geçici veritabanı / kullanıcı önceden yok' (-not $exist) "$exist"
  $provaTm = $PROVA_DBS['kardiyo_tam_metin']
  $provaPrivs = 'SELECT, INSERT, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES, CREATE VIEW, SHOW VIEW, CREATE ROUTINE, ALTER ROUTINE, EXECUTE, TRIGGER, LOCK TABLES'
  $sql = ''
  if ($mode -ne 'gerial') { $sql += "CREATE DATABASE ``$provaTm`` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;`n" }
  foreach ($h in $HOSTS) {
    $sql += "CREATE USER '$OKUR_USER'@'$h' IDENTIFIED BY '$okurPwd';`n"
    foreach ($p in $PROD + $TEST) { $sql += "GRANT SELECT, SHOW VIEW ON ``$(Esc $p)``.* TO '$OKUR_USER'@'$h';`n" }
    $sql += "GRANT EXECUTE ON PROCEDURE ``kardiyo_tam_metin``.``kaynak_bul`` TO '$OKUR_USER'@'$h';`n"
    if ($mode -ne 'gerial') {
      $sql += "CREATE USER '$PROVA_USER'@'$h' IDENTIFIED BY '$provaPwd';`n"
      $sql += "GRANT $provaPrivs ON ``$(Esc $provaTm)``.* TO '$PROVA_USER'@'$h';`n"
    }
  }
  $usersCreated = $true; $dbCreated = ($mode -ne 'gerial')
  Invoke-My 'mysql.exe' $adminUser $adminPwd @() $sql | Out-Null
  $sql = $null
  Add-Result 'geçici kullanıcılar ve prova veritabanı oluşturuldu (nesne sahibi olmazlar)' $true "$OKUR_USER$(if ($mode -ne 'gerial') { ", $PROVA_USER, $provaTm" })"

  $env:Y1_HOST = $dbHost; $env:Y1_PORT = $dbPort
  $env:Y1_ADMIN_USER = $adminUser; $env:Y1_ADMIN_PWD = $adminPwd
  $env:Y1_OKUR_USER = $OKUR_USER; $env:Y1_OKUR_PWD = $okurPwd
  if ($mode -ne 'gerial') { $env:Y1_GERI_USER = $PROVA_USER; $env:Y1_GERI_PWD = $provaPwd }
  else { $env:Y1_GERI_USER = $OKUR_USER; $env:Y1_GERI_PWD = $okurPwd }
  $env:Y1_GERI_DBS = ($PROVA_DBS | ConvertTo-Json -Compress)
  $env:Y1_BEKLENEN_SATIR = "$BeklenenSatir"; $env:Y1_BEKLENEN_VERIFIED = "$BeklenenVerified"
  $env:Y4_WORK = $work; $env:Y4_KAYNAK = $kaynak; $env:Y4_RAPOR = $deg; $env:Y4_MOD = $mode
  $env:Y4_SEMANTIK = $SemantikSonuc; $env:Y4_ESLEME = $HashEsleme; $env:Y4_YEDEK_DIR = (Join-Path $a1 'yedek')
  $env:Y4_SONRAKI = $(if ($SonrakiParti) { '1' } else { '' })
  $env:PYTHONIOENCODING = 'utf-8'
  $FULL_CHECK = $COMMON + $A1_DURUM
  $ORTAK = $COMMON + $A1_DEFS + "`n" + $PY_A4_ORTAK + "`n"
  $yedekTm = Join-Path $a1 'yedek\kardiyo_tam_metin.sql'

  if ($mode -eq 'gerial') {
    Add-Result 'geri alma (yalnız bu geçişin partisi ve görünümleri)' ((Invoke-Py ($ORTAK + $PY_A4_GERIAL) @('sonra', $a1)) -eq 0)
    Add-Result 'geri alma sonrası Aşama 1 kaydıyla eşitlik' ((Invoke-Py ($ORTAK + $PY_A4_SONRA) @('sonra', $a1)) -eq 0)
    $exitCode = 0
  } else {
    # 2) Yetki izolasyonu, üretim = Aşama 1 kaydı, doğrulanmış güncel yedek
    Add-Result 'geçici kullanıcı yetkileri (okuma / yalnız prova veritabanı)' ((Invoke-Py ($COMMON + $A1_YETKI)) -eq 0)
    Add-Result 'ön kontrol: üretim Aşama 1 kaydıyla birebir aynı' ((Invoke-Py $FULL_CHECK @('sonra', $a1)) -eq 0)
    Add-Result 'doğrulanmış güncel yedek (manifest, salt okunur, geri yükleme raporu, < 24 saat)' ((Invoke-Py $PY_A4_YEDEK @($yedekTm)) -eq 0)

    # 3) İzole prova: temizlenmiş kopya -> prova veritabanı (yalnız prova kullanıcısı) -> kurulum, parti, geri alma provası
    Add-Result 'temizlenmiş kopya izole (USE / CREATE DATABASE / olay / DEFINER / üretim-test adı yok)' ((Invoke-Py ($COMMON + $TEMIZLE) @($work)) -eq 0)
    $file = ((Resolve-Path -LiteralPath "$work\prova_yukleme\kardiyo_tam_metin.sql").Path -replace '\\', '/')
    Invoke-My 'mysql.exe' $PROVA_USER $provaPwd @('--default-character-set=utf8mb4', "--database=$provaTm", '-e', "SOURCE $file") | Out-Null
    Add-Result "prova veritabanına yüklendi (prova kullanıcısı): $provaTm" $true
    Add-Result 'izole prova: görünümler, parti, karşılaştırma, geri alma provası' ((Invoke-Py ($ORTAK + $PY_A4_PROVA) @('sonra', $a1)) -eq 0)

    if ($mode -eq 'prova') {
      Add-Info 'PROVA: üretimde değişiklik yapılmadı' $true "$work\prova_sonucu.json"
      $exitCode = 0
    } else {
      # 4) Uygulama: ön kayıt -> görünümler (yönetici) -> parti (kalite-yaz --uretim) -> son kontrol
      Add-Result 'ön kontrol ve geri alma ön kaydı' ((Invoke-Py ($ORTAK + $PY_A4_ON) @('sonra', $a1)) -eq 0)
      $ok1 = $false; $ok2 = $false; $ok3 = $false
      if ($SonrakiParti) {
        $ok1 = $true
        Add-Info 'sonraki parti: görünümler kurulu, yeniden kurulmadı' $ok1
      } else {
        try {
          $viewFile = ((Resolve-Path -LiteralPath 'sql\rag_dogrulama_core.sql').Path -replace '\\', '/')
          Invoke-My 'mysql.exe' $adminUser $adminPwd @('--default-character-set=utf8mb4', '--database=kardiyo_tam_metin', '-e', "SOURCE $viewFile") | Out-Null
          $ok1 = $true
        } catch { Write-Host "HATA görünüm kurulumu: $($_.Exception.Message)" -ForegroundColor Red }
        Add-Info 'kurulum: sql/rag_dogrulama_core.sql (yönetici, kardiyo_tam_metin)' $ok1
      }
      if ($ok1) {
        $env:MYSQL_USER = $adminUser; $env:MYSQL_PASSWORD = $adminPwd; $env:MYSQL_HOST = $dbHost; $env:MYSQL_PORT = $dbPort
        $env:MYSQL_DATABASE = 'kardiyo_tam_metin'
        $kArgs = @('-m', 'src.rag_dogrulama', 'kalite-yaz')
        if ($deg) { $kArgs += @('--rapor', $deg) }
        if ($SemantikSonuc) { $kArgs += @('--semantik', $SemantikSonuc) }
        if ($HashEsleme) { $kArgs += @('--hash-esleme', $HashEsleme) }
        $kArgs += @('--uretim', '--yedek', $yedekTm, '--kayit', "$work\geri_alma\kalite_parti.json", '--yaz')
        & $py @kArgs | Out-Host; $ok2 = ($LASTEXITCODE -eq 0)
        foreach ($n in 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE', 'MYSQL_HOST', 'MYSQL_PORT') {
          [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process')
        }
        Add-Info 'kalite partisi (kalite-yaz --uretim --yedek --kayit --yaz)' $ok2
      }
      if ($ok2) {
        $ok3 = ((Invoke-Py ($ORTAK + $PY_A4_SONRA) @('sonra', $a1)) -eq 0)
        Add-Info 'son kontrol (klinik satırlar, verified, 8 görünüm, kaynak_bul, nesneler, yetkiler, kalite satır izi)' $ok3
      }
      if (-not ($ok1 -and $ok2 -and $ok3)) {
        Add-Info 'beklenmeyen hata / fark' $false 'otomatik geri alma başlıyor'
        $env:Y4_MOD = 'gerial'
        $g1 = Invoke-Py ($ORTAK + $PY_A4_GERIAL) @('sonra', $a1)
        $g2 = Invoke-Py ($ORTAK + $PY_A4_SONRA) @('sonra', $a1)
        Add-Info 'otomatik geri alma ve Aşama 1 kaydıyla eşitlik' ($g1 -eq 0 -and $g2 -eq 0) $(if ($g1 -eq 0 -and $g2 -eq 0) { 'üretim Aşama 4 öncesi duruma döndü' } else { "GERİ ALINAMADI: $work\geri_alma kayıtları ve Aşama 1 yedeği ile elle geri dönüş gerekir" })
        throw 'Aşama 4 geri alındı ya da geri alma gerekli'
      }
      Add-Result 'Aşama 4 uygulaması ve son kontrol' $true "geri alma kaydı: $work\geri_alma"
      $exitCode = 0
    }
  }
}
catch {
  Write-Host "DURDURULDU: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
  if ($jsonBefore) {
    $jsonAfter = Get-JsonHashes $inputs
    $changed = @($jsonBefore.Keys | Where-Object { $jsonAfter[$_] -ne $jsonBefore[$_] }) + @($jsonAfter.Keys | Where-Object { -not $jsonBefore.ContainsKey($_) })
    $results.Add([pscustomobject]@{ Adim = 'JSON dosyaları değişmedi'; Sonuc = $(if (-not $changed) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "$($changed | Select-Object -First 5)" })
    if ($changed) { $exitCode = 1 }
  }
  try {
    $cleanup = ''
    if ($dbCreated) { foreach ($g in $PROVA_DBS.Values) { if ($g -match $PROVA_RE) { $cleanup += "DROP DATABASE IF EXISTS ``$g``;`n" } } }
    if ($usersCreated) { foreach ($h in $HOSTS) { $cleanup += "DROP USER IF EXISTS '$OKUR_USER'@'$h', '$PROVA_USER'@'$h';`n" } }
    if ($cleanup) { Invoke-My 'mysql.exe' $adminUser $adminPwd @() $cleanup | Out-Null }
    if ($usersCreated -or $dbCreated) {
      $left = Invoke-My 'mysql.exe' $adminUser $adminPwd @('-N', '-B', '-e',
        "SELECT CONCAT('db:',SCHEMA_NAME) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME IN ($names) UNION ALL SELECT CONCAT('user:',User,'@',Host) FROM mysql.user WHERE User IN ('$OKUR_USER','$PROVA_USER')")
      $results.Add([pscustomobject]@{ Adim = 'geçici kullanıcı / prova veritabanı temizliği'; Sonuc = $(if (-not $left) { 'GECTI' } else { 'BASARISIZ' }); Bilgi = "kalan: $(if ($left) { $left -join ', ' } else { 'yok' })" })
      if ($left) { $exitCode = 1 }
    }
  } catch { Write-Host "TEMİZLİK UYARISI: $($_.Exception.Message)" -ForegroundColor Red; $exitCode = 1 }
  foreach ($n in $saved.Keys) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
  $okurPwd = $null; $provaPwd = $null; $adminPwd = $null
  Write-Host "`n=== ÖZET ($mode)" -ForegroundColor Cyan
  $results | Format-Table -AutoSize
  Write-Host $(if ($exitCode -eq 0) { "SONUÇ: TAMAM ($mode). Kayıtlar: $work" } else { 'SONUÇ: BAŞARISIZ (yukarıdaki tabloya bakın)' }) `
    -ForegroundColor $(if ($exitCode -eq 0) { 'Green' } else { 'Red' })
}
exit $exitCode
