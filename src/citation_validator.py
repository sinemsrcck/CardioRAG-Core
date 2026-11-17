
import re


CITATION_PATTERN = re.compile(r"\[([\d,\s]+)\]")


def extract_citations(answer):
    """[1], [1,2], [1, 2, 3] biçimlerini ayrıştırır."""
    numbers = []

    for match in CITATION_PATTERN.finditer(answer or ""):
        for part in match.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                numbers.append(int(part))

    return numbers


def validate_citations(answer, sources):
    """Atıf numaralarını ve atıfsız paragrafları kontrol eder."""
    valid_numbers = {
        source["number"] for source in sources
    }

    cited_numbers = set(extract_citations(answer))

    paragraphs = [
        paragraph.strip()
        for paragraph in (answer or "").split("\n\n")
        if paragraph.strip()
    ]

    paragraphs_without_citations = [
        index
        for index, paragraph in enumerate(paragraphs, start=1)
        if not extract_citations(paragraph)
    ]

    unique_documents = {
        source.get("document_id") or source.get("filename")
        for source in sources
    }

    invalid = sorted(cited_numbers - valid_numbers)

    return {
        "valid": (
            bool(cited_numbers)
            and not invalid
            and not paragraphs_without_citations
        ),
        "used_citations": sorted(cited_numbers),
        "invalid_citations": invalid,
        "uncited_sources": sorted(
            valid_numbers - cited_numbers
        ),
        "paragraphs_without_citations": paragraphs_without_citations,
        "unique_document_count": len(unique_documents),
    }


def verify_claim(claim, evidence, client, model):
    """
    LLM ile iddia-kaynak uyumunu değerlendirir.

    client ve model dışarıdan alınır;
    böylece rag.py ile döngüsel import oluşmaz.
    """
    if not claim.strip() or not evidence.strip():
        return {
            "label": "NOT_SUPPORTED",
            "explanation": "İddia veya kanıt metni boş.",
        }

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=250,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a biomedical evidence verifier. "
                    "Use only the supplied evidence. "
                    "Return exactly one label on the first line: "
                    "SUPPORTED, PARTIALLY_SUPPORTED, "
                    "or NOT_SUPPORTED. "
                    "Explain briefly in Turkish on the second line. "
                    "If evidence is insufficient, return "
                    "NOT_SUPPORTED."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"CLAIM:\n{claim}\n\n"
                    f"EVIDENCE:\n{evidence}"
                ),
            },
        ],
    )

    result = (
        response.choices[0].message.content or ""
    ).strip()

    lines = result.splitlines()
    label = lines[0].strip() if lines else ""

    allowed = {
        "SUPPORTED",
        "PARTIALLY_SUPPORTED",
        "NOT_SUPPORTED",
    }

    if label not in allowed:
        return {
            "label": "UNKNOWN",
            "explanation": result,
        }

    return {
        "label": label,
        "explanation": "\n".join(lines[1:]).strip(),
    }
