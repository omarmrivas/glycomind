"""Resolucion de texto libre a alimento canonico.

Politica central: **un emparejamiento aproximado nunca se asigna solo.** Asignar mal un
alimento corrompe el modelo estadistico en silencio y sin dejar rastro, que es
exactamente el modo de fallo que este proyecto existe para evitar. Solo las coincidencias
exactas de alias se aplican automaticamente; lo demas se propone y espera confirmacion.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Float, func, select
from sqlalchemy.orm import Session

from glycomind.catalog.text import ParsedItem, normalize, parse_meal_text, strip_stopwords
from glycomind.db.models import Food, FoodAlias

# Solo un alias exacto se aplica sin preguntar.
AUTO_ASSIGN_MIN_CONFIDENCE = 0.95
# Por debajo de esto ni siquiera se sugiere: mas ruido que ayuda.
SUGGEST_MIN_SIMILARITY = 0.45


@dataclass(frozen=True, slots=True)
class FoodCandidate:
    food_id: UUID
    slug: str
    canonical_name: str
    confidence: float
    method: str  # 'user_alias' | 'exact_alias' | 'fuzzy'

    @property
    def is_auto_assignable(self) -> bool:
        return self.confidence >= AUTO_ASSIGN_MIN_CONFIDENCE


@dataclass(frozen=True, slots=True)
class ResolvedItem:
    parsed: ParsedItem
    candidates: tuple[FoodCandidate, ...]

    @property
    def best(self) -> FoodCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.candidates) and not self.candidates[0].is_auto_assignable

    @property
    def is_unresolved(self) -> bool:
        return not self.candidates


def _exact_matches(db: Session, normalized: str, user_id: UUID | None) -> list[FoodCandidate]:
    """Alias exacto. El alias propio del usuario gana sobre el global del catalogo."""
    rows = db.execute(
        select(FoodAlias.food_id, Food.slug, Food.canonical_name, FoodAlias.user_id)
        .join(Food, Food.id == FoodAlias.food_id)
        .where(
            FoodAlias.normalized == normalized,
            (FoodAlias.user_id == user_id) | (FoodAlias.user_id.is_(None)),
        )
    ).all()

    out = [
        FoodCandidate(
            food_id=food_id,
            slug=slug,
            canonical_name=name,
            confidence=1.0 if alias_user is not None else 0.97,
            method="user_alias" if alias_user is not None else "exact_alias",
        )
        for food_id, slug, name, alias_user in rows
    ]
    return sorted(out, key=lambda c: -c.confidence)


def _fuzzy_matches(
    db: Session, normalized: str, user_id: UUID | None, limit: int
) -> list[FoodCandidate]:
    """Similitud por trigramas (pg_trgm). Siempre requiere confirmacion."""
    similarity = func.similarity(FoodAlias.normalized, normalized).cast(Float)
    rows = db.execute(
        select(FoodAlias.food_id, Food.slug, Food.canonical_name, similarity.label("sim"))
        .join(Food, Food.id == FoodAlias.food_id)
        .where(
            (FoodAlias.user_id == user_id) | (FoodAlias.user_id.is_(None)),
            similarity >= SUGGEST_MIN_SIMILARITY,
        )
        .order_by(similarity.desc())
        .limit(limit * 3)
    ).all()

    best_per_food: dict[UUID, FoodCandidate] = {}
    for food_id, slug, name, sim in rows:
        # El tope es 0.90: por debajo del umbral de auto-asignacion, siempre.
        confidence = min(0.90, float(sim))
        current = best_per_food.get(food_id)
        if current is None or confidence > current.confidence:
            best_per_food[food_id] = FoodCandidate(
                food_id=food_id,
                slug=slug,
                canonical_name=name,
                confidence=confidence,
                method="fuzzy",
            )
    return sorted(best_per_food.values(), key=lambda c: -c.confidence)[:limit]


def resolve_label(
    db: Session, label: str, *, user_id: UUID | None = None, limit: int = 5
) -> list[FoodCandidate]:
    """Candidatos para una etiqueta suelta, de mas a menos confianza."""
    normalized = normalize(label)
    if not normalized:
        return []

    exact = _exact_matches(db, normalized, user_id)
    if exact:
        return exact[:limit]

    # Segundo intento sin articulos: 'tortilla de maiz' -> 'tortilla maiz'.
    stripped = strip_stopwords(normalized)
    if stripped != normalized:
        exact = _exact_matches(db, stripped, user_id)
        if exact:
            return exact[:limit]

    return _fuzzy_matches(db, normalized, user_id, limit)


def resolve_meal_text(db: Session, text: str, *, user_id: UUID | None = None) -> list[ResolvedItem]:
    """Resuelve la descripcion completa de una comida.

    Se intenta primero la cadena entera, porque muchos platillos llevan separadores en
    el nombre ("arroz con pollo", "huevos a la mexicana"). Solo si eso no da una
    coincidencia exacta se trocea en items.
    """
    text = (text or "").strip()
    if not text:
        return []

    from glycomind.catalog.text import parse_item, split_on_connector

    whole = resolve_label(db, text, user_id=user_id)
    if whole and whole[0].is_auto_assignable:
        return [ResolvedItem(parsed=parse_item(text), candidates=tuple(whole))]

    out: list[ResolvedItem] = []
    for item in parse_meal_text(text):
        candidates = resolve_label(db, item.label, user_id=user_id)
        if candidates and candidates[0].is_auto_assignable:
            out.append(ResolvedItem(parsed=item, candidates=tuple(candidates)))
            continue

        # Respaldo: trocear por ' con '. Solo se acepta si produce MAS coincidencias
        # exactas que dejarlo entero; asi "arroz con pollo" sigue siendo un plato y
        # "avena con platano" se convierte en dos alimentos.
        split = _try_connector_split(db, item.label, user_id, split_on_connector, parse_item)
        if split is not None:
            out.extend(split)
        else:
            out.append(ResolvedItem(parsed=item, candidates=tuple(candidates)))
    return out


def _try_connector_split(
    db: Session, label: str, user_id: UUID | None, splitter, parser
) -> list[ResolvedItem] | None:
    parts = splitter(label)
    if not parts:
        return None
    resolved = [
        ResolvedItem(
            parsed=parser(part), candidates=tuple(resolve_label(db, part, user_id=user_id))
        )
        for part in parts
    ]
    exact = sum(1 for r in resolved if r.best is not None and r.best.is_auto_assignable)
    return resolved if exact >= 2 or (exact == 1 and len(parts) == 2) else None


@dataclass(frozen=True, slots=True)
class ResolutionStats:
    items_total: int
    auto_assigned: int
    needs_confirmation: int
    unresolved: int

    @property
    def coverage(self) -> float:
        return self.auto_assigned / self.items_total if self.items_total else 0.0
