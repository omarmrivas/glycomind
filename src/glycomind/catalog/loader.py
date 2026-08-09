"""Carga del catalogo semilla. Idempotente: se puede reejecutar tras editar el YAML."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from glycomind.catalog.text import normalize
from glycomind.db.models import Food, FoodAlias, RecipeComponent

SEED_PATH = Path(__file__).parent / "seed" / "foods_mx.yaml"

_FRACTION_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class SeedResult:
    foods_created: int
    foods_updated: int
    aliases_created: int
    recipes_linked: int
    warnings: list[str]


def load_seed(db: Session, path: Path | None = None) -> SeedResult:
    raw: dict[str, Any] = yaml.safe_load((path or SEED_PATH).read_text(encoding="utf-8"))
    warnings: list[str] = []

    entries = list(raw.get("foods", [])) + [
        {**r, "is_recipe": True} for r in raw.get("recipes", [])
    ]

    existing = {f.slug: f for f in db.execute(select(Food)).scalars()}
    created = updated = 0

    for entry in entries:
        slug = entry["slug"]
        food = existing.get(slug)
        if food is None:
            food = Food(id=uuid.uuid4(), slug=slug)
            db.add(food)
            existing[slug] = food
            created += 1
        else:
            updated += 1
        food.canonical_name = entry["name"]
        food.category = entry["category"]
        food.is_recipe = bool(entry.get("is_recipe", False))
        food.default_portion_g = entry.get("default_portion_g")
        food.portion_unit = entry.get("portion_unit")
        # Los macros NO se escriben aqui: se importan de FoodData Central con su
        # referencia. Esto es solo la pista de busqueda.
        food.fdc_query_hint = entry.get("fdc_query")
    db.flush()

    aliases_created = _sync_aliases(db, entries, existing)
    recipes_linked, recipe_warnings = _sync_recipes(db, raw.get("recipes", []), existing)
    warnings.extend(recipe_warnings)

    return SeedResult(
        foods_created=created,
        foods_updated=updated,
        aliases_created=aliases_created,
        recipes_linked=recipes_linked,
        warnings=warnings,
    )


def _sync_aliases(db: Session, entries: list[dict], foods: dict[str, Food]) -> int:
    known = {
        (a.normalized, a.food_id)
        for a in db.execute(select(FoodAlias).where(FoodAlias.user_id.is_(None))).scalars()
    }
    # Un normalized global solo puede apuntar a un alimento: si dos entradas reclaman
    # el mismo alias, gana la primera y se avisa en vez de crear ambiguedad silenciosa.
    taken = {
        a.normalized
        for a in db.execute(select(FoodAlias).where(FoodAlias.user_id.is_(None))).scalars()
    }
    created = 0
    for entry in entries:
        food = foods[entry["slug"]]
        candidates = [entry["name"], *entry.get("aliases", [])]
        for alias in candidates:
            norm = normalize(alias)
            if not norm or (norm, food.id) in known or norm in taken:
                continue
            db.add(
                FoodAlias(
                    id=uuid.uuid4(),
                    food_id=food.id,
                    alias=alias,
                    normalized=norm,
                    user_id=None,
                    source="seed",
                )
            )
            known.add((norm, food.id))
            taken.add(norm)
            created += 1
    db.flush()
    return created


def _sync_recipes(
    db: Session, recipes: list[dict], foods: dict[str, Food]
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    linked = 0
    for recipe in recipes:
        parent = foods[recipe["slug"]]
        components = recipe.get("components", [])
        total = sum(c["fraction"] for c in components)
        if abs(total - 1.0) > _FRACTION_TOLERANCE:
            warnings.append(f"{recipe['slug']}: las fracciones suman {total:.2f}, no 1.0")

        existing = {
            rc.component_food_id: rc
            for rc in db.execute(
                select(RecipeComponent).where(RecipeComponent.recipe_food_id == parent.id)
            ).scalars()
        }
        for component in components:
            child = foods.get(component["food"])
            if child is None:
                warnings.append(f"{recipe['slug']}: el componente '{component['food']}' no existe")
                continue
            row = existing.get(child.id)
            if row is None:
                db.add(
                    RecipeComponent(
                        id=uuid.uuid4(),
                        recipe_food_id=parent.id,
                        component_food_id=child.id,
                        mass_fraction=component["fraction"],
                    )
                )
                linked += 1
            else:
                row.mass_fraction = component["fraction"]
    db.flush()
    return linked, warnings
