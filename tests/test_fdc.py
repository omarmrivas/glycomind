"""Tests del extractor de nutrientes de FoodData Central.

Sin red: se prueba el parseo de las dos formas de respuesta que devuelve la API segun
el endpoint, que es donde de verdad se rompen estas integraciones.
"""

from __future__ import annotations

import pytest

from glycomind.catalog.fdc import extract_nutrients

# Forma abreviada: la que devuelve /foods/search.
ABRIDGED = {
    "fdcId": 168872,
    "description": "Tortillas, ready-to-bake or -fry, corn",
    "foodNutrients": [
        {"nutrientId": 1008, "nutrientNumber": "208", "unitName": "KCAL", "value": 218.0},
        {"nutrientId": 1003, "nutrientNumber": "203", "unitName": "G", "value": 5.7},
        {"nutrientId": 1004, "nutrientNumber": "204", "unitName": "G", "value": 2.85},
        {"nutrientId": 1005, "nutrientNumber": "205", "unitName": "G", "value": 44.6},
        {"nutrientId": 1079, "nutrientNumber": "291", "unitName": "G", "value": 5.2},
        {"nutrientId": 1087, "nutrientNumber": "301", "unitName": "MG", "value": 81.0},
    ],
}

# Forma completa: la que devuelve /food/{fdcId}, con el nutriente anidado.
FULL = {
    "fdcId": 173727,
    "description": "Beans, black, mature seeds, cooked, boiled, without salt",
    "foodNutrients": [
        {
            "type": "FoodNutrient",
            "nutrient": {
                "id": 1005,
                "number": "205",
                "name": "Carbohydrate, by difference",
                "unitName": "g",
            },
            "amount": 23.71,
        },
        {
            "type": "FoodNutrient",
            "nutrient": {
                "id": 1079,
                "number": "291",
                "name": "Fiber, total dietary",
                "unitName": "g",
            },
            "amount": 8.7,
        },
        {
            "type": "FoodNutrient",
            "nutrient": {"id": 1003, "number": "203", "name": "Protein", "unitName": "g"},
            "amount": 8.86,
        },
    ],
}


def test_extracts_abridged_shape():
    out = extract_nutrients(ABRIDGED)
    assert out["carbohydrate"] == (44.6, "g")
    assert out["fiber"] == (5.2, "g")
    assert out["protein"] == (5.7, "g")
    assert out["fat"] == (2.85, "g")
    assert out["energy_kcal"] == (218.0, "kcal")


def test_extracts_full_nested_shape():
    """Regresion: las dos formas conviven segun el endpoint y hay que soportar ambas."""
    out = extract_nutrients(FULL)
    assert out["carbohydrate"] == (23.71, "g")
    assert out["fiber"] == (8.7, "g")
    assert out["protein"] == (8.86, "g")
    assert "fat" not in out


def test_ignores_nutrients_we_do_not_track():
    out = extract_nutrients(ABRIDGED)
    assert set(out) <= {"energy_kcal", "protein", "fat", "carbohydrate", "fiber", "sugars"}


def test_matches_by_legacy_number_when_id_is_absent():
    payload = {"foodNutrients": [{"nutrientNumber": "205", "unitName": "G", "value": 30.0}]}
    assert extract_nutrients(payload)["carbohydrate"] == (30.0, "g")


def test_missing_amount_is_skipped_not_zeroed():
    """Un nutriente sin valor NO es un cero: es un dato ausente."""
    payload = {"foodNutrients": [{"nutrientId": 1005, "unitName": "G", "value": None}]}
    assert "carbohydrate" not in extract_nutrients(payload)


@pytest.mark.parametrize("payload", [{}, {"foodNutrients": None}, {"foodNutrients": []}])
def test_empty_payloads_do_not_raise(payload: dict):
    assert extract_nutrients(payload) == {}


def test_malformed_entries_are_ignored():
    payload = {"foodNutrients": ["basura", None, 42, {"nutrientId": 1003, "value": 7.0}]}
    assert extract_nutrients(payload) == {"protein": (7.0, "g")}
