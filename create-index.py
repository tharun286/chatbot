import os
import shutil
from pathlib import Path
from langchain_community.vectorstores import FAISS
# from langchain_openai import AzureOpenAIEmbeddings
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.hls_platform.utils import extract_text_from_file
from src.hls_platform.hls_constants import get_constant
from sqlalchemy.ext.asyncio import AsyncSession
from src.hls_platform.embedding_provider_p import get_azure_embeddings

# ── Config ────────────────────────────────────────────────────────────────────

INDEX_FOLDER  = "./HLS_chatbot_index"
CHUNK_SIZE    = 1000
CHUNK_OVERLAP = 200

# ─────────────────────────────────────────────────────────────────────────────


# async def get_azure_embeddings(db: AsyncSession) -> AzureOpenAIEmbeddings:
#     return AzureOpenAIEmbeddings(
#         azure_deployment=await get_constant("azure_deployment", db),
#         openai_api_version=await get_constant("openai_api_version", db),
#         azure_endpoint=await get_constant("azure_endpoint", db),
#         # api_key=await get_constant("embedding_api_key", db),
#     )


def chunk_text(raw_text: str, file_path: str, document_unique_id: str = None) -> list[Document]:
    chunks = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, length_function=len
    ).split_text(raw_text)
    return [
        Document(
            page_content=chunk,
            metadata={
                "file_path":          str(Path(file_path).resolve()),
                "filename":           Path(file_path).name,
                "chunk_id":           idx + 1,
                "document_unique_id": document_unique_id or "",
            },
        )
        for idx, chunk in enumerate(chunks)
    ]


async def build_or_update_index(file_path: str, index_folder: str = None, document_unique_id: str = None, db = None) -> None:
    target_folder = index_folder or INDEX_FOLDER

    if not Path(file_path).exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    print(f"📄 Extracting text from: {file_path}")
    raw_text = extract_text_from_file(file_path)

    if not raw_text.strip():
        raise ValueError(f"No text extracted from: {file_path}")

    documents = chunk_text(raw_text, file_path, document_unique_id=document_unique_id)
    print(f"   → {len(documents)} chunks created (size={CHUNK_SIZE}, overlap={CHUNK_OVERLAP})")

    embeddings = await get_azure_embeddings(db)
    new_store  = FAISS.from_documents(documents, embeddings)

    index_file = Path(target_folder) / "index.faiss"
    if index_file.exists():
        print(f"🔄 Existing index found — merging into '{target_folder}'...")
        existing_store = FAISS.load_local(
            target_folder, embeddings, "index", allow_dangerous_deserialization=True
        )
        existing_store.merge_from(new_store)
        existing_store.save_local(target_folder, "index")
    else:
        print(f"🆕 Creating new index at '{target_folder}'...")
        os.makedirs(target_folder, exist_ok=True)
        new_store.save_local(target_folder, "index")

    print(f"✅ Index saved to '{target_folder}'")


def remove_stale_documents(index_folder: str, valid_doc_ids: set, db=None) -> None:
    index_file = Path(index_folder) / "index.faiss"
    if not index_file.exists():
        print("ℹ️  No existing index — skipping stale check.")
        return

    print("🔍 Checking FAISS index for stale documents...")
    embeddings  = get_azure_embeddings(db)
    faiss_store = FAISS.load_local(
        index_folder, embeddings, "index", allow_dangerous_deserialization=True
    )

    stale_ids = [
        store_id
        for store_id, doc in faiss_store.docstore._dict.items()
        if doc.metadata.get("document_unique_id")
        and doc.metadata["document_unique_id"] not in valid_doc_ids
    ]

    if not stale_ids:
        print("✅ No stale vectors found.")
        return

    print(f"🗑️  Removing {len(stale_ids)} stale vectors — rebuilding index...")
    remaining_docs = [
        doc
        for store_id, doc in faiss_store.docstore._dict.items()
        if store_id not in stale_ids
    ]

    if remaining_docs:
        FAISS.from_documents(remaining_docs, embeddings).save_local(index_folder, "index")
        print(f"✅ Index rebuilt with {len(remaining_docs)} remaining vectors.")
    else:
        shutil.rmtree(index_folder, ignore_errors=True)
        print("✅ All documents removed; index directory deleted.")
