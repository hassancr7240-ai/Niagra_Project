from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Machine(Base):
    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    manufacturer: Mapped[str] = mapped_column(String(128))
    model: Mapped[str] = mapped_column(String(128))
    machine_type: Mapped[str] = mapped_column(String(32))  # KRONES | THIRD_PARTY
    maintenance_chapters: Mapped[Optional[str]] = mapped_column(Text)  # JSON array
    is_hour_based: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    location: Mapped[Optional[str]] = mapped_column(String(128))
    asset_tag: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[list[Task]] = relationship("Task", back_populates="machine")
    pm_records: Mapped[list[PMRecord]] = relationship("PMRecord", back_populates="machine")


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    machine_id: Mapped[str] = mapped_column(String(64), ForeignKey("machines.machine_id"))
    interval_hours: Mapped[int] = mapped_column(Integer)
    task_no: Mapped[int] = mapped_column(Integer)
    area: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    machine_state: Mapped[str] = mapped_column(String(32))  # RUNNING | STOPPED | POWERED_OFF
    safety_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    part_number: Mapped[Optional[str]] = mapped_column(String(64))
    source_chapter: Mapped[Optional[str]] = mapped_column(String(64))
    source_section: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    machine: Mapped[Machine] = relationship("Machine", back_populates="tasks")

    __table_args__ = (
        UniqueConstraint("machine_id", "interval_hours", "task_no", name="uq_task_pos"),
    )


class PMInterval(Base):
    __tablename__ = "pm_intervals"

    interval_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    machine_id: Mapped[str] = mapped_column(String(64), ForeignKey("machines.machine_id"))
    hours: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(32))
    natural_label: Mapped[str] = mapped_column(String(64))
    source: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("machine_id", "hours", name="uq_interval"),
    )


class PMRecord(Base):
    """Immutable audit record — rows are never deleted per GMP compliance."""

    __tablename__ = "pm_records"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    machine_id: Mapped[str] = mapped_column(String(64), ForeignKey("machines.machine_id"))
    interval_hours: Mapped[int] = mapped_column(Integer)
    work_order: Mapped[str] = mapped_column(String(128))
    technician_name: Mapped[str] = mapped_column(String(128))
    technician_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    # PENDING | IN_PROGRESS | COMPLETED | APPROVED
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    approved_by: Mapped[Optional[str]] = mapped_column(String(128))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    storage_target: Mapped[Optional[str]] = mapped_column(String(32))
    blob_url: Mapped[Optional[str]] = mapped_column(Text)
    file_name: Mapped[Optional[str]] = mapped_column(String(256))
    file_hash: Mapped[Optional[str]] = mapped_column(String(128))
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # updated_at intentionally omitted — records are append-only

    machine: Mapped[Machine] = relationship("Machine", back_populates="pm_records")
    completed_tasks: Mapped[list[CompletedTask]] = relationship(
        "CompletedTask", back_populates="pm_record"
    )


class CompletedTask(Base):
    __tablename__ = "completed_tasks"

    completed_task_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=_uuid
    )
    record_id: Mapped[str] = mapped_column(String(64), ForeignKey("pm_records.record_id"))
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("tasks.task_id"))
    task_no: Mapped[int] = mapped_column(Integer)
    initialed_by: Mapped[Optional[str]] = mapped_column(String(32))
    is_done: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    pm_record: Mapped[PMRecord] = relationship("PMRecord", back_populates="completed_tasks")


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # Azure AD OID
    email: Mapped[str] = mapped_column(String(256), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32))  # SUPERVISOR | TECHNICIAN | MANAGER | ENGINEER
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    """Immutable audit log — rows are never deleted per GMP compliance."""

    __tablename__ = "audit_logs"

    log_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[Optional[str]] = mapped_column(String(128))
    user_email: Mapped[Optional[str]] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    details: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ManualUpload(Base):
    __tablename__ = "manual_uploads"

    manual_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    machine_id: Mapped[Optional[str]] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(String(256))
    blob_url: Mapped[Optional[str]] = mapped_column(Text)
    local_path: Mapped[Optional[str]] = mapped_column(Text)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    detected_manufacturer: Mapped[Optional[str]] = mapped_column(String(128))
    detected_chapters: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    status: Mapped[str] = mapped_column(
        String(32), default="UPLOADED"
    )  # UPLOADED | CLASSIFYING | CHUNKING | EMBEDDING | EXTRACTING | PENDING_REVIEW | APPROVED | FAILED
    extracted_tasks: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(128))
    approved_by: Mapped[Optional[str]] = mapped_column(String(128))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MachineHours(Base):
    """Tracks current operating hours to drive overdue/due-soon logic."""

    __tablename__ = "machine_hours"

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    machine_id: Mapped[str] = mapped_column(String(64), ForeignKey("machines.machine_id"))
    current_hours: Mapped[int] = mapped_column(Integer)
    recorded_by: Mapped[Optional[str]] = mapped_column(String(128))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(128))
    machine_id: Mapped[Optional[str]] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128), default="New Chat")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list[ChatMessage]] = relationship("ChatMessage", back_populates="session")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("chat_sessions.session_id"))
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    has_checklist: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    session: Mapped[ChatSession] = relationship("ChatSession", back_populates="messages")


class Citation(Base):
    """Tracks which page/section each extracted chunk came from — enables clickable citations in UI."""

    __tablename__ = "citations"

    citation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    manual_id: Mapped[str] = mapped_column(String(64), ForeignKey("manual_uploads.manual_id"))
    chunk_id: Mapped[str] = mapped_column(String(128))
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    section: Mapped[Optional[str]] = mapped_column(String(256))
    # procedure | warning | loto | ppe | parts_list | startup | toc | table
    content_type: Mapped[str] = mapped_column(String(32), default="procedure")
    text_excerpt: Mapped[Optional[str]] = mapped_column(Text)  # first 500 chars for preview
    manual_version: Mapped[Optional[str]] = mapped_column(String(64))
    manufacturer: Mapped[Optional[str]] = mapped_column(String(128))
    machine_model: Mapped[Optional[str]] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ManualApproval(Base):
    """Records engineer approve/reject decisions per manual per interval."""

    __tablename__ = "manual_approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_uuid)
    manual_id: Mapped[str] = mapped_column(String(64), ForeignKey("manual_uploads.manual_id"))
    # 0 means approval applies to all intervals
    interval_hours: Mapped[int] = mapped_column(Integer, default=0)
    # APPROVED | REJECTED
    status: Mapped[str] = mapped_column(String(16))
    comment: Mapped[Optional[str]] = mapped_column(Text)  # required when REJECTED
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
