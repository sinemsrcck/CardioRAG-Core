"""Toplu RAG makale işleme: mevcut modüllerle tek akış (yeni çıkarım / doğrulama motoru yok).

Adımlar (makale başına durum tutulur; her komut kaldığı yerden sürer):
  plan            API YOK, varsayılan veritabanı YOK. PDF belge kimliği (SHA-256), mükerrerlik (aynı içerik bu listede,
                  önceki toplu çalıştırmalarda; --sql-kontrol ile üretimde salt okunur: doğrulama partisi / pdf_belgeleri),
                  çıkarım görev planı ve önbellekte olmayan çağrı sayısı (src.rag_klinik_cikarim.run plan_only).
  cikarim         src.rag_klinik_cikarim.run (--sinir --tablo ile aynı; önbellek data/rag_cikarim/<pdf>/llm_onbellek.jsonl).
                  GERÇEK API: --api-onay ve --cikarim-butce zorunlu. Makalenin bekleyen çağrısı kalan bütçeye sığmazsa
                  makale 'butce_yetersiz' kalır (hiç çağrı yapılmaz), sıradaki makaleye geçilir. Hata: hatalar.jsonl;
                  tamamlanan çağrılar önbellekte kalır, tekrar çalıştırmada harcanmaz.
  taslak          python -m src.rag_aktarim taslak --dogrulama-raporu: pending taslak + bağlı deterministik rapor. Mevcut
                  SQL kayıtlarıyla tekrar kontrolü yalnız SELECT (--sql-okuma-yok ile kapatılır). Bütün adaylar pending.
  semantik-hazirla scripts/rag_semantik_pilot.py toplu-hazirla (API yok; varsayılan semantik_dogrulama_gerekli adaylar).
  semantik        scripts/rag_semantik_pilot.py calistir. GERÇEK API: --api-onay ve --semantik-butce zorunlu; kısmi
                  bütçe, önbellek, kaldığı yerden devam. Her çalıştırmadan sonra sonuçların değişmez kopyası
                  (sonuclar_<zaman>.json) yazılır; önceki kopyadaki tamamlanmış bir karar kaybolur ya da değişirse kopya
                  yazılmaz ve hata verilir.
  ingestion       İsteğe bağlı; yalnız insan onaylı aktarımdan (rag_aktarim aktar) önce gerekir. ÜRETİM YAZIMI:
                  --uretim-onay ve --yedek (doğrulanmış güncel Aşama 1 yedeği) zorunlu. pdf_belgeleri'de olmayan PDF'ler
                  src.ingest.ingest_pdf ile (init_schema ÇALIŞTIRILMAZ); commit ÖNCESİ klinik_bulgular, kalite_isaretleri
                  ve sekiz sunum görünümü önce = sonra, değilse ROLLBACK.
  sql-komutlari   Doğrulama sonuçlarının üretim yazımı bu betikte YAPILMAZ: Aşama 1 + Aşama 4 -SonrakiParti komutlarını
                  yazdırır (izole prova; açık -Uygula). Aynı girdi ikinci kez yazılamaz (kalite-yaz).
  durum           Makale / adım durumu.

Durum: data/rag_toplu/<ad>/durum.json (atomik yazım), hata kaydı: data/rag_toplu/<ad>/hatalar.jsonl.
Hiçbir aday verified yapılmaz; klinik_bulgular'a ve hoca görünümlerine yazılmaz.

    python scripts/rag_toplu_isleme.py plan --ad <parti> --pdf data/pdfs/a.pdf data/pdfs/b.pdf [--sql-kontrol]
    python scripts/rag_toplu_isleme.py calistir --ad <parti> --adim cikarim --api-onay --cikarim-butce 200
    python scripts/rag_toplu_isleme.py calistir --ad <parti> --adim taslak
    python scripts/rag_toplu_isleme.py calistir --ad <parti> --adim semantik-hazirla
    python scripts/rag_toplu_isleme.py calistir --ad <parti> --adim semantik --api-onay --semantik-butce 100
    python scripts/rag_toplu_isleme.py ingestion --ad <parti> --uretim-onay --yedek <A1>\\yedek\\kardiyo_tam_metin.sql
    python scripts/rag_toplu_isleme.py sql-komutlari --ad <parti>
    python scripts/rag_toplu_isleme.py durum --ad <parti>
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOPLU_KOKU = ROOT / 'data' / 'rag_toplu'
PDF_KOKU = ROOT / 'data' / 'pdfs'
ADIMLAR = ('cikarim', 'taslak')
AD_RE = re.compile(r'[A-Za-z0-9_.-]{1,64}')


def _now():
    return datetime.now().isoformat(timespec='seconds')


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _rel(path):
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def klasor(ad):
    if not AD_RE.fullmatch(ad or ''):
        raise SystemExit(f'geçersiz parti adı: {ad!r} (harf, rakam, _ . -)')
    return TOPLU_KOKU / ad


def durum_oku(ad):
    path = klasor(ad) / 'durum.json'
    if not path.exists():
        raise SystemExit(f'{path} yok: önce plan komutunu çalıştırın')
    return json.loads(path.read_text(encoding='utf-8'))


def durum_yaz(ad, state):
    path = klasor(ad) / 'durum.json'
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, path)


def hata_kaydet(ad, makale, adim, exc):
    with open(klasor(ad) / 'hatalar.jsonl', 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'zaman': _now(), 'makale': makale, 'adim': adim,
                                 'hata': f'{type(exc).__name__}: {exc}',
                                 'iz': traceback.format_exc(limit=6)}, ensure_ascii=False) + '\n')


def load_pilot():
    spec = importlib.util.spec_from_file_location('rag_semantik_pilot', ROOT / 'scripts' / 'rag_semantik_pilot.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pdf_listesi(items):
    out = []
    for item in items:
        path = Path(item)
        found = sorted(path.glob('*.pdf')) if path.is_dir() else [path]
        for p in found:
            if not p.is_file():
                raise SystemExit(f'PDF yok: {p}')
            if p.resolve().parent != PDF_KOKU.resolve():
                raise SystemExit(f'{p}: PDF data/pdfs altında olmalı (taslak, semantik ve aktarım bu klasörü kullanır)')
            out.append(p.resolve())
    return out


def onceki_partiler(ad):
    """{belge: parti adı}: başka toplu çalıştırmalarda taslağı tamamlanmış belgeler."""
    out = {}
    for path in sorted(TOPLU_KOKU.glob('*/durum.json')):
        if path.parent.name == ad:
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        for doc, m in data.get('makaleler', {}).items():
            if (m.get('adimlar', {}).get('taslak') or {}).get('durum') == 'tamam':
                out[doc] = path.parent.name
    return out


def sql_kontrol(doc_ids):
    """Üretimde SALT OKUNUR: belge başına rag doğrulama partisi ve pdf_belgeleri kaydı."""
    from src.rag_aktarim import _readonly_connection
    conn = _readonly_connection()
    try:
        cur = conn.cursor()
        marks = ', '.join(['%s'] * len(doc_ids))
        cur.execute("SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(ayrinti, '$.belge_kimligi')) FROM kalite_isaretleri "
                    "WHERE kontrol_surumu LIKE %s AND kontrol_kodu = 'rag_det_ozet'", ('rag-dogrulama-%',))
        validated = {r[0] for r in cur.fetchall()}
        cur.execute(f'SELECT document_id FROM pdf_belgeleri WHERE document_id IN ({marks})', tuple(doc_ids))
        ingested = {r[0] for r in cur.fetchall()}
        return {d: {'uretimde_dogrulama_partisi': d in validated, 'pdf_belgeleri': d in ingested} for d in doc_ids}
    finally:
        conn.rollback()
        conn.close()


def cikarim_plani(pdf, top_k):
    from src import rag_klinik_cikarim as K
    meta, _ = K.run(str(pdf), plan_only=True, boundary=True, table=True, top_k=top_k)
    return meta


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def plan(args):
    out = klasor(args.ad)
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'durum.json'
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
        'ad': args.ad, 'olusturma': _now(), 'makaleler': {}, 'mukerrer': [], 'semantik': {}}
    pdfs = pdf_listesi(args.pdf)
    prior = onceki_partiler(args.ad)
    ids = {}
    for pdf in pdfs:
        doc = _sha256(pdf)
        if doc in ids:
            state['mukerrer'].append({'pdf': _rel(pdf), 'ayni_icerik': _rel(ids[doc]), 'zaman': _now()})
            print(f'MÜKERRER (aynı içerik bu listede): {pdf.name} = {ids[doc].name}; atlandı')
            continue
        ids[doc] = pdf
    checked = sql_kontrol(list(ids)) if args.sql_kontrol and ids else {}
    for doc, pdf in ids.items():
        m = state['makaleler'].setdefault(doc, {'pdf': _rel(pdf), 'dosya': pdf.name, 'adimlar': {}})
        m['atla'] = None
        if doc in prior and not args.yeniden_isle:
            m['atla'] = f'önceki toplu partide işlendi ({prior[doc]})'
        elif (checked.get(doc) or {}).get('uretimde_dogrulama_partisi') and not args.yeniden_isle:
            m['atla'] = 'üretimde bu belgenin doğrulama partisi var'
        if checked:
            m['uretim'] = checked[doc]
        try:
            meta = cikarim_plani(pdf, args.top_k)
            if meta['document_id'] != doc:
                raise RuntimeError('pdf_pages belge kimliği dosya SHA-256 ile aynı değil')
            m['plan'] = {k: meta[k] for k in ('gorev_sayisi', 'bekleyen_cagri', 'sayfa_sayisi', 'chunk_sayisi',
                                               'sinir_gorev_sayisi', 'tablo_gorev_sayisi')} | {'zaman': _now()}
        except Exception as exc:  # noqa: BLE001 - makale bazında kayıt, diğerleri sürer
            hata_kaydet(args.ad, pdf.name, 'plan', exc)
            m['plan'] = {'hata': f'{type(exc).__name__}: {exc}', 'zaman': _now()}
    durum_yaz(args.ad, state)
    goster(state)
    todo = [m for m in state['makaleler'].values() if not m.get('atla') and 'bekleyen_cagri' in (m.get('plan') or {})]
    print(f"\nÇıkarım için bekleyen çağrı toplamı (önbellekte olmayan görev): "
          f"{sum(m['plan']['bekleyen_cagri'] for m in todo)} ({len(todo)} makale). API çağrısı yapılmadı.")
    return state


# ---------------------------------------------------------------------------
# calistir
# ---------------------------------------------------------------------------

def adim_durumu(m, adim):
    return (m.get('adimlar', {}).get(adim) or {}).get('durum')


def calistir_cikarim(args, state):
    if not args.api_onay or args.cikarim_butce is None:
        raise SystemExit('cikarim gerçek API çağrısı yapar: --api-onay ve --cikarim-butce N zorunlu')
    from src import rag_klinik_cikarim as K
    remaining = args.cikarim_butce
    for doc, m in state['makaleler'].items():
        if m.get('atla') or adim_durumu(m, 'cikarim') == 'tamam':
            continue
        pdf = ROOT / m['pdf']
        try:
            pending = cikarim_plani(pdf, args.top_k)['bekleyen_cagri']
            if pending > remaining:
                m['adimlar']['cikarim'] = {'durum': 'butce_yetersiz', 'bekleyen_cagri': pending, 'kalan_butce': remaining,
                                           'zaman': _now()}
                print(f'BÜTÇE YETERSİZ: {m["dosya"]} bekleyen {pending} > kalan {remaining}; çağrı yapılmadı')
                continue
            meta, run_dir = K.run(str(pdf), max_calls=pending, boundary=True, table=True, top_k=args.top_k)
            remaining -= meta['api_cagrisi']
            m['adimlar']['cikarim'] = {'durum': 'tamam', 'calistirma': _rel(run_dir), 'api_cagrisi': meta['api_cagrisi'],
                                       'gecerli_kayit': meta['gecerli_kayit'], 'reddedilen': meta['reddedilen'],
                                       'zaman': _now()}
            print(f"TAMAM çıkarım: {m['dosya']} | çağrı {meta['api_cagrisi']} | kayıt {meta['gecerli_kayit']}")
        except Exception as exc:  # noqa: BLE001 - hata kaydı; tamamlanan çağrılar önbellekte
            hata_kaydet(args.ad, m['dosya'], 'cikarim', exc)
            try:
                remaining -= max(0, pending - cikarim_plani(pdf, args.top_k)['bekleyen_cagri'])
            except Exception:  # noqa: BLE001
                remaining = 0                      # harcanan bilinemiyorsa güvenli taraf: bütçe bitti sayılır
            m['adimlar']['cikarim'] = {'durum': 'hata', 'hata': f'{type(exc).__name__}: {exc}', 'zaman': _now()}
            print(f'HATA çıkarım: {m["dosya"]}: {exc}')
        finally:
            durum_yaz(args.ad, state)
    print(f'Kalan çıkarım bütçesi: {remaining}')


def calistir_taslak(args, state):
    from src import rag_aktarim as A
    from src import rag_dogrulama as V
    tdir = klasor(args.ad) / 'taslak'
    tdir.mkdir(parents=True, exist_ok=True)
    for doc, m in state['makaleler'].items():
        if m.get('atla') or adim_durumu(m, 'cikarim') != 'tamam' or adim_durumu(m, 'taslak') == 'tamam':
            continue
        out = tdir / f"{Path(m['dosya']).stem}_rag_taslak.json"
        report = out.with_name(out.stem + '.dogrulama.json')
        try:
            if out.exists() and not report.exists():          # yarım kalmış: silinmez, kenara alınır
                out.rename(out.with_name(f'{out.stem}.yarim_{datetime.now():%Y%m%d%H%M%S}.json'))
            if not out.exists():
                argv = ['taslak', '--calistirma', str(ROOT / m['adimlar']['cikarim']['calistirma']),
                        '--pdf', str(ROOT / m['pdf']), '--cikti', str(out), '--dogrulama-raporu']
                argv += ['--sql-okuma-yok'] if args.sql_okuma_yok else []
                argv += ['--aktarim-disi', args.aktarim_disi] if args.aktarim_disi else []
                A.main(argv)
            data = json.loads(report.read_text(encoding='utf-8'))
            info = V.aday_dosyasi(report, data)
            taslak = json.loads(out.read_text(encoding='utf-8'))
            if any(r.get('review_status') != 'pending' for r in taslak['adaylar']):
                raise RuntimeError('taslakta pending olmayan aday var')
            m['adimlar']['taslak'] = {'durum': 'tamam', 'taslak': _rel(out), 'taslak_sha256': info['aday_dosyasi_sha256'],
                                      'rapor': _rel(report), 'rapor_sha256': info['rapor_sha256'],
                                      'sayilar': taslak['sayilar'], 'dogrulama_ozeti': data['ozet']['genel_sonuc'],
                                      'sql_tekrar_kontrolu': not args.sql_okuma_yok, 'zaman': _now()}
        except Exception as exc:  # noqa: BLE001
            hata_kaydet(args.ad, m['dosya'], 'taslak', exc)
            m['adimlar']['taslak'] = {'durum': 'hata', 'hata': f'{type(exc).__name__}: {exc}', 'zaman': _now()}
            print(f'HATA taslak: {m["dosya"]}: {exc}')
        finally:
            durum_yaz(args.ad, state)


def rapor_ozetleri(state):
    return sorted((m['adimlar']['taslak']['rapor'], m['adimlar']['taslak']['rapor_sha256'])
                  for m in state['makaleler'].values() if adim_durumu(m, 'taslak') == 'tamam' and not m.get('atla'))


def calistir_semantik_hazirla(args, state):
    from src import rag_klinik_cikarim as K
    pending = [m['dosya'] for m in state['makaleler'].values() if not m.get('atla') and adim_durumu(m, 'taslak') != 'tamam']
    if pending and not args.kismi:
        raise SystemExit(f'taslağı tamamlanmamış makaleler var ({len(pending)}): {pending[:5]}; '
                         f'önce taslak adımı ya da bilinçli olarak --kismi')
    reports = rapor_ozetleri(state)
    if not reports:
        raise SystemExit('taslağı tamamlanmış makale yok')
    sdir = klasor(args.ad) / 'semantik'
    if sdir.exists():
        raise SystemExit(f'{sdir} zaten var (raporlar: {state["semantik"].get("raporlar") == reports}); '
                         f'yeni makaleler için yeni bir parti adı kullanın')
    rdir = klasor(args.ad) / 'semantik_raporlar'          # yalnız tamamlanan raporlar (bağ: rapor içindeki aday dosyası)
    rdir.mkdir(exist_ok=False)
    for rel, sha in reports:
        target = rdir / Path(rel).name
        target.write_bytes((ROOT / rel).read_bytes())
        if _sha256(target) != sha:
            raise SystemExit(f'{rel}: rapor değişti')
    P = load_pilot()
    P.toplu_hazirla(SimpleNamespace(rapor=[str(rdir)], tum=args.tum, model=args.model or K.default_model(),
                                    cikti=str(sdir)))
    state['semantik'] = {'hazirlik': _rel(sdir), 'raporlar': reports, 'zaman': _now(), 'kopyalar': []}
    durum_yaz(args.ad, state)


def tamamlanan_kararlar(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return {r['kimlik']: r.get('karar') for r in data['sonuclar'] if r.get('durum') == 'tamamlandi'}


def calistir_semantik(args, state):
    if not args.api_onay or args.semantik_butce is None:
        raise SystemExit('semantik gerçek API çağrısı yapar: --api-onay ve --semantik-butce N zorunlu')
    sem = state.get('semantik') or {}
    if not sem.get('hazirlik'):
        raise SystemExit('önce --adim semantik-hazirla')
    sdir = ROOT / sem['hazirlik']
    P = load_pilot()
    try:
        P.calistir(SimpleNamespace(hazirlik=str(sdir), max_calls=args.semantik_butce, onay=True, deneme=args.deneme))
    except BaseException as exc:  # noqa: BLE001 - SystemExit / bütçe dahil kayıt
        hata_kaydet(args.ad, '-', 'semantik', exc)
        raise
    new = tamamlanan_kararlar(sdir / 'sonuclar.json')
    if sem.get('kopyalar'):
        old = tamamlanan_kararlar(ROOT / sem['kopyalar'][-1]['dosya'])
        lost = {k: v for k, v in old.items() if new.get(k) != v}
        if lost:
            exc = RuntimeError(f'{len(lost)} önceki tamamlanmış semantik karar yeni sonuçlarda yok / farklı; kopya yazılmadı')
            hata_kaydet(args.ad, '-', 'semantik', exc)
            raise exc
    copy = sdir / f'sonuclar_{datetime.now():%Y%m%d-%H%M%S}.json'
    with open(copy, 'xb') as handle:
        handle.write((sdir / 'sonuclar.json').read_bytes())
    os.chmod(copy, 0o444)
    data = json.loads(copy.read_text(encoding='utf-8'))
    sem.setdefault('kopyalar', []).append({'dosya': _rel(copy), 'sha256': _sha256(copy), 'tamamlanan': len(new),
                                           'kontrol_edilmeyen': data.get('kontrol_edilmeyen', 0), 'zaman': _now()})
    state['semantik'] = sem
    durum_yaz(args.ad, state)
    print(f'Semantik sonuç kopyası (değişmez): {copy} | tamamlanan {len(new)} | kontrol edilmeyen '
          f"{data.get('kontrol_edilmeyen', 0)}")


def calistir(args):
    state = durum_oku(args.ad)
    {'cikarim': calistir_cikarim, 'taslak': calistir_taslak, 'semantik-hazirla': calistir_semantik_hazirla,
     'semantik': calistir_semantik}[args.adim](args, state)
    goster(state)


# ---------------------------------------------------------------------------
# ingestion (üretim yazımı; açık onay + doğrulanmış yedek)
# ---------------------------------------------------------------------------

VIEWS8 = ('measurements', 'measurements_by_group', 'clinical_outcomes', 'treatments', 'general_clinical_findings',
          'diseases', 'diagnostics', 'symptoms')


def _koruma_izi(conn):
    """klinik_bulgular (bütün satırların bütün alanları), kalite_isaretleri satır izi, sekiz sunum görünümü."""
    from src.rag_aktarim import table_fingerprint
    from src import rag_dogrulama as V
    cur = conn.cursor()
    try:
        views = {}
        for v in VIEWS8:
            cur.execute(f'SELECT * FROM `kardiyo_sunum`.`{v}`')
            rows = sorted(json.dumps(list(r), default=str, ensure_ascii=False) for r in cur.fetchall())
            views[v] = hashlib.sha256('\n'.join(rows).encode()).hexdigest()
        return {'klinik_bulgular': table_fingerprint(conn), 'kalite_isaretleri': V._kalite_izi(cur), 'sunum': views}
    finally:
        cur.close()


class DenetimliBaglanti:
    """ingest_pdf için bağlantı: commit() ÖNCE koruma izini karşılaştırır; fark varsa hata (ingest_pdf ROLLBACK eder)."""

    def __init__(self, conn, before):
        self._conn, self._before = conn, before

    def commit(self):
        after = _koruma_izi(self._conn)
        diff = [k for k in self._before if self._before[k] != after[k]]
        if diff:
            raise RuntimeError(f'commit öncesi denetim: {diff} değişti; ROLLBACK')
        self._conn.commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def ingestion(args):
    if not args.uretim_onay:
        raise SystemExit('ingestion üretim veritabanına yazar: --uretim-onay zorunlu')
    from src import rag_dogrulama as V
    print(json.dumps({'yedek': V.yedek_dogrula(args.yedek)}, ensure_ascii=False))
    from src.chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
    from src.database import get_connection
    from src.embeddings import DEFAULT_MODEL
    from src.ingest import ingest_pdf
    state = durum_oku(args.ad)
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute('SELECT DATABASE()')
        if cur.fetchone()[0] != V.URETIM_KAYNAK:
            raise SystemExit(f'hedef veritabanı {V.URETIM_KAYNAK} değil')
        cur.close()
        conn.autocommit = True                 # okumalar anında; yazım ingest_pdf içinde tek transaction
        for doc, m in state['makaleler'].items():
            if m.get('atla') or adim_durumu(m, 'ingestion') == 'tamam':
                continue
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM pdf_belgeleri WHERE document_id = %s', (doc,))
            exists = cur.fetchone()[0]
            cur.close()
            if exists:
                m['adimlar']['ingestion'] = {'durum': 'tamam', 'not': 'pdf_belgeleri kaydı zaten var; yazılmadı',
                                             'zaman': _now()}
                durum_yaz(args.ad, state)
                continue
            try:
                before = _koruma_izi(conn)
                res = ingest_pdf(DenetimliBaglanti(conn, before), str(ROOT / m['pdf']), args.model or DEFAULT_MODEL,
                                 DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP)
                m['adimlar']['ingestion'] = {'durum': 'tamam', 'sonuc': res, 'zaman': _now()}
                print(f"TAMAM ingestion: {m['dosya']} | chunk {res['chunks']} (yeni {res['chunks_new']})")
            except Exception as exc:  # noqa: BLE001 - ingest_pdf ROLLBACK etti; hata yalnız dosyaya
                hata_kaydet(args.ad, m['dosya'], 'ingestion', exc)
                m['adimlar']['ingestion'] = {'durum': 'hata', 'hata': f'{type(exc).__name__}: {exc}', 'zaman': _now()}
                print(f'HATA ingestion: {m["dosya"]}: {exc}')
            finally:
                durum_yaz(args.ad, state)
    finally:
        conn.close()
    goster(state)


# ---------------------------------------------------------------------------
# sql-komutlari / durum
# ---------------------------------------------------------------------------

def sql_komutlari(args):
    state = durum_oku(args.ad)
    tdir = _rel(klasor(args.ad) / 'semantik_raporlar') if state.get('semantik', {}).get('hazirlik') else None
    reports = rapor_ozetleri(state)
    copies = (state.get('semantik') or {}).get('kopyalar') or []
    rdir = tdir or _rel(klasor(args.ad) / 'taslak')
    print('# Üretim yazımı (açık onay): önce taze yedek, sonra izole prova, sonra -Uygula. Aynı girdi ikinci kez yazılamaz.')
    print('powershell -ExecutionPolicy Bypass -File scripts\\uretim_asama1_yedek.ps1')
    base = (f'powershell -ExecutionPolicy Bypass -File scripts\\uretim_asama4_rag.ps1 -Asama1Klasoru <A1> '
            f'-DegerlendirmeKlasoru {rdir}')
    if reports:
        sem = f' -SemantikSonuc {copies[-1]["dosya"]}' if copies else ''
        print(f'# 1) deterministik raporlar ({len(reports)} makale){" + son semantik kopya" if sem else ""}')
        print(base + sem + ' -SonrakiParti')
        print(base + sem + ' -SonrakiParti -Uygula')
    if len(copies) > 1 or (copies and not reports):
        print('# 2) kısmi semantik devamı: yalnız YENİ kopya, rapor klasörü olmadan (önceki kararlar korunur)')
        print(f'powershell -ExecutionPolicy Bypass -File scripts\\uretim_asama4_rag.ps1 -Asama1Klasoru <A1> '
              f'-SemantikSonuc {copies[-1]["dosya"]} -SonrakiParti [-Uygula]')
    if not tdir:
        print('# Not: semantik-hazirla yapılmadıysa rapor klasörü taslak klasörüdür (yalnız tamamlanan raporlar).')


def goster(state):
    print(f"\nParti: {state['ad']} | makale {len(state['makaleler'])} | mükerrer (atlanan) {len(state.get('mukerrer', []))}")
    for doc, m in state['makaleler'].items():
        steps = ', '.join(f"{a}={(m.get('adimlar', {}).get(a) or {}).get('durum', '-')}" for a in ADIMLAR + ('ingestion',))
        plan_ = m.get('plan') or {}
        print(f"  {m['dosya'][:48]:<48} {('ATLA: ' + m['atla']) if m.get('atla') else ''} görev {plan_.get('gorev_sayisi', '-')}"
              f" bekleyen {plan_.get('bekleyen_cagri', plan_.get('hata', '-'))} | {steps}")
    sem = state.get('semantik') or {}
    if sem:
        print(f"  semantik: hazırlık {sem.get('hazirlik')} | kopya {len(sem.get('kopyalar', []))}")


def durum(args):
    goster(durum_oku(args.ad))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='komut', required=True)
    a = sub.add_parser('plan', help='API yok; varsayılan veritabanı yok')
    a.add_argument('--ad', required=True)
    a.add_argument('--pdf', required=True, nargs='+', help='data/pdfs altındaki PDF dosyaları ya da data/pdfs')
    a.add_argument('--sql-kontrol', action='store_true', help='üretimde salt okunur mükerrerlik kontrolü')
    a.add_argument('--yeniden-isle', action='store_true', help='daha önce işlenmiş belgeleri bilinçli olarak yeniden işle')
    a.add_argument('--top-k', type=int, default=6)
    c = sub.add_parser('calistir')
    c.add_argument('--ad', required=True)
    c.add_argument('--adim', required=True, choices=('cikarim', 'taslak', 'semantik-hazirla', 'semantik'))
    c.add_argument('--api-onay', action='store_true', help='gerçek API çağrısına açık onay (cikarim / semantik)')
    c.add_argument('--cikarim-butce', type=int, help='bu çalıştırmadaki en çok çıkarım çağrısı (makaleler toplamı)')
    c.add_argument('--semantik-butce', type=int, help='bu çalıştırmadaki en çok semantik çağrı')
    c.add_argument('--deneme', type=int, default=1, help='semantik: kayıt başına en çok istek')
    c.add_argument('--top-k', type=int, default=6)
    c.add_argument('--sql-okuma-yok', action='store_true', help='taslak: mevcut SQL kayıtlarıyla tekrar kontrolü yapma')
    c.add_argument('--aktarim-disi', help='taslak: aktarım dışı listesi')
    c.add_argument('--tum', action='store_true', help='semantik-hazirla: bütün adaylar')
    c.add_argument('--kismi', action='store_true', help='semantik-hazirla: taslağı tamamlanmayan makaleler varken')
    c.add_argument('--model', help='semantik-hazirla: doğrulayıcı modeli (varsayılan .env LLM_MODEL)')
    g = sub.add_parser('ingestion', help='ÜRETİM YAZIMI: --uretim-onay + --yedek')
    g.add_argument('--ad', required=True)
    g.add_argument('--uretim-onay', action='store_true')
    g.add_argument('--yedek', required=True)
    g.add_argument('--model', help='embedding modeli (varsayılan src.embeddings.DEFAULT_MODEL)')
    for name in ('sql-komutlari', 'durum'):
        x = sub.add_parser(name)
        x.add_argument('--ad', required=True)
    args = p.parse_args(argv)
    return {'plan': plan, 'calistir': calistir, 'ingestion': ingestion, 'sql-komutlari': sql_komutlari,
            'durum': durum}[args.komut](args)


if __name__ == '__main__':
    main()
