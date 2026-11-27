"""LLM tabanlı semantik doğrulayıcı (pilot). Çıkarımdan AYRI ve sürümlü istem; çıkarım istemleri değişmez.

Mevcut altyapı yeniden kullanılır (paralel altyapı yok):
  - çağrı: src.pilot_auto_extract.request_with_retry (aynı {"findings": [...]} yanıt sözleşmesi; tek elemanlı liste),
  - önbellek: src.rag_klinik_cikarim.Cache; anahtar = sha256(model, sistem istemi, yük); yük kaynak bağlamını, kayıt
    içeriğini ve doğrulayıcı sürümünü içerir -> kaynak, kayıt, model ya da sürüm değişirse anahtar değişir,
  - bütçe: extract ile aynı kural — önbellekte olmayan çağrı sayısı max_calls'ı aşarsa HİÇ çağrı yapılmadan durur,
  - kesintiden devam: her başarılı yanıt önbelleğe eklenir (JSONL); yeniden çalıştırma yalnız eksikleri çağırır,
  - alıntı doğrulama: rag_klinik_cikarim.source_span (kaynakta birebir ya da yalnız biçim farkıyla TEKİL eşleşme).

Güvenlik:
  - Doğrulayıcıya referans (manuel doğrulanmış) bilgi ve deterministik kontrol sonuçları VERİLMEZ.
  - Geçersiz model çıktısı, zaman aşımı, kota / bağlantı hatası hiçbir zaman 'destekleniyor' sayılmaz ve önbelleğe
    yazılmaz ('gecersiz_cikti' / 'calistirilamadi').
  - LLM'in alıntısı kaynakta doğrulanamazsa karar 'belirsiz'e indirilir (modelin kararı ayrıca saklanır).
  - Kayıtlar değiştirilmez; review_status'a dokunulmaz; otomatik 'verified' yok. Semantik karar insan doğrulaması
    DEĞİLDİR ve karar dağılımı klinik doğruluk oranı olarak adlandırılmaz.
"""
import json
import re

SURUM = 'rag-semantik-v1'
KARARLAR = ('destekleniyor', 'kismen_destekleniyor', 'kaynakla_celisiyor', 'yetersiz_kanit', 'belirsiz')
DURUMLAR = ('tamamlandi', 'gecersiz_cikti', 'calistirilamadi')
ALANLAR = ('klinik_bilgi_en', 'bilgi_turu', 'baglam', 'deger', 'unit', 'p', 'ci95', 'etki_turu', 'hasta_grubu',
           'comparator', 'sonuc_olcutu_tr', 'mudahale_en', 'outcome_type')
NOT_ALANLARI = ('tablo_no', 'tablo_basligi', 'sutun', 'degisken', 'alt_bolum', 'analiz', 'p_sutun', 'regresyon_katsayisi',
                'se', 'tani_olcusu', 'kaynak_calisma', 'alt_grup', 'kol', 'olcu', 'oneri_sinifi', 'kanit_duzeyi',
                'oneri_on_kosulu', 'tablo_iliskisi', 'tahmin_modeli', 'gercek_sinif', 'tahmin_sinifi', 'olcut', 'liste_sutunu',
                'test', 'deger_bilesenleri', 'kaynakca_numaralari')

SYSTEM = f"""You are an independent verifier of clinical data records extracted from a scientific article.
Verifier version: {SURUM}. You do NOT extract new information and you do NOT fix records.

You receive ONE extracted record (its fields) and the ORIGINAL SOURCE context from the PDF: the page paragraph(s)
around the evidence, the preceding/following context, and for table records the table caption, header rows, the
record's row(s) and footnotes. Do not assume the extracted record is correct. Judge ONLY against the source text.

Check every applicable aspect:
- clinical subject and relation (who/what the statement is about and what is claimed),
- negation and condition scope (no/not/without/unless/if; contraindications),
- comparison direction (higher/lower, which group is compared with which),
- patient group / population and time window,
- value, unit and statistic type (mean vs median, SD vs SE vs IQR vs range vs 95% CI, OR/HR/RR/AUC, p),
- study identity and whether the result is quoted from ANOTHER study (literature) or is the article's own result,
- aim vs method vs limitation vs recommendation vs realised result,
- table row/column relations (correct row label, column header, group, test, time window) and all evidence parts.

Decision values (field "karar"):
- "destekleniyor": every filled field is supported by the source;
- "kismen_destekleniyor": the core statement is supported but at least one field is wrong, missing context or too
  broad (list them);
- "kaynakla_celisiyor": the source states something incompatible with the record (e.g. negation lost, wrong group,
  wrong direction, wrong statistic);
- "yetersiz_kanit": the provided source context is not enough to decide;
- "belirsiz": the source is ambiguous.

Rules:
- Quotes in "kanit_alintilari" MUST be copied VERBATIM from the provided source text (same characters), each with its
  page number. Do not paraphrase inside quotes. Give at least one quote unless "karar" is "yetersiz_kanit".
- Never invent clinical facts. Never output corrected values; only name the problematic fields and the reason.
- "yeniden_cikarim_gerekli" is true only if the record cannot be repaired by editing a field and must be re-extracted
  from the source (e.g. the extracted fact text itself lost negation or scope).

Return ONLY JSON of this exact form:
{{"findings": [{{"karar": "...", "hatali_alanlar": [{{"alan": "<field name>", "sorun": "<short reason>"}}],
  "gerekce": "<short reasoning>", "kanit_alintilari": [{{"metin": "<verbatim quote>", "sayfa": <int>}}],
  "konum": {{"sayfa": <int>, "tablo": "<caption or null>", "satir": "<row label or null>",
             "sutun": "<column header or null>"}},
  "yeniden_cikarim_gerekli": <true|false>}}]}}"""


# ---------------------------------------------------------------------------
# Kaynak bağlamı (yalnız özgün PDF metni; referans ve deterministik sonuç yok)
# ---------------------------------------------------------------------------

MAX_PARAGRAPH = 2500
TABLE_LINES = 40


def _paragraphs(text):
    """(başlangıç, bitiş) aralıkları: boş satırla ayrılmış paragraflar."""
    spans, start = [], 0
    for m in re.finditer(r'\n[ \t]*\n', text):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _clip(text, limit=MAX_PARAGRAPH):
    return text if len(text) <= limit else text[:limit] + ' …'


def text_context(quote, page_text):
    """Kanıtı içeren paragraf + bir önceki ve bir sonraki paragraf (yoksa boş)."""
    at = page_text.find(quote.strip()[:80]) if quote else -1
    spans = _paragraphs(page_text)
    if at < 0 or not spans:
        return {'paragraf': None, 'onceki': None, 'sonraki': None, 'bulundu': False}
    k = next((i for i, (s, e) in enumerate(spans) if s <= at < e), None)
    if k is None:
        return {'paragraf': None, 'onceki': None, 'sonraki': None, 'bulundu': False}

    def part(i):
        return _clip(page_text[spans[i][0]:spans[i][1]].strip()) if 0 <= i < len(spans) else None
    return {'paragraf': part(k), 'onceki': part(k - 1), 'sonraki': part(k + 1), 'bulundu': True}


def table_context(row_text, page_text):
    """Tablo başlığı (bloğun üstündeki 'Table n' satırı), başlık satırları, kaydın satırı(ları) ve alt notlar."""
    import src.pilot_tablo as PT
    lines = page_text.splitlines()
    first = row_text.split('\n')[0].strip()
    hits = [i for i, l in enumerate(lines) if l.strip() == first]
    if len(hits) != 1:
        return {'bulundu': False}
    at = hits[0]
    start = at
    while start > 0 and lines[start - 1].strip().startswith('|'):
        start -= 1
    end = at
    while end < len(lines) and lines[end].strip().startswith('|'):
        end += 1
    caption = next((lines[i].strip() for i in range(start - 1, max(-1, start - 6), -1)
                    if PT.CAPTION.match(lines[i]) or PT.CAPTION_BOLD.match(lines[i])), None)
    sep = next((i for i in range(start, end) if PT.SEPARATOR.match(lines[i].strip())), start)
    header = [lines[i] for i in range(start, min(end, sep + 2)) if not PT.SEPARATOR.match(lines[i].strip())]
    block = lines[start:end]
    if len(block) > TABLE_LINES:                        # büyük tabloda kaydın çevresi
        lo = max(start, at - TABLE_LINES // 2)
        block = header + ['…'] + lines[lo:min(end, lo + TABLE_LINES)]
    notes = [l for l in lines[end:end + 6] if l.strip() and not l.strip().startswith('|')]
    return {'bulundu': True, 'tablo_basligi': caption, 'baslik_satirlari': header, 'tablo': '\n'.join(block),
            'kayit_satirlari': row_text, 'alt_notlar': notes}


def _note(cand):
    note = cand.get('taslak_notu') or {}
    if isinstance(note, str):
        try:
            note = json.loads(note)
        except ValueError:
            note = {}
    return note


def build_payload(cand, pages):
    """Doğrulayıcı yükü: kayıt alanları + seçili tablo notları + özgün kaynak bağlamı (çok parçalı kanıtta her parça
    kendi sayfasının bağlamıyla). Referans / deterministik sonuç / review_status YOK."""
    note = _note(cand)
    record = {k: cand.get(k) for k in ALANLAR if cand.get(k) not in (None, '')}
    extra = {k: note.get(k) for k in NOT_ALANLARI if note.get(k) not in (None, '', [], {})}
    if extra:
        record['tablo_ve_baglam_notlari'] = extra
    parts = cand.get('kanit_parcalari') or note.get('kanit_parcalari')
    if parts:
        evidence = [{'sayfa': p.get('sayfa'), 'metin': p.get('metin')} for p in parts]
    else:
        evidence = [{'sayfa': cand.get('sayfa'), 'metin': cand.get('kanit_cumle')}]
    source = []
    for part in evidence:
        page_text = str(pages.get(part['sayfa'], ''))
        if cand.get('kanit_turu') == 'tablo' and not parts:
            ctx = table_context(part['metin'] or '', page_text)
        else:
            ctx = text_context(part['metin'] or '', page_text)
        source.append({'sayfa': part['sayfa'], 'kanit_metni': part['metin'], 'baglam': ctx})
    return {'dogrulayici_surumu': SURUM, 'kayit': record, 'kanit_turu': cand.get('kanit_turu'), 'kaynak': source}


# ---------------------------------------------------------------------------
# Yanıt doğrulama
# ---------------------------------------------------------------------------

class GecersizCikti(ValueError):
    pass


def parse_output(findings):
    """request_findings'in döndürdüğü liste -> doğrulanmış karar sözlüğü; şema dışıysa GecersizCikti."""
    if not isinstance(findings, list) or len(findings) != 1 or not isinstance(findings[0], dict):
        raise GecersizCikti('yanıt tek elemanlı findings listesi değil')
    out = findings[0]
    if out.get('karar') not in KARARLAR:
        raise GecersizCikti(f"geçersiz karar: {out.get('karar')!r}")
    fields = out.get('hatali_alanlar', [])
    if not isinstance(fields, list) or any(not isinstance(f, dict) or not isinstance(f.get('alan'), str) for f in fields):
        raise GecersizCikti('hatali_alanlar biçimi geçersiz')
    quotes = out.get('kanit_alintilari', [])
    if not isinstance(quotes, list) or any(not isinstance(q, dict) or not isinstance(q.get('metin'), str)
                                           for q in quotes):
        raise GecersizCikti('kanit_alintilari biçimi geçersiz')
    if not isinstance(out.get('gerekce'), str) or not out['gerekce'].strip():
        raise GecersizCikti('gerekce boş')
    if not isinstance(out.get('yeniden_cikarim_gerekli'), bool):
        raise GecersizCikti('yeniden_cikarim_gerekli bool değil')
    return out


def verify_quotes(quotes, cand, pages):
    """Her alıntı kaydın kanıt sayfalarından birinde source_span ile doğrulanır (birebir ya da yalnız biçim farkı,
    tekil). [(alıntı, sayfa, doğrulandı, kaynaktaki birebir metin)]."""
    from src.rag_klinik_cikarim import source_span
    allowed = {cand.get('sayfa')}
    end = cand.get('sayfa_bitis') or cand.get('sayfa')
    if isinstance(cand.get('sayfa'), int) and isinstance(end, int):
        allowed |= set(range(cand['sayfa'], end + 1))
    out = []
    for q in quotes:
        page = q.get('sayfa')
        page = int(page) if isinstance(page, (int, str)) and str(page).isdigit() else None
        span = source_span(q['metin'], str(pages.get(page, ''))) if page in allowed else None
        out.append({'metin': q['metin'], 'sayfa': page, 'dogrulandi': span is not None, 'kaynak_metni': span})
    return out


def decide(parsed, quotes):
    """(nihai karar, alıntı doğrulaması geçti mi). Alıntı gerektiren kararda alıntı yoksa ya da biri kaynakta
    doğrulanamıyorsa karar 'belirsiz'e indirilir."""
    needs_quote = parsed['karar'] != 'yetersiz_kanit'
    ok = bool(quotes) and all(q['dogrulandi'] for q in quotes) if needs_quote else all(q['dogrulandi'] for q in quotes)
    return (parsed['karar'] if ok else 'belirsiz'), ok


# ---------------------------------------------------------------------------
# Çalıştırma (bütçe, önbellek, hata güvenliği, token)
# ---------------------------------------------------------------------------

class KullanimSayaci:
    """İstemci sarmalayıcısı: chat.completions.create çağrılarının token kullanımını toplar (yanıt değişmez)."""

    def __init__(self, client):
        self._client = client
        self.cagri = 0
        self.girdi = 0
        self.cikti = 0
        outer = self

        class _Completions:
            def create(self, **kw):
                response = outer._client.chat.completions.create(**kw)
                outer.cagri += 1
                usage = getattr(response, 'usage', None)
                outer.girdi += int(getattr(usage, 'prompt_tokens', 0) or 0)
                outer.cikti += int(getattr(usage, 'completion_tokens', 0) or 0)
                return response

        class _Chat:
            completions = _Completions()
        self.chat = _Chat()


class ButceAsildi(RuntimeError):
    pass


def run(items, pages_by_doc, client, model, cache, *, max_calls, attempts=3, sleep=None):
    """items: [{'kimlik', 'belge', 'aday', ...}]. Önbellekte olmayan çağrı sayısı max_calls'ı aşarsa hiç çağrı
    yapılmadan ButceAsildi. Dönen: (sonuçlar, kullanım sayacı)."""
    from src.pilot_auto_extract import request_with_retry
    from src.rag_klinik_cikarim import Cache
    prepared = []
    for item in items:
        payload = build_payload(item['aday'], pages_by_doc[item['belge']])
        prepared.append((item, payload, Cache.key(model, SYSTEM, payload)))
    pending = sum(cache.get(key) is None for _, _, key in prepared)
    if pending > max_calls:
        raise ButceAsildi(f'çağrı bütçesi yetersiz: {pending} bekleyen çağrı > {max_calls}; hiç çağrı yapılmadı')
    counter = KullanimSayaci(client) if client is not None else None
    results = []
    for item, payload, key in prepared:
        base = {'kimlik': item['kimlik'], 'belge': item['belge'], 'grup': item.get('grup'), 'model': model,
                'dogrulayici_surumu': SURUM, 'onbellek_anahtari': key, 'insan_dogrulamasi_degildir': True}
        findings = cache.get(key)
        source = 'onbellek'
        if findings is None:
            source = 'api'
            try:
                kw = {'attempts': attempts} if sleep is None else {'attempts': attempts, 'sleep': sleep}
                findings = request_with_retry(counter, model, SYSTEM, payload, item['aday'].get('sayfa'), **kw)
            except ValueError as exc:                    # eksik / geçersiz JSON yanıt
                results.append({**base, 'durum': 'gecersiz_cikti', 'karar': None, 'hata': str(exc), 'kaynak': source})
                continue
            except Exception as exc:  # noqa: BLE001 - zaman aşımı / kota / bağlantı: karar üretilmez
                results.append({**base, 'durum': 'calistirilamadi', 'karar': None,
                                'hata': f'{type(exc).__name__}: {exc}', 'kaynak': source})
                continue
        try:
            parsed = parse_output(findings)
        except GecersizCikti as exc:
            results.append({**base, 'durum': 'gecersiz_cikti', 'karar': None, 'hata': str(exc), 'kaynak': source})
            continue
        if source == 'api':
            cache.put(key, findings)                     # yalnız şemaya uyan yanıt önbelleğe girer
        quotes = verify_quotes(parsed.get('kanit_alintilari', []), item['aday'], pages_by_doc[item['belge']])
        final, quotes_ok = decide(parsed, quotes)
        results.append({**base, 'durum': 'tamamlandi', 'kaynak': source, 'model_karari': parsed['karar'],
                        'karar': final, 'alinti_dogrulama': quotes_ok, 'alintilar': quotes,
                        'hatali_alanlar': parsed.get('hatali_alanlar', []), 'gerekce': parsed['gerekce'],
                        'konum': parsed.get('konum'), 'yeniden_cikarim_gerekli': parsed['yeniden_cikarim_gerekli']})
    return results, counter


# ---------------------------------------------------------------------------
# Pilot ölçümleri (LLM kararı klinik doğruluk oranı DEĞİLDİR)
# ---------------------------------------------------------------------------

def metrics(results, sample, references=None, price=None):
    """sample: {kimlik: örnek kaydı (grup, makale, kanit_turu, bilgi_turu, deterministik_sonuc ...)};
    references: {kimlik: referansla toleranslı eşleşme bool} — YALNIZ pilot sonrası karşılaştırma için;
    price: {'girdi': USD / 1M token, 'cikti': USD / 1M token} verilirse maliyet."""
    from collections import Counter, defaultdict
    done = [r for r in results if r['durum'] == 'tamamlandi']
    by = defaultdict(Counter)
    for r in done:
        s = sample[r['kimlik']]
        by['makale:' + s['makale']][r['karar']] += 1
        by['kanit_turu:' + str(s['kanit_turu'])][r['karar']] += 1
        by['grup:' + s['grup']][r['karar']] += 1
    problem = ('kismen_destekleniyor', 'kaynakla_celisiyor')
    det_pass_issue = [r['kimlik'] for r in done if sample[r['kimlik']].get('deterministik_sonuc') == 'gecti'
                      and r['karar'] in problem]
    det_flag_supported = [r['kimlik'] for r in done if sample[r['kimlik']].get('deterministik_sonuc')
                          in ('belirsiz', 'basarisiz') and r['karar'] == 'destekleniyor']
    out = {
        'not': 'LLM karar dağılımı klinik doğruluk oranı değildir; semantik doğrulama insan doğrulaması değildir.',
        'durum': dict(Counter(r['durum'] for r in results)),
        'karar': dict(Counter(r['karar'] for r in done)),
        'model_karari': dict(Counter(r['model_karari'] for r in done)),
        'hatali_alan_turu': dict(Counter(f['alan'] for r in done for f in r['hatali_alanlar'])),
        'yeniden_cikarim_gerekli': sum(r['yeniden_cikarim_gerekli'] for r in done),
        'alinti_dogrulama': {'gecti': sum(r['alinti_dogrulama'] for r in done),
                             'kaldi': sum(not r['alinti_dogrulama'] for r in done),
                             'alinti_sayisi': sum(len(r['alintilar']) for r in done),
                             'dogrulanan_alinti': sum(q['dogrulandi'] for r in done for q in r['alintilar'])},
        'deterministik_gecti_semantik_sorunlu': det_pass_issue,
        'deterministik_isaretli_semantik_destekli': det_flag_supported,
        'kirilimlar': {k: dict(v) for k, v in sorted(by.items())},
    }
    if references is not None:
        out['referansla_eslesen_semantik_sorunlu'] = [r['kimlik'] for r in done if references.get(r['kimlik'])
                                                      and r['karar'] in problem]
    return out


def usage_report(counter, price=None):
    if counter is None:
        return {'cagri': 0, 'girdi_token': 0, 'cikti_token': 0}
    out = {'cagri': counter.cagri, 'girdi_token': counter.girdi, 'cikti_token': counter.cikti}
    if price:
        out['maliyet_usd'] = round(counter.girdi / 1e6 * price['girdi'] + counter.cikti / 1e6 * price['cikti'], 4)
    return out
