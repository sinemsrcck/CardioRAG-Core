"""Bağımsız deterministik doğrulama raporu (LLM / API / embedding / veritabanı bağlantısı yok).

Mevcut kontrolleri TEK aşamada sıralar; yeni paralel doğrulama kuralı yazmaz:
  kaynak_kanit      rag_aktarim.source_check (= pilot_clinical.validate_records + rag_klinik_cikarim.source_problem
                    + müdahale sayfada + izinli alanlar): kanıt PDF sayfasında birebir, bütün parçalar, bilgi kanıtta,
                    değer / p / birim / grup / sonuç ölçütü kaynakta.
  tablo_hucre       özel tablo kaydı: rag_tablo_ozel.audit_record; hizalanmış kayıt: rag_tablo_hizalama.audit_record;
                    modelsiz tablo kaydı: değerin satırda TEKİL hücre konumu (rag_klinik_cikarim.cell_positions).
                    Kaynak sayfa yeniden okunur; çıkarımın meta verisi doğru kabul edilmez.
  anlam             rag_anlam_denetimi.assess kayıt üzerinde yeniden çalıştırılır (+ kayıttaki uyarılar / kapsam).
  dq_*              src.data_quality satır ve grup kontrolleri (check_numeric, check_ci_p, check_group_consistency,
                    check_statistical_groups, check_model_terminology, check_duplicates). check_source veritabanı
                    chunk bağlamı ister -> 'calistirilmadi' (yerine kaynak_kanit); check_row_consistency eski hattın
                    not biçimi içindir -> 'uygulanamaz'.
  klinik_iliski     clinical_validation.assess_clinical_relationships (yerel metin desteği; klinik doğruluk değil).
  metin_tablo       metin kaydındaki değer ile AYNI ölçüm + grup + değer biçimindeki TEKİL tablo hücresi (uyarı).

Sonuçlar: gecti | belirsiz | basarisiz | uygulanamaz | calistirilmadi ; seviye: hata | uyari | bilgi.
Rapor kayıtları DEĞİŞTİRMEZ, review_status'a dokunmaz. Otomatik deterministik 'gecti' klinik ya da semantik doğrulama
DEĞİLDİR; 'verified' yalnız insan kararıdır.

Not: data_quality ve clinical_validation import zinciri src.clinical_candidates üzerinden src.database modülünü belleğe
yükler (bağlantı açılmaz). Bu nedenle bu modül çıkarım hattına (rag_klinik_cikarim) import EDİLMEZ; ayrı aşamadır.
"""
import copy
import json
import re
from collections import Counter

import src.pilot_tablo as PT

SURUM = 'rag-dogrulama-v1'
SONUCLAR = ('gecti', 'belirsiz', 'basarisiz', 'uygulanamaz', 'calistirilmadi')
ONCELIK = {'basarisiz': 0, 'belirsiz': 1, 'gecti': 2}
DQ_AILELERI = {
    'dq_sayisal': ('yuzde_100_ustu', 'p_aralik_disi', 'p_bicim_supheli', 'ci_ters', 'deger_ci_disinda',
                   'oran_sifir_veya_negatif', 'auc_aralik_disi', 'negatif_sayi', 'deger_birimsiz', 'doz_birimsiz',
                   'sayi_ayristirilamadi', 'ondalik_belirsiz'),
    'dq_ci_p': ('ci_p_celiskisi',),
    'dq_grup': ('farkli_doz', 'farkli_hasta_sayisi', 'farkli_deger', 'kategorik_p_yeniden_uretilemedi',
                'surekli_p_supheli'),
    'dq_terminoloji': ('model_etki_olcusu_celiskisi',),
    'dq_tekrar': ('tekrar_ayni_bilgi', 'tekrar_yalniz_mudahale', 'tekrar_metin_tablo', 'tekrar_farkli_sayfa',
                  'tekrar_benzer'),
}
LEVEL_TO_RESULT = {'hata': 'basarisiz', 'uyari': 'belirsiz', 'bilgi': 'gecti'}


def kontrol(kod, sonuc, seviye, gerekce, kanit=None):
    assert sonuc in SONUCLAR
    return {'kod': kod, 'surum': SURUM, 'sonuc': sonuc, 'seviye': seviye, 'gerekce': gerekce, 'kanit': kanit or {}}


def _c(text):
    return re.sub(r'\s+', ' ', PT.clean(str(text or ''))).strip().casefold()


# ---------------------------------------------------------------------------
# Kontroller
# ---------------------------------------------------------------------------

def check_source(cand, pages):
    from src.rag_aktarim import source_check
    problem = source_check(cand, pages)
    evidence = {'sayfa': cand.get('sayfa'), 'kanit_cumle': cand.get('kanit_cumle')}
    if cand.get('kanit_parcalari'):
        evidence['kanit_parcalari'] = cand['kanit_parcalari']
    if problem:
        return kontrol('kaynak_kanit', 'basarisiz', 'hata', problem, evidence)
    return kontrol('kaynak_kanit', 'gecti', 'bilgi', 'kanıt, bilgi ve yapılandırılmış alanlar kaynak sayfada birebir',
                   evidence)


def check_table_cell(item, pages, tables):
    from src import rag_tablo_hizalama as H
    from src import rag_tablo_ozel as O
    from src.rag_klinik_cikarim import cell_positions
    if item.get('kanit_turu') != 'tablo':
        return kontrol('tablo_hucre', 'uygulanamaz', 'bilgi', 'metin kaydı')
    model = item.get('tablo_modeli') or {}
    evidence = {'sayfa': item.get('sayfa'), 'tablo_no': model.get('tablo_no'), 'sutun': model.get('sutun'),
                'deger': item.get('deger')}
    if model.get('durum') == O.STATE:
        problems = O.audit_record(item, pages)
        source = 'özel tablo modeli (rag_tablo_ozel.audit_record)'
    elif model.get('durum') in H.AUDIT_STATES:
        problems = H.audit_record(item, tables)
        source = 'tablo hizalama modeli (rag_tablo_hizalama.audit_record)'
    else:
        value = model.get('ham_deger') or item.get('deger') or item.get('klinik_bilgi_en')
        positions = cell_positions(value, item.get('kanit_cumle'))
        if len(positions) == 1:
            return kontrol('tablo_hucre', 'gecti', 'bilgi', 'modelsiz tablo kaydı: değer satırda tekil hücre konumunda',
                           {**evidence, 'hucre_konumu': positions[0]})
        return kontrol('tablo_hucre', 'belirsiz', 'uyari',
                       f'modelsiz tablo kaydı: değerin hücre konumu tekil değil ({len(positions)} konum)', evidence)
    if problems:
        return kontrol('tablo_hucre', 'basarisiz', 'hata', '; '.join(problems), {**evidence, 'kaynak': source})
    return kontrol('tablo_hucre', 'gecti', 'bilgi', 'tablo / satır / sütun / hücre ilişkisi kaynak tablodan yeniden '
                                                     'doğrulandı', {**evidence, 'kaynak': source})


def check_meaning(item):
    from src import rag_anlam_denetimi as D
    parts = item.get('kanit_parcalari')
    quote = ' '.join(p.get('metin', '') for p in parts) if parts else None
    probe = {k: v for k, v in item.items() if k != D.NOTE_KEY}
    _, stage, reason = D.assess(probe, quote)
    meta = item.get(D.NOTE_KEY) or {}
    if stage:
        return [kontrol('anlam', 'basarisiz', 'hata', f'{stage}: {reason}', {'kanit_cumle': item.get('kanit_cumle')})]
    out = []
    for warning in meta.get('uyarilar') or []:
        out.append(kontrol('anlam_uyari', 'belirsiz', 'uyari', warning, {'deger': item.get('deger')}))
    if meta.get('kapsam_uyarisi'):
        out.append(kontrol('anlam_kapsam', 'belirsiz', 'uyari', meta['kapsam_uyarisi'],
                           {'klinik_bilgi_en': item.get('klinik_bilgi_en'), 'kanit_cumle': item.get('kanit_cumle')}))
    if not out:
        out.append(kontrol('anlam', 'gecti', 'bilgi', 'değer anlamı / olumsuzluk / tür denetiminden geçti'))
    return out


def _fields(item):
    kind, outcome = item.get('bilgi_turu'), item.get('outcome_type')
    intervention = item.get('mudahale_en')
    return {'patient_group': item.get('hasta_grubu'), 'comparison': item.get('comparator'),
            'clinical_outcome': item.get('sonuc_olcutu_tr'), 'numeric_value': item.get('deger'),
            'value_unit': item.get('unit'),
            'drug': intervention if kind in ('ilac', 'doz') else None,
            'procedure': intervention if kind == 'tedavi' else None,
            'diagnostic_method': intervention if kind == 'teshis_yontemi' or outcome == 'tanısal performans' else None}


RELATION = {'supported_local': ('gecti', 'bilgi', 'yerel metin desteği var (klinik doğruluk değil)'),
            'contradicted': ('basarisiz', 'uyari', 'yapılandırılmış alan yerel kanıtla çelişiyor'),
            'uncertain': ('belirsiz', 'bilgi', 'ilişki yerel metin kurallarıyla kurulamadı; semantik doğrulama adayı'),
            'not_applicable': ('uygulanamaz', 'bilgi', 'ilişki alanı yok')}


def check_relationship(item, pages):
    from src.clinical_validation import assess_clinical_relationships
    if item.get('kanit_turu') == 'tablo':
        # Genel kural alanların ALINTIDA birebir geçmesini ister; tabloda grup / ölçüm sütun başlığındadır. Tablo
        # ilişkisi tablo_hucre kontrolünde kaynak tablodan doğrulanır (yanlış 'çelişki' üretilmez).
        return kontrol('klinik_iliski', 'uygulanamaz', 'bilgi', 'tablo kaydı: ilişki tablo_hucre kontrolünde '
                                                                 '(clinical_validation genel kuralı metin alıntısı içindir)')
    fields = {k: v for k, v in _fields(item).items() if v not in (None, '')}
    if not fields:
        return kontrol('klinik_iliski', 'uygulanamaz', 'bilgi', 'ilişki alanı yok')
    multi = sorted(k for k, v in fields.items() if isinstance(v, str) and ';' in v)
    if multi:
        # source_problem ';' ile ayrılmış bileşenleri tek tek doğrular; yerel ilişki kuralı alanı tek parça arar ->
        # çelişki değil, kural uygulanamıyor: semantik doğrulama adayı.
        return kontrol('klinik_iliski', 'belirsiz', 'bilgi', 'çok bileşenli alan (' + ', '.join(multi) + '); yerel '
                       'ilişki kuralı bileşen bazında değerlendiremiyor', {'alanlar': fields})
    try:
        verdict = assess_clinical_relationships(fields, item.get('kanit_cumle') or '', pages.get(item.get('sayfa'), ''),
                                                item.get('kanit_turu') or 'metin')
    except Exception as exc:  # noqa: BLE001 - kontrol çalışmadı; kayıt başarısız sayılmaz
        return kontrol('klinik_iliski', 'calistirilmadi', 'bilgi', f'kontrol çalışmadı: {type(exc).__name__}: {exc}')
    result, level, why = RELATION.get(verdict, ('belirsiz', 'bilgi', f'bilinmeyen sonuç {verdict!r}'))
    return kontrol('klinik_iliski', result, level, why, {'alanlar': fields, 'sonuc_kodu': verdict})


STRUCTURED_FORMATS = ('ortalama±SS', 'ortalama±SS (min–maks)', 'n (%)', 'değer (aralık)')


def _numbers(text):
    return [float(x.replace(',', '')) for x in re.findall(r'-?\d+(?:,\d{3})*(?:\.\d+)?', PT.clean(str(text or '')))]


def _decimals(text):
    return [len(x.split('.')[1]) if '.' in x else 0 for x in re.findall(r'-?\d+(?:,\d{3})*(?:\.\d+)?',
                                                                         PT.clean(str(text or '')))]


def _fmt(value):
    """pilot_tablo.value_format; parantez içindeki yüzde işareti biçimi değiştirmez ('16 (80%)' -> n (%))."""
    return PT.value_format(re.sub(r'(\d)\s*%\s*\)', r'\1)', PT.clean(str(value or ''))).strip())


def _close(a, b, da, db):
    """(eşit, yalnız yuvarlama farkı)."""
    if a == b:
        return True, False
    rounding = len(a) == len(b) and all(abs(x - y) <= 0.5 * 10 ** -min(p, q) + 1e-9 for x, y, p, q in zip(a, b, da, db))
    return False, rounding


def check_text_table(item, tables):
    """Metin kaydındaki yapılandırılmış değer(ler) ile AYNI ölçümün TEKİL tablo satırı karşılaştırılır.

    - Değer(ler) ortalama±SS / n (%) / değer (aralık) biçiminde olmalı ('a vs b' çifti de olabilir); düz sayı
      karşılaştırılmaz (eşleme tahmine dayanır).
    - Satır etiketi kanıtta kelime olarak geçmeli ve tekil olmalı; birim varsa aynı olmalı.
    - Tek değer: grup kaydın hasta_grubu'na eşit ya da sütun başlığı kanıtta geçmeli; hücre tekil olmalı.
    - Çift değer: her değer satırın aynı biçimdeki farklı bir hücresiyle eşit / yuvarlama farkında olmalı; grup
      eşlemesi DOĞRULANMAZ (raporda belirtilir).
    Eşleşme tekil değilse karşılaştırılmaz. Değerler değiştirilmez, hiçbiri doğru kabul edilmez."""
    if item.get('kanit_turu') != 'metin' or not item.get('deger'):
        return kontrol('metin_tablo', 'uygulanamaz', 'bilgi', 'metin değer kaydı değil')
    value = PT.clean(str(item['deger'])).strip()
    parts = [p.strip() for p in re.split(r'\s+vs\.?\s+', value) if p.strip()]
    fmts = {_fmt(p) for p in parts}
    if len(fmts) != 1 or next(iter(fmts)) not in STRUCTURED_FORMATS or len(parts) > 2:
        return kontrol('metin_tablo', 'uygulanamaz', 'bilgi', f"değer biçimi karşılaştırmaya uygun değil ({sorted(fmts)})")
    fmt = next(iter(fmts))
    evidence = _c(item.get('kanit_cumle'))
    group = _c(item.get('hasta_grubu'))
    rows = []
    for t in tables:
        for row in t.get('satirlar') or []:
            label = _c(row.get('ad') or row.get('degisken'))
            if len(label) < 2 or not re.search(r'(?<!\w)' + re.escape(label) + r'(?!\w)', evidence):
                continue
            if item.get('unit') and row.get('unit') and _c(item['unit']) not in _c(row['unit']):
                continue
            cells = [(col, cell) for col, cell in (row.get('sutun_degerleri') or {}).items() if _fmt(cell) == fmt]
            if cells:
                rows.append((t, row, cells))
    if len(rows) != 1:
        return kontrol('metin_tablo', 'uygulanamaz', 'bilgi',
                       f'eşdeğer tablo satırı tekil değil ({len(rows)} aday); zorla eşleştirilmedi')
    t, row, cells = rows[0]
    table_ref = {'sayfa': t['sayfa'], 'tablo_no': t['tablo_no'], 'satir': row.get('degisken')}
    text_ref = {'sayfa': item.get('sayfa'), 'deger': item['deger'], 'kanit_cumle': item.get('kanit_cumle')}
    if len(parts) == 1:
        chosen = []
        for col, cell in cells:
            base = _c(re.sub(r'\(\s*n\s*=\s*\d+\s*\)', '', col))
            if group:
                if group in (base, _c(col)):
                    chosen.append((col, cell))
            elif base and re.search(r'(?<!\w)' + re.escape(base) + r'(?!\w)', evidence):
                chosen.append((col, cell))
        if len(chosen) > 1 and not group:
            # Bir başlık diğerinin içinde geçiyorsa ('event' / 'no event') kanıtta geçen daha özgül olan seçilir.
            bases = {col: _c(re.sub(r'\(\s*n\s*=\s*\d+\s*\)', '', col)) for col, _ in chosen}
            chosen = [(col, cell) for col, cell in chosen
                      if not any(o != col and bases[col] in bases[o] for o in bases)]
        if len(chosen) != 1:
            return kontrol('metin_tablo', 'uygulanamaz', 'bilgi',
                           f'eşdeğer grup hücresi tekil değil ({len(chosen)} aday); zorla eşleştirilmedi')
        col, cell = chosen[0]
        source = {'metin': text_ref, 'tablo': {**table_ref, 'sutun': col, 'hucre': cell}}
        same, rounding = _close(_numbers(parts[0]), _numbers(cell), _decimals(parts[0]), _decimals(cell))
        if same:
            return kontrol('metin_tablo', 'gecti', 'bilgi', 'metin ve tablo değeri aynı', source)
        if rounding:
            return kontrol('metin_tablo', 'gecti', 'bilgi', 'yalnız yuvarlama farkı', source)
        return kontrol('metin_tablo', 'belirsiz', 'uyari', 'KAYNAK İÇİ TUTARSIZLIK: metin ve tablo değeri farklı; '
                                                            'hiçbiri doğru kabul edilmedi', source)
    used, found, rounding_any = set(), [], False
    for part in parts:
        hit = None
        for k, (col, cell) in enumerate(cells):
            if k in used:
                continue
            same, rounding = _close(_numbers(part), _numbers(cell), _decimals(part), _decimals(cell))
            if same or rounding:
                hit = k
                rounding_any = rounding_any or rounding
                break
        if hit is None:
            found.append((part, None))
        else:
            used.add(hit)
            found.append((part, cells[hit]))
    source = {'metin': text_ref, 'tablo': {**table_ref, 'hucreler': dict(cells)},
              'eslesme': [{'metin': p, 'tablo_sutun': c[0] if c else None} for p, c in found],
              'grup_eslemesi_dogrulanmadi': True}
    if all(c for _, c in found):
        why = 'çift değerin her biri satırın ayrı bir hücresinde' + (' (yuvarlama farkıyla)' if rounding_any else '')
        return kontrol('metin_tablo', 'gecti', 'bilgi', why + '; grup eşlemesi doğrulanmadı', source)
    return kontrol('metin_tablo', 'belirsiz', 'uyari', 'KAYNAK İÇİ TUTARSIZLIK: metindeki değerlerden en az biri '
                                                        'tablo satırında yok; hiçbiri doğru kabul edilmedi', source)


def data_quality_checks(rows):
    """src.data_quality satır ve grup kontrolleri; {satir_hash: [işaret]}."""
    from src import data_quality as Q
    flags = []
    for row in rows:
        flags += Q.check_numeric(row) + Q.check_ci_p(row)
    flags += Q.check_group_consistency(rows) + Q.check_statistical_groups(rows) + Q.check_model_terminology(rows)
    flags += Q.check_duplicates(rows)
    out = {}
    for f in flags:
        out.setdefault(f['satir_hash'], []).append(f)
    return out


def _dq_results(flags):
    out = []
    by_code = {}
    for f in flags:
        by_code.setdefault(f['kontrol_kodu'], []).append(f)
    for family, codes in DQ_AILELERI.items():
        hits = [f for c in codes for f in by_code.get(c, [])]
        if not hits:
            out.append(kontrol(family, 'gecti', 'bilgi', 'işaret yok'))
            continue
        for f in hits:
            out.append(kontrol(family, LEVEL_TO_RESULT[f['seviye']], f['seviye'], f"{f['kontrol_kodu']}: {f['aciklama']}",
                               {'grup_id': f.get('grup_id'), 'ayrinti': f.get('ayrinti')}))
    out.append(kontrol('dq_kaynak_chunk', 'calistirilmadi', 'bilgi',
                       'data_quality.check_source veritabanı chunk bağlamı ister; kaynak doğrulaması kaynak_kanit ile'))
    out.append(kontrol('dq_satir_tutarliligi', 'uygulanamaz', 'bilgi',
                       'data_quality.check_row_consistency eski hattın taslak_notu biçimi (extracted_fields) içindir'))
    return out


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

SEMANTIC_CODES = ('anlam_kapsam', 'klinik_iliski', 'anlam_uyari', 'metin_tablo')


def verify(records, draft, pages, document_id):
    """Taslak adayı başına doğrulama raporu. records: çıkarım kayıtları (build_draft girdisi); draft: build_draft
    çıktısı (aday_kaynaklari ile). Hiçbir girdi değiştirilmez (kopyalar üzerinde çalışılır)."""
    from src.rag_aktarim import satir_hash, serializable
    records = copy.deepcopy(records)
    candidates = copy.deepcopy(draft['adaylar'])
    sources = draft.get('aday_kaynaklari') or [None] * len(candidates)
    tables = PT.parse_tables(pages)
    rows = []
    for cand in candidates:
        row = serializable(cand)
        row.update({'satir_hash': satir_hash(document_id, cand), 'document_id': document_id,
                    'kanit_sayfa': cand.get('sayfa')})
        rows.append(row)
    dq = data_quality_checks(rows)
    out = []
    for cand, row, index in zip(candidates, rows, sources):
        item = records[index] if index is not None else cand
        checks = [check_source(cand, pages), check_table_cell(item, pages, tables)]
        checks += check_meaning(item)
        checks += _dq_results(dq.get(row['satir_hash'], []))
        checks.append(check_relationship(cand, pages))
        checks.append(check_text_table(item, tables))
        decided = [c['sonuc'] for c in checks if c['sonuc'] in ONCELIK]
        overall = min(decided, key=ONCELIK.get) if decided else 'uygulanamaz'
        semantic = sorted({c['kod'] for c in checks if c['kod'] in SEMANTIC_CODES and c['sonuc'] in ('belirsiz', 'basarisiz')})
        out.append({'kayit_kimligi': row['satir_hash'], 'belge_kimligi': document_id, 'kaynak_sira': index,
                    'sayfa': cand.get('sayfa'), 'kanit_turu': cand.get('kanit_turu'),
                    'klinik_bilgi_en': cand.get('klinik_bilgi_en'), 'deger': cand.get('deger'),
                    'review_status': cand.get('review_status'),
                    'dogrulama_turu': 'otomatik_deterministik', 'klinik_veya_semantik_dogrulama': False,
                    'genel_sonuc': overall, 'semantik_dogrulama_gerekli': bool(semantic),
                    'semantik_gerekce': semantic, 'kontroller': checks})
    return {'surum': SURUM, 'belge_kimligi': document_id, 'kayit_sayisi': len(out), 'kayitlar': out,
            'ozet': summarize(out),
            'not': "Otomatik deterministik 'gecti' klinik ya da semantik doğrulama değildir; review_status değişmedi."}


def summarize(entries):
    by_check = {}
    for e in entries:
        for c in e['kontroller']:
            by_check.setdefault(c['kod'], Counter())[c['sonuc']] += 1
    return {'genel_sonuc': dict(Counter(e['genel_sonuc'] for e in entries)),
            'semantik_dogrulama_gerekli': sum(e['semantik_dogrulama_gerekli'] for e in entries),
            'semantik_gerekce': dict(Counter(g for e in entries for g in e['semantik_gerekce'])),
            'kontrol_bazinda': {k: dict(v) for k, v in sorted(by_check.items())},
            'basarisiz_kontroller': dict(Counter(c['kod'] for e in entries for c in e['kontroller']
                                                 if c['sonuc'] == 'basarisiz'))}


def dumps(report):
    return json.dumps(report, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# SQL saklama: mevcut kalite_isaretleri tablosu (şema değişikliği yok)
# ---------------------------------------------------------------------------
# Durumlar birbirinden ayrıdır:
#   - insan doğrulaması      : YALNIZ klinik_bulgular.review_status = 'verified' (anlamı değişmez),
#   - otomatik deterministik : kalite_isaretleri, kontrol_surumu = SURUM ('rag-dogrulama-v1'), kontrol_kodu 'rag_det_*',
#   - semantik (LLM)         : kalite_isaretleri, kontrol_surumu = 'rag-semantik-v1', kontrol_kodu 'rag_sem_*'.
# Aday henüz içe aktarılmadığı için klinik_bulgu_id NULL kalır; satir_hash ile bağlanır (içe aktarıldığında
# klinik_bulgular.satir_hash ile eşleşir). kalite_calistirmalari'na YAZILMAZ: v_kalite_ozet "en son sürüm"
# kuralıyla data_quality sürümünü göstermeye devam eder.
#
# Parti: her yazım bir parti kimliği alır (grup_id = '<YYYYmmddHHMMSS>-<girdi dosyalarının SHA-256 özeti, 16>').
# Yazım YALNIZ EKLEMEDİR: mevcut hiçbir kalite satırı silinmez / değişmez; aynı adayın eski partisi geçmiş olarak kalır.
# Her satırın ayrintisinda belge_kimligi ve kaynak (aday dosyası yolu + SHA-256, rapor / sonuç dosyası + SHA-256,
# doğrulama sürümü) bulunur. v_rag_dogrulama_durumu belge başına EN SON deterministik partiyi (güncel aday dosyası)
# esas alır: yeniden çıkarımda artık bulunmayan adayların eski sonuçları güncel adaylara bağlanmaz; semantik sonuç
# yalnız aynı satir_hash'e (birebir aynı içerik) bağlanır. Tamamlanmamış semantik sonuç (bütçe dışı, çağrı / çıktı
# hatası) yazılmaz: aday "semantik kontrol yapılmadı" (NULL) olarak kalır.
# Üretim (kardiyo_tam_metin) yazımı: açık izin (uretim=True / --uretim) + doğrulanmış güncel yedek (--yedek: Aşama 1
# manifest SHA-256'sı, salt okunur, son 24 saat) + tek transaction + commit ÖNCESİ önce / sonra eşitlik denetimi.

URETIM_VERITABANLARI = ('kardiyo_tam_metin', 'kardiyo_sunum', 'kardiyo_clinical_data')
URETIM_KAYNAK = 'kardiyo_tam_metin'
PARTI_RE = re.compile(r'\d{14}-[0-9a-f]{16}')
KATEGORI = {'kaynak_kanit': 'kaynak', 'tablo_hucre': 'kaynak', 'dq_kaynak_chunk': 'kaynak', 'dq_tekrar': 'tekrar',
            'dq_sayisal': 'sayisal', 'dq_ci_p': 'sayisal'}
SEMANTIK_SEVIYE = {'destekleniyor': 'bilgi', 'kismen_destekleniyor': 'uyari', 'kaynakla_celisiyor': 'hata',
                   'yetersiz_kanit': 'uyari', 'belirsiz': 'uyari'}
# Satır izi: bütün sütunlar (id dahil). Önce / sonra karşılaştırması ve geri alma kaydı için.
KALITE_IZ_SQL = ("SELECT id, SHA2(CONCAT_WS('|', id, COALESCE(klinik_bulgu_id, '-'), satir_hash, kontrol_kodu, kategori, "
                 "seviye, grup_id, COALESCE(aciklama, '-'), COALESCE(CAST(ayrinti AS CHAR), '-'), kontrol_surumu, "
                 "olusturma_zamani), 256) FROM kalite_isaretleri")
KALITE_INSERT_SQL = ('INSERT INTO kalite_isaretleri (klinik_bulgu_id, satir_hash, kontrol_kodu, kategori, seviye, '
                     'grup_id, aciklama, ayrinti, kontrol_surumu) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)')


def _kalite(satir_hash, kod, kategori, seviye, aciklama, ayrinti, parti=''):
    return {'klinik_bulgu_id': None, 'satir_hash': satir_hash, 'kontrol_kodu': kod[:64], 'kategori': kategori,
            'seviye': seviye, 'grup_id': parti, 'aciklama': str(aciklama)[:2000], 'ayrinti': ayrinti}


def kalite_satirlari(report, semantic_results=None, semantic_version=None, hash_map=None, *, parti='', kaynak=None,
                     semantik_kaynak=None):
    """{kontrol_surumu: [kalite_isaretleri satırı]}. Kayıt başına bir özet satırı (rag_det_ozet) + yalnız belirsiz /
    başarısız kontroller; tamamlanan semantik sonuç başına bir satır (rag_sem_ozet). Hiçbir satır insan onayı anlamı
    taşımaz. hash_map: {eski_satir_hash: {'yeni_satir_hash', 'degisiklik', ...}} (scripts/rag_semantik_pilot.py
    hash-esleme): yalnız metaveri düzeltmesiyle hash'i değişen kayıtların semantik sonucu yeni hash'e bağlanır; eski
    hash ayrintida kalır. Semantik sonuçların kendisi değişmez. parti / kaynak / semantik_kaynak: izlenebilirlik."""
    det = []
    level = {'basarisiz': 'hata', 'belirsiz': 'uyari'}
    for e in report['kayitlar']:
        h = e['kayit_kimligi']
        common = {'belge_kimligi': e['belge_kimligi'], **({'kaynak': kaynak} if kaynak else {})}
        det.append(_kalite(h, 'rag_det_ozet', 'kaynak', level.get(e['genel_sonuc'], 'bilgi'),
                           f"otomatik deterministik doğrulama: {e['genel_sonuc']} (klinik / semantik doğrulama değildir)",
                           {'genel_sonuc': e['genel_sonuc'], 'dogrulama_turu': 'otomatik_deterministik',
                            'semantik_dogrulama_gerekli': e['semantik_dogrulama_gerekli'],
                            'semantik_gerekce': e['semantik_gerekce'],
                            'sayfa': e['sayfa'], 'kanit_turu': e['kanit_turu'], 'klinik_bilgi_en': e['klinik_bilgi_en'],
                            'deger': e['deger'], 'kontrol_sonuclari': {c['kod']: c['sonuc'] for c in e['kontroller']},
                            'surum': SURUM, **common}, parti))
        for c in e['kontroller']:
            if c['sonuc'] in ('belirsiz', 'basarisiz'):
                det.append(_kalite(h, 'rag_det:' + c['kod'], KATEGORI.get(c['kod'], 'tutarsizlik'), c['seviye'],
                                   c['gerekce'], {'sonuc': c['sonuc'], 'kanit': c['kanit'], 'surum': c['surum'],
                                                  **common}, parti))
    out = {SURUM: det}
    if semantic_results is not None:
        from src.rag_semantik_dogrulama import SURUM as SEM_SURUM
        sem = []
        for r in semantic_results:
            if r.get('durum') != 'tamamlandi':
                continue                      # kontrol edilmedi / başarısız: yazılmaz ("semantik kontrol yapılmadı")
            karar = r.get('karar')
            link = (hash_map or {}).get(r['kimlik'])
            extra = ({'eski_satir_hash': r['kimlik'], 'hash_esleme': link.get('degisiklik')} if link else {})
            sem.append(_kalite(link['yeni_satir_hash'] if link else r['kimlik'], 'rag_sem_ozet', 'tutarsizlik',
                               SEMANTIK_SEVIYE.get(karar, 'uyari'),
                               f"semantik (LLM) doğrulama: {karar} (insan doğrulaması değildir)",
                               {'karar': karar, 'durum': r.get('durum'), 'model_karari': r.get('model_karari'),
                                'hatali_alanlar': r.get('hatali_alanlar'), 'alinti_dogrulama': r.get('alinti_dogrulama'),
                                'yeniden_cikarim_gerekli': r.get('yeniden_cikarim_gerekli'), 'model': r.get('model'),
                                'dogrulayici_surumu': r.get('dogrulayici_surumu'), 'insan_dogrulamasi_degildir': True,
                                'belge_kimligi': r.get('belge') or (link or {}).get('belge'), **extra,
                                **({'kaynak': semantik_kaynak} if semantik_kaynak else {})}, parti))
        out[semantic_version or SEM_SURUM] = sem
    return out


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def aday_dosyasi(rapor_yolu, report, rapor_okunur=True):
    """Raporun aday dosyası (adayların tam içeriği, JSON) ve bağ denetimi. Rapor 'aday_dosyasi' taşıyorsa (rag_aktarim
    taslak --dogrulama-raporu) o dosya, yoksa aynı klasördeki <ad>.taslak.json (rag_onbellek_degerlendirme). Sürüm
    SURUM olmalı, dosyanın SHA-256'sı raporda kayıtlıysa aynı olmalı, belge aynı olmalı ve raporun kayıt kimlikleri
    dosyadaki adayların satir_hash kümesine EŞİT olmalı; aksi hâlde ValueError (sonuç yanlış adaylara bağlanmaz).
    rapor_okunur=False: rapor henüz yazılmadı (taslak --dogrulama-raporu yazmadan önce denetler)."""
    from pathlib import Path
    from src.rag_aktarim import satir_hash
    rapor_yolu = Path(rapor_yolu)
    kayit = report.get('aday_dosyasi') or {}
    if report.get('surum') != SURUM:
        raise ValueError(f'{rapor_yolu.name}: rapor sürümü {report.get("surum")!r}, beklenen {SURUM!r}')
    if kayit.get('yol'):
        path = Path(kayit['yol'])
    elif rapor_yolu.name.endswith('.dogrulama.json'):
        path = rapor_yolu.with_name(rapor_yolu.name[:-len('.dogrulama.json')] + '.taslak.json')
    else:
        raise ValueError(f'{rapor_yolu.name}: aday dosyası belirlenemedi')
    if not path.is_file():
        raise ValueError(f'{rapor_yolu.name}: aday dosyası yok: {path}')
    sha = _sha256(path)
    if kayit.get('sha256') and kayit['sha256'] != sha:
        raise ValueError(f'{rapor_yolu.name}: aday dosyası rapordan sonra değişti ({path})')
    data = json.loads(path.read_text(encoding='utf-8'))
    doc = data.get('document_id')
    if doc != report.get('belge_kimligi'):
        raise ValueError(f'{rapor_yolu.name}: aday dosyasının belgesi farklı ({path})')
    hashes = {satir_hash(doc, c) for c in data['adaylar']}
    ids = {e['kayit_kimligi'] for e in report['kayitlar']}
    if ids != hashes or any(e.get('belge_kimligi') != doc for e in report['kayitlar']):
        raise ValueError(f'{rapor_yolu.name}: rapor kimlikleri aday dosyasıyla eşleşmiyor (yalnız raporda '
                         f'{len(ids - hashes)}, yalnız dosyada {len(hashes - ids)})')
    return {'aday_dosyasi': path.as_posix(), 'aday_dosyasi_sha256': sha, 'rapor_dosyasi': rapor_yolu.as_posix(),
            'rapor_sha256': _sha256(rapor_yolu) if rapor_okunur else None, 'dogrulama_surumu': SURUM,
            'pdf': data.get('pdf')}


def kalite_gruplari(rapor_yollari, semantik=None, hash_esleme=None, simdi=None):
    """Raporlar (+ semantik sonuçlar) -> ({sürüm: satırlar}, parti, kaynaklar). Bağ denetimi aday_dosyasi ile.
    Rapor verilmezse yalnız semantik parti (kısmi semantik doğrulamanın devamı): deterministik parti değişmez."""
    import hashlib
    from datetime import datetime
    from pathlib import Path
    files = []
    for p in rapor_yollari or []:
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            raise ValueError(f'doğrulama raporu yolu yok: {path}')
        found = sorted(path.glob('*.dogrulama.json')) if path.is_dir() else [path]
        if not found:
            raise ValueError(f'doğrulama raporu bulunamadı: {path}')
        files += found
    if not files and not semantik:
        raise ValueError('doğrulama raporu ya da semantik sonuç gerekli')
    loaded = []
    for f in files:
        report = json.loads(f.read_text(encoding='utf-8'))
        loaded.append((report, aday_dosyasi(f, report)))
    sem = links = sem_kaynak = None
    if semantik:
        sem = json.loads(Path(semantik).read_text(encoding='utf-8'))['sonuclar']
        sem_kaynak = {'sonuc_dosyasi': Path(semantik).as_posix(), 'sonuc_sha256': _sha256(semantik)}
        if hash_esleme:
            links = json.loads(Path(hash_esleme).read_text(encoding='utf-8'))['eslesme']
            sem_kaynak.update({'hash_esleme_dosyasi': Path(hash_esleme).as_posix(),
                               'hash_esleme_sha256': _sha256(hash_esleme)})
    elif hash_esleme:
        raise ValueError('--hash-esleme yalnız --semantik ile kullanılır')
    parts = sorted(x for _, k in loaded for x in (k['rapor_sha256'], k['aday_dosyasi_sha256']))
    parts += [v for k, v in sorted((sem_kaynak or {}).items()) if k.endswith('_sha256')]
    parti = f"{(simdi or datetime.now()):%Y%m%d%H%M%S}-{hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]}"
    groups = {}
    for report, kaynak in loaded:
        for version, rows in kalite_satirlari(report, parti=parti, kaynak=kaynak).items():
            groups.setdefault(version, []).extend(rows)
    if sem is not None:
        for version, rows in kalite_satirlari({'kayitlar': []}, sem, hash_map=links, parti=parti,
                                              semantik_kaynak=sem_kaynak).items():
            if rows:
                groups.setdefault(version, []).extend(rows)
    current = {e['kayit_kimligi'] for report, _ in loaded for e in report['kayitlar']}
    sem_rows = [r for v, rows in groups.items() if v != SURUM for r in rows]
    kaynaklar = {'raporlar': [k for _, k in loaded], 'semantik': sem_kaynak,
                 'semantik_yazilmayan_tamamlanmamis': sum(1 for r in sem or [] if r.get('durum') != 'tamamlandi'),
                 'semantik_guncel_aday_disi': sum(1 for r in sem_rows if r['satir_hash'] not in current)}
    return groups, parti, kaynaklar


def yedek_dogrula(path, db=URETIM_KAYNAK):
    """Üretim yazımı öncesi doğrulanmış güncel yedek: dosya var, boş değil, son 24 saat (src.aktarim.yedek_kontrolu);
    aynı klasördeki manifest.json'da bu veritabanı için kayıtlı ad / boyut / SHA-256 ile aynı; salt okunur; Aşama 1
    izole geri yükleme raporu (../geri_yukleme/temizleme_raporu.json) aynı SHA-256'yı kaynak olarak gösteriyor."""
    import os
    from pathlib import Path
    from src.aktarim import yedek_kontrolu
    info = yedek_kontrolu(path)
    p = Path(path)
    try:
        entry = json.loads((p.parent / 'manifest.json').read_text(encoding='utf-8-sig')).get(db) or {}
        report = json.loads((p.parent.parent / 'geri_yukleme' / 'temizleme_raporu.json')
                            .read_text(encoding='utf-8-sig')).get(db) or {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'yedek doğrulanamadı (Aşama 1 manifest / geri yükleme raporu): {exc}') from None
    sha = _sha256(p)
    problems = [x for x, bad in (
        (f'manifest {db} kaydı bu dosya değil', entry.get('dosya') != p.name),
        ('boyut manifestle aynı değil', str(entry.get('boyut')) != str(p.stat().st_size)),
        ('SHA-256 manifestle aynı değil', str(entry.get('sha256', '')).upper() != sha.upper()),
        ('geri yükleme raporu bu yedeği göstermiyor', str(report.get('kaynak_sha256', '')).upper() != sha.upper()),
        ('yedek salt okunur değil', os.access(p, os.W_OK))) if bad]
    if problems:
        raise RuntimeError(f'yedek doğrulanamadı ({p}): ' + '; '.join(problems))
    return {**info, 'sha256': sha, 'veritabani': db}


def _hedef(cursor, uretim):
    """Hedef veritabanı denetimi. Üretim adlarında yalnız açık izinle ve yalnız kaynak veritabanı."""
    cursor.execute('SELECT DATABASE()')
    db = cursor.fetchone()[0]
    if db in URETIM_VERITABANLARI:
        if not uretim:
            raise RuntimeError(f'üretim veritabanına ({db}) açık izin (--uretim) olmadan yazılmaz')
        if db != URETIM_KAYNAK:
            raise RuntimeError(f'{db}: otomatik doğrulama sonuçları yalnız {URETIM_KAYNAK} veritabanına yazılır')
    elif uretim:
        raise RuntimeError(f'--uretim verildi ama hedef veritabanı ({db}) {URETIM_KAYNAK} değil')
    return db


def _kalite_izi(cursor, where='', params=()):
    cursor.execute(KALITE_IZ_SQL + where, params)
    return {int(i): h.decode('ascii') if isinstance(h, (bytes, bytearray)) else str(h) for i, h in cursor.fetchall()}


def _klinik_izi(conn):
    from src.rag_aktarim import table_fingerprint
    return table_fingerprint(conn)


GIRDI_ANAHTARLARI = ('rapor_sha256', 'sonuc_sha256')


def _onceki_girdiler(cursor, parti, rows):
    """Aynı girdinin tekrar yazılmasını engeller: aynı girdi özetli parti ya da girdi dosyalarından biri (rapor /
    semantik sonuç dosyası SHA-256) daha önce yazılmış partiler. Önceki partiler korunur; yeni parti yazılmaz."""
    cursor.execute('SELECT DISTINCT grup_id FROM kalite_isaretleri WHERE grup_id LIKE %s', ('%-' + parti.split('-', 1)[1],))
    found = {r[0] for r in cursor.fetchall()}
    shas = sorted({(r['ayrinti'].get('kaynak') or {}).get(k) for _, r in rows for k in GIRDI_ANAHTARLARI} - {None})
    if shas:
        marks = ', '.join(['%s'] * len(shas))
        cursor.execute("SELECT DISTINCT grup_id FROM kalite_isaretleri WHERE kontrol_surumu LIKE %s AND ("
                       f"JSON_UNQUOTE(JSON_EXTRACT(ayrinti, '$.kaynak.rapor_sha256')) IN ({marks}) OR "
                       f"JSON_UNQUOTE(JSON_EXTRACT(ayrinti, '$.kaynak.sonuc_sha256')) IN ({marks}))",
                       ['rag-%', *shas, *shas])
        found |= {r[0] for r in cursor.fetchall()}
    return sorted(str(x.decode() if isinstance(x, (bytes, bytearray)) else x) for x in found)


def _yeni_transaction(conn):
    conn.rollback()                       # açık okuma anlık görüntüsü / örtük transaction kalmasın
    conn.start_transaction()


def kalite_yaz(conn, groups, parti, *, yaz=False, uretim=False, kayit_yaz=None):
    """groups: {kontrol_surumu: [satır]} (kalite_gruplari). YALNIZ EKLEME, tek transaction; mevcut kalite satırları
    silinmez / değişmez, kalite_calistirmalari ve klinik_bulgular'a yazılmaz. Commit ÖNCESİ denetim: hedef veritabanı,
    parti daha önce yok, eklenen = beklenen (hepsi bu parti, 'rag-' sürümü, klinik_bulgu_id NULL), mevcut kalite
    satırları ve klinik_bulgular'ın bütün satırlarının bütün alanları önce = sonra. Biri tutmazsa ROLLBACK + hata.
    yaz=False: prova (ROLLBACK). kayit_yaz(kayit): commit'ten hemen ÖNCE çağrılır (geri alma kaydı; hata -> ROLLBACK).
    Dönüş: kayıt {veritabani, parti, surumler, eklenen {id: iz}, kayit, kalici}."""
    if not PARTI_RE.fullmatch(str(parti)):
        raise ValueError(f'geçersiz parti kimliği: {parti!r}')
    if any(not str(v).startswith('rag-') for v in groups):
        raise ValueError("kalite_yaz yalnız 'rag-' ad alanındaki sürümleri yazar")
    rows = [(v, r) for v, rs in groups.items() for r in rs]
    if any(r['grup_id'] != parti or r['klinik_bulgu_id'] is not None for _, r in rows):
        raise ValueError('satırlar bu partiye ait değil ya da klinik_bulgu_id dolu')
    cursor = conn.cursor()
    try:
        db = _hedef(cursor, uretim)
        _yeni_transaction(conn)
        cursor.execute('SELECT COUNT(*) FROM kalite_isaretleri WHERE grup_id = %s', (parti,))
        if cursor.fetchone()[0]:
            raise RuntimeError(f'parti {parti} zaten var; hiçbir şey yazılmadı')
        same = _onceki_girdiler(cursor, parti, rows)
        if same:
            raise RuntimeError(f'aynı girdi daha önce yazılmış (parti {same}); hiçbir şey yazılmadı. Kısmi semantik '
                               f'devamı için yalnız yeni sonuç dosyası (--semantik, --rapor olmadan) verilir')
        before_q, before_k = _kalite_izi(cursor), _klinik_izi(conn)
        if rows:
            cursor.executemany(KALITE_INSERT_SQL, [
                (r['klinik_bulgu_id'], r['satir_hash'], r['kontrol_kodu'], r['kategori'], r['seviye'], r['grup_id'],
                 r['aciklama'], json.dumps(r['ayrinti'], ensure_ascii=False, sort_keys=True, default=str), v)
                for v, r in rows])
        added = _kalite_izi(cursor, ' WHERE grup_id = %s', (parti,))
        after_q, after_k = _kalite_izi(cursor), _klinik_izi(conn)
        cursor.execute('SELECT COUNT(*) FROM kalite_isaretleri WHERE grup_id = %s AND '
                       '(klinik_bulgu_id IS NOT NULL OR kontrol_surumu NOT LIKE %s)', (parti, 'rag-%'))
        foreign = cursor.fetchone()[0]
        problems = [x for x, bad in (
            (f'eklenen {len(added)}, beklenen {len(rows)}', len(added) != len(rows)),
            ('eklenenler yeni satır değil', bool(set(added) & set(before_q))),
            ('mevcut kalite satırları değişti', {i: h for i, h in after_q.items() if i not in added} != before_q),
            ('klinik_bulgular değişti', after_k != before_k),
            ("bağlı ya da 'rag-' dışı satır", foreign != 0)) if bad]
        if problems:
            raise RuntimeError('commit öncesi denetim başarısız: ' + '; '.join(problems) + '; ROLLBACK')
        kayit = {'veritabani': db, 'parti': parti, 'surumler': {v: len(rs) for v, rs in groups.items()},
                 'eklenen': {str(i): h for i, h in sorted(added.items())},
                 'kayit': len({r['satir_hash'] for _, r in rows}), 'kalici': bool(yaz)}
        if yaz:
            if kayit_yaz:
                kayit_yaz(kayit)
            conn.commit()
        else:
            conn.rollback()
        return kayit
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def kalite_geri_al(conn, kayit, *, yaz=False, uretim=False):
    """kalite_yaz kaydındaki partiyi geri alır: YALNIZ o partide eklenen satırlar (id + satır izi kayıtla birebir aynı
    olmalı; değilse hiçbir şey silinmez). Commit öncesi: silinen = kayıt, diğer kalite satırları ve klinik_bulgular
    önce = sonra; tutmazsa ROLLBACK. yaz=False: prova."""
    parti = kayit['parti']
    if not PARTI_RE.fullmatch(str(parti)):
        raise ValueError(f'geçersiz parti kimliği: {parti!r}')
    want = {int(i): h for i, h in kayit['eklenen'].items()}
    cursor = conn.cursor()
    try:
        db = _hedef(cursor, uretim)
        if db != kayit['veritabani']:
            raise RuntimeError(f"kayıt {kayit['veritabani']} veritabanına ait; bağlantı {db}")
        _yeni_transaction(conn)
        current = _kalite_izi(cursor, ' WHERE grup_id = %s', (parti,))
        if current != want:
            raise RuntimeError(f'parti {parti} satırları kayıtla birebir aynı değil (kayıt {len(want)}, mevcut '
                               f'{len(current)}); geri alma yapılmadı')
        before_q, before_k = _kalite_izi(cursor), _klinik_izi(conn)
        ids, deleted = sorted(want), 0
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            cursor.execute('DELETE FROM kalite_isaretleri WHERE grup_id = %s AND klinik_bulgu_id IS NULL AND '
                           'kontrol_surumu LIKE %s AND id IN (' + ', '.join(['%s'] * len(part)) + ')',
                           [parti, 'rag-%', *part])
            deleted += cursor.rowcount
        after_q, after_k = _kalite_izi(cursor), _klinik_izi(conn)
        problems = [x for x, bad in (
            (f'silinen {deleted}, beklenen {len(ids)}', deleted != len(ids)),
            ('diğer kalite satırları değişti', after_q != {i: h for i, h in before_q.items() if i not in want}),
            ('klinik_bulgular değişti', after_k != before_k)) if bad]
        if problems:
            raise RuntimeError('commit öncesi denetim başarısız: ' + '; '.join(problems) + '; ROLLBACK')
        if yaz:
            conn.commit()
        else:
            conn.rollback()
        return {'veritabani': db, 'parti': parti, 'silinen': deleted, 'kalici': bool(yaz)}
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def main(argv=None):
    """python -m src.rag_dogrulama kalite-yaz --rapor <klasör|*.dogrulama.json> [--semantik sonuclar.json]
        [--hash-esleme hash_esleme.json] [--uretim --yedek <Aşama 1 yedek .sql>] [--yaz --kayit <yeni.json>]
    python -m src.rag_dogrulama kalite-geri-al --kayit <kayit.json> [--uretim --yedek <...>] [--yaz]"""
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(prog='python -m src.rag_dogrulama')
    sub = parser.add_subparsers(dest='komut', required=True)
    k = sub.add_parser('kalite-yaz', help='doğrulama sonuçlarını kalite_isaretleri tablosuna ekler (varsayılan prova)')
    k.add_argument('--rapor', nargs='+', default=[], help='*.dogrulama.json ya da klasör (yalnız semantik devamında verilmez)')
    k.add_argument('--semantik')
    k.add_argument('--hash-esleme', help='eski -> yeni satir_hash eşlemesi (yalnız metaveri düzeltmesi)')
    g = sub.add_parser('kalite-geri-al', help='bir kalite-yaz partisini geri alır (varsayılan prova)')
    for p in (k, g):
        p.add_argument('--uretim', action='store_true', help=f'açık izin: hedef {URETIM_KAYNAK}')
        p.add_argument('--yedek', help='üretimde zorunlu: Aşama 1 yedek\\kardiyo_tam_metin.sql (doğrulanmış, < 24 saat)')
        p.add_argument('--yaz', action='store_true')
    k.add_argument('--kayit', help='--yaz ile zorunlu: geri alma kaydı (yeni dosya; üzerine yazılmaz)')
    g.add_argument('--kayit', required=True, help='kalite-yaz geri alma kaydı')
    args = parser.parse_args(argv)
    if args.uretim:
        print(json.dumps({'yedek': yedek_dogrula(args.yedek)}, ensure_ascii=False))
    elif args.yedek:
        parser.error('--yedek yalnız --uretim ile kullanılır')
    from src.database import get_connection
    if args.komut == 'kalite-geri-al':
        kayit = json.loads(Path(args.kayit).read_text(encoding='utf-8'))
        conn = get_connection()
        try:
            print(json.dumps(kalite_geri_al(conn, kayit, yaz=args.yaz, uretim=args.uretim), ensure_ascii=False))
        finally:
            conn.close()
        return
    if args.yaz and not args.kayit:
        parser.error('--yaz için --kayit <yeni dosya> zorunlu (geri alma kaydı)')
    if args.kayit and Path(args.kayit).exists():
        parser.error(f'{args.kayit} zaten var; üzerine yazılmaz')
    groups, parti, kaynaklar = kalite_gruplari(args.rapor, args.semantik, args.hash_esleme)
    print(json.dumps({'parti': parti, 'satir': {v: len(r) for v, r in groups.items()},
                      'semantik_yazilmayan_tamamlanmamis': kaynaklar['semantik_yazilmayan_tamamlanmamis'],
                      'semantik_guncel_aday_disi': kaynaklar['semantik_guncel_aday_disi']}, ensure_ascii=False))

    def kayit_yaz(kayit):
        Path(args.kayit).parent.mkdir(parents=True, exist_ok=True)
        with open(args.kayit, 'x', encoding='utf-8') as handle:
            json.dump({**kayit, 'kaynaklar': kaynaklar,
                       'geri_alma': f'python -m src.rag_dogrulama kalite-geri-al --kayit {args.kayit}'
                                    + (' --uretim --yedek <yedek>' if args.uretim else '') + ' [--yaz]'},
                      handle, ensure_ascii=False, indent=1)

    conn = get_connection()
    try:
        out = kalite_yaz(conn, groups, parti, yaz=args.yaz, uretim=args.uretim, kayit_yaz=kayit_yaz)
        print(json.dumps({k: out[k] for k in ('veritabani', 'parti', 'surumler', 'kayit', 'kalici')}
                         | {'eklenen': len(out['eklenen'])}, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == '__main__':
    main()
