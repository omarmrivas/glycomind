"""Integracion del catalogo contra PostgreSQL: semilla, resolutor y enlace.

Necesita pg_trgm, asi que solo corre con la base real.

    uv run pytest -m db
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select, text

from glycomind.catalog.linking import confirm_item, link_user_meals, list_pending
from glycomind.catalog.loader import load_seed
from glycomind.catalog.resolver import resolve_label, resolve_meal_text
from glycomind.db.models import AppUser, Food, FoodAlias, Meal, MealItem, RecipeComponent
from glycomind.db.session import SessionLocal, engine

pytestmark = pytest.mark.db

TZ = "America/Mexico_City"


@pytest.fixture(scope="module", autouse=True)
def _require_db():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL no disponible: {exc}")


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    """El catalogo es global: se carga una vez para todo el modulo."""
    session = SessionLocal()
    try:
        load_seed(session)
        session.commit()
    finally:
        session.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture
def user(db):
    u = AppUser(id=uuid.uuid4(), email=f"cat-{uuid.uuid4().hex[:10]}@example.invalid", timezone=TZ)
    db.add(u)
    db.commit()
    yield u
    db.execute(delete(AppUser).where(AppUser.id == u.id))
    db.commit()


# --------------------------------------------------------------------------------------
# Semilla
# --------------------------------------------------------------------------------------


def test_seed_is_idempotent(db):
    before = db.execute(select(func.count(Food.id))).scalar_one()
    result = load_seed(db)
    db.commit()
    after = db.execute(select(func.count(Food.id))).scalar_one()
    assert after == before
    assert result.foods_created == 0
    assert not result.warnings


def test_recipes_decompose_into_components(db):
    """Los platillos se modelan por ingredientes para poder atribuir el efecto."""
    chilaquiles = db.execute(select(Food).where(Food.slug == "chilaquiles")).scalar_one()
    assert chilaquiles.is_recipe

    components = (
        db.execute(select(RecipeComponent).where(RecipeComponent.recipe_food_id == chilaquiles.id))
        .scalars()
        .all()
    )
    assert len(components) >= 3
    assert sum(c.mass_fraction for c in components) == pytest.approx(1.0, abs=0.01)


def test_no_seed_food_has_invented_macros(db):
    """Los macros solo entran por importacion citable, nunca desde el YAML."""
    from glycomind.db.models import FoodNutrient

    invented = db.execute(
        select(func.count(FoodNutrient.id)).where(FoodNutrient.source == "seed")
    ).scalar_one()
    assert invented == 0


# --------------------------------------------------------------------------------------
# Resolutor
# --------------------------------------------------------------------------------------


def test_exact_alias_resolves_automatically(db):
    candidates = resolve_label(db, "tortillas")
    assert candidates
    assert candidates[0].slug == "tortilla-maiz"
    assert candidates[0].is_auto_assignable


def test_accents_and_case_do_not_matter(db):
    assert resolve_label(db, "PLÁTANO")[0].slug == "platano"
    assert resolve_label(db, "platano")[0].slug == "platano"


@pytest.mark.parametrize(
    ("typo", "expected_slug"),
    [
        ("tortila", "tortilla-maiz"),
        ("aguacata", "aguacate"),
        ("pechuga de poyo", "pollo-pechuga"),
        ("frijol negro cosido", "frijol-negro"),
    ],
)
def test_fuzzy_match_suggests_but_never_auto_assigns(db, typo: str, expected_slug: str):
    """Un emparejamiento aproximado corrompe el modelo en silencio si se aplica solo.

    Se sugiere, y nunca se asigna: el techo de confianza del metodo difuso (0.90) queda
    por debajo del umbral de auto-asignacion (0.95) por construccion.
    """
    candidates = resolve_label(db, typo)
    assert candidates
    assert candidates[0].slug == expected_slug
    assert candidates[0].method == "fuzzy"
    assert not candidates[0].is_auto_assignable
    assert candidates[0].confidence <= 0.90


def test_unknown_food_returns_nothing(db):
    assert resolve_label(db, "zzzqwerty") == []


def test_very_distant_typo_is_not_even_suggested(db):
    """'tortiya' queda en ~0.42 de similitud: sugerirlo seria mas ruido que ayuda."""
    assert resolve_label(db, "tortiya") == []


def test_dish_name_with_connector_stays_whole(db):
    """'arroz con pollo' es un platillo, no dos alimentos."""
    resolved = resolve_meal_text(db, "arroz con pollo")
    assert len(resolved) == 1
    assert resolved[0].best is not None
    assert resolved[0].best.slug == "arroz-con-pollo"


def test_connector_splits_when_it_yields_real_foods(db):
    """'avena con platano' si son dos alimentos: el respaldo debe activarse."""
    resolved = resolve_meal_text(db, "avena con platano y leche")
    slugs = [r.best.slug for r in resolved if r.best]
    assert slugs == ["avena", "platano", "leche-entera"]
    assert all(r.best.is_auto_assignable for r in resolved if r.best)


def test_quantities_survive_resolution(db):
    resolved = resolve_meal_text(db, "2 tortillas de maiz y 1 taza de frijoles negros")
    assert [r.parsed.quantity_value for r in resolved] == [2.0, 1.0]
    assert [r.parsed.quantity_unit for r in resolved] == [None, "taza"]
    assert [r.best.slug for r in resolved] == ["tortilla-maiz", "frijol-negro"]


# --------------------------------------------------------------------------------------
# Enlace de comidas
# --------------------------------------------------------------------------------------


def _add_meal(db, user, text_: str) -> Meal:
    from datetime import UTC, datetime

    meal = Meal(
        id=uuid.uuid4(),
        user_id=user.id,
        consumed_at=datetime.now(UTC),
        tz_offset_min=-360,
        free_text=text_,
        source="test",
    )
    db.add(meal)
    db.flush()
    return meal


def test_link_creates_items_and_assigns_known_foods(db, user):
    _add_meal(db, user, "tortillas, frijoles, pollo, aguacate")
    db.commit()

    report = link_user_meals(db, user_id=user.id)
    db.commit()

    assert report.items_created == 4
    assert report.auto_assigned == 4
    assert report.auto_assign_rate == 1.0

    items = db.execute(select(MealItem).join(Meal).where(Meal.user_id == user.id)).scalars().all()
    assert all(i.food_id is not None for i in items)
    assert all(i.resolution_method == "exact_alias" for i in items)


def test_unknown_food_stays_unassigned_rather_than_guessed(db, user):
    _add_meal(db, user, "pizza hawaiana")
    db.commit()
    report = link_user_meals(db, user_id=user.id)
    db.commit()

    assert report.unresolved == 1
    assert report.auto_assigned == 0
    item = db.execute(select(MealItem).join(Meal).where(Meal.user_id == user.id)).scalar_one()
    assert item.food_id is None
    assert item.raw_label == "pizza hawaiana"  # el texto crudo nunca se pierde


def test_link_is_idempotent_and_respects_manual_corrections(db, user):
    _add_meal(db, user, "tortillas y frijoles")
    db.commit()
    link_user_meals(db, user_id=user.id)
    db.commit()

    second = link_user_meals(db, user_id=user.id)
    db.commit()
    assert second.items_created == 0


def test_confirming_an_item_teaches_a_user_alias(db, user):
    """Confirmar una vez hace que esa etiqueta se resuelva sola la proxima."""
    _add_meal(db, user, "mi pan de siempre")
    db.commit()
    link_user_meals(db, user_id=user.id)
    db.commit()

    pending = list_pending(db, user_id=user.id)
    assert len(pending) == 1

    bolillo = db.execute(select(Food).where(Food.slug == "bolillo")).scalar_one()
    confirm_item(db, meal_item_id=pending[0].meal_item_id, food_id=bolillo.id, user_id=user.id)
    db.commit()

    learned = db.execute(select(FoodAlias).where(FoodAlias.user_id == user.id)).scalars().all()
    assert len(learned) == 1
    assert learned[0].food_id == bolillo.id

    # Ahora esa etiqueta resuelve sola, y con prioridad sobre el catalogo global.
    candidates = resolve_label(db, "mi pan de siempre", user_id=user.id)
    assert candidates[0].slug == "bolillo"
    assert candidates[0].method == "user_alias"
    assert candidates[0].is_auto_assignable

    # Y no contamina a otros usuarios.
    assert resolve_label(db, "mi pan de siempre") == []
