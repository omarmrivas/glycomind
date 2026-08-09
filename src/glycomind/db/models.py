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
    """Lo que escribio el usuario. NUNCA se sobrescribe al resolver: es el dato crudo."""
    food_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("food.id", ondelete="SET NULL"))
    resolution_confidence: Mapped[float | None] = mapped_column(Float)
    resolution_method: Mapped[str | None] = mapped_column(String(24))
    """'exact_alias' | 'user_alias' | 'fuzzy' | 'manual'. Un fuzzy nunca se asigna solo."""
    quantity_value: Mapped[float | None] = mapped_column(Float)
    quantity_unit: Mapped[str | None] = mapped_column(String(32))
    quantity_low: Mapped[float | None] = mapped_column(Float)
    quantity_high: Mapped[float | None] = mapped_column(Float)
    preparation: Mapped[str | None] = mapped_column(String(32))
    carbs_g: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False)
    user_corrected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    meal: Mapped[Meal] = relationship(back_populates="items")
    food: Mapped[Food | None] = relationship()


class Food(Base):
    """Alimento canonico.

    Existe para que el motor estadistico pueda agrupar por identidad y no por cadena de
    texto: hoy "tortillas" y "tortillas, frijoles, pollo" son grupos distintos en
    v_food_summary, lo que hace imposible aprender nada. El modelo jerarquico de la
    Fase 2 necesita ``food_id`` y macros como covariables, no strings.
    """

    __tablename__ = "food"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    is_recipe: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Un platillo compuesto se modela como receta con componentes, no como entrada
    monolitica: el objetivo es aprender que COMPONENTE mueve la glucosa."""
    default_portion_g: Mapped[float | None] = mapped_column(Float)
    portion_unit: Mapped[str | None] = mapped_column(String(32))
    fdc_query_hint: Mapped[str | None] = mapped_column(Text)
    """Termino de busqueda para el importador de FoodData Central. Los macros NO se
    escriben a mano: se importan de una fuente citable."""
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    aliases: Mapped[list[FoodAlias]] = relationship(
        back_populates="food", cascade="all, delete-orphan"
    )
    nutrients: Mapped[list[FoodNutrient]] = relationship(
        back_populates="food", cascade="all, delete-orphan"
    )


class FoodAlias(Base):
    """Como llama la gente a un alimento. Incluye plurales y regionalismos."""

    __tablename__ = "food_alias"
    __table_args__ = (
        UniqueConstraint("normalized", "user_id", name="uq_food_alias_normalized_user"),
        Index(
            "ix_food_alias_trgm",
            "normalized",
            postgresql_using="gin",
            postgresql_ops={"normalized": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(160), nullable=False)
    normalized: Mapped[str] = mapped_column(String(160), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    """NULL = alias global del catalogo. No nulo = como lo escribe ESTE usuario."""
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="seed")

    food: Mapped[Food] = relationship(back_populates="aliases")


class FoodNutrient(Base):
    """Composicion por 100 g. Cada valor con su fuente: nunca se inventa un macro."""

    __tablename__ = "food_nutrient"
    __table_args__ = (UniqueConstraint("food_id", "nutrient"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"))
    nutrient: Mapped[str] = mapped_column(String(24), nullable=False)
    """'carbohydrate' | 'fiber' | 'protein' | 'fat' | 'energy_kcal' | 'sugars'"""
    amount_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(12), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(120))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    food: Mapped[Food] = relationship(back_populates="nutrients")


class FoodSourceMap(Base):
    """Correspondencia con catalogos externos. Preserva la trazabilidad del dato."""

    __tablename__ = "food_source_map"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    """'fdc' | 'openfoodfacts' | 'innsz' | 'smae' | 'imss'"""
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    external_name: Mapped[str | None] = mapped_column(Text)
    matched_by: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")


class RecipeComponent(Base):
    """Descomposicion de un platillo en ingredientes, con su proporcion en masa."""

    __tablename__ = "recipe_component"
    __table_args__ = (UniqueConstraint("recipe_food_id", "component_food_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    recipe_food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"))
    component_food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"))
    mass_fraction: Mapped[float] = mapped_column(Float, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserFoodPortion(Base):
    """La porcion habitual de ESTE usuario para ESTE alimento.

    Es la mitigacion mas rentable del problema de estimar porciones: contar objetos
    discretos ("2 tortillas") es mucho mas fiable que estimar volumen, y los mejores
    modelos multimodales tienen ~36% de error en peso.
    """

    __tablename__ = "user_food_portion"
    __table_args__ = (UniqueConstraint("user_id", "food_id", "label"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    food_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(48), nullable=False, default="default")
    grams: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="user_reported")
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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
