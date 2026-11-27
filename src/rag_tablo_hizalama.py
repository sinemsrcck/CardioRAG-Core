"""RAG tablo kayıtlarının deterministik tablo modeline (src.pilot_tablo) hizalanması. LLM, API, SQL yok.

Tablo kuralları BURADA YENİDEN YAZILMAZ: src.pilot_tablo'nun ayrıştırıcısı (parse_tables), satır eşlemesi (match_rows)
ve kayıt önizlemesi (sql_preview) olduğu gibi kullanılır. Böylece sütun rolleri (grup / etki / p / regresyon katsayısı /
tanısal test), analiz etiketi (univariate / multivariate), p sütununun sahibi, %95 GA ayrımı, ±SE, Total sütunu, değer
biçimi, test / post-hoc işaretleri, alt alta yazılmış alt satırlar, bölüm kapsamı, tanısal performans tablosu ve
literatür / karmaşık tablo tespiti pilot ile aynı kurallardan gelir.

Her RAG tablo kaydı (kanit_turu='tablo') için:
  - Tablo modeli kaydın satırını 'elle_inceleme' olarak işaretlediyse (literatür / özet tablosu, karmaşık tablo):
    kayıt SİLİNMEZ, gerekçesiyle incelemeye ayrılır (HOLD_STAGE).
  - Satır modelde tekil bulunur; alt alta yazılmış satırlarda kaydın klinik bilgisiyle tekil alt satır seçilir.
    Klinik bilgisi bölünmemiş hücrenin tamamı olan kayıt 'bolunmemis' işaretlenir (eşdeğerlik denetimi tekrar
    ayıklamasında yapılır).
  - Değer, modelin önizlemesindeki tek hücreye bağlanır (kayıttaki grup / ölçü etiketi yalnız birden çok aday
    varken ayırt etmek için). Değer satırda yoksa ya da bağlanamıyorsa tahmin yapılmaz.
  - Bağlanan hücre için modelin türettiği yapılandırılmış alanlar uygulanır (etki türü, değer / GA ayrımı, p,
    birim, tanısal test). Kayıttaki dolu bir alan modelle çelişiyorsa düzeltilmez, kayıt incelemeye ayrılır.
  - Düzeltilen kayıt kaynakta yeniden doğrulanır; doğrulanamazsa incelemeye ayrılır.
  - Kanıt metni (kanit_cumle), kanıt parçaları ve klinik_bilgi_en DEĞİŞMEZ.
Sonuç kaydın 'tablo_modeli' alanına yazılır (adaptör taslak_notu'na taşır; SQL'e ayrı alan olarak gitmez).
"""
import json
import re

from src import pilot_tablo as PT

HOLD_STAGE = 'tablo_hizalama_inceleme'
KATSAYI_STAGE = 'katsayi_etkiye_baglandi'
EFFECT_CODES = {code for _, code in PT.EFFECT_TYPES} | set(PT.EFFECT_MARKS.values())
# Modelin önizleme notundan kayda taşınan alanlar (pilot taslak_notu anahtarlarıyla aynı adlar).
NOTE_KEYS = ('analiz', 'p_sutun', 'regresyon_katsayisi', 'katsayi_sutunu', 'etki_kaynagi', 'etki_isareti',
             'bicim_etiketi', 'deger_bicimi', 'p_test_isareti', 'posthoc_isareti', 'test_isareti', 'sutun_rolu',
             'karsilastirma_turu', 'grup_ici_nedeni', 'tablo_turu', 'tani_olcusu', 'se', 'se_bicimi', 'karsilastirma',
             'karsilastirilan_sutunlar', 'p_baglandigi_satir', 'p_baglandigi_olcu', 'p_baglanamadi', 'grup_n')


def _c(text):
    from src.rag_klinik_cikarim import _cmp
    return _cmp(PT.clean(str(text or '')))


# Pilot modelini satırsız bırakan ama RAG hattının kendi test edilmiş denetimleriyle karşılanan gerekçeler:
# - metin değerli sütun (nitel etki tablosu: 'Decreased', 'P (A-B)=0.291'): rag_klinik_cikarim nitel hücre kuralları;
# - çok seviyeli başlık (kalın ikinci başlık satırı): rag_klinik_cikarim.locate_header kalın alt katmanı seçer.
# Bunlar dışındaki her yapısal gerekçe (sayısal satır adı, iç içe tablo ...) ve literatür tablosu incelemeye ayrılır.
NON_BLOCKING_REASONS = ('metin değerli sütun', 'çok seviyeli başlık')


def _manual_reason(table):
    """Elle inceleme tablosunun inceleme gerekçesi ya da None (None: model yok sayılır, kayıt değişmeden mevcut
    denetimlerle devam eder). Literatür / özet tablosu ve NON_BLOCKING_REASONS dışındaki yapısal karmaşıklık
    incelemeye ayrılır."""
    issues = {i.get('tur'): i for i in (table or {}).get('sorunlar', [])}
    if 'literatur_tablosu' in issues:
        return ('literatür / özet tablosu (başka çalışmaların verisi; satır adları çalışma / yazar kimliği): '
                'otomatik ayrıştırılmadı, elle inceleme; otomatik çıkarımı açık eksiklik')
    parts = issues.get('elle_inceleme_gerekli', {}).get('parca') or []
    structural = [p for p in parts if not str(p).startswith(NON_BLOCKING_REASONS)]
    if not structural:
        return None
    return 'karmaşık tablo (otomatik ayrıştırılamadı): ' + '; '.join(structural[:3])


def _cell_texts(row, key):
    """Modeldeki hücrenin karşılaştırma biçimleri: temiz değer ve (varsa) işaretli hali ('0.100' + 'u')."""
    value = row['sutun_degerleri'].get(key, '') or ''
    out = {_c(value)}
    marks = [(row.get('isaretler') or {}).get(key), ((row.get('p_etki') or {}).get(key) or {}).get('isaret')]
    for mark in filter(None, marks):
        out.add(_c(f'{value} {mark}'))
    return {t for t in out if t}


def _row_of(item, rows):
    fact = item.get('klinik_bilgi_en')
    if len(rows) == 1:
        return rows[0], None
    if re.search(r'<br\s*/?>', str(fact or ''), re.I):
        return None, 'bolunmemis'                        # alt satırların tamamı tek kayıtta (ham <br> korunmuş)
    chosen = [r for r in rows if _c(fact) in {_c(r['ad']), _c(r['degisken']), _c(r['degisken_ham'])}]
    return (chosen[0], None) if len(chosen) == 1 else (None, 'belirsiz_satir')


def _candidates(item, table, row):
    """[(tür, önizleme kaydı, önizleme notu)]: değerin bağlanabildiği hücreler. tür: 'deger' | 'p' | 'katsayi'."""
    previews = PT.sql_preview({'kanit_cumle': item.get('kanit_cumle'), 'bilgi_turu': item.get('bilgi_turu'),
                               'baglam': item.get('baglam')}, table, row)
    value = _c(item.get('deger'))
    out = []
    for prev in previews:
        note = json.loads(prev['taslak_notu'])
        key = note.get('sutun')
        if value and (value in _cell_texts(row, key) or value == _c(prev.get('deger'))):
            out.append(('deger', prev, note))
        elif value and prev.get('p') and note.get('p_sutun') and value in (_cell_texts(row, note['p_sutun']) | {_c(prev['p'])}):
            out.append(('p', prev, note))
        elif value and note.get('regresyon_katsayisi') and value == _c(note['regresyon_katsayisi']):
            out.append(('katsayi', prev, note))
    return out


def _label_filter(item, cands):
    """Birden çok aday varken kaydın kendi etiketiyle (grup / ölçü / test) tekil olanı seç; yoksa hepsi kalır."""
    labels = [_c(item.get(f)) for f in ('hasta_grubu', 'sonuc_olcutu_tr', 'mudahale_en') if item.get(f)]
    if len(cands) < 2 or not labels:
        return cands

    def fits(cand):
        prev, note = cand[1], cand[2]
        names = [_c(prev.get('hasta_grubu')), _c(note.get('sutun')), _c(prev.get('mudahale_en')),
                 _c(note.get('p_sutun')), _c(note.get('katsayi_sutunu'))]
        return any(n and lab and (n == lab or n in lab or lab in n) for n in names for lab in labels)
    kept = [c for c in cands if fits(c)]
    return kept or cands


def _model_info(table, row, note, state):
    info = {'durum': state, 'tablo_no': table['tablo_no'], 'satir_no': row['satir_no'], 'degisken': row['degisken'],
            'alt_bolum': row.get('alt_bolum'), 'sutun': note.get('sutun')}
    info.update({k: note[k] for k in NOTE_KEYS if note.get(k) not in (None, '', [])})
    return info


def _dash(text):
    return re.sub(r'[‐-―−]', '-', str(text or ''))


def _source_split(raw_value, model_value, model_ci):
    """Modelin ayırdığı değer / GA'yı kaydın ÖZGÜN hücre metninden keser (tire, boşluk gibi karakterler kaynaktaki
    gibi kalır; pilot önizlemesi GA'yı ASCII tire ile yeniden kurar). Model ile aynı değilse (None, None)."""
    # Sondaki tek / iki harfli işaret ('or', 'u') hücrenin parçasıdır; değer ve GA'ya girmez (pilot split_marker).
    match = re.match(r'^\s*(\S+)\s*\(\s*(.+?)\s*\)\s*(?:[A-Za-z]{1,2})?\s*$', PT.clean(str(raw_value or '')))
    if not match or _c(match.group(1)) != _c(model_value) or _c(_dash(match.group(2))) != _c(_dash(model_ci)):
        return None, None
    return match.group(1), match.group(2)


def _apply(item, kind, prev, note):
    """(yeni kayıt, çelişki nedeni). Yalnız modelin kesin verdiği alanlar; dolu alanla çelişki -> düzeltme yok."""
    new = dict(item)
    changed = []

    def put(field, value):
        if value in (None, ''):
            return None
        current = new.get(field)
        if current in (None, ''):
            new[field] = value
            changed.append(field)
            return None
        if _c(current) == _c(value):
            return None
        mark = note.get('p_test_isareti') if field == 'p' else None
        if isinstance(mark, str) and _c(current) == _c(f'{value} {mark}'):
            new[field] = value                           # test işareti p'den ayrılır (notta p_test_isareti)
            changed.append(field)
            return None
        return f'{field} modelle çelişiyor (kayıt {current!r}, tablo modeli {value!r})'

    diagnostic = note.get('tablo_turu') == PT.TABLO_TANISAL
    if kind == 'katsayi':
        if new.get('etki_turu') in EFFECT_CODES:
            new['etki_turu'] = None                      # B katsayısı etki ölçüsü (OR / HR / RR) değildir
            changed.append('etki_turu')
    elif kind == 'deger':
        model_value, model_ci = prev.get('deger'), prev.get('ci95')
        if model_ci:
            value, ci = _source_split(item.get('deger'), model_value, model_ci)
            if value is None:
                return item, None                        # özgün hücreden birebir ayrılamıyor: değişiklik yok
            if _c(value) != _c(new.get('deger')):
                new['deger'] = value                     # %95 GA aynı hücrenin parçası; ci95 alanına ayrılır
                changed.append('deger')
            model_ci = ci
        elif model_value and _c(model_value) != _c(new.get('deger')):
            new['deger'] = model_value                   # SE / işaret ayrımı (aynı hücrenin parçası)
            changed.append('deger')
        elif model_value in (None, '') and diagnostic and prev.get('p'):
            new.pop('deger', None)                       # ikili karşılaştırma / P satırı: değer değil p
            changed.append('deger')
        for field, model_field in (('ci95', model_ci), ('p', prev.get('p')), ('unit', prev.get('unit'))):
            problem = put(field, model_field)
            if problem:
                return item, problem
        # Etki türü sütun rolünden gelir (analiz / p sütunundaki etki / tanısal ölçü); grup hücrelerinde dokunulmaz.
        effect = prev.get('etki_turu')
        if effect and (note.get('analiz') or note.get('etki_kaynagi') or diagnostic) and new.get('etki_turu') != effect:
            new['etki_turu'] = effect
            changed.append('etki_turu')
        if diagnostic:
            for field in ('mudahale_en', 'mudahale_turu', 'outcome_type', 'comparator'):
                problem = put(field, prev.get(field))
                if problem:
                    return item, problem
    if changed:
        new['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme') or []) + [f'tablo_modeli:{f}' for f in changed]
    return new, None


def align_record(item, tables, pages):
    """(kayıt, inceleme nedeni). İnceleme nedeni varsa kayıt değiştirilmeden incelemeye ayrılır."""
    from src.pilot_clinical import validate_records
    from src.rag_klinik_cikarim import source_problem
    if item.get('kanit_turu') != 'tablo':
        return item, None
    status, table, rows = PT.match_rows(item, tables)
    if status == 'elle_inceleme':
        return item, _manual_reason(table)               # None: yalnız metin değerli sütun -> model yok, değişiklik yok
    if status != 'eslesti':
        return item, None
    row, state = _row_of(item, rows)
    if state == 'bolunmemis':
        lines = [_c(x) for x in re.split(r'<br\s*/?>', str(item.get('deger') or ''), flags=re.I) if _c(x)]
        keys = [k for k in rows[0]['sutun_degerleri']
                if [_c(r['sutun_degerleri'].get(k)) for r in rows] == lines] if len(lines) == len(rows) else []
        info = {'durum': 'bolunmemis', 'tablo_no': table['tablo_no'], 'satirlar': [r['satir_no'] for r in rows],
                'sutun': keys[0] if len(keys) == 1 else None,
                'hucreler': [r['sutun_degerleri'].get(keys[0]) for r in rows] if len(keys) == 1 else None}
        return {**item, 'tablo_modeli': info}, None
    if row is None:
        return item, None
    if item.get('deger') in (None, ''):
        if _c(item.get('klinik_bilgi_en')) in {_c(row['ad']), _c(row['degisken']), _c(row['degisken_ham'])}:
            info = {'durum': 'satir_etiketi', 'tablo_no': table['tablo_no'], 'satir_no': row['satir_no'],
                    'degisken': row['degisken'], 'alt_bolum': row.get('alt_bolum')}
            return {**item, 'tablo_modeli': info}, None
        return item, None
    cands = _label_filter(item, _candidates(item, table, row))
    kinds = {(k, n.get('sutun')) for k, _, n in cands}
    if len(kinds) != 1:
        # Bağlanamayan (modelin kayıt üretmediği sütun, modelin bölmediği alt alta hücre) ya da belirsiz hücre:
        # tahmin ve değişiklik yok; kayıt mevcut denetimlerle (adaptörün hücre konumu kontrolü dahil) devam eder.
        return item, None
    kind, prev, note = cands[0]
    new, conflict = _apply(item, kind, prev, note)
    if conflict:
        return item, f'olası kaynak / ilişki tutarsızlığı: {conflict}'
    state = {'deger': 'eslesti', 'p': 'p_hucresi', 'katsayi': 'katsayi'}[kind]
    new['tablo_modeli'] = _model_info(table, row, note, state)
    if _c(new.get('deger')) != _c(item.get('deger')):
        new['tablo_modeli']['ham_deger'] = item.get('deger')   # hücre konumu için özgün hücre değeri (kanıtta birebir)
    if kind == 'katsayi':
        new['tablo_modeli'].update(istatistik='regresyon katsayısı (B)', sutun=note.get('katsayi_sutunu'),
                                   etki_sutunu=note.get('sutun'))
    if new is not item and any(str(f).startswith('tablo_modeli:') for f in new.get('otomatik_duzeltme') or []):
        probe = {k: v for k, v in new.items() if k != 'tablo_modeli'}
        problem = source_problem(probe, pages.get(new.get('sayfa'), ''))
        _, errors = validate_records([probe], pages)
        if problem or errors:
            return item, f"tablo modeli düzeltmesi kaynakta doğrulanamadı: {problem or errors[0]['hata']}"
    return new, None


AUDIT_STATES = ('eslesti', 'p_hucresi', 'katsayi', 'bolunmemis')


def audit_record(item, tables):
    """Hizalanmış kaydı kaynak tablodan BAĞIMSIZ yeniden denetler (kaydın meta verisi doğru kabul edilmez): tablo ve
    satır kaynakta tekil; kanıt satırı modeldeki ham satırla aynı; değer (özgün hücre değeri) kayıtlı sütunun
    hücresinde. Hata nedeni listesi (boş = geçti). tables: pilot_tablo.parse_tables(pages)."""
    m = item.get('tablo_modeli') or {}
    state = m.get('durum')
    found = [t for t in tables if t.get('tablo_no') == m.get('tablo_no') and t.get('sayfa') == item.get('sayfa')]
    if len(found) != 1:
        return [f'tablo kaynak sayfada tekil bulunamadı ({len(found)})']
    rows = found[0].get('satirlar') or []
    if state == 'bolunmemis':
        wanted = m.get('satirlar') or []
        sub = [r for r in rows if r['satir_no'] in wanted]
        if len(sub) != len(wanted) or not wanted:
            return ['alt satırlar kaynak tabloda bulunamadı']
        if m.get('sutun') and m.get('hucreler'):
            if [_c(r['sutun_degerleri'].get(m['sutun'])) for r in sub] != [_c(h) for h in m['hucreler']]:
                return ['alt satır hücreleri kayıtlı sütunla aynı değil']
        return []
    row = [r for r in rows if r['satir_no'] == m.get('satir_no')]
    if len(row) != 1:
        return ['satır kaynak tabloda tekil bulunamadı']
    row = row[0]
    problems = []
    if _c(row.get('ham')) != _c(item.get('kanit_cumle')):
        problems.append('kanıt satırı modeldeki kaynak satır değil')
    key = m.get('p_sutun') if state == 'p_hucresi' else m.get('sutun')
    if key not in row['sutun_degerleri']:
        problems.append(f"kayıtlı sütun kaynak satırda yok ({key!r})")
    else:
        raw = m.get('ham_deger') or item.get('deger')
        texts = _cell_texts(row, key) | {_c(row['sutun_degerleri'][key])}
        if _c(raw) not in texts and _c(raw) not in _c(row['sutun_degerleri'][key]):
            problems.append('değer kayıtlı sütunun hücresinde yok')
    return problems


def align_tables(records, pages, rejected):
    """Bütün tablo kayıtlarını hizalar. İncelemeye ayrılan ve etki kaydına bağlanan B kayıtları silinmez,
    gerekçeleriyle ret raporuna yazılır."""
    tables = PT.parse_tables(pages)
    out = []
    for item in records:
        new, reason = align_record(item, tables, pages)
        if reason:
            rejected.append({'sayfa': item.get('sayfa'), 'asama': HOLD_STAGE, 'neden': reason, 'kayit': item})
            continue
        out.append(new)
    # B katsayısı: aynı satırın etki kaydı varsa ve notunda aynı katsayı duruyorsa ayrı kayıt olarak tutulmaz
    # (pilot kuralı: taslak_notu.regresyon_katsayisi). Etki kaydı yoksa B kaydı kalır (etki türü OR değil).
    final = []
    for item in out:
        model = item.get('tablo_modeli') or {}
        if model.get('durum') == 'katsayi':
            owner = [o for o in out if (o.get('tablo_modeli') or {}).get('durum') == 'eslesti'
                     and o.get('sayfa') == item.get('sayfa') and _c(o.get('kanit_cumle')) == _c(item.get('kanit_cumle'))
                     and o['tablo_modeli'].get('satir_no') == model.get('satir_no')
                     and o['tablo_modeli'].get('sutun') == model.get('etki_sutunu')
                     and _c(o['tablo_modeli'].get('regresyon_katsayisi')) == _c(item.get('deger'))]
            if owner:
                rejected.append({'sayfa': item.get('sayfa'), 'asama': KATSAYI_STAGE, 'kayit': item,
                                 'neden': f"B katsayısı ({item.get('deger')}) aynı satırın etki kaydında "
                                          f"regresyon_katsayisi olarak korunuyor ({model.get('etki_sutunu')})"})
                continue
        final.append(item)
    return final
