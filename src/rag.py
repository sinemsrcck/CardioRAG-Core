
import os
import re

from dotenv import load_dotenv
from openai import OpenAI

from src.retrieval import hybrid_search
from src.reranker import rerank, rerank_hybrid
from src.source_diversity import select_diverse_sources

load_dotenv()

DEFAULT_LLM = os.getenv("LLM_MODEL", "Qwen/Qwen3-8B")


def get_llm_client():
    """OpenAI uyumlu Qwen API sunucusuna bağlanır."""
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")

    if not base_url:
        raise ValueError("LLM_BASE_URL .env dosyasında tanımlanmalı.")

    return OpenAI(
        api_key=api_key or "not-required",
        base_url=base_url,
    )


def build_context(chunks, max_chars=14000):
    """Kaynakları numaralandırıp LLM bağlamı oluşturur."""
    sections = []
    sources = []
    used_chars = 0

    for chunk in chunks:
        text = chunk.get("text", "").strip()
        if not text:
            continue

        filename = chunk.get("filename", "Bilinmeyen PDF")
        page_number = chunk.get("page_number", "?")

        source_number = len(sources) + 1

        section = (
            f"[{source_number}]\n"
            f"Dosya: {filename}\n"
            f"Sayfa: {page_number}\n"
            f"Metin:\n{text}"
        )

        if used_chars + len(section) > max_chars:
            break

        sections.append(section)
        sources.append({
            "number": source_number,
            "chunk_id": chunk.get("chunk_id"),
            "document_id": chunk.get("document_id"),
            "filename": filename,
            "page_number": page_number,
        })

        used_chars += len(section)

    return "\n\n---\n\n".join(sections), sources


SYSTEM_PROMPT = """
Sen kardiyovasküler bilimsel literatür için geliştirilmiş
bir araştırma asistanısın.

Kurallar:
1. Yanıtlarını yalnızca verilen kaynak metinlerine dayandır.
2. Kaynaklarda bulunmayan bilgileri üretme.
3. Her önemli bilimsel iddiaya [1], [2] gibi kaynak numarası ekle.
4. Sayısal değerleri, ilaç dozlarını ve çalışma sonuçlarını
   kaynaklarda geçtiği biçimde aktar.
5. Kaynaklar soruyu yanıtlamak için yetersizse açıkça belirt.
6. Kaynaklarda çelişki varsa bunu belirt.
7. Kullanıcının sorusunu Türkçe yanıtla.
8. Kaynak numaraları dışında kaynak uydurma.
9. Klinik karar veya kişiye özel tedavi önerisi verme.
"""


def generate_answer(question, context, sources):
    """Qwen3-8B ile kaynaklı yanıt oluşturur."""
    if not sources or not context.strip():
        return (
            "Bu soruyu yanıtlamak için yeterli kaynak "
            "bulunamadı."
        )

    client = get_llm_client()

    user_prompt = (
        f"Kaynaklar:\n{context}\n\n"
        f"Soru: {question}\n\n"
        "Yalnızca kaynaklara dayanarak yanıtla. "
        "Her önemli iddiayı kaynak numarasıyla belirt."
    )

    response = client.chat.completions.create(
        model=DEFAULT_LLM,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=1200,
    )

    answer = response.choices[0].message.content or ""

    return answer.strip()


def extract_citations(answer):
    """Yanıttaki [1], [2] biçimindeki atıfları çıkarır."""
    return sorted({
        int(number)
        for number in re.findall(r"\[(\d+)\]", answer)
    })


def validate_citation_numbers(answer, sources):
    """
    Atıf numaralarının mevcut kaynaklara karşılık
    gelip gelmediğini kontrol eder.

    Not: Bu kontrol iddiaların bilimsel doğruluğunu
    veya kaynak tarafından desteklenmesini doğrulamaz.
    """
    cited = extract_citations(answer)
    valid_numbers = {s["number"] for s in sources}

    invalid = [
        number for number in cited
        if number not in valid_numbers
    ]

    return {
        "valid": len(invalid) == 0,
        "cited_numbers": cited,
        "invalid_numbers": invalid,
    }


def answer_question(
    question,
    chunks,
    embedding_model="minilm",
    reranker_model="bge-m3",
    rerank_method="cross_encoder",
    candidate_k=20,
    top_k=5,
):
    """
    RAG işlem hattı.

    chunks:
        MySQL'den yüklenmiş, embedding içeren kayıtlar.
    """
    if not question.strip():
        raise ValueError("Soru boş olamaz.")

    retrieved = hybrid_search(
        question=question,
        chunks=chunks,
        model_name=embedding_model,
        candidate_k=candidate_k,
        top_k=candidate_k,
    )

    if rerank_method == "hybrid":
        selected = rerank_hybrid(
            question,
            retrieved,
            model_name=reranker_model,
            top_k=top_k,
        )

    elif rerank_method == "cross_encoder":
        selected = rerank(
            question,
            retrieved,
            model_name=reranker_model,
            top_k=top_k,
        )

    else:
        raise ValueError(
            "rerank_method 'hybrid' veya "
            "'cross_encoder' olmalı."
        )

    selected = select_diverse_sources(
        selected,
        top_k=top_k,
        max_chunks_per_document=2,
    )
    
    context, sources = build_context(selected)

    answer = generate_answer(
        question=question,
        context=context,
        sources=sources,
    )

    citation_check = validate_citation_numbers(
        answer,
        sources,
    )

    return {
        "question": question,
        "answer": answer,
        "sources": sources,
        "citation_check": citation_check,
        "retrieved_count": len(retrieved),
        "selected_count": len(sources),
    }
