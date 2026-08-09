"""Tests del parseo de texto libre de comidas. Puros, sin base de datos."""

from __future__ import annotations

import pytest

from glycomind.catalog.text import (
    detect_preparation,
    normalize,
    parse_item,
    parse_meal_text,
    parse_quantity,
    split_items,
    split_on_connector,
    strip_stopwords,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tortilla de maíz", "tortilla de maiz"),
        ("PLÁTANO", "platano"),
        ("café,  sin   azúcar", "cafe sin azucar"),
        ("Frijoles (de la olla)", "frijoles de la olla"),
        ("  ", ""),
    ],
)
def test_normalize(raw: str, expected: str):
    assert normalize(raw) == expected


def test_strip_stopwords():
    assert strip_stopwords("tortilla de maiz") == "tortilla maiz"
    # Si solo quedaran stopwords se devuelve el original: mejor eso que una cadena vacia.
    assert strip_stopwords("de la") == "de la"


@pytest.mark.parametrize(
    ("raw", "value", "unit", "rest"),
    [
        ("2 tortillas", 2.0, None, "tortillas"),
        ("1 taza de arroz", 1.0, "taza", "de arroz"),
        ("150 g de pollo", 150.0, "g", "de pollo"),
        ("medio aguacate", 0.5, None, "aguacate"),
        ("una manzana", 1.0, None, "manzana"),
        ("1.5 tazas de frijoles", 1.5, "taza", "de frijoles"),
        ("2,5 cucharadas de aceite", 2.5, "cucharada", "de aceite"),
        ("aguacate", None, None, "aguacate"),
    ],
)
def test_parse_quantity(raw: str, value: float | None, unit: str | None, rest: str):
    assert parse_quantity(raw) == (value, unit, rest)


def test_quantity_does_not_swallow_the_food_name():
    """'2 huevos estrellados': 'huevos' es el alimento, no una unidad."""
    value, unit, rest = parse_quantity("2 huevos estrellados")
    assert value == 2.0
    assert unit is None
    assert rest == "huevos estrellados"


def test_split_items_on_commas_and_conjunctions():
    assert split_items("tortillas, frijoles y pollo") == ["tortillas", "frijoles", "pollo"]
    assert split_items("arroz + pollo") == ["arroz", "pollo"]
    assert split_items("solo pollo") == ["solo pollo"]


def test_connector_is_not_a_primary_separator():
    """' con ' NO puede separar de entrada: rompería 'arroz con pollo'."""
    assert split_items("arroz con pollo") == ["arroz con pollo"]


def test_split_on_connector_is_available_as_fallback():
    assert split_on_connector("avena con platano") == ["avena", "platano"]
    assert split_on_connector("cafe sin azucar") == ["cafe", "azucar"]
    assert split_on_connector("aguacate") == []


def test_leading_stopwords_are_stripped_from_label():
    """'1 taza de frijoles' -> la etiqueta buscable es 'frijoles', no 'de frijoles'."""
    item = parse_item("1 taza de frijoles negros")
    assert item.label == "frijoles negros"
    assert item.quantity_value == 1.0
    assert item.quantity_unit == "taza"


def test_trailing_stopwords_inside_a_name_are_preserved():
    """'frijoles de la olla' es un nombre real: no se toca lo que no va al inicio."""
    assert parse_item("frijoles de la olla").label == "frijoles de la olla"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("papas fritas", "frito"),
        ("pollo a la plancha", "asado"),
        ("frijoles de la olla", "hervido"),
        ("verduras al horno", "horneado"),
        ("zanahoria cruda", "crudo"),
        ("arroz blanco", None),
    ],
)
def test_detect_preparation(text: str, expected: str | None):
    assert detect_preparation(normalize(text)) == expected


def test_parse_meal_text_end_to_end():
    items = parse_meal_text("2 tortillas de maíz, 1 taza de frijoles y pollo asado")
    assert [i.label for i in items] == ["tortillas de maíz", "frijoles", "pollo asado"]
    assert [i.quantity_value for i in items] == [2.0, 1.0, None]
    assert [i.quantity_unit for i in items] == [None, "taza", None]
    assert items[2].preparation == "asado"


def test_raw_text_is_never_lost():
    """El texto crudo del usuario se conserva siempre: es el dato, lo demas es lectura."""
    raw = "1 taza de FRIJOLES de la olla"
    item = parse_item(raw)
    assert item.raw == raw
    assert item.raw != item.label
