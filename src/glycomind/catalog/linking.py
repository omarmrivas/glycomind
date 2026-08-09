"""Enlace de comidas ya registradas con el catalogo canonico.

Convierte ``meal.free_text`` en ``meal_item`` con ``food_id``. Reejecutable: procesa lo
que falte y respeta lo que un humano ya corrigio.

Regla que no se negocia: **una coincidencia aproximada nunca se asigna automaticamente.**
Un alimento mal asignado corrompe el modelo estadistico en silencio, y el silencio es
peor que el hueco. Los items dudosos quedan con ``food_id`` nulo y su mejor sugerencia
disponible para que alguien la confirme.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from glycomind.catalog.resolver import ResolvedItem, resolve_meal_text
from glycomind.db.models import Meal, MealItem
from glycomind.domain.enums import Provenance


@dataclass(slots=True)
class LinkReport:
    meals_processed: int = 0
    items_created: int = 0
    auto_assigned: int = 0
    needs_confirmation: int = 0
    unresolved: int = 0
    unresolved_labels: dict[str, int] = field(default_factory=dict)

    @property
    def auto_assign_rate(self) -> float:
        total = self.auto_assigned + self.needs_confirmation + self.unresolved
        return self.auto_assigned / total if total else 0.0


def link_user_meals(db: Session, *, user_id: UUID, rebuild: bool = False) -> LinkReport:
    """Genera ``meal_item`` a partir del texto libre de cada comida del usuario."""
    report = LinkReport()
    meals = list(
        db.execute(select(Meal).where(Meal.user_id == user_id).order_by(Meal.consumed_at)).scalars()
    )

    for meal in meals:
        if not (meal.free_text or "").strip():
            continue

        current = list(db.execute(select(MealItem).where(MealItem.meal_id == meal.id)).scalars())
        # Lo que un humano corrigio a mano se respeta siempre.
        if current and not rebuild:
            continue
        if current and rebuild:
            if any(item.user_corrected for item in current):
                continue
            for item in current:
                db.delete(item)
            db.flush()

        resolved = resolve_meal_text(db, meal.free_text or "", user_id=user_id)
        if not resolved:
            continue
        report.meals_processed += 1

        for item in resolved:
            db.add(_build_item(meal.id, item))
            report.items_created += 1
            if item.best is not None and item.best.is_auto_assignable:
                report.auto_assigned += 1
            elif item.needs_confirmation:
                report.needs_confirmation += 1
            else:
                report.unresolved += 1
                label = item.parsed.normalized or item.parsed.raw
                report.unresolved_labels[label] = report.unresolved_labels.get(label, 0) + 1
    db.flush()
    return report


def _build_item(meal_id: UUID, resolved: ResolvedItem) -> MealItem:
    best = resolved.best
    assign = best is not None and best.is_auto_assignable
    return MealItem(
        id=uuid.uuid4(),
        meal_id=meal_id,
        raw_label=resolved.parsed.raw,
        food_id=best.food_id if assign else None,
        resolution_confidence=best.confidence if best else None,
        resolution_method=best.method if assign else None,
        quantity_value=resolved.parsed.quantity_value,
        quantity_unit=resolved.parsed.quantity_unit,
        preparation=resolved.parsed.preparation,
        provenance=Provenance.USER.value,
    )


@dataclass(frozen=True, slots=True)
class PendingItem:
    meal_item_id: UUID
    raw_label: str
    suggestion: str | None
    suggestion_food_id: UUID | None
    confidence: float | None


def list_pending(db: Session, *, user_id: UUID, limit: int = 50) -> list[PendingItem]:
    """Items sin resolver, ordenados por frecuencia: confirmar los mas comunes primero."""
    rows = db.execute(
        select(MealItem)
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(Meal.user_id == user_id, MealItem.food_id.is_(None))
        .limit(limit)
    ).scalars()

    out: list[PendingItem] = []
    for item in rows:
        candidates = resolve_meal_text(db, item.raw_label, user_id=user_id)
        best = candidates[0].best if candidates else None
        out.append(
            PendingItem(
                meal_item_id=item.id,
                raw_label=item.raw_label,
                suggestion=best.canonical_name if best else None,
                suggestion_food_id=best.food_id if best else None,
                confidence=best.confidence if best else None,
            )
        )
    return out


def confirm_item(
    db: Session, *, meal_item_id: UUID, food_id: UUID, user_id: UUID, learn_alias: bool = True
) -> None:
    """Asigna un alimento a mano y, opcionalmente, aprende como lo llama el usuario.

    Aprender el alias es lo que hace que el catalogo mejore con el uso: la proxima vez
    esa misma etiqueta se resolvera sola y de forma exacta.
    """
    from glycomind.catalog.text import normalize
    from glycomind.db.models import FoodAlias

    item = db.get(MealItem, meal_item_id)
    if item is None:
        raise ValueError(f"meal_item {meal_item_id} no existe")

    item.food_id = food_id
    item.resolution_method = "manual"
    item.resolution_confidence = 1.0
    item.user_corrected = True

    if learn_alias:
        norm = normalize(item.raw_label)
        exists = db.execute(
            select(FoodAlias).where(FoodAlias.normalized == norm, FoodAlias.user_id == user_id)
        ).scalar_one_or_none()
        if norm and exists is None:
            db.add(
                FoodAlias(
                    id=uuid.uuid4(),
                    food_id=food_id,
                    alias=item.raw_label,
                    normalized=norm,
                    user_id=user_id,
                    source="user_confirmed",
                )
            )
    db.flush()
