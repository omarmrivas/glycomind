"""CLI de GlycoMind (Fase 1)."""

from __future__ import annotations

import contextlib
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from glycomind.analysis.pipeline import analyze_user
from glycomind.config import ALGORITHM_VERSION, settings
from glycomind.db.models import AppUser, Meal, MealGlucoseResponse
from glycomind.db.session import db_session
from glycomind.domain.enums import MealType, ResponseQuality
from glycomind.ingest.libreview import LibreViewParseError
from glycomind.ingest.service import import_libreview_csv


def _force_utf8_output() -> None:
    """La consola de Windows usa cp1252 por defecto y no puede imprimir 'Δ', '≥' ni '→'.

    Sin esto, el CLI revienta al mostrar resultados correctos, que es la peor clase de
    fallo: el trabajo esta hecho pero parece roto.
    """
    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None)
        if enc and enc.lower().replace("-", "") != "utf8":
            with contextlib.suppress(AttributeError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


_force_utf8_output()

app = typer.Typer(
    help="Pipeline determinista comida <-> respuesta glucemica.", no_args_is_help=True
)
db_app = typer.Typer(help="Gestion del esquema.", no_args_is_help=True)
user_app = typer.Typer(help="Usuarios.", no_args_is_help=True)
meal_app = typer.Typer(help="Comidas.", no_args_is_help=True)
food_app = typer.Typer(help="Catalogo canonico de alimentos.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(meal_app, name="meal")
app.add_typer(food_app, name="food")

console = Console()


def _get_user(db, email: str) -> AppUser:
    user = db.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none()
    if user is None:
        raise typer.BadParameter(
            f"No existe el usuario {email}. Crealo con 'glycomind user create'."
        )
    return user


@db_app.command("create-all")
def db_create_all() -> None:
    """Crea el esquema directamente (solo desarrollo; en produccion usa Alembic)."""
    from glycomind.db.models import Base
    from glycomind.db.session import engine

    Base.metadata.create_all(engine)
    console.print("[green]Esquema creado.[/green]")


@db_app.command("apply-views")
def db_apply_views(
    path: Annotated[Path, typer.Option(help="Archivo SQL de vistas.")] = Path("sql/views.sql"),
) -> None:
    """(Re)crea las vistas de lectura usadas por Grafana. Idempotente."""
    from sqlalchemy import text

    from glycomind.db.session import engine

    sql = path.read_text(encoding="utf-8")
    with engine.begin() as conn:
        conn.execute(text(sql))
    console.print(f"[green]Vistas aplicadas desde {path}.[/green]")


@user_app.command("create")
def user_create(
    email: Annotated[str, typer.Option(help="Identificador del usuario.")],
    timezone: Annotated[str, typer.Option(help="Zona IANA.")] = settings.default_timezone,
    name: Annotated[str | None, typer.Option(help="Nombre para mostrar.")] = None,
) -> None:
    """Crea un usuario.

    Minimizacion de datos: solo correo, alias y zona horaria. Ni nombre completo, ni
    direccion, ni CURP. Ver docs/08-regulatorio.md.
    """
    ZoneInfo(timezone)
    with db_session() as db:
        if db.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none():
            console.print(f"[yellow]{email} ya existe.[/yellow]")
            return
        db.add(AppUser(id=uuid.uuid4(), email=email, display_name=name, timezone=timezone))
    console.print(f"[green]Usuario {email} creado ({timezone}).[/green]")


@app.command("import-libreview")
def import_libreview(
    path: Annotated[Path, typer.Argument(help="CSV descargado de LibreView.")],
    email: Annotated[str, typer.Option("--user", help="Correo del usuario.")],
    timezone: Annotated[str | None, typer.Option(help="Sobrescribe la zona del usuario.")] = None,
    date_order: Annotated[
        str | None,
        typer.Option(help="DMY|MDY|YMD. Solo si la autodeteccion resulta ambigua."),
    ] = None,
) -> None:
    """Importa un export de LibreView.

    La exportacion de Abbott es manual y esta protegida por reCAPTCHA, asi que no puede
    automatizarse. Reimportar archivos solapados es seguro e idempotente.
    """
    data = path.read_bytes()
    with db_session() as db:
        user = _get_user(db, email)
        try:
            result = import_libreview_csv(
                db,
                user=user,
                data=data,
                filename=path.name,
                timezone=timezone,
                date_order_hint=date_order,
            )
        except LibreViewParseError as exc:
            console.print(f"[red]No pude leer el archivo:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    table = Table(title=f"Importacion — {path.name}", show_header=False)
    table.add_row("Filas leidas", str(result.rows_parsed))
    table.add_row("Lecturas encontradas", str(result.readings_found))
    table.add_row("Lecturas nuevas", f"[green]{result.readings_inserted}[/green]")
    table.add_row("Ya conocidas", str(result.readings_duplicate))
    table.add_row("Sensores nuevos", str(result.sessions_created))
    table.add_row("Resolucion detectada", f"{result.detected_resolution_min} min")
    table.add_row("Comidas desde la app", str(result.food_entries_inserted))
    if result.first_ts_utc and result.last_ts_utc:
        table.add_row(
            "Rango (UTC)",
            f"{result.first_ts_utc:%Y-%m-%d %H:%M} → {result.last_ts_utc:%Y-%m-%d %H:%M}",
        )
    console.print(table)
    for w in result.warnings:
        console.print(f"[yellow]aviso:[/yellow] {w}")


@meal_app.command("add")
def meal_add(
    email: Annotated[str, typer.Option("--user")],
    at: Annotated[str, typer.Option(help="Hora local: 'YYYY-MM-DD HH:MM'.")],
    text: Annotated[str, typer.Option(help="Descripcion libre.")],
    meal_type: Annotated[MealType, typer.Option(help="Tipo de comida.")] = MealType.UNKNOWN,
) -> None:
    """Registra una comida en hora local del usuario."""
    with db_session() as db:
        user = _get_user(db, email)
        tz = ZoneInfo(user.timezone)
        local = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        offset = local.utcoffset()
        db.add(
            Meal(
                id=uuid.uuid4(),
                user_id=user.id,
                consumed_at=local,
                tz_offset_min=int(offset.total_seconds() // 60) if offset else 0,
                meal_type=meal_type.value,
                free_text=text,
                source="cli",
                entry_completeness=0.5,
            )
        )
    console.print(f"[green]Comida registrada:[/green] {at} — {text}")


@meal_app.command("import")
def meal_import(
    path: Annotated[Path, typer.Argument(help="Archivo 'YYYY-MM-DD HH:MM|descripcion' por linea.")],
    email: Annotated[str, typer.Option("--user")],
) -> None:
    """Carga comidas en lote desde un archivo de texto.

    Pensado para rellenar historico. La captura del dia a dia deberia tener mucha menos
    friccion (foto + un toque): si registrar cuesta, el usuario deja de hacerlo y el
    ratio de ventanas validas se hunde.
    """
    added = skipped = 0
    with db_session() as db:
        user = _get_user(db, email)
        tz = ZoneInfo(user.timezone)
        existing = {
            ts for (ts,) in db.execute(select(Meal.consumed_at).where(Meal.user_id == user.id))
        }
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            raw_ts, _, text = line.partition("|")
            try:
                local = datetime.strptime(raw_ts.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            except ValueError:
                console.print(f"[yellow]linea {lineno}: fecha ilegible, omitida[/yellow]")
                continue
            if local in existing:
                skipped += 1
                continue
            offset = local.utcoffset()
            db.add(
                Meal(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    consumed_at=local,
                    tz_offset_min=int(offset.total_seconds() // 60) if offset else 0,
                    free_text=text.strip() or None,
                    source="bulk_import",
                    entry_completeness=0.5,
                )
            )
            existing.add(local)
            added += 1
    console.print(f"[green]{added} comidas anadidas[/green], {skipped} ya existian.")


@food_app.command("seed")
def food_seed() -> None:
    """Carga el catalogo semilla de alimentos mexicanos. Idempotente."""
    from glycomind.catalog.loader import load_seed

    with db_session() as db:
        result = load_seed(db)
    console.print(
        f"[green]{result.foods_created} alimentos nuevos[/green], "
        f"{result.foods_updated} actualizados, {result.aliases_created} alias, "
        f"{result.recipes_linked} componentes de receta."
    )
    for w in result.warnings:
        console.print(f"[yellow]aviso:[/yellow] {w}")
    console.print(
        "\n[dim]El catalogo NO trae macronutrientes: se importan de FoodData Central "
        "con 'glycomind food import-nutrients' para que cada cifra tenga fuente.[/dim]"
    )


@food_app.command("import-nutrients")
def food_import_nutrients(
    api_key: Annotated[
        str, typer.Option(envvar="FDC_API_KEY", help="Clave de api.data.gov (gratuita).")
    ],
    slug: Annotated[list[str] | None, typer.Option(help="Limitar a estos slugs.")] = None,
    overwrite: Annotated[bool, typer.Option(help="Reemplaza valores ya importados.")] = False,
    limit: Annotated[int | None, typer.Option(help="Maximo de alimentos a procesar.")] = None,
) -> None:
    """Importa macronutrientes desde USDA FoodData Central.

    Cada valor queda con su ``fdcId`` en food_source_map: ninguna cifra nutricional del
    sistema es inventada. Limite de la API: 1000 peticiones por hora.
    """
    from glycomind.catalog.fdc import FdcClient, import_nutrients

    with db_session() as db:
        report = import_nutrients(
            db,
            FdcClient(api_key=api_key),
            slugs=list(slug) if slug else None,
            overwrite=overwrite,
            limit=limit,
        )

    console.print(
        f"[green]{report.imported} importados[/green], "
        f"{report.skipped_existing} ya tenian datos, {report.attempted} intentos."
    )
    if report.not_found:
        console.print(f"[yellow]sin coincidencia:[/yellow] {', '.join(report.not_found[:12])}")
    for err in report.errors:
        console.print(f"[red]error:[/red] {err}")


@food_app.command("search")
def food_search(
    text: Annotated[str, typer.Argument(help="Texto libre a resolver.")],
    email: Annotated[
        str | None, typer.Option("--user", help="Usa tambien los alias de este usuario.")
    ] = None,
) -> None:
    """Prueba el resolutor sobre un texto, sin escribir nada."""
    from glycomind.catalog.resolver import resolve_meal_text

    with db_session() as db:
        user_id = _get_user(db, email).id if email else None
        resolved = resolve_meal_text(db, text, user_id=user_id)

        table = Table(title=f"Resolucion de: {text!r}")
        for col in ("Item", "Cantidad", "Candidato", "Confianza", "Metodo", "Accion"):
            table.add_column(col)
        for item in resolved:
            qty = (
                f"{item.parsed.quantity_value:g} {item.parsed.quantity_unit or ''}".strip()
                if item.parsed.quantity_value is not None
                else "—"
            )
            if item.best is None:
                table.add_row(item.parsed.label, qty, "—", "—", "—", "[red]sin resolver[/red]")
                continue
            action = (
                "[green]automatico[/green]"
                if item.best.is_auto_assignable
                else "[yellow]requiere confirmacion[/yellow]"
            )
            table.add_row(
                item.parsed.label,
                qty,
                item.best.canonical_name,
                f"{item.best.confidence:.2f}",
                item.best.method,
                action,
            )
        console.print(table)


@food_app.command("link")
def food_link(
    email: Annotated[str, typer.Option("--user")],
    rebuild: Annotated[bool, typer.Option(help="Rehace los items no corregidos a mano.")] = False,
) -> None:
    """Convierte el texto libre de las comidas en items enlazados al catalogo."""
    from glycomind.catalog.linking import link_user_meals

    with db_session() as db:
        user = _get_user(db, email)
        report = link_user_meals(db, user_id=user.id, rebuild=rebuild)

    table = Table(title="Enlace con el catalogo", show_header=False)
    table.add_row("Comidas procesadas", str(report.meals_processed))
    table.add_row("Items creados", str(report.items_created))
    table.add_row("Asignados automaticamente", f"[green]{report.auto_assigned}[/green]")
    table.add_row("Requieren confirmacion", f"[yellow]{report.needs_confirmation}[/yellow]")
    table.add_row("Sin resolver", f"[red]{report.unresolved}[/red]")
    console.print(table)
    console.print(f"\n[bold]auto_assign_rate: {report.auto_assign_rate:.0%}[/bold]")

    if report.unresolved_labels:
        top = sorted(report.unresolved_labels.items(), key=lambda kv: -kv[1])[:10]
        pend = Table(title="Etiquetas sin resolver mas frecuentes")
        pend.add_column("Etiqueta")
        pend.add_column("N", justify="right")
        for label, count in top:
            pend.add_row(label, str(count))
        console.print(pend)
        console.print(
            "[dim]Anadelas al catalogo semilla o confirmalas con 'glycomind food pending'.[/dim]"
        )


@food_app.command("pending")
def food_pending(
    email: Annotated[str, typer.Option("--user")],
    limit: Annotated[int, typer.Option()] = 30,
) -> None:
    """Lista items sin alimento asignado, con la sugerencia del resolutor."""
    from glycomind.catalog.linking import list_pending

    with db_session() as db:
        user = _get_user(db, email)
        pending = list_pending(db, user_id=user.id, limit=limit)

    if not pending:
        console.print("[green]No hay items pendientes.[/green]")
        return
    table = Table(title="Items pendientes de confirmar")
    for col in ("Etiqueta", "Sugerencia", "Confianza", "meal_item_id"):
        table.add_column(col)
    for item in pending:
        table.add_row(
            item.raw_label,
            item.suggestion or "[red]ninguna[/red]",
            f"{item.confidence:.2f}" if item.confidence else "—",
            str(item.meal_item_id),
        )
    console.print(table)


@food_app.command("stats")
def food_stats() -> None:
    """Cobertura del catalogo: cuantos alimentos tienen macros importados."""
    from sqlalchemy import func as sqlfunc

    from glycomind.db.models import Food, FoodAlias, FoodNutrient

    with db_session() as db:
        total = db.execute(select(sqlfunc.count(Food.id))).scalar_one()
        recipes = db.execute(
            select(sqlfunc.count(Food.id)).where(Food.is_recipe.is_(True))
        ).scalar_one()
        aliases = db.execute(select(sqlfunc.count(FoodAlias.id))).scalar_one()
        with_macros = db.execute(
            select(sqlfunc.count(sqlfunc.distinct(FoodNutrient.food_id)))
        ).scalar_one()

    table = Table(title="Catalogo de alimentos", show_header=False)
    table.add_row("Alimentos", str(total))
    table.add_row("  de ellos, recetas", str(recipes))
    table.add_row("Alias", str(aliases))
    table.add_row("Con macronutrientes", f"{with_macros} / {total - recipes}")
    console.print(table)
    if with_macros == 0:
        console.print(
            "\n[yellow]Ningun alimento tiene macros todavia.[/yellow] Consigue una clave "
            "gratuita en https://api.data.gov/signup/ y ejecuta:\n"
            "  glycomind food import-nutrients --api-key TU_CLAVE"
        )


@app.command("analyze")
def analyze(
    email: Annotated[str, typer.Option("--user")],
    recompute: Annotated[bool, typer.Option(help="Recalcula todo, ignorando lo ya hecho.")] = False,
) -> None:
    """Empareja comidas con ventanas glucemicas y calcula metricas."""
    with db_session() as db:
        user = _get_user(db, email)
        summary = analyze_user(db, user_id=user.id, recompute=recompute)

    table = Table(title=f"Analisis — {ALGORITHM_VERSION}", show_header=False)
    table.add_row("Comidas", str(summary.meals_total))
    table.add_row("Calculadas ahora", str(summary.computed))
    table.add_row("Ya calculadas", str(summary.skipped_existing))
    table.add_row("Utilizables", f"[green]{summary.usable}[/green]")
    table.add_row("  de ellas, degradadas", str(summary.degraded))
    table.add_row("Excluidas", f"[red]{summary.excluded}[/red]")
    console.print(table)

    ratio = summary.pairing_valid_ratio
    color = "green" if ratio >= 0.6 else "red"
    console.print(f"\n[bold]pairing_valid_ratio: [{color}]{ratio:.0%}[/{color}][/bold]")
    if summary.computed and ratio < 0.6:
        console.print(
            "[yellow]Por debajo del 60%: el cuello de botella es la captura de comidas o "
            "la adherencia al sensor, no la estadistica.[/yellow]"
        )

    if summary.exclusion_counts:
        ex = Table(title="Por que se excluyeron")
        ex.add_column("Razon")
        ex.add_column("N", justify="right")
        for reason, count in summary.exclusion_counts.items():
            ex.add_row(reason, str(count))
        console.print(ex)


@app.command("report")
def report(
    email: Annotated[str, typer.Option("--user")],
    limit: Annotated[int, typer.Option(help="Cuantas respuestas mostrar.")] = 20,
) -> None:
    """Lista las respuestas postprandiales calculadas."""
    with db_session() as db:
        user = _get_user(db, email)
        rows = db.execute(
            select(MealGlucoseResponse, Meal)
            .join(Meal, Meal.id == MealGlucoseResponse.meal_id)
            .where(MealGlucoseResponse.user_id == user.id)
            .order_by(Meal.consumed_at.desc())
            .limit(limit)
        ).all()

    if not rows:
        console.print("[yellow]No hay respuestas calculadas. Ejecuta 'glycomind analyze'.[/yellow]")
        return

    table = Table(title=f"Respuestas postprandiales — {email}")
    for col in ("Fecha local", "Comida", "Calidad", "Basal", "Δ pico", "TTP", "iAUC-120", "Forma"):
        table.add_column(col)
    for resp, meal in rows:
        local = meal.consumed_at.astimezone(ZoneInfo(user.timezone))
        quality_color = {
            ResponseQuality.OK.value: "green",
            ResponseQuality.DEGRADED.value: "yellow",
            ResponseQuality.EXCLUDED.value: "red",
        }[resp.quality]
        detail = (
            ", ".join(resp.exclusion_reasons)
            if resp.quality == ResponseQuality.EXCLUDED.value
            else resp.quality
        )
        # El pico a resolucion gruesa es una COTA INFERIOR: se marca con '>='.
        peak = "—"
        if resp.peak_delta_mgdl is not None:
            prefix = "≥" if resp.peak_underestimated else ""
            peak = f"{prefix}{resp.peak_delta_mgdl:+.0f}"
        table.add_row(
            f"{local:%Y-%m-%d %H:%M}",
            (meal.free_text or "—")[:32],
            f"[{quality_color}]{detail}[/{quality_color}]",
            f"{resp.baseline_mgdl:.0f}" if resp.baseline_mgdl else "—",
            peak,
            f"{resp.time_to_peak_min:.0f}m" if resp.time_to_peak_min is not None else "—",
            f"{resp.iauc_120:.0f}" if resp.iauc_120 is not None else "—",
            resp.curve_shape or "—",
        )
    console.print(table)
    console.print(
        "\n[dim]≥ indica que el pico es una cota inferior: a 15 min de resolucion el "
        "maximo real cae entre muestras.[/dim]"
    )


if __name__ == "__main__":
    app()
