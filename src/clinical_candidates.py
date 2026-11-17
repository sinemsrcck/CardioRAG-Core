
"""
Klinik bulgu çıkarımı için ADAY cümleleri listeler (yorum yapmaz).

Kaynak: makaleler.tam_metin, yalnızca bizim makalelerimiz
(document_id IS NOT NULL). Veritabanından yalnızca okur.

Çalıştırma:
    python -m src.clinical_candidates
Çıktı:
    data/review/klinik_aday_cumleler.csv  (UTF-8 BOM, Excel uyumlu)

Her aday: dosya, sayfa (=== Sayfa N === işaretinden), cümle (markdown
vurguları temizlenmiş, kelimeler aynen), kaynak türü (metin/tablo) ve
takıldığı kalıplar.
"""

import csv
import hashlib
import json
import re
from pathlib import Path

from src.database import get_connection


OUTPUT_FILE = Path("data/review/klinik_aday_cumleler.csv")
PAGE_MARKER = re.compile(r"^=== Sayfa (\d+) ===$", re.MULTILINE)
CANDIDATE_ID_VERSION = "clinical-candidate-v1"


def candidate_binding(candidate):
    """Bind exact candidate text to document/page/span, independent of ordering.

    No whitespace normalization: any change to the text supplied to the LLM
    invalidates an earlier approval. Filename and collection ordinal are excluded.
    """
    if not isinstance(candidate, dict):
        raise ValueError("Invalid candidate")
    document_id, text = candidate.get("document_id"), candidate.get("cumle")
    page = candidate.get("sayfa")
    end = candidate.get("sayfa_bitis")
    end = page if end in (None, "") else end
    kind = candidate.get("kaynak_turu", "metin")
    joined = candidate.get("birlestirme")
    joined = "" if joined is None else joined
    if not isinstance(document_id, str) or not document_id.strip() or not isinstance(text, str) or not text.strip():
        raise ValueError("Missing candidate source")
    if type(page) is not int or page < 1 or type(end) is not int or end < page:
        raise ValueError("Invalid candidate page span")
    if not isinstance(kind, str) or not kind or not isinstance(joined, str):
        raise ValueError("Invalid candidate source type")
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    binding = {"document_id": document_id, "sayfa": page, "sayfa_bitis": end,
               "kaynak_turu": kind, "birlestirme": joined, "source_text_sha256": source_hash}
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**binding, "aday_id": CANDIDATE_ID_VERSION + ":" + hashlib.sha256(encoded).hexdigest()}

# ---------------------------------------------------------------------------
# Kalıplar
# ---------------------------------------------------------------------------

NUMBER = r"\d+(?:[.,]\d+)?"
RANGE = rf"{NUMBER}(?:\s*(?:–|-|to|ila)\s*{NUMBER})?(?:\s?(?:million|milyon))?"
DOSE_UNIT = r"(?:mg|µg|μg|mcg|g|IU|U|mL|ml|mmol|mEq)"
DOSE_SUFFIX = r"(?:/(?:kg|day|d|h|hr|hour|min|dose|week|wk|gün|saat|m2|m²))*"
# Sondaki (?![\w/]) koşulu "13 mg/dL" gibi değerlerin "13 mg" olarak
# yakalanmasını engeller (izin verilen ekler dışında '/' gelirse eşleşme yok).
DOSE_PATTERN = re.compile(rf"(?<![\w.]){RANGE}\s?{DOSE_UNIT}{DOSE_SUFFIX}(?![\w/])")

# Laboratuvar konsantrasyonları / akış hızları doz değildir (ek güvence).
LAB_UNIT = re.compile(r"(?:mg|µg|μg|mcg|g|mmol|mEq|IU|U)/(?:dL|dl|L|l|mL|ml)\b|mL/min|ml/min")

DIAGNOSTIC_PATTERNS = {
    "troponin": r"troponin\w*|\bhs-?cTn[IT]?\b|\bcTn[IT]\b",
    "EKG": r"\bECGs?\b|\bEKG\b|electrocardiogra\w*|elektrokardiyogra\w*",
    "ekokardiyografi": r"echocardiogra\w*|ekokardiyogra\w*|\bTTE\b|\bTEE\b",
    "anjiyografi/BT": r"angiogra\w*|anjiyogra\w*|\bCCTA\b|\bCTA\b|computed tomography|\bCT\b",
    "diğer görüntüleme": r"magnetic resonance|\bCMR\b|\bMRI\b|\bSPECT\b|\bPET\b|scintigraph\w*|ultrason\w*|ultrasound|doppler",
    "ritim izlemi": (r"\bHolter\b|loop recorder|\bILRs?\b|wearable\w*|smartwatch\w*|"
                     r"ambulatory (?:ECG )?monitoring"),
    "biyobelirteç": r"\bBNP\b|NT-?proBNP|D-dimer|biomarker\w*|biyobelirte\w*|C-peptid\w*|HbA1c",
    "kriter": r"criteri(?:a|on)\b|kriter\w*",
    "eşik/cut-off": r"cut-?off|threshold\w*|eşik\w*|kesme değer\w*",
    "tanısal doğruluk": (r"sensitivit\w*|specificit\w*|duyarlılı\w*|özgüllü\w*|\bAUC\b|\bROC\b|"
                         r"positive predictive|negative predictive|\bPPV\b|\bNPV\b"),
}
DIAGNOSTIC_REGEX = {name: re.compile(p, re.IGNORECASE) for name, p in DIAGNOSTIC_PATTERNS.items()}

# İlaç / müdahale (doz şart değil). İngilizce + Türkçe karşılıklar.
TREATMENT_PATTERNS = {
    "antikoagülan": (r"anticoagula\w*|antikoagül\w*|\bN?OACs?\b|\bDOACs?\b|apixaban|apiksaban|"
                     r"rivaroxaban|rivaroksaban|dabigatran|edoxaban|edoksaban|warfarin|varfarin|"
                     r"vitamin K antagonist\w*|\bVKAs?\b|heparin\w*|enoxaparin\w*|enoksaparin\w*"),
    "antiplatelet": (r"aspirin\w*|asetilsalisilik|clopidogrel|klopidogrel|ticagrelor|tikagrelor|"
                     r"prasugrel|antiplatelet\w*|antiagregan\w*|\bDAPT\b|P2Y12\w*"),
    "lipid düşürücü": (r"statins?\b|statin therap\w*|atorvastatin|rosuvastatin|ezetimib\w*|"
                       r"PCSK9\w*|evolocumab|alirocumab|inclisiran|statin\w*"),
    "beta bloker": (r"beta\s?[- ]?\s?blocker\w*|β\s?[- ]?\s?blocker\w*|beta[- ]?bloker\w*|"
                    r"metoprolol|bisoprolol|carvedilol|karvedilol|esmolol"),
    "RAAS inhibitörü": (r"ACE[- ]inhibit\w*|\bACEIs?\b|angiotensin[- ]converting|angiotensin receptor\w*|"
                        r"anjiyotensin\w*|\bARBs?\b|\bARNI\b|sacubitril|sakubitril|valsartan|ramipril|"
                        r"mineralocorticoid\w*|spironolacton\w*|eplerenon\w*"),
    "SGLT2 inhibitörü": r"SGLT-?2\w*|empagli\w*|dapagli\w*|\w+gliflozin\w*",
    "kolşisin": r"colchicin\w*|kolşisin\w*",
    "antiaritmik": (r"amiodaron\w*|antiarrhythmic\w*|antiaritmik\w*|flecainid\w*|dronedaron\w*|"
                    r"sotalol|digoxin|digoksin|\bAADs?\b|rate control|rhythm control"),
    "diğer ilaç": (r"nitrates?\b|nitroglycerin\w*|ranolazin\w*|ivabradin\w*|calcium channel blocker\w*|"
                   r"\bCCBs?\b|diuretic\w*|diüretik\w*|insulin\w*|metformin|GLP-1\w*|semaglutid\w*|"
                   r"fibrinoly\w*|thromboly\w*"),
    "girişimsel": (r"ablation\w*|ablasyon\w*|\bPCI\b|percutaneous coronary intervention\w*|perkütan koroner|"
                   r"\bCABG\b|coronary artery bypass|bypass graft\w*|stent\w*|revasculari[sz]ation|"
                   r"revaskülarizasyon\w*|cardioversion\w*|kardiyoversiyon\w*|\bp?LAAO\b|"
                   r"left atrial appendage occlusion|pacemaker\w*|implantable cardioverter|"
                   r"endarterectom\w*|endarterektom\w*|thrombectom\w*"),
    "resüsitasyon": (r"defibrillat\w*|defibrilasyon\w*|\bCPR\b|cardiopulmonary resuscitation|"
                     r"kardiyopulmoner resüsitasyon|resuscitation|adrenalin\w*|epinephrin\w*|\bAEDs?\b"),
    "yaşam tarzı": (r"weight loss|exercise training|smoking cessation|alcohol (?:reduction|abstinence)|"
                    r"lifestyle (?:and risk factor )?modification|kilo kaybı|sigara(?:yı)? bırak\w*"),
    "kan basıncı kontrolü": (r"blood pressure (?:control|lowering|reduction)|antihypertensi\w*|"
                             r"kan basıncı kontrol\w*"),
}
TREATMENT_REGEX = {name: re.compile(p, re.IGNORECASE) for name, p in TREATMENT_PATTERNS.items()}

# Cümle bölmede nokta ile biten ama cümle sonu olmayan kısaltmalar.
ABBREVIATIONS = (
    "e.g", "i.e", "et al", "vs", "Fig", "Figs", "No", "Dr", "approx", "cf",
    "Ref", "Refs", "Eq", "St", "Jr", "Inc", "Ltd", "min", "max", "resp",
    "Tab", "Vol", "pp", "ca",
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'“(\[]?[A-ZÇĞİÖŞÜ0-9])")
# JAMA/AMA biçimi: noktadan hemen sonra gelen kaynak numarası ("stroke.4 No direct",
# "warfarin.41– 44,58 After"). Numara önceki cümlede kalır.
CITATION_END = re.compile(r"(?<=[a-z)\]%])\.(\d{1,3}(?:\s?[–,\-]\s?\d{1,3})*)\s+(?=[A-Z][a-z])")


# ---------------------------------------------------------------------------
# Metin yardımcıları
# ---------------------------------------------------------------------------

def _as_text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def strip_markdown(text):
    """Markdown vurgu/başlık işaretlerini kaldırır; kelimelere dokunmaz."""
    text = re.sub(r"<br\s*/?>", " / ", text)
    text = re.sub(r"</?(?:sup|sub|mark|span|u|i|b|em|strong)(?:\s[^>]*)?>", "", text)
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_citations(text):
    pieces, start = [], 0
    for match in CITATION_END.finditer(text):
        pieces.append(text[start:match.end(1)])
        start = match.end()
    pieces.append(text[start:])
    return pieces


def split_sentences(paragraph):
    pieces = [p for piece in SENTENCE_END.split(paragraph) for p in _split_citations(piece)]
    sentences = []
    for piece in pieces:
        if sentences and any(sentences[-1].endswith(f" {a}.") or sentences[-1] == f"{a}."
                             for a in ABBREVIATIONS):
            sentences[-1] += " " + piece
        else:
            sentences.append(piece)
    return [s.strip() for s in sentences if s.strip()]


PICTURE_START = re.compile(r"Start of picture text", re.IGNORECASE)
PICTURE_END = re.compile(r"End of picture text", re.IGNORECASE)
CAPTION = re.compile(r"^(?:FIGURE|TABLE|Figure|Table|Fig\.|BOX|Box)\s*\d+[A-Za-z]?\s*[.:]")
# Bu kelimelerle biten paragraf cümlenin ortasında kesilmiştir.
DANGLING_END = re.compile(
    r"(?:\b(?:and|or|of|the|a|an|to|in|on|for|with|by|from|between|than|as|at|"
    r"that|which|is|are|was|were|ve|ile|veya|bir)|[,\-–])\s*$",
    re.IGNORECASE,
)

# Şekil içinden okunan yazılar (grafik etiketleri) aday sayılmaz; sayılır.
EXCLUDED = {"şekil_ici_blok": 0}


def page_blocks(page, page_text):
    """Sayfayı bloklara ayırır: ('metin'|'başlık'|'tablo', metin, sayfa)."""
    blocks, paragraph = [], []
    in_picture = False

    def flush():
        if paragraph:
            blocks.append(["metin", strip_markdown(" ".join(paragraph)), page])
            paragraph.clear()

    for line in page_text.splitlines():
        stripped = line.strip()
        if PICTURE_START.search(stripped):
            flush()
            in_picture = True
            EXCLUDED["şekil_ici_blok"] += 1
            # Yer tutucu: şekil üzerinden yapılan birleştirme 'ara_blok' sayılır.
            blocks.append(["şekil", "[şekil içi yazı]", page])
            if not PICTURE_END.search(stripped):
                continue
        if in_picture:
            if PICTURE_END.search(stripped):
                in_picture = False
            continue
        if stripped.startswith("|"):
            flush()
            if re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", stripped):
                continue  # tablo başlık ayracı
            cells = [strip_markdown(c) for c in stripped.strip("|").split("|")]
            row = " | ".join(c for c in cells if c)
            if row:
                blocks.append(["tablo", row, page])
        elif not stripped or "intentionally omitted" in stripped:
            flush()
        elif re.match(r"^#{1,6}\s", stripped):
            flush()
            blocks.append(["başlık", strip_markdown(stripped), page])
        else:
            paragraph.append(stripped)
    flush()
    return [b for b in blocks if b[1]]


def _is_float_block(block, after_caption):
    """Metnin akışını bölen ara blok mu? (tablo, şekil/tablo başlığı, açıklaması)"""
    kind, text, _ = block
    if kind in ("tablo", "şekil"):
        return True
    if kind == "metin" and CAPTION.match(text):
        return True
    # Başlıktan hemen sonraki kısa açıklama satırı ("HCR: HbA1c/C-peptide ratio.")
    return (kind == "metin" and after_caption and len(text) < 250
            and not re.match(r"[a-zçğıöşü]", text))


def merge_broken_paragraphs(blocks):
    """
    Sayfa/sütun sınırında ya da araya giren tablo/şekil yüzünden bölünen
    paragrafları birleştirir. Noktalama ile bitmeyen bir metin bloğu:
      - araya tablo / şekil-tablo başlığı / başlık açıklaması girmiş olsa da,
        sonraki küçük harfle başlayan metin bloğuyla birleştirilir;
      - bağlaç/edat/virgülle bitiyorsa hemen sonraki metin bloğuyla
        (büyük harfle başlasa da) birleştirilir.
    Ara bloklar yerinde kalır. Satır sonu tiresi (peo- / ple) birleştirilir.
    Döndürür: [(tür, [(parça_metni, sayfa, ara_blok_atlandı), ...])]
    """
    consumed = set()
    merged = []

    for i, (kind, text, page) in enumerate(blocks):
        if i in consumed:
            continue
        parts = [(text, page, False)]
        last = i  # en son birleştirilen bloğun indeksi
        while kind == "metin" and not re.search(r"[.!?:;]\s*$", parts[-1][0]):
            j, after_caption = last + 1, False
            while j < len(blocks) and j not in consumed and _is_float_block(blocks[j], after_caption):
                after_caption = after_caption or bool(CAPTION.match(blocks[j][1]))
                j += 1
            if j >= len(blocks) or j in consumed or blocks[j][0] != "metin":
                break
            nxt_text, nxt_page = blocks[j][1], blocks[j][2]
            lower_start = bool(re.match(r"[a-zçğıöşü(]", nxt_text))
            gapped = j != last + 1  # araya tablo/şekil girdi mi?
            dangling = bool(DANGLING_END.search(parts[-1][0]))
            if not (lower_start or (dangling and not gapped)):
                break
            last_text, last_page, last_gap = parts[-1]
            if re.search(r"[a-zçğıöşü]-$", last_text) and lower_start:
                parts[-1] = (last_text[:-1], last_page, last_gap)
                parts.append(("\0" + nxt_text, nxt_page, gapped))  # \0: boşluksuz
            else:
                parts.append((nxt_text, nxt_page, gapped))
            consumed.add(j)
            last = j
        merged.append((kind, parts))
    return merged


def iter_units(blocks):
    """(tür, cümle, başlangıç_sayfası, bitiş_sayfası, birleştirme) verir.

    birleştirme: '' | 'sayfa' (sayfa sınırını aşıyor) |
                 'ara_blok' (araya giren tablo/şekil üzerinden birleştirildi;
                 cümle tam metinde tek parça halinde geçmez)
    """
    for kind, parts in merge_broken_paragraphs(blocks):
        if kind == "şekil":
            continue
        if kind != "metin":
            yield kind, parts[0][0], parts[0][1], parts[0][1], ""
            continue

        text, spans = "", []  # spans: (başlangıç_offset, sayfa, ara_blok)
        for part, page, gapped in parts:
            if part.startswith("\0"):
                part = part[1:]
            elif text:
                text += " "
            spans.append((len(text), page, gapped))
            text += part

        def span_at(offset):
            return [s for s in spans if s[0] <= offset][-1]

        cursor = 0
        for sentence in split_sentences(text):
            start = text.find(sentence, cursor)
            if start < 0:
                start = cursor
            end = start + len(sentence) - 1
            cursor = end + 1
            crossed = [s for s in spans if start < s[0] <= end]
            if any(s[2] for s in crossed):
                joined = "ara_blok"
            elif span_at(start)[1] != span_at(end)[1]:
                joined = "sayfa"
            else:
                joined = ""
            yield "metin", sentence, span_at(start)[1], span_at(end)[1], joined


def iter_pages(full_text):
    """tam_metin'i (sayfa_no, metin) parçalarına böler."""
    matches = list(PAGE_MARKER.finditer(full_text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        yield int(match.group(1)), full_text[match.end():end]


def find_patterns(sentence):
    doses = [m.group(0) for m in DOSE_PATTERN.finditer(sentence)
             if not LAB_UNIT.search(m.group(0))]
    diagnostics = sorted({name for name, regex in DIAGNOSTIC_REGEX.items()
                          if regex.search(sentence)})
    treatments = sorted({name for name, regex in TREATMENT_REGEX.items()
                         if regex.search(sentence)})
    return doses, diagnostics, treatments


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

def load_our_articles(conn):
    cursor = conn.cursor()
    cursor.execute("START TRANSACTION READ ONLY")
    cursor.execute(
        "SELECT b.filename, m.makale_id, m.document_id, m.tam_metin "
        "FROM makaleler AS m JOIN pdf_belgeleri AS b ON b.document_id = m.document_id "
        "WHERE m.document_id IS NOT NULL ORDER BY b.filename"
    )
    rows = [tuple(_as_text(v) for v in row) for row in cursor.fetchall()]
    conn.rollback()
    cursor.close()
    return rows


def collect_candidates(articles):
    candidates = []
    for filename, makale_id, document_id, full_text in articles:
        if not PAGE_MARKER.search(full_text or ""):
            raise ValueError(f"tam_metin sayfa işaretsiz: {filename}")
        blocks = []
        for page, page_text in iter_pages(full_text):
            blocks += page_blocks(page, page_text)

        seen = set()
        for source_type, sentence, page, end_page, joined in iter_units(blocks):
            key = (page, sentence)
            if key in seen:
                continue
            seen.add(key)
            doses, diagnostics, treatments = find_patterns(sentence)
            if not doses and not diagnostics and not treatments:
                continue
            kinds = [k for k, hit in (("doz", doses), ("ilaç", treatments),
                                      ("tanı", diagnostics)) if hit]
            patterns = ([f"doz: {d}" for d in doses]
                        + [f"ilaç: {t}" for t in treatments]
                        + [f"tanı: {d}" for d in diagnostics])
            candidate = {
                "dosya": filename,
                "makale_id": makale_id,
                "document_id": document_id,
                "sayfa": page,
                "sayfa_bitis": end_page if end_page != page else "",
                "birlestirme": joined,
                "kaynak_turu": source_type,
                "aday_turu": "+".join(kinds),
                "eslesen_kaliplar": "; ".join(patterns),
                "cumle": sentence,
                "karakter": len(sentence),
            }
            binding = candidate_binding(candidate)
            candidate.update(aday_id=binding["aday_id"], source_text_sha256=binding["source_text_sha256"],
                             candidate_id_version=CANDIDATE_ID_VERSION)
            candidates.append(candidate)
    return candidates


def write_csv(candidates, path=OUTPUT_FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(candidates[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(candidates)


def print_summary(candidates, articles):
    def counts(rows):
        return (sum("doz" in c["aday_turu"] for c in rows),
                sum("ilaç" in c["aday_turu"] for c in rows),
                sum("tanı" in c["aday_turu"] for c in rows),
                sum(c["kaynak_turu"] == "tablo" for c in rows),
                len(rows))

    print(f"\n{'Dosya':<62} {'Doz':>5} {'İlaç':>5} {'Tanı':>5} {'Tablo':>6} {'Toplam':>7}")
    print("-" * 95)
    for filename, *_ in articles:
        dose, drug, diag, table, total = counts([c for c in candidates if c["dosya"] == filename])
        print(f"{filename[:62]:<62} {dose:>5} {drug:>5} {diag:>5} {table:>6} {total:>7}")
    print("-" * 95)
    dose, drug, diag, table, total = counts(candidates)
    print(f"{'TOPLAM':<62} {dose:>5} {drug:>5} {diag:>5} {table:>6} {total:>7}")
    print("(Bir aday birden fazla türde sayılabilir; Toplam benzersiz aday sayısıdır.)")

    for filename, *_ in articles:
        rows = [c for c in candidates if c["dosya"] == filename]
        picks = []
        for wanted in (lambda c: "ilaç" in c["aday_turu"] and c["kaynak_turu"] == "metin",
                       lambda c: "tanı" in c["aday_turu"] and c["kaynak_turu"] == "metin",
                       lambda c: c["kaynak_turu"] == "tablo",
                       lambda c: True):
            pick = next((c for c in rows if wanted(c) and c not in picks), None)
            if pick and len(picks) < 3:
                picks.append(pick)
        print(f"\n== {filename}")
        for c in picks:
            text = c["cumle"] if len(c["cumle"]) <= 220 else c["cumle"][:220] + "…"
            print(f"  [s.{c['sayfa']}, {c['kaynak_turu']}] {{{c['eslesen_kaliplar']}}}")
            print(f"     {text}")


def main():
    conn = get_connection()
    try:
        articles = load_our_articles(conn)
    finally:
        conn.close()
    if not articles:
        raise SystemExit("makaleler'de document_id dolu satır yok.")

    candidates = collect_candidates(articles)
    write_csv(candidates)
    print_summary(candidates, articles)
    joined = {k: sum(c["birlestirme"] == k for c in candidates) for k in ("sayfa", "ara_blok")}
    print(f"\nBirleştirilmiş cümle: sayfa sınırı {joined['sayfa']}, "
          f"ara tablo/şekil {joined['ara_blok']} (CSV 'birlestirme' sütunu)")
    print(f"Şekil içi yazı (grafik etiketi) olarak dışarıda bırakılan blok: "
          f"{EXCLUDED['şekil_ici_blok']}")
    print(f"CSV: {OUTPUT_FILE} ({len(candidates)} aday, UTF-8 BOM, ayraç ';')")


if __name__ == "__main__":
    main()
