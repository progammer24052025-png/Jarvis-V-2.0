import json
import hashlib
import logging
import os
from pathlib import Path
from typing import List, Optional

# Force HuggingFace offline mode to prevent httpx client errors on Python 3.14+.
# The embedding model is cached locally — no network calls needed at startup.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
 
from config import (
    LEARNING_DATA_DIR,
    CHATS_DATA_DIR,
    VECTOR_STORE_DIR,
    EMBEDDING_MODEL,
    EMBEDDING_CACHE_DIR,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
)
 
logger = logging.getLogger("J.A.R.V.I.S")

_HASH_FILE = VECTOR_STORE_DIR / ".content_hash"

class VectorStoreService:
    def __init__(self):
        self.embeddings = HuggingFaceEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            cache_folder=str(EMBEDDING_CACHE_DIR),
        )
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
        )
        self.vector_store: Optional[FAISS] = None
        self._retriever_cache: dict = {}
 
    def load_learning_data(self) -> List[Document]:
        documents = []
        for file_path in sorted(LEARNING_DATA_DIR.glob("*.txt")):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        documents.append(Document(page_content=content, metadata={"source": str(file_path.name)}))
                        logger.info("[VECTOR] Loaded learning data: %s (%d chars)", file_path.name, len(content))
            except Exception as e:
                logger.warning("Could not load learning data file %s: %s", file_path, e)
        logger.info("[VECTOR] Total learning data files loaded: %d", len(documents))
        return documents
 
    def load_chat_history(self) -> List[Document]:
        documents = []
        for file_path in sorted(CHATS_DATA_DIR.glob("*.json")):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    chat_data = json.load(f)
 
                messages = chat_data.get("messages", [])
                if not isinstance(messages, list):
                    continue
 
                lines = []
                for msg in messages:
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role") or "assistant"
                    content = msg.get("content") or ""
                    prefix = "User: " if role == "user" else "Assistant: "
                    lines.append(prefix + content)
                chat_content = "\n".join(lines)
 
                if chat_content.strip():
                    documents.append(Document(page_content=chat_content, metadata={"source": f"chat_{file_path.stem}"}))
                    logger.info("[VECTOR] Loaded chat history: %s (%d messages)", file_path.name, len(messages))
            except Exception as e:
                logger.warning("Could not load chat history file %s: %s", file_path, e)
        logger.info("[VECTOR] Total chat history files loaded: %d", len(documents))
        return documents

    def _compute_content_hash(self) -> str:
        """Compute a hash of all learning data and chat files to detect changes."""
        hasher = hashlib.sha256()
        # Hash learning data files
        for fp in sorted(LEARNING_DATA_DIR.glob("*.txt")):
            try:
                hasher.update(fp.name.encode())
                hasher.update(fp.read_bytes())
            except Exception:
                pass
        # Hash chat data files (just names + sizes for speed)
        for fp in sorted(CHATS_DATA_DIR.glob("*.json")):
            try:
                hasher.update(fp.name.encode())
                hasher.update(str(fp.stat().st_size).encode())
            except Exception:
                pass
        return hasher.hexdigest()

    def _load_saved_hash(self) -> str:
        """Load the previously saved content hash."""
        try:
            if _HASH_FILE.exists():
                return _HASH_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return ""

    def _save_hash(self, content_hash: str):
        """Save the content hash to disk."""
        try:
            _HASH_FILE.write_text(content_hash, encoding="utf-8")
        except Exception as e:
            logger.warning("[VECTOR] Could not save content hash: %s", e)

    def create_vector_store(self) -> FAISS:
        """Build or load the FAISS index. Skips rebuild if content hasn't changed."""
        current_hash = self._compute_content_hash()
        saved_hash = self._load_saved_hash()
        index_path = VECTOR_STORE_DIR / "index.faiss"

        # Try to load existing index if content hasn't changed
        if current_hash and current_hash == saved_hash and index_path.exists():
            try:
                self.vector_store = FAISS.load_local(
                    str(VECTOR_STORE_DIR), self.embeddings,
                    allow_dangerous_deserialization=True,
                )
                logger.info("[VECTOR] Loaded existing FAISS index (content unchanged)")
                self._retriever_cache.clear()
                return self.vector_store
            except Exception as e:
                logger.warning("[VECTOR] Failed to load saved index, rebuilding: %s", e)

        # Full rebuild
        learning_docs = self.load_learning_data()
        chat_docs = self.load_chat_history()
        all_documents = learning_docs + chat_docs
        logger.info("[VECTOR] Total documents to index: %d (learning: %d, chat: %d)",
                    len(all_documents), len(learning_docs), len(chat_docs))
 
        if not all_documents:
            self.vector_store = FAISS.from_texts(["No data available yet."], self.embeddings)
            logger.info("[VECTOR] No documents found, created placeholder index")
        else:
            chunks = self.text_splitter.split_documents(all_documents)
            logger.info("[VECTOR] Split into %d chunks (chunk_size=%d, overlap=%d)",
                        len(chunks), CHUNK_SIZE, CHUNK_OVERLAP)
            self.vector_store = FAISS.from_documents(chunks, self.embeddings)
            logger.info("[VECTOR] FAISS index built successfully with %d vectors", len(chunks))
 
        self._retriever_cache.clear()
        self.save_vector_store()
        self._save_hash(current_hash)
        return self.vector_store

    def add_documents(self, documents: List[Document]):
        """Incrementally add new documents to the existing FAISS index without full rebuild."""
        if not documents:
            return
        if not self.vector_store:
            logger.warning("[VECTOR] Cannot add documents — vector store not initialized")
            return
        try:
            chunks = self.text_splitter.split_documents(documents)
            if chunks:
                self.vector_store.add_documents(chunks)
                self._retriever_cache.clear()
                self.save_vector_store()
                # Update the hash to reflect the new state
                self._save_hash(self._compute_content_hash())
                logger.info("[VECTOR] Incrementally added %d chunks from %d documents", len(chunks), len(documents))
        except Exception as e:
            logger.error("[VECTOR] Failed to add documents incrementally: %s", e)
 
    def save_vector_store(self):
        if self.vector_store:
            try:
                self.vector_store.save_local(str(VECTOR_STORE_DIR))
            except Exception as e:
                logger.error("Failed to save vector store to disk: %s", e)
 
    def get_retriever(self, k: int = 10):
        if not self.vector_store:
            raise RuntimeError("Vector store not initialized. This should not happen.")
        if k not in self._retriever_cache:
            self._retriever_cache[k] = self.vector_store.as_retriever(search_kwargs={"k": k})
        return self._retriever_cache[k]