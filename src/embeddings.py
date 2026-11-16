
from functools import lru_cache

import numpy as np
from sentence_transformers import SentenceTransformer


EMBEDDING_MODELS = {
    "minilm": (
        "sentence-transformers/"
        "paraphrase-multilingual-MiniLM-L12-v2"
    ),
    "bge-m3": "BAAI/bge-m3",
}

DEFAULT_MODEL = "minilm"


@lru_cache(maxsize=2)
def get_embedding_model(model_name=DEFAULT_MODEL):
    """Embedding modelini bir kez yükler ve önbellekte tutar."""
    if model_name not in EMBEDDING_MODELS:
        raise ValueError(
            f"Bilinmeyen model: {model_name}. "
            f"Seçenekler: {list(EMBEDDING_MODELS)}"
        )

    model_id = EMBEDDING_MODELS[model_name]

    print(f"[EMBEDDING] Model yükleniyor: {model_id}")

    return SentenceTransformer(model_id)


def embed_documents(
    texts,
    model_name=DEFAULT_MODEL,
    batch_size=16,
):
    """PDF chunk'larını embedding vektörlerine dönüştürür."""
    model = get_embedding_model(model_name)

    if not texts:
        return np.empty(
            (0, model.get_sentence_embedding_dimension()),
            dtype=np.float32,
        )

    vectors = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=True,
    )

    return np.asarray(vectors, dtype=np.float32)


def embed_query(question, model_name=DEFAULT_MODEL):
    """Kullanıcı sorusunun embedding vektörünü oluşturur."""
    if not question.strip():
        raise ValueError("Soru boş olamaz.")

    model = get_embedding_model(model_name)

    vector = model.encode(
        question,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    return np.asarray(vector, dtype=np.float32)


def get_embedding_dimension(model_name=DEFAULT_MODEL):
    """Seçilen modelin embedding boyutunu döndürür."""
    model = get_embedding_model(model_name)

    return model.get_sentence_embedding_dimension()


def available_models():
    """Kullanılabilir embedding modellerini listeler."""
    return EMBEDDING_MODELS.copy()
