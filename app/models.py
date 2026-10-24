from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base


def _now():
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document", order_by="DocumentVersion.version_no")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_no: Mapped[int]
    title: Mapped[str] = mapped_column(Text, default="")
    source_file: Mapped[str] = mapped_column(String(500), default="")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")      # parser warnings (skipped level, out of order, ...)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    document: Mapped[Document] = relationship(back_populates="versions")
    nodes: Mapped[list["Node"]] = relationship(back_populates="version", order_by="Node.order_index")


class Node(Base):
    """One section in one version. `id` is unique per version row; `logical_id` is the stable identity across versions."""
    __tablename__ = "nodes"
    __table_args__ = (Index("ix_nodes_version_logical", "version_id", "logical_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"))
    logical_id: Mapped[str] = mapped_column(String(100))
    number: Mapped[str] = mapped_column(String(50))
    heading: Mapped[str] = mapped_column(Text)
    level: Mapped[int]
    order_index: Mapped[int]
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text, default="")
    tables_json: Mapped[str] = mapped_column(Text, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64))
    change_status: Mapped[str] = mapped_column(String(20), default="new")   # new | unchanged | changed (vs previous version)
    prev_node_id: Mapped[Optional[int]] = mapped_column(ForeignKey("nodes.id"), nullable=True)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    page: Mapped[int] = mapped_column(default=0)
    version: Mapped[DocumentVersion] = relationship(back_populates="nodes")
    parent: Mapped[Optional["Node"]] = relationship(foreign_keys=[parent_id], remote_side=[id])


class Selection(Base):
    """A named set of sections. Each item points at a Node ROW, which belongs to exactly one version,
    so a selection stays pinned to the exact text the user picked even after newer versions are ingested."""
    __tablename__ = "selections"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    items: Mapped[list["SelectionItem"]] = relationship(back_populates="selection", cascade="all, delete-orphan",
                                                        order_by="SelectionItem.id")


class SelectionItem(Base):
    __tablename__ = "selection_items"
    __table_args__ = (UniqueConstraint("selection_id", "node_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    selection_id: Mapped[int] = mapped_column(ForeignKey("selections.id"))
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"))
    selection: Mapped[Selection] = relationship(back_populates="items")
    node: Mapped[Node] = relationship()