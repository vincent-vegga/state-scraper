#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STATE SCRAPER · Paso 4a — Cribado semántico
===========================================

Decide si una licitación es una oportunidad real para un profesional del
espectáculo en vivo, leyendo su título, órgano, importe y códigos CPV.

Por qué existe este paso: el CPV no discrimina. `92312250` significa
"servicios prestados por artistas individuales" y lo usan por igual un
cantautor y un apoderado taurino. No hay ningún filtro por códigos que
los separe, así que la precisión tiene que venir de leer el texto.

Tres salidas, nunca dos:

    si     -> es una oportunidad
    quizas -> podría serlo; NO se descarta, se muestra igual
    no     -> no lo es

El "quizas" no es indecisión, es diseño. Una llamada al modelo cuesta
céntimos; una oportunidad perdida cuesta un cliente. Ante la duda, el
cribado deja pasar y decide la persona.

Uso:
    python cribador.py                # clasifica lo pendiente
    python cribador.py --muestra 20   # prueba 20 sin guardar nada
    python cribador.py --limite 50    # tope de llamadas en esta pasada

Variables de entorno:
    SUPABASE_URL, SUPABASE_KEY   -> obligatorias
    OPENAI_API_KEY               -> obligatoria (salvo en --muestra vacía)
    MODELO_CRIBADO               -> por defecto gpt-4o-mini
    MAX_CRIBADO_POR_EJECUCION    -> tope de seguridad, por defecto 300
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

# ==============================================================
# 1. CONFIGURACIÓN
# ==============================================================

MODELO = os.environ.get("MODELO_CRIBADO", "gpt-4o-mini")

# Versión del prompt. SUBIR ESTE NÚMERO cada vez que se cambie el
# texto de abajo: es lo que permite comparar iteraciones y saber de
# qué versión viene cada veredicto guardado.
VERSION_PROMPT = "v2"

TABLA = "licitaciones"
VISTA_PENDIENTES = "licitaciones_por_cribar"

MAX_POR_EJECUCION = int(os.environ.get("MAX_CRIBADO_POR_EJECUCION", "300"))
TAMANO_LOTE_ESCRITURA = 50
REINTENTOS = 3
ESPERA_REINTENTO = 4  # segundos, se duplica en cada intento

VEREDICTOS_VALIDOS = {"si", "quizas", "no"}


# ==============================================================
# 2. EL PROMPT
#
#    Los ejemplos NO son decorativos: son casos reales clasificados
#    a mano por el Director de Proyecto. Definen la frontera mejor
#    que cualquier regla abstracta, sobre todo en los "quizas".
# ==============================================================

INSTRUCCIONES = """\
Eres un analista de contratación pública española especializado en el \
sector del espectáculo en vivo y la producción cultural.

TU CLIENTE es un profesional o pequeña empresa de ese sector: músicos y \
grupos, cantautores, humoristas, compañías de teatro y danza, productoras \
de eventos culturales, técnicos de sonido e iluminación, empresas de \
montaje escénico y de producción de exposiciones.

LA PREGUNTA QUE DEBES RESPONDER NO ES "¿esto tiene que ver con la cultura?" \
sino "¿PODRÍA MI CLIENTE EJECUTAR LA PRESTACIÓN PRINCIPAL de este \
contrato?". Muchos contratos ocurren en un contexto festivo o cultural sin \
que el trabajo en sí sea de producción artística: esos NO valen.

Fíjate siempre en el OBJETO PRINCIPAL del contrato, no en el contexto ni en \
las palabras sueltas del título.

RESPONDE "si" solo cuando el objeto PRINCIPAL sea:
- Ejecutar actuaciones artísticas, conciertos o espectáculos en vivo.
- Producir, dirigir o coordinar técnicamente un evento cultural, festival o \
programación escénica.
- Prestar servicios técnicos de espectáculo: sonido, iluminación, \
escenografía, montaje escénico, regiduría.
- Producir y montar una exposición.

RESPONDE "quizas" cuando:
- El contrato incluya producción o programación cultural junto a otras \
prestaciones ajenas (contrato "ómnibus").
- Haya componente escénico o de producción, pero el objeto principal sea \
otro o no se deduzca del título.
- Sean servicios auxiliares o de comunicación de un festival o espectáculo \
concreto.
- Se organice un evento no cultural (institucional, deportivo, académico) \
pero que requiere producción y medios técnicos escénicos.

RESPONDE "no" cuando el objeto principal sea:
- Espectáculos taurinos de cualquier tipo.
- Hostelería, catering, barras o restauración.
- Ferias comerciales, stands, promoción turística o participación \
institucional en ferias, sea cual sea el sector de la feria.
- Mercados y mercadillos, incluidos navideños y de feriantes.
- Visitas guiadas, atención al visitante, auxiliares de sala de museo.
- Bibliotecas, archivos, gestión, custodia o destrucción de documentación.
- ENSEÑANZA o formación artística: escuelas de música, teatro o danza, \
talleres y clases. Enseñar no es actuar.
- Actividades deportivas, saludables, de ocio infantil o socioeducativas \
sin componente escénico.
- Alquiler o arrendamiento de material sin producción (carpas, carrozas, \
mobiliario, estructuras).
- Control de acceso, seguridad, limpieza, vigilancia o mantenimiento.
- Obras, construcción o reforma de inmuebles.
- Artes plásticas, escultura o diseño gráfico sin espectáculo en vivo.

REGLA DE ORO: entre "quizas" y "no", ante duda razonable elige "quizas". \
Pero entre "si" y "quizas", exige que el objeto principal sea inequívoco \
para decir "si". Perder una oportunidad es grave; llamar "si" a lo que solo \
es "quizas" hace inútil la distinción.

Devuelve EXCLUSIVAMENTE un objeto JSON, sin texto alrededor ni marcas de \
código, con esta forma:
{"veredicto": "si|quizas|no", "motivo": "una frase breve en español"}\
"""

# Casos reales clasificados a mano, más los fallos detectados en la v1.
EJEMPLOS: list[tuple[str, str, str]] = [
    ("Contrato para la producción de conciertos en la semana grande de Laredo 2026",
     "si", "Producción directa de conciertos."),
    ("Serveis de producció, regidoria, coordinació tècnica i execució d'activitats "
     "culturals",
     "si", "Producción y regiduría de actividades culturales."),
    ("Servicios de organización, gestión y explotación de espectáculos taurinos "
     "Fiestas del Cristo 2026",
     "no", "Espectáculo taurino."),
    ("Contracte de serveis d'auxiliars d'espai per a les Festes de la Mercè 2026",
     "quizas", "Servicios auxiliares en un festival: depende del alcance."),
    ("Servicio de producción, montaje y desmontaje de la exposición temporal "
     "'La ilusión de la simetría'",
     "quizas", "Producción de exposición: encaja a nivel de producción."),
    ("Uso temporal de terrenos del Recinto Ferial para la instalación de una barra",
     "no", "Explotación de barra: hostelería."),
    ("Organización, programación, desarrollo y ejecución de la programación de "
     "actividades de los centros culturales",
     "quizas", "Contrato amplio que incluye programación cultural."),
    ("Contratación de una empresa especializada en diseño, montaje y alquiler de "
     "carrozas para cabalgatas",
     "no", "Alquiler de material sin producción artística."),
    # --- Fallos corregidos respecto a la v1 ---
    ("Organización de la Gala del Deporte de Xirivella",
     "quizas", "Gala no cultural, pero requiere producción y medios técnicos."),
    ("Servicios necesarios para la participación del Ministerio de Educación en "
     "una feria educativa",
     "no", "Participación institucional en feria."),
    ("Contrato de Servicios de la Gestión y Desarrollo de las clases de Bailes de "
     "Salón",
     "no", "Enseñanza de baile, no espectáculo."),
    ("Contrato Mixto de Servicio de recogida y destrucción de documentación "
     "confidencial",
     "no", "Gestión documental."),
    ("Servicio de actividades saludables",
     "no", "Actividad deportiva o de salud sin componente escénico."),
]


def construir_mensajes(licitacion: dict[str, Any]) -> list[dict[str, str]]:
    """Arma la conversación: instrucciones, ejemplos resueltos y el caso real."""
    mensajes: list[dict[str, str]] = [{"role": "system", "content": INSTRUCCIONES}]

    for titulo, veredicto, motivo in EJEMPLOS:
        mensajes.append({"role": "user", "content": f"Título: {titulo}"})
        mensajes.append({
            "role": "assistant",
            "content": json.dumps({"veredicto": veredicto, "motivo": motivo},
                                  ensure_ascii=False),
        })

    cpvs = licitacion.get("cpvs") or []
    if isinstance(cpvs, str):
        try:
            cpvs = json.loads(cpvs)
        except json.JSONDecodeError:
            cpvs = [cpvs]

    ficha = [f"Título: {licitacion.get('titulo', '')}"]
    if licitacion.get("organo"):
        ficha.append(f"Órgano: {licitacion['organo']}")

    presupuesto = licitacion.get("presupuesto")
    if presupuesto is not None:
        # Notación española: 67.990,00. El separador se intercambia con un
        # símbolo puente porque hacerlo en dos pasos directos se pisa a sí mismo.
        importe = f"{float(presupuesto):,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
        ficha.append(f"Presupuesto: {importe} EUR")
    if cpvs:
        ficha.append(f"CPV: {', '.join(str(c) for c in cpvs[:8])}")

    mensajes.append({"role": "user", "content": "\n".join(ficha)})
    return mensajes


# ==============================================================
# 3. UTILIDADES
# ==============================================================

def configurar_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def dividir_en_lotes(elementos, tamano):
    for inicio in range(0, len(elementos), tamano):
        yield elementos[inicio:inicio + tamano]


def obtener_cliente_supabase():
    """Conexión a Supabase, con las mismas cautelas que el lector de feeds."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    for sufijo in ("/rest/v1", "/rest"):
        if url.endswith(sufijo):
            url = url[: -len(sufijo)].rstrip("/")
    clave = os.environ.get("SUPABASE_KEY", "").strip()

    if not url or not clave:
        logging.error("Faltan SUPABASE_URL o SUPABASE_KEY en los secrets.")
        sys.exit(1)
    try:
        return create_client(url, clave)
    except Exception as error:
        logging.error("No se pudo conectar con Supabase: %s", error)
        sys.exit(1)


def obtener_cliente_openai():
    """Cliente de OpenAI. La clave nunca aparece en el código ni en los registros."""
    from openai import OpenAI

    clave = os.environ.get("OPENAI_API_KEY", "").strip()
    if not clave:
        logging.error(
            "Falta OPENAI_API_KEY. Créala en Settings > Secrets and variables > "
            "Actions del repositorio."
        )
        sys.exit(1)
    return OpenAI(api_key=clave)


# ==============================================================
# 4. CLASIFICACIÓN
# ==============================================================

def clasificar(cliente_ia, licitacion: dict[str, Any]) -> dict[str, str] | None:
    """
    Pide un veredicto al modelo para una licitación.

    Devuelve None si tras los reintentos no se obtiene respuesta válida.
    Un fallo puntual no debe tumbar la pasada entera: la licitación se
    queda sin veredicto y se reintentará en la siguiente ejecución, que
    es exactamente lo que hace falta que pase.
    """
    espera = ESPERA_REINTENTO
    for intento in range(1, REINTENTOS + 1):
        try:
            respuesta = cliente_ia.chat.completions.create(
                model=MODELO,
                messages=construir_mensajes(licitacion),
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=150,
            )
            bruto = (respuesta.choices[0].message.content or "").strip()
            datos = json.loads(bruto)
            veredicto = str(datos.get("veredicto", "")).strip().lower()

            if veredicto not in VEREDICTOS_VALIDOS:
                raise ValueError(f"veredicto no reconocido: {veredicto!r}")

            return {
                "veredicto": veredicto,
                "motivo": str(datos.get("motivo", "")).strip()[:300],
            }

        except json.JSONDecodeError as error:
            logging.warning("Respuesta no interpretable (intento %d/%d): %s",
                            intento, REINTENTOS, error)
        except Exception as error:
            logging.warning("Error del modelo (intento %d/%d): %s",
                            intento, REINTENTOS, error)

        if intento < REINTENTOS:
            time.sleep(espera)
            espera *= 2

    logging.error("Sin veredicto tras %d intentos: %s",
                  REINTENTOS, licitacion.get("titulo", "")[:70])
    return None


# ==============================================================
# 5. LECTURA Y ESCRITURA
# ==============================================================

def leer_pendientes(cliente, limite: int) -> list[dict[str, Any]]:
    """Licitaciones vivas y sin veredicto, de más reciente a más antigua."""
    try:
        respuesta = (
            cliente.table(VISTA_PENDIENTES)
            .select("id_licitacion, titulo, organo, presupuesto, cpvs, enlace")
            .limit(limite)
            .execute()
        )
        return respuesta.data or []
    except Exception as error:
        logging.error("No se pudo leer la cola de cribado: %s", error)
        sys.exit(1)


def guardar_veredictos(cliente, resultados: list[dict[str, Any]]) -> int:
    """
    Escribe los veredictos.

    Solo se envían las columnas del cribado: PostgREST deja intactas las
    que no recibe, así que no se pisa nada de lo que guardó el scraper.
    """
    if not resultados:
        return 0

    ahora = datetime.now(timezone.utc).isoformat()
    filas = [
        {
            "id_licitacion": r["id_licitacion"],
            "cribado_veredicto": r["veredicto"],
            "cribado_motivo": r["motivo"],
            "cribado_fecha": ahora,
            "cribado_version": VERSION_PROMPT,
            "cribado_modelo": MODELO,
        }
        for r in resultados
    ]

    guardados = 0
    for lote in dividir_en_lotes(filas, TAMANO_LOTE_ESCRITURA):
        try:
            cliente.table(TABLA).upsert(list(lote), on_conflict="id_licitacion").execute()
            guardados += len(lote)
        except Exception as error:
            logging.error("Fallo al guardar un lote de %d veredictos: %s",
                          len(lote), error)
    return guardados


def publicar_informe(resultados: list[dict[str, Any]]) -> None:
    """Resumen en el registro y en la pantalla de GitHub Actions."""
    reparto = Counter(r["veredicto"] for r in resultados)
    total = len(resultados)

    logging.info("--- REPARTO DE VEREDICTOS ---")
    for veredicto in ("si", "quizas", "no"):
        n = reparto.get(veredicto, 0)
        logging.info("  %-8s %4d  (%5.1f %%)", veredicto, n,
                     100 * n / total if total else 0)

    ruta = os.environ.get("GITHUB_STEP_SUMMARY")
    if not ruta or not total:
        return
    try:
        with open(ruta, "a", encoding="utf-8") as fichero:
            fichero.write(f"\n## Cribado ({VERSION_PROMPT}, {MODELO})\n\n")
            fichero.write(f"Clasificadas **{total}** · "
                          f"sí {reparto.get('si', 0)} · "
                          f"quizás {reparto.get('quizas', 0)} · "
                          f"no {reparto.get('no', 0)}\n\n")
            relevantes = [r for r in resultados if r["veredicto"] in ("si", "quizas")]
            if relevantes:
                fichero.write("| Veredicto | Título | Motivo |\n|---|---|---|\n")
                for r in relevantes[:40]:
                    titulo = r["titulo"][:80].replace("|", "/")
                    motivo = r["motivo"][:70].replace("|", "/")
                    fichero.write(f"| {r['veredicto']} | {titulo} | {motivo} |\n")
    except OSError as error:
        logging.warning("No se pudo escribir el informe: %s", error)


# ==============================================================
# 6. ORQUESTACIÓN
# ==============================================================

def main() -> int:
    argumentos = argparse.ArgumentParser(
        description="State Scraper · Paso 4a, cribado semántico."
    )
    argumentos.add_argument("--muestra", type=int, metavar="N",
                            help="Clasifica N licitaciones y las muestra SIN guardar.")
    argumentos.add_argument("--limite", type=int, metavar="N",
                            help="Tope de llamadas en esta pasada.")
    opciones = argumentos.parse_args()

    configurar_logging()
    es_prueba = opciones.muestra is not None
    limite = opciones.muestra or opciones.limite or MAX_POR_EJECUCION
    limite = min(limite, MAX_POR_EJECUCION)

    logging.info("=" * 62)
    logging.info("CRIBADO SEMÁNTICO · prompt %s · modelo %s", VERSION_PROMPT, MODELO)
    logging.info("Modo: %s | Tope de llamadas: %d",
                 "PRUEBA (no guarda)" if es_prueba else "normal", limite)
    logging.info("=" * 62)

    cliente = obtener_cliente_supabase()
    pendientes = leer_pendientes(cliente, limite)

    if not pendientes:
        logging.info("No hay licitaciones pendientes de cribar.")
        return 0

    logging.info("A clasificar: %d licitaciones.", len(pendientes))
    cliente_ia = obtener_cliente_openai()

    resultados: list[dict[str, Any]] = []
    fallos = 0

    for indice, licitacion in enumerate(pendientes, 1):
        veredicto = clasificar(cliente_ia, licitacion)
        if veredicto is None:
            fallos += 1
            continue

        resultados.append({
            "id_licitacion": licitacion["id_licitacion"],
            "titulo": licitacion.get("titulo", ""),
            **veredicto,
        })

        if es_prueba or indice % 25 == 0:
            logging.info("  [%3d/%d] %-6s · %s",
                         indice, len(pendientes), veredicto["veredicto"],
                         licitacion.get("titulo", "")[:75])

    if fallos:
        logging.warning("%d licitaciones sin veredicto. Se reintentarán "
                        "en la próxima ejecución.", fallos)

    publicar_informe(resultados)

    if es_prueba:
        logging.info("MODO PRUEBA: no se ha guardado nada en Supabase.")
        return 0

    guardados = guardar_veredictos(cliente, resultados)
    logging.info("Guardados %d veredictos.", guardados)
    return 0


if __name__ == "__main__":
    sys.exit(main())
