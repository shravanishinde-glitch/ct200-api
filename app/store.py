"""Document store for LLM-generated output (the NoSQL side of the system).

Two interchangeable backends with the same three methods (insert / get / find):
  * MongoStore    - used when MONGO_URI is set (local MongoDB or Atlas free tier).
  * JsonFileStore - zero-setup fallback: one JSON file, atomic writes, process-wide lock.

Why a document store at all: a generation is a nested, schema-flexible blob (snapshots of source text,
test cases, raw LLM output, attempt log). It is written once and read whole, never joined, so it does not
belong in the relational tree. It also stays valid when nodes are re-versioned because it embeds its own snapshot.
"""
import json
import os
import threading
import uuid
from typing import Optional


def _match(doc: dict, flt: dict) -> bool:
    """Mongo-style equality: if the stored field is a list, the value matches when it is a member."""
    for key, want in flt.items():
        have = doc.get(key)
        if isinstance(have, list):
            if want not in have:
                return False
        elif have != want:
            return False
    return True


class JsonFileStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)           # atomic: a crash never leaves a half-written file

    def insert(self, doc: dict) -> str:
        doc = dict(doc)
        doc.setdefault("_id", uuid.uuid4().hex)
        with self._lock:
            data = self._load()
            data[doc["_id"]] = doc
            self._save(data)
        return doc["_id"]

    def get(self, doc_id: str) -> Optional[dict]:
        with self._lock:
            return self._load().get(doc_id)

    def find(self, flt: dict) -> list:
        with self._lock:
            docs = [d for d in self._load().values() if _match(d, flt)]
        return sorted(docs, key=lambda d: d.get("created_at", ""), reverse=True)


class MongoStore:
    def __init__(self, uri: str, db_name: str = "ct200", collection: str = "generations"):
        from pymongo import MongoClient           # imported lazily so the JSON fallback needs no pymongo
        self._col = MongoClient(uri, serverSelectionTimeoutMS=3000)[db_name][collection]

    def insert(self, doc: dict) -> str:
        doc = dict(doc)
        doc.setdefault("_id", uuid.uuid4().hex)
        self._col.insert_one(doc)
        return doc["_id"]

    def get(self, doc_id: str) -> Optional[dict]:
        return self._col.find_one({"_id": doc_id})

    def find(self, flt: dict) -> list:
        return list(self._col.find(flt).sort("created_at", -1))     # array fields match by membership natively


_store = None


def get_store():
    """FastAPI dependency. Tests override it."""
    global _store
    if _store is None:
        uri = os.getenv("MONGO_URI")
        _store = MongoStore(uri) if uri else JsonFileStore(os.getenv("GEN_STORE_PATH", "./generations.json"))
    return _store
