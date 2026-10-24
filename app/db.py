import os
from dotenv import load_dotenv

load_dotenv()   # reads .env if present (GROQ_API_KEY, MONGO_URI, ...)
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

SQLITE_URL = os.getenv("SQLITE_URL", "sqlite:///./ct200.db")

engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False} if SQLITE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from app import models  # noqa: F401  (registers tables)
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        