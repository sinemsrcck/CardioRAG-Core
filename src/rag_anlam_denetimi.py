"""RAG klinik kayıtları için değer anlamı ve klinik bağlam denetimi (LLM'siz, deterministik).

Çıkarım hattında tekrar ayıklamasından ve tablo incelemesinden SONRA çalışır (rag_klinik_cikarim.extract).
Hiçbir kural makaleye özel değildir; kaynak metin, kanıt cümlesi ve klinik bilgi metni DEĞİŞTİRİLMEZ, yeni klinik bilgi
üretilmez. Her bulgu dört sınıftan birine girer:

  1. Otomatik düzeltme: kaynakta kesin olarak doğrulanan, tek anlamlı düzeltme. Yapılandırılmış alan değişir, eski değer
     kayıtta ('anlam_denetimi.ham_deger') ve 'otomatik_duzeltme' notunda kalır.
       - değerdeki kaynakça numarası (mevcut CITATION deseni / çok numaralı üst simge): ayrılan değer kanıtta birebir
         geçiyorsa 'deger' temizlenir; geçmiyorsa 'deger' özgün kalır, temiz biçim notta durur.
  2. Ayrılan (deterministik, bilgi taşımayan): değeri yalnız 'bildirilmemiş' işareti (NA, NR, not reported, tek başına
     tire) olan kayıt; gerçek sıfır ('0', '0.0', '0%') ayrılmaz. Kayıt silinmez, ret raporuna yazılır.
  3. İnceleme (gerçekten belirsiz, gerekçeli): türün kanıtla çeliştiği ama doğrusunun kaynakta tek olmadığı durumlar
     (hasta sayısı / süre 'doz' olarak, doz birimi yok, cihaz 'ilaç' olarak, dahil edilme kriteri 'tanı kriteri' olarak,
     amaç / sınırlılık cümlesi 'bulgu' olarak, GA alanında IQR / aralık).
  4. Yeniden çıkarım gerekli (yeni LLM çağrısı ister): klinik bilginin anlamını tersine çeviren olumsuzluk /
     kontrendikasyon ya da koşul ifadesi bilgi metninin DIŞINDA kalmış. Doğru metin aralığını üretmek yeni çıkarım
     gerektirir; önbellekten yalnız tespit edilir.

Ayrıca kayıt AYRILMADAN 'anlam_denetimi' meta verisine yazılanlar (adaptör taslak_notu'na taşır):
  - deger_bilesenleri: tanımlayıcı istatistik bileşenleri (ortalama / SD / SE / medyan / Q1–Q3 / min–maks / n (%));
    yalnız biçim etiketi bileşeni açıkça adlandırıyorsa adlandırılır, aksi halde tür 'belirtilmemiş' kalır.
  - uyarilar: tanımlayıcı istatistik bileşenlerinin iç tutarlılığı (aralık sırası, merkez değer min–maks / IQR
    dışında, n (%) etiketli değerde yüzde > 100 ya da grup n'i ile uyumsuz), atıflı değer. Değerler tahminle
    DEĞİŞTİRİLMEZ. GA–p, p aralığı, GA sırası ve değerin GA içinde olması src.data_quality kontrolleridir; bağımsız
    doğrulama aşamasında (src.rag_dogrulama) çalıştırılır, burada tekrarlanmaz.
  - kapsam_uyarisi + semantik_dogrulama_gerekli: bilgi metninin dışında kalan koşul / hasta grubu / zaman öbeği.
Atıflı değerler 'literature_background' bağlamına TAŞINMAZ; yalnız işaretlenir.
"""
import re

import src.pilot_tablo as PT

NOTE_KEY = 'anlam_denetimi'
STAGE_MISSING = 'deger_bildirilmemis'
STAGE_REVIEW = 'anlam_inceleme'
STAGE_REEXTRACT = 'yeniden_cikarim_gerekli'

# ---------------------------------------------------------------------------
# Değer biçimi yardımcıları (pilot_tablo desenleri yeniden kullanılır)
# ---------------------------------------------------------------------------

_MARKUP = re.compile(r'\*\*|(?<!\w)_(?=\S)|(?<=\S)_(?!\w)|</?(?:i|b|em|strong)>', re.I)
_BR = re.compile(r'<br\s*/?>', re.I)
# Tek başına "bildirilmemiş" işareti. 'B-NR' gibi kanıt düzeyleri ya da 'NR' içeren sözcükler EŞLEŞMEZ (tam eşleşme).
MISSING = re.compile(r'^(?:n\s*/?\s*a|n\.\s*a\.|nr|n\.\s*r\.|nd|n\.\s*d\.|not\s+(?:reported|available|applicable|done|'
                     r'assessed|measured)|unknown|none\s+reported|[-–—−‒]|\.{3}|…)$', re.I)
ZERO = re.compile(r'^0+(?:[.,]0+)?\s*%?$')
# Sayı: ASCII eksi, Unicode eksi (U+2212) ve sayının başındaki en tire negatif işarettir; iki sayı arasındaki tire
# ayraçtır. Binlik virgül ('2,735.621') desteklenir; ondalık virgül yalnız tek virgül ve 1–2 haneli kesirde.
_NUM = r'[-−–]?\s?\d+(?:,\d{3})*(?:\.\d+)?'


def clean(text):
    """Karşılaştırma biçimi: markdown vurgusu ve <br> boşluk olur; tire ve sayı karakterlerine dokunulmaz."""
    return re.sub(r'\s+', ' ', _BR.sub(' ', _MARKUP.sub('', str(text or '')))).strip()


def to_number(text):
    """'−1.5' / '-1.5' / '–1.5' -> -1.5; '2,735.621' -> 2735.621; '<0.001' -> 0.001 (karşılaştırma işareti atılır).
    Sayı değilse None."""
    s = clean(text).replace('−', '-').replace('–', '-').replace('—', '-')
    s = re.sub(r'^[<>≤≥=]+\s*', '', s).rstrip('%').strip()
    if re.fullmatch(r'-?\s?\d+(?:,\d{3})+(?:\.\d+)?', s):
        s = s.replace(',', '')
    elif re.fullmatch(r'-?\s?\d+,\d{1,2}', s):
        s = s.replace(',', '.')
    s = s.replace(' ', '')
    try:
        return float(s)
    except ValueError:
        return None


RANGE = re.compile(rf'^\s*({_NUM})\s*(?:[-–—‒]|to)\s*({_NUM})\s*$', re.I)


def split_range(text):
    """'0.892-1.372' / '−2.1 – −0.5' / '1 to 3' -> (alt, üst) sayıları; ilk tire negatif işaret olabilir, ayraç olan tire
    iki sayının arasındakidir. Aralık değilse None."""
    match = RANGE.match(clean(text).replace('−', '-'))
    if not match:
        return None
    low, high = to_number(match.group(1)), to_number(match.group(2))
    return None if low is None or high is None else (low, high)


def missing_marker(value):
    """Değer yalnız 'bildirilmemiş' işaretiyse işaret (alt alta hücrede bütün parçalar işaret olmalı); yoksa None.
    Gerçek sıfır işaret değildir."""
    if value in (None, ''):
        return None
    parts = [clean(p) for p in _BR.split(str(value))]
    parts = [p for p in parts if p]
    if parts and all(MISSING.match(p) for p in parts):
        return ' / '.join(parts)
    return None


# ---------------------------------------------------------------------------
# Kaynakça numaraları
# ---------------------------------------------------------------------------

# rag_klinik_cikarim.CITATION ile aynı biçim: italik parantezli numara '_(30)_', '_(12, 14)_', '_(3–5)_'.
CITATION_MD = re.compile(r'\s*_\(\s*\d+(?:\s*[,–-]\s*\d+)*\s*\)_')
# Üst simge kaynakça: yalnız ÇOK numaralı liste / aralık ('<sup>4,52</sup>', '<sup>3-5</sup>'). Tek numaralı üst simge
# ('m<sup>2</sup>', '10<sup>3</sup>', 'B-R<sup>4</sup>') üs ya da dipnot olabilir; belirsiz olduğu için dokunulmaz.
CITATION_SUP = re.compile(r'\s*<sup>\s*\d+(?:\s*[,–-]\s*\d+)+\s*</sup>', re.I)


def split_citations(value):
    """(kaynakçasız değer, numaralar). Kaynakça yoksa (değer, [])."""
    text = str(value or '')
    numbers = []
    for pattern in (CITATION_MD, CITATION_SUP):
        for match in pattern.finditer(text):
            numbers += re.findall(r'\d+', match.group(0))
        text = pattern.sub('', text)
    return re.sub(r'\s{2,}', ' ', text).strip(), numbers


# ---------------------------------------------------------------------------
# Tanımlayıcı istatistik bileşenleri
# ---------------------------------------------------------------------------

def _label_terms(label):
    low = (label or '').casefold()
    return {
        'sd': bool(re.search(r'\bs\.?d\.?\b|\bss\b|standard deviation', low)),
        'se': bool(re.search(r'\bsem?\b|\bs\.e\.?m?\.?|standard error', low)),
        'median': 'median' in low or 'medyan' in low,
        'mean': bool(re.search(r'\bmean\b|ortalama|\bx\b|x̄', low)),
        'iqr': bool(re.search(r'iqr|interquartile|q1|q3|25(?:th)?\s*[-–,]\s*75', low)),
        'range': bool(re.search(r'\bmin|\bmax|range|aral[ıi]k', low)),
        'ci': bool(re.search(r'\bci\b|confidence|güven', low)),
        'n_pct': bool(re.search(r'\bn\b', low.replace('min', '').replace('mean', ''))) and '%' in low,
    }


def value_components(value, label):
    """Biçim etiketi bileşenleri AÇIKÇA adlandırıyorsa adlandırılmış bileşenler; etiket yoksa ya da eksikse tür
    'belirtilmemiş' olarak döner. Değer pilot_tablo biçim desenlerinden birine uymuyorsa None."""
    v = clean(value)
    t = _label_terms(label)
    if PT.MEAN_SD_RANGE.match(v):
        center, rest = v.split('±', 1)
        spread, rng = rest.split('(', 1)
        low, high = _range_texts(rng.rstrip(') '))
        if low is None:
            return None
        spread_name = 'sd' if t['sd'] and not t['se'] else 'se' if t['se'] and not t['sd'] else None
        out = {'bicim': 'ortalama ± yayılım (aralık)', 'merkez': center.strip(), 'yayilim': spread.strip(),
               'aralik_alt': low, 'aralik_ust': high}
        out['merkez_turu'] = 'ortalama' if t['mean'] or spread_name else 'belirtilmemiş'
        out['yayilim_turu'] = spread_name or 'belirtilmemiş'
        out['aralik_turu'] = 'min–maks' if t['range'] else 'belirtilmemiş'
        return out
    if PT.MEAN_SD.match(v):
        center, spread = (x.strip() for x in v.split('±', 1))
        spread_name = 'sd' if t['sd'] and not t['se'] else 'se' if t['se'] and not t['sd'] else None
        return {'bicim': 'merkez ± yayılım', 'merkez': center, 'yayilim': spread,
                'merkez_turu': 'ortalama' if t['mean'] or spread_name else 'belirtilmemiş',
                'yayilim_turu': spread_name or 'belirtilmemiş (SD / SE)'}
    if PT.N_PCT.match(v) and not PT.VAL_RANGE.match(v):
        n, pct = v.split('(', 1)
        if t['n_pct']:
            return {'bicim': 'n (%)', 'n': n.strip(), 'yuzde': pct.rstrip(') ').strip()}
        return {'bicim': 'değer (değer)', 'deger': n.strip(), 'parantez': pct.rstrip(') ').strip(),
                'parantez_turu': 'belirtilmemiş'}
    match = PT.VAL_RANGE.match(v)
    if match:
        center, low, high = match.groups()
        if t['median'] and t['iqr']:
            kind, center_kind = 'IQR (Q1–Q3)', 'medyan'
        elif t['median'] and t['range']:
            kind, center_kind = 'min–maks', 'medyan'
        elif t['ci'] and not (t['iqr'] or t['range']):
            kind, center_kind = '%95 GA', 'nokta tahmini'
        else:
            kind, center_kind = 'belirtilmemiş', 'medyan' if t['median'] else 'belirtilmemiş'
        return {'bicim': 'değer (aralık)', 'merkez': center, 'merkez_turu': center_kind, 'aralik_alt': low,
                'aralik_ust': high, 'aralik_turu': kind}
    return None


def _range_texts(text):
    """'56-90' / '−3–5' -> ('56', '90'): ayraç, ilk sayıdan SONRAKİ ilk tiredir (negatif işaret bölünmez)."""
    match = RANGE.match(text)
    return (match.group(1).strip(), match.group(2).strip()) if match else (None, None)


# Belge düzeyinde biçim bildirimi (genellikle yöntem bölümü): "Continuous variables were expressed as mean ± standard
# deviation", "Categorical variables ... as numbers and percentages / n (%)".
_DECLARE = r'(?:expressed|presented|reported|given|shown|summari[sz]ed|described|displayed)\s+as\b'
DECLARE_SD = re.compile(_DECLARE + r'[^.]{0,40}?(?:mean|average)\s*(?:±|\+/-|and|with)?\s*(?:the\s+)?'
                        r'(?:standard\s+deviation|SD|S\.D\.)', re.I)
DECLARE_SE = re.compile(_DECLARE + r'[^.]{0,40}?(?:mean|average)\s*(?:±|\+/-|and|with)?\s*(?:the\s+)?'
                        r'(?:standard\s+error|SEM?|S\.E\.M?\.?)', re.I)
DECLARE_N_PCT = re.compile(_DECLARE + r'[^.]{0,40}?(?:n\s*\(\s*%\s*\)|numbers?\s+(?:and|with)\s+(?:the\s+)?percentages?|'
                           r'percentages?\s+and\s+(?:the\s+)?numbers?|frequenc(?:y|ies)\s+(?:and|with)\s+percentages?|'
                           r'counts?\s+(?:and|with)\s+percentages?)', re.I)


def document_formats(pages):
    """{'±': 'sd' | 'se' | None, 'n (%)': bool}. ± anlamı yalnız belge TEK tür (yalnız SD ya da yalnız SE) bildiriyorsa."""
    text = ' '.join(clean(t) for t in (pages or {}).values())
    sd, se = bool(DECLARE_SD.search(text)), bool(DECLARE_SE.search(text))
    return {'±': 'sd' if sd and not se else 'se' if se and not sd else None, 'n (%)': bool(DECLARE_N_PCT.search(text))}


def _format_label(item, doc_formats=None):
    """(biçim etiketi, kaynağı). Önce tablo modelindeki ara başlık / sütun biçimi (değerle tutarlıysa); yoksa belgenin
    yöntem bölümündeki tek anlamlı bildirim. Hiçbiri yoksa (None, None): bileşen türü 'belirtilmemiş' kalır."""
    model = item.get('tablo_modeli') or {}
    value = clean(item.get('deger'))
    for label in (model.get('bicim_etiketi'), model.get('sutun')):
        if label and PT.format_matches(label, value):
            return label, 'tablo'
    formats = doc_formats or {}
    if formats.get('±') and (PT.MEAN_SD.match(value) or PT.MEAN_SD_RANGE.match(value)):
        return f"mean ± {formats['±']}", 'belge_bildirimi'
    if formats.get('n (%)') and PT.N_PCT.match(value) and not PT.VAL_RANGE.match(value):
        return 'n (%)', 'belge_bildirimi'
    return None, None


# ---------------------------------------------------------------------------
# Kaynak içi sayısal tutarlılık (yalnız uyarı; değer değişmez)
# ---------------------------------------------------------------------------

def consistency_warnings(item, components):
    """Yalnız tanımlayıcı istatistik BİLEŞENLERİNİN iç tutarlılığı (data_quality'de karşılığı yok): aralık sırası,
    merkez değerin min–maks / IQR içinde olması, n (%) etiketli değerde yüzde ve grup n'i. p aralığı, GA sırası,
    değerin GA içinde olması, %-işaretli yüzde ve GA–p uyumu src.data_quality'nin (check_numeric, ci_p_assessment)
    işidir ve bağımsız doğrulama aşamasında (src.rag_dogrulama) oradan çalıştırılır; burada tekrarlanmaz."""
    out = []
    if components:
        num = {k: to_number(components.get(k)) for k in ('merkez', 'aralik_alt', 'aralik_ust', 'n', 'yuzde')}
        if num['aralik_alt'] is not None and num['aralik_ust'] is not None:
            if num['aralik_alt'] > num['aralik_ust']:
                out.append(f'aralık alt sınırı üst sınırdan büyük ({item.get("deger")})')
            elif num['merkez'] is not None and components.get('aralik_turu') in ('min–maks', 'IQR (Q1–Q3)') \
                    and not num['aralik_alt'] <= num['merkez'] <= num['aralik_ust']:
                out.append(f'merkez değer {components["aralik_turu"]} aralığının dışında ({item.get("deger")})')
        if components.get('bicim') == 'n (%)' and num['yuzde'] is not None:
            if num['yuzde'] > 100:
                out.append(f'yüzde 100\'den büyük ({item.get("deger")})')
            group_n = _group_n(item)
            if group_n and num['n'] is not None:
                decimals = len(components['yuzde'].split('.')[1]) if '.' in components['yuzde'] else 0
                tolerance = 0.5 * 10 ** -decimals + 0.05
                if abs(num['n'] / group_n * 100 - num['yuzde']) > tolerance:
                    out.append(f'n (%) grup n\'i ile uyumsuz ({item.get("deger")}, grup n={group_n})')
    return out


def _group_n(item):
    model = item.get('tablo_modeli') or {}
    value = model.get('grup_n')
    if isinstance(value, (int, float)) and value > 0:
        return value
    match = re.search(r'\(\s*n\s*=\s*(\d[\d,]*)\s*\)', str(item.get('hasta_grubu') or ''), re.I)
    return to_number(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Tür / bağlam çelişkileri (inceleme)
# ---------------------------------------------------------------------------

DOSE_UNIT = re.compile(r'(?<![a-z])(?:mg|mcg|µg|μg|ug|ng|g|kg|ml|mL|l|iu|IU|units?|u|mmol|mol|meq|mEq)(?:\s*/\s*'
                       r'(?:kg|m2|m<sup>2</sup>|day|d|h|hr|min|dose|ml|l))?(?![a-z])', re.I)
PATIENT_COUNT = re.compile(r'\b\d[\d,]*\s*(?:patients?|subjects?|participants?|individuals?|cases?|persons?|people|'
                           r'women|men|children)\b|\bn\s*=\s*\d', re.I)
DURATION = re.compile(r'\b\d+(?:\.\d+)?\s*(?:-|–|to)?\s*\d*\s*(?:years?|months?|weeks?|days?|hours?|hrs?|minutes?|'
                      r'yıl|ay|hafta|gün|saat)\b|\(\s*[<>≥≤]?\s*\d+\s*(?:years?|months?|weeks?|days?)\s*\)', re.I)
# Kullanım sıklığı / rejim doz bilgisinin parçasıdır (birimsiz olabilir); tedavi süresi değildir.
FREQUENCY = re.compile(r'\b(?:daily|once|twice|thrice|(?:three|four)\s+times|bid|b\.i\.d\.|tid|t\.i\.d\.|qid|qd|o\.d\.|'
                       r'nightly|weekly|monthly|per\s+day|a\s+day|every\s+\d+\s*(?:h|hours?|days?|weeks?)|q\d+h|'
                       r'loading|maintenance|bolus|günde|haftada)\b', re.I)
DEVICE = re.compile(r'\b(?:stents?|catheters?|pacemakers?|defibrillators?|ICDs?|CRT(?:-[DP])?|valves?|balloons?|'
                    r'devices?|implants?|occluders?|grafts?|monitors?|recorders?|ILR|LVAD|pumps?|wires?|leads?)\b')
DRUG_HINT = re.compile(r'(?:mab|nib|pril|sartan|olol|statin|parin|xaban|gatran|dipine|azole|mycin|cillin|floxacin|'
                       r'thiazide|semide|tide|gliflozin)\b|\b(?:drug(?!-eluting|\s+eluting)|medication|agent|therapy|inhibitor|antagonist|'
                       r'agonist|blocker|anticoagula\w*|antiplatelet\w*|aspirin|warfarin|heparin)\b', re.I)
INCLUSION = re.compile(r'\b(?:were|was)\s+(?:included|enrolled|recruited|eligible)\b|\binclusion criteria\b|'
                       r'\beligib(?:le|ility)\b|\benrolled\b', re.I)
DIAGNOSTIC_CRITERION = re.compile(r'\bdiagnos\w*|\bdefined as\b|\bdefinition\b|\bcriteri\w* for\b|\bconfirmed\b', re.I)
AIM = re.compile(r'^\W*(?:the\s+)?(?:(?:main|primary|principal)\s+)?(?:aims?|objectives?|purposes?|goals?)\s+of\s+'
                 r'(?:this|the\s+present|our)\b|\b(?:this|the\s+present|our)\s+study\s+(?:aims?|aimed|was\s+designed|'
                 r'sought|seeks)\b|\bwe\s+(?:aimed|sought|aim|intended)\s+to\b', re.I)
# Sınırlılık cümlesi: sınırlılık cümlenin öznesi ('One limitation of ... is', 'Our study has several limitations',
# '... is limited by'); 'Despite these limitations, ...' / 'Although ... the main limitation, ...' yan öbektir, değil.
LIMITATION = re.compile(r'^\W*(?:(?:an?|one|another|the|a\s+further|other|several|some|(?:the\s+)?(?:main|major|first|'
                        r'second|third|primary|key|important))\s+)*limitations?\s+(?:of|was|is|were|are|include|includes|'
                        r'relates?|concerns?|lies)\b|\b(?:study|analysis|review|trial|work)\s+(?:has|had|have)\s+'
                        r'(?:\w+\s+){0,2}limitations?\b|\b(?:is|was|are|were)\s+limited\s+by\b', re.I)


def type_problem(item):
    """Türün kanıtla çeliştiği, doğrusunun tek olmadığı durum -> inceleme gerekçesi; yoksa None."""
    kind, context = item.get('bilgi_turu'), item.get('baglam')
    fact = clean(item.get('klinik_bilgi_en'))
    value = clean(item.get('deger'))
    unit = clean(item.get('unit'))
    quote = clean(item.get('kanit_cumle'))
    own = ' '.join(x for x in (fact, value, unit) if x)
    if kind == 'doz':
        if PATIENT_COUNT.search(own) and not DOSE_UNIT.search(own):
            return 'tür doz ama değer hasta sayısı'
        if DURATION.search(own) and not DOSE_UNIT.search(own):
            return 'tür doz ama değer tedavi süresi / zaman aralığı (doz birimi yok)'
        if not DOSE_UNIT.search(own) and not FREQUENCY.search(own):
            return 'tür doz ama doz birimi yok'
    if kind == 'ilac' and DEVICE.search(fact) and not DRUG_HINT.search(fact):
        return 'tür ilaç ama bilgi tıbbi cihaz / işlem'
    if kind == 'tani_kriteri' and INCLUSION.search(quote) and not DIAGNOSTIC_CRITERION.search(quote):
        return 'tür tanı kriteri ama kanıt araştırmaya dahil edilme kriteri'
    if context == 'study_finding' and AIM.search(quote):
        return 'bağlam bulgu ama kanıt araştırma amacı cümlesi'
    if context == 'study_finding' and LIMITATION.search(quote):
        return 'bağlam bulgu ama kanıt sınırlılık cümlesi'
    if item.get('ci95'):
        label = ' '.join(str(x) for x in ((item.get('tablo_modeli') or {}).get('bicim_etiketi'),
                                           (item.get('tablo_modeli') or {}).get('sutun')) if x)
        t = _label_terms(label)
        if label and (t['iqr'] or t['range'] or t['sd']) and not t['ci']:
            return f'ci95 dolu ama sütun biçimi GA değil ({label})'
    return None


# ---------------------------------------------------------------------------
# Olumsuzluk / koşul kapsamı (yeniden çıkarım)
# ---------------------------------------------------------------------------

NEGATION = re.compile(r"\b(?:no|not|never|without|neither|nor|none\s+of|absence\s+of|lack\s+of|free\s+of|"
                      r"avoid(?:ed|ing)?|contraindicat\w*|unless|except|cannot)\b|n't\b", re.I)
NEGATION_AFTER = re.compile(r"^\W{0,3}(?:(?:is|are|was|were|does|do|did|has|have|had|should|could|can|may|might|must|"
                            r"will|would)\s+)?(?:not|never)\b|^\W{0,3}\w+n't\b|^\W{0,3}(?:is|are|was|were|should\s+be)\s+"
                            r"(?:contraindicated|avoided|discouraged)\b|^\W{0,3}cannot\b", re.I)
# Her iki seçeneği birlikte söyleyen kalıplar olumsuzluk değildir ('may or may not', 'with or without').
HEDGE = re.compile(r'\b(?:may|might|can|could|does|do|did|is|are|was|were)\s+or\s+(?:may|might|can|could|does|do|did|is|'
                   r'are|was|were)\s+not\b|\bwith\s+or\s+without\b|\bwhether\s+or\s+not\b|\bor\s+not\b', re.I)
# Yan cümle sınırı (virgül sınır değildir: virgüllü koşul öbeği ayrıca ele alınır).
CLAUSE_BREAK = re.compile(r'[.;:!?]|\b(?:but|whereas|while|although|however|though)\b', re.I)
PREPOSITIONS = {'of', 'in', 'on', 'to', 'for', 'with', 'by', 'at', 'from', 'about', 'underlying', 'regarding', 'between',
                'among', 'into', 'than', 'during', 'after', 'before', 'via', 'over', 'under', 'within', 'across'}
# Olumsuzlanan ilişki adı ('no difference in X', 'no association with X'): olumsuzluk X hakkındaki iddiayı yönetir.
RELATION_NOUN = re.compile(r'\b(?:differences?|associations?|correlations?|relationships?|relations?|effects?|benefits?|'
                           r'evidence|roles?|changes?|increases?|decreases?|reductions?|improvements?|impact|risks?|need|'
                           r'indications?|significance|link|signs?|history)$', re.I)
VERB_AFTER = re.compile(r'^\W{0,3}(?:is|are|was|were|has|have|had|should|can|could|may|might|must|will|would|does|do|did|'
                        r'remains?|seems?|appears?|provides?|offers?|reduces?|increases?|improves?)\b', re.I)
# Virgüllü öncül öbek yalnız koşul / hasta grubu belirteciyle başlıyorsa kapsam sayılır (en çok üç sözcüklük bağlaç
# öbeği atlanır: 'Differently, in patients ...', 'On the contrary, if ...'); karşıtlık yan cümlesi ('although ...') değil.
CONDITION_START = re.compile(r'^\W*(?:[A-Za-z]+(?:\s+[A-Za-z]+){0,2},\s*)?(?:in|for|among|amongst|if|when|whenever|unless|'
                             r'except|once|after|before|during|until|provided|only|where)\b', re.I)
CONCESSIVE = re.compile(r'although|though|while|whereas|but|however', re.I)
WINDOW_BEFORE = 8
WINDOW_GAP = 4


def _fact_span(fact, quote):
    """Bilgi metninin kanıttaki konumu (biçimlendirme yok sayılarak); tekil değilse None."""
    if not fact:
        return None
    hits = [m.start() for m in re.finditer(re.escape(fact), quote)]
    if len(hits) != 1:
        hits = [m.start() for m in re.finditer(re.escape(fact), quote, re.I)]
    if len(hits) != 1:
        return None
    return hits[0], hits[0] + len(fact)


def _is_term(fact):
    return len(fact.split()) <= 3 and not VERB_AFTER.search(' ' + fact)


def scope_problem(item):
    """(tür, gerekçe) ya da None. Bilgi metni kanıtta tekil konumdaysa, aynı yan cümledeki olumsuzluk / koşul ifadesi:
      - 'tersine': olumsuzluk bilgiyi doğrudan yönetiyor ve bilgi metninin dışında kalmış (anlam ters dönmüş):
          * olumsuzluk ile bilgi arasında virgül yok, en fazla 4 sözcük; aradaki sözcükler bir edatla bitiyorsa yalnız
            olumsuzlanan ilişki adıyla ('no difference in X') — 'without P waves on [ECG]' X'i olumsuzlamaz;
          * aynı olumsuzluğun virgüllü listesindeki terim ('absence of HF, [ischemia]');
          * bilgi yan cümlenin öznesi ve hemen ardından olumsuz yüklem geliyor ('[X] will not be sufficient');
            bilgi bir edatın tümleciyse ('presence of [DM] could not be achieved') özne değildir.
      - 'kapsam': olumsuzluk / koşul virgülle ayrılmış öncül öbekte ('in patients without AF, [X is reasonable]'):
          anlam ters dönmez, uygulama koşulu / hasta grubu bilgi metninde yoktur.
    exclusion_criterion bağlamı olumsuzluğu zaten taşır; olumsuzluğu bilgi metninin içinde olan kayıt sorunsuzdur;
    'may or may not' / 'with or without' olumsuzluk sayılmaz."""
    if item.get('baglam') == 'exclusion_criterion' or item.get('kanit_turu') == 'tablo':
        return None
    fact = clean(item.get('klinik_bilgi_en'))
    quote = clean(item.get('kanit_cumle'))
    span = _fact_span(fact, quote)
    if span is None or NEGATION.search(fact):
        return None
    before, after = quote[:span[0]], quote[span[1]:]
    breaks = list(CLAUSE_BREAK.finditer(before))
    clause_before = before[breaks[-1].end():] if breaks else before
    window = ' '.join(clause_before.split()[-WINDOW_BEFORE:])
    cues = [m for m in NEGATION.finditer(window)]
    if cues and not HEDGE.search(window):
        cue = cues[-1]
        gap = window[cue.end():]
        words = gap.replace(',', ' ').split()
        if ',' in gap:
            if _is_term(fact) and not VERB_AFTER.match(after) and len(words) <= WINDOW_GAP:
                return 'tersine', f"bilgi aynı olumsuzluğun ('{cue.group(0)}') listesinde: '{window} [{fact}]'"
            condition = clause_before.strip().rstrip(',').strip()
            if CONDITION_START.match(condition) and not (breaks and CONCESSIVE.fullmatch(breaks[-1].group(0))):
                return 'kapsam', f"koşul / hasta grubu bilgi metninde yok: '{condition}'"
            return None
        if not words:
            return 'tersine', f"olumsuzluk ('{cue.group(0)}') bilgi metninin dışında: '{window} [{fact}]'"
        if len(words) <= WINDOW_GAP:
            if words[-1].lower() not in PREPOSITIONS or RELATION_NOUN.search(' '.join(words[:-1])):
                return 'tersine', f"olumsuzluk ('{cue.group(0)}') bilgi metninin dışında: '{window} [{fact}]'"
    cut = CLAUSE_BREAK.search(after)
    head = ' '.join((after[:cut.start()] if cut else after).split()[:4])
    match = NEGATION_AFTER.search(head)
    last = clause_before.split()[-1].lower().strip(',') if clause_before.split() else ''
    if match and last not in PREPOSITIONS and not HEDGE.search(head):
        return 'tersine', f"olumsuz yüklem ('{match.group(0).strip()}') bilgi metninin dışında: '[{fact}] {head}'"
    return population_time_scope(item, fact, clause_before, breaks, after)


# Hasta grubu / zaman öbeği (olumsuzluk olmadan). Yalnız uyarı: anlam ters dönmez, uygulama kapsamı bilgi metninde
# yoktur; eksik bağlam TAHMİNLE tamamlanmaz, kayıt sonraki semantik doğrulama için işaretlenir.
POPULATION_NOUN = r'(?:patients?|subjects?|participants?|individuals?|adults?|children|women|men|people|persons?|cases|' \
                  r'those|population|cohort)'
PREFIX_SCOPE = re.compile(r'^\W*(?:[A-Za-z]+(?:\s+[A-Za-z]+){0,2},\s*)?(?:(?:in|among|for)\s+(?:\S+\s+){0,6}?'
                          + POPULATION_NOUN + r'\b|(?:at|after|within|during|over|by)\s+(?:the\s+)?(?:\d+(?:\.\d+)?|'
                          r'one|two|three|four|five|six|twelve)[-\s]*(?:days?|weeks?|months?|years?|hours?)\b|during\s+'
                          r'(?:the\s+)?follow-?up\b)', re.I)
POPULATION_AFTER = re.compile(r'^\W{0,3}(?:in|among)\s+(?:\S+\s+){0,3}?' + POPULATION_NOUN + r'\b', re.I)
TIME_AFTER = re.compile(r'^\W{0,3}(?:at|after|within|during|over|by)\s+(?:the\s+)?(?:\d+(?:\.\d+)?|one|two|three|four|'
                        r'five|six|twelve)[-\s]*(?:days?|weeks?|months?|years?|hours?)\b|^\W{0,3}during\s+(?:the\s+)?'
                        r'follow-?up\b', re.I)
SCOPE_CONTEXTS = ('study_finding', 'recommendation', 'literature_background')


def population_time_scope(item, fact, clause_before, breaks, after):
    """('kapsam', gerekçe) ya da None. Aynı yan cümlede, bilgi metnine BİTİŞİK:
      - öncül öbek: yan cümle başından bilgiye kadar, virgülle biten ve hasta grubu / zaman belirteciyle başlayan öbek
        ('In patients with diabetes, [X reduced events]'); karşıtlık yan cümlesi ('although ...') değil;
      - ardıl öbek: bilginin hemen ardından 'in / among ... patients' ya da 'at / after N months', 'during follow-up'.
    Öbek bilgi metninin içindeyse sorun yoktur. Yalnız bulgu / öneri / literatür bağlamında."""
    if item.get('baglam') not in SCOPE_CONTEXTS:
        return None
    prefix = clause_before.strip()
    if prefix.endswith(',') and PREFIX_SCOPE.match(prefix) and not (breaks and CONCESSIVE.fullmatch(breaks[-1].group(0))) \
            and not PREFIX_SCOPE.match(fact):
        return 'kapsam', f"hasta grubu / zaman öbeği bilgi metninde yok: '{prefix.rstrip(',').strip()}'"
    cut = CLAUSE_BREAK.search(after)
    tail = (after[:cut.start()] if cut else after).strip()
    m = POPULATION_AFTER.match(tail) or TIME_AFTER.match(tail)
    if m:
        phrase = ' '.join(tail.split()[:8])
        kind = 'hasta grubu' if POPULATION_AFTER.match(tail) else 'zaman'
        return 'kapsam', f"{kind} öbeği bilgi metninin hemen ardında, metinde yok: '[{fact}] {phrase}'"
    return None


def single_term(item):
    """Tek terimlik (<= 3 sözcük, sayı ve yüklem yok) metin kaydı: varlık (hastalık / ilaç / yöntem) kaydı olarak
    geçerlidir, iddia kanıt cümlesindedir. Yalnız işaretlenir; topluca elenmez."""
    fact = clean(item.get('klinik_bilgi_en'))
    if item.get('kanit_turu') == 'tablo' or item.get('deger') not in (None, '') or not fact:
        return False
    return len(fact.split()) <= 3 and not re.search(r'\d', fact)


# ---------------------------------------------------------------------------
# Hat aşaması
# ---------------------------------------------------------------------------

def assess(item, quote_source=None, doc_formats=None):
    """(yeni kayıt, aşama, gerekçe). aşama None ise kayıt kalır (gerekirse düzeltilmiş / işaretlenmiş)."""
    marker = missing_marker(item.get('deger'))
    if marker:
        return item, STAGE_MISSING, f"değer bildirilmemiş işareti ({marker}); gerçek sıfır değil, ölçüm değeri yok"
    view = {**item, 'kanit_cumle': quote_source} if quote_source else item   # çok parçalı kanıt: birleşik metin
    scope = scope_problem(view)
    if scope and scope[0] == 'tersine':
        return item, STAGE_REEXTRACT, scope[1]
    reason = type_problem(view)
    if reason:
        return item, STAGE_REVIEW, reason
    new = dict(item)
    meta = dict(item.get(NOTE_KEY) or {})
    fixes = []
    warnings = []
    if scope:
        meta['kapsam_uyarisi'] = scope[1]
        meta['semantik_dogrulama_gerekli'] = True          # kapsam kaybı deterministik olarak düzeltilemez
    value = item.get('deger')
    if value not in (None, ''):
        stripped, numbers = split_citations(value)
        if numbers and stripped:
            meta['kaynakca_numaralari'] = numbers
            evidence = quote_source if quote_source is not None else str(item.get('kanit_cumle') or '')
            if stripped in evidence:
                meta['ham_deger'] = value
                new['deger'] = stripped
                fixes.append('deger')
            else:
                meta['deger_kaynakcasiz'] = stripped   # ayrılmış değer kanıtta bitişik değil: 'deger' özgün kalır
            if item.get('baglam') == 'study_finding':
                warnings.append('atıflı değer study_finding bağlamında (bağlam değiştirilmedi)')
        label, source = _format_label(new, doc_formats)
        components = value_components(new.get('deger'), label)
        if components:
            if source:
                components['etiket'], components['etiket_kaynagi'] = label, source
            meta['deger_bilesenleri'] = components
        warnings += consistency_warnings(new, components)
    if single_term(item):
        meta['tek_terim'] = True
    if warnings:
        meta['uyarilar'] = list(dict.fromkeys(list(meta.get('uyarilar') or []) + warnings))
    if fixes:
        new['otomatik_duzeltme'] = list(item.get('otomatik_duzeltme') or []) + [f'anlam_denetimi:{f}' for f in fixes]
    if meta:
        new[NOTE_KEY] = meta
    return new, None, None


def check_meaning(records, pages, rejected):
    """Bütün kayıtlar. Ayrılan / incelemeye / yeniden çıkarıma giden kayıt SİLİNMEZ: aşaması ve gerekçesiyle ret
    raporuna yazılır. Kalan kayıtlar düzeltilmiş ve işaretlenmiş olarak döner."""
    from src.rag_klinik_cikarim import source_problem
    doc_formats = document_formats(pages)
    out = []
    for item in records:
        parts = item.get('kanit_parcalari')
        quote = ' '.join(p.get('metin', '') for p in parts) if parts else None
        new, stage, reason = assess(item, quote, doc_formats)
        if stage:
            rejected.append({'sayfa': item.get('sayfa'), 'asama': stage, 'neden': reason, 'kayit': item})
            continue
        if new.get('deger') != item.get('deger'):
            probe = {k: v for k, v in new.items() if k not in (NOTE_KEY, 'tablo_modeli')}
            if source_problem(probe, pages.get(new.get('sayfa'), '')):
                # Kaynakta doğrulanamayan düzeltme uygulanmaz: değer ve düzeltme notu özgün, temiz biçim notta kalır.
                meta = dict(new[NOTE_KEY])
                meta['deger_kaynakcasiz'] = new['deger']
                meta.pop('ham_deger', None)
                new = {**new, 'deger': item.get('deger'), NOTE_KEY: meta}
                if item.get('otomatik_duzeltme'):
                    new['otomatik_duzeltme'] = list(item['otomatik_duzeltme'])
                else:
                    new.pop('otomatik_duzeltme', None)
        out.append(new)
    return out
