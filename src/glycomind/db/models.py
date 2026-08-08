"""Modelos SQLAlchemy 2.0. Fuente de verdad del esquema (Alembic autogenera desde aqui)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngestBatch(Base):
    """Linaje de cada importacion.

    ``legal_basis`` e ``is_official_api`` no son decorativos: si algun dia hay auditoria
    regulatoria o de privacidad, hay que poder demostrar la base legal de cada dato.
    """

    __tablename__ = "ingest_batch"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    vendor: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filename: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    is_official_api: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legal_basis: Mapped[str] = mapped_column(String(64), nullable=False)
    assumed_timezone: Mapped[str | None] = mapped_column(String(64))
    detected_date_order: Mapped[str | None] = mapped_column(String(8))
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    warnings: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CgmSensorSession(Base):
    """Una sesion = un sensor fisico. Delimitada por el numero de serie del dispositivo.

    Es la unidad de correccion de sesgo: el escalon entre sensores es la mayor fuente de
    artefacto sistematico en CGM.
    """

    __tablename__ = "cgm_sensor_session"
    __table_args__ = (UniqueConstraint("user_id", "device_serial_hash", "started_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    vendor: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    device_serial_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    native_resolution_min: Mapped[int | None] = mapped_column(SmallInteger)
    """Detectada empiricamente (mediana de diferencias consecutivas), nunca asumida."""
    n_readings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    step_vs_previous_mgdl: Mapped[float | None] = mapped_column(Float)

    readings: Mapped[list[GlucoseReading]] = relationship(back_populates="session")


class GlucoseReading(Base):
    __tablename__ = "glucose_reading"
    __table_args__ = (
        CheckConstraint("value_mgdl > 0", name="ck_glucose_positive"),
        Index("ix_glucose_reading_ts_brin", "ts_utc", postgresql_using="brin"),
        Index("ix_glucose_reading_user_ts", "user_id", "ts_utc"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    ts_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cgm_sensor_session.id", ondelete="CASCADE"), primary_key=True
    )
    tz_offset_min: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    value_mgdl: Mapped[float] = mapped_column(Float, nullable=False)
    source_record: Mapped[str] = mapped_column(String(16), nullable=False)
    quality_flags: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ingest_batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingest_batch.id"))

    session: Mapped[CgmSensorSession] = relationship(back_populates="readings")


class Meal(Base):
    __tablename__ = "meal"
    __table_args__ = (Index("ix_meal_user_time", "user_id", "consumed_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tz_offset_min: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    meal_type: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    free_text: Mapped[str | None] = mapped_column(Text)
    eating_duration_min: Mapped[int | None] = mapped_column(SmallInteger)
    order_pattern: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    photo_object_key: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    """'manual' | 'telegram' | 'libreview_food' — la friccion de captura importa."""
    entry_completeness: Mapped[float | None] = mapped_column(Float)
    ingest_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ingest_batch.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    items: Mapped[list[MealItem]] = relationship(
        back_populates="meal", cascade="all, delete-orphan"
    )
    response: Mapped[MealGlucoseResponse | None] = relationship(
        back_populates="meal", cascade="all, delete-orphan", uselist=False
    )


class MealItem(Base):
    __tablename__ = "meal_item"

    id: Mapped[uuid.UUID] = _uuid_pk()
    meal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meal.id", ondelete="CASCADE"))
    raw_label: Mapped[str] = mapped_column(Text, nullable=False)
    quantity_value: Mapped[float | None] = mapped_column(Float)
    quantity_unit: Mapped[str | None] = mapped_column(String(32))
    quantity_low: Mapped[float | None] = mapped_column(Float)
    quantity_high: Mapped[float | None] = mapped_column(Float)
    preparation: Mapped[str | None] = mapped_column(String(32))
    carbs_g: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False)
    user_corrected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    meal: Mapped[Meal] = relationship(back_populates="items")


class MealGlucoseResponse(Base):
    """La tabla central del producto. Si esta sucia, todo lo demas es teatro."""

    __tablename__ = "meal_glucose_response"

    id: Mapped[uuid.UUID] = _uuid_pk()
    meal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("meal.id", ondelete="CASCADE"), unique=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cgm_sensor_session.id", ondelete="SET NULL")
    )

    quality: Mapped[str] = mapped_column(String(16), nullable=False)
    exclusion_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    degradation_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )

    baseline_mgdl: Mapped[float | None] = mapped_column(Float)
    baseline_sd: Mapped[float | None] = mapped_column(Float)
    baseline_n: Mapped[int | None] = mapped_column(SmallInteger)
    peak_mgdl: Mapped[float | None] = mapped_column(Float)
    peak_delta_mgdl: Mapped[float | None] = mapped_column(Float)
    time_to_peak_min: Mapped[float | None] = mapped_column(Float)
    peak_underestimated: Mapped[bool | None] = mapped_column(Boolean)
    iauc_120: Mapped[float | None] = mapped_column(Float)
    iauc_180: Mapped[float | None] = mapped_column(Float)
    iauc_net_120: Mapped[float | None] = mapped_column(Float)
    auc_total_120: Mapped[float | None] = mapped_column(Float)
    time_above_baseline_min: Mapped[float | None] = mapped_column(Float)
    time_to_return_baseline_min: Mapped[float | None] = mapped_column(Float)
    cv_pct: Mapped[float | None] = mapped_column(Float)
    curve_shape: Mapped[str | None] = mapped_column(String(16))

    coverage_pct: Mapped[float | None] = mapped_column(Float)
    max_gap_min: Mapped[float | None] = mapped_column(Float)
    n_points: Mapped[int | None] = mapped_column(SmallInteger)
    resolution_min: Mapped[int | None] = mapped_column(SmallInteger)

    # Covariables congeladas al momento del calculo: reproducibilidad.
    hour_local: Mapped[int | None] = mapped_column(SmallInteger)
    prev_meal_gap_min: Mapped[float | None] = mapped_column(Float)
    next_meal_gap_min: Mapped[float | None] = mapped_column(Float)
    sensor_age_hours: Mapped[float | None] = mapped_column(Float)
    vendor: Mapped[str | None] = mapped_column(String(32))

    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    meal: Mapped[Meal] = relationship(back_populates="response")
