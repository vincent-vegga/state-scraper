# State Scraper

Alertas automáticas de contratación pública para pymes y autónomos.

El objetivo es reducir la fricción burocrática de descubrir oportunidades de
contratación pública: en lugar de que un profesional revise a mano portales
oficiales que publican miles de anuncios diarios, un robot los lee cada
mañana, filtra por sector y avisa solo de lo relevante.

El nicho de partida es **eventos y cultura**, pero el sector es configuración,
no código: cambiar de dominio es editar una línea.

> Proyecto desarrollado en el marco de un programa de fellowship, con un mes
> de plazo para llegar a MVP funcional.

---

## Estado del proyecto

El sistema está concebido como un pipeline de cinco pasos:

| Paso | Descripción | Estado |
|---|---|---|
| 1 | Conexión a feeds ATOM oficiales | ✅ Operativo |
| 2 | Filtrado por CPV y control de estado | ✅ Operativo |
| 2b | Filtro por estado del expediente (solo `PUB`) | ✅ Operativo |
| 4a | Cribado semántico con LLM | ✅ Operativo |
| 5 | Alerta al usuario | ⬜ Siguiente |
| 3 | Descarga de documentos, si la solvencia remite al pliego | ⬜ Mejora |
| 4b | Extracción de requisitos del PDF | ⬜ Mejora |

El orden de ejecución **no** es 1→2→3→4→5. El cribado semántico se adelanta
a la descarga de documentos, y la descarga es condicional. Ver Decisiones 15
y 19 en `DECISIONES.md`:

```
1 → 2 → 2b → 4a (cribado) → 5 (alerta)
                      └── ¿la solvencia remite al pliego? ──sí──→ 3 → 4b
```

Todo lo marcado como operativo se ejecuta solo cada mañana.

---

## Arquitectura

```
  Feeds ATOM oficiales
  (PLACSP + plataformas agregadas)
            │
            ▼
  GitHub Actions  ── cron diario 06:00 UTC ──┐
            │                                │
            ▼                                │
  lector_atom.py                             │  100 % en la nube:
   ├─ descarga con reintentos                │  no requiere ninguna
   ├─ parseo XML (CODICE)                    │  instalación local
   ├─ extractor por fuente                   │
   ├─ filtro por prefijo CPV                 │
   └─ control de estado ─────────────────────┘
            │
            ▼
      Supabase (PostgreSQL)
      tabla `licitaciones`
            │
            ▼
      Pasos 3, 4 y 5  (pendientes)
```

Dos principios de diseño gobiernan el conjunto:

**La base de datos es la única fuente de verdad.** El repositorio contiene
código y nada más. No se generan ficheros de datos ni commits automáticos.
La tabla `licitaciones` es simultáneamente la memoria (qué se ha visto ya)
y la cola de trabajo (qué falta por procesar, vía `estado_pipeline`).

**Los fallos deben ser ruidosos.** Un scraper que devuelve cero resultados
en silencio es indistinguible de uno que funciona en un mercado tranquilo.
El script instrumenta lo que hace y avisa cuando algo se desvía de lo
esperado, en lugar de degradarse discretamente.

---

## Ficheros

| Fichero | Función |
|---|---|
| `lector_atom.py` | Todo el pipeline de los pasos 1 y 2 |
| `requirements.txt` | Dependencias de Python |
| `.github/workflows/scraper.yml` | Programación y configuración del robot |
| `DECISIONES.md` | Registro de decisiones de arquitectura y su motivo |
| `cribador.py` | Paso 4a: cribado semántico con LLM |
| `esquema.sql` | Esquema completo de la base de datos, reproducible |

---

## Fuentes de datos

Ambas son canales oficiales de sindicación del Ministerio de Hacienda, en
formato ATOM con el estándar **CODICE** (dialecto XML basado en UBL).

| Fuente | Canal | Volumen aproximado |
|---|---|---|
| Perfiles de contratante del Estado | sindicación 643 | ~2.400 entradas/día |
| Plataformas agregadas (CC. AA.) | sindicación 1044 | ~475 entradas/día |

La Plataforma de Serveis de Contractació Pública de Catalunya sindica de
forma bidireccional con PLACSP, por lo que las licitaciones catalanas
llegan a través del canal agregado. Ese canal transporta **todas** las
comunidades autónomas, así que el extractor catalán aplica un filtro de
procedencia.

---

## Operación

### Ejecución automática

Cada día a las 06:00 UTC (08:00 en horario de verano peninsular, 07:00 en
invierno). No requiere intervención.

### Ejecución manual

Pestaña **Actions** → *Scraper de licitaciones* → **Run workflow**. Dos
parámetros disponibles:

- **modo**
  - `normal`: escribe en Supabase.
  - `diagnostico`: lee los feeds, aplica los filtros e informa de cobertura
    de datos y reparto por CPV, **sin tocar la base de datos**. Es la forma
    de probar hipótesis sin consecuencias.
  - `solo_cribado`: clasifica sin volver a leer los feeds.
  - `cribado_prueba`: clasifica 20 y las imprime **sin guardar**.
- **cpv_prefijos**: prefijos solo para esa ejecución. Vacío usa los del
  workflow.

### Qué mirar en el registro

Actions → ejecución → job → desplegar *Ejecutar el scraper*.

| Buscar | Significa |
|---|---|
| `Ventana adaptativa` | Configuración con la que arrancó |
| `Ventana cubierta hasta` | Profundidad real de vigilancia por feed |
| `Entrada más antigua leída` | Hasta dónde miró, aunque descartara |
| `FRENO DE EMERGENCIA` | ⚠️ Posible hueco sin vigilar. Investigar |
| `Ejecución completada` | Total de licitaciones nuevas guardadas |
| `Refrescado el estado de` | Expedientes conocidos actualizados |
| `Guardados N de M veredictos` | ⚠️ Si N ≠ M, se perdió trabajo |

Los avisos en amarillo (`WARNING`) son los que importan. El aviso de
obsolescencia de Node.js que emite GitHub es ajeno al proyecto.

---

## Configuración

Todo se ajusta desde `.github/workflows/scraper.yml`, sin tocar Python.

| Variable | Por defecto | Función |
|---|---|---|
| `CPV_PREFIJOS` | `7995,923,925` | **Define el sector vigilado** |
| `DIAS_ANTIGUEDAD_MAX` | `14` | Tope de retroceso temporal |
| `DIAS_MARGEN_SOLAPE` | `2` | Solape sobre lo ya guardado |
| `MAX_PAGINAS_POR_FEED` | `25` | Freno de emergencia |
| `UMBRAL_CORTE_PAGINA` | `0.9` | Cuándo dejar de paginar (calibrable) |
| `SOLO_CATALUNYA_AGREGADO` | `true` | Filtro geográfico del canal agregado |
| `MODELO_CRIBADO` | `gpt-4o-mini` | Modelo del paso 4a |
| `MAX_CRIBADO_POR_EJECUCION` | `300` | Freno de gasto del cribado |
| `VEREDICTOS_INFORME` | `si,quizas,no` | Qué secciones lista el informe |

Credenciales, como *secrets* del repositorio: `SUPABASE_URL`,
`SUPABASE_KEY` y `OPENAI_API_KEY`.

Credenciales, como *secrets* del repositorio: `SUPABASE_URL` y
`SUPABASE_KEY` (clave secreta con privilegios de servidor).

### Ventana adaptativa

El script no vigila un número fijo de días. Consulta en Supabase cuál es la
publicación más reciente que ya tiene guardada de cada fuente y retrocede
solo hasta ahí, con el margen de solape. En operación normal son pocas
páginas; tras una caída, retrocede lo necesario para tapar el hueco por sí
solo, hasta el tope configurado.

---

## Esquema de datos

Tabla `public.licitaciones` en Supabase.

| Columna | Tipo | Notas |
|---|---|---|
| `id_licitacion` | text (PK) | Identificador ATOM o enlace |
| `fuente` | text | Canal del que procede |
| `origen` | text | `Estado` / `Catalunya` |
| `expediente` | text | Referencia del órgano |
| `titulo` | text | |
| `organo` | text | Órgano de contratación |
| `enlace` | text | URL pública del expediente |
| `codigo_postal` | text | Prioridad: lugar de ejecución > dirección del órgano |
| `presupuesto` | numeric | |
| `cpvs` | jsonb | Todos los CPV de la licitación |
| `estado_licitacion` | text | Código de estado CODICE |
| `fecha_publicacion` | timestamptz | Normalizada a ISO |
| `estado_pipeline` | text | `pendiente_analisis` → ... → `alertado` |
| `fecha_deteccion` | timestamptz | Cuándo lo vio el scraper |

Vista `licitaciones_pendientes`: lo que queda por procesar.

RLS activado sin políticas: nada es accesible con claves públicas.

---

## Métricas observadas

Medidas sobre 86 licitaciones reales (agosto de 2026, filtro
`7995,923,925`).

**Cobertura de campos:**

| Campo | Cobertura |
|---|---|
| Presupuesto | 100 % |
| Órgano | 100 % |
| Expediente | 100 % |
| Código postal | 84,9 % |

**Reparto del volumen por prefijo CPV:**

| Prefijo | Volumen |
|---|---|
| `923` (entretenimiento) | 51,2 % |
| `7995` (eventos y ferias) | 50,0 % |
| `925` (bibliotecas y museos) | 11,6 % |

La cobertura del código postal por encima del 80 % hace viable el filtrado
por provincia como funcionalidad de producto.

---

## El embudo

Medido sobre datos reales de agosto de 2026:

| Etapa | Filas | Filtro |
|---|---|---|
| Capturado de los feeds | 926 | prefijo CPV |
| Vivo | 124 | estado `PUB` y plazo abierto |
| **Relevante** | **44** | cribado semántico |

De 926 a 44. Cada etapa elimina una clase distinta de ruido, y ninguna
podría hacer el trabajo de las otras: el CPV no distingue un concierto de
una corrida de toros, el estado no distingue lo relevante de lo
irrelevante, y el modelo no sabría por sí solo si un expediente sigue
abierto.

Del volumen final, **una de cada tres licitaciones vivas del nicho resulta
relevante**. Eso es una alerta útil, no una lista.

---

## Límites conocidos

- **Los canales utilizados excluyen los contratos menores** (servicios por
  debajo de 15.000 €). Existe un canal específico para ellos, aún no
  integrado.
- **El CPV no discrimina por sí solo.** Ver `DECISIONES.md`. El filtrado
  fino corresponde al paso 4.
- **No hay recuperación de histórico.** La ventana adaptativa mantiene la
  continuidad hacia delante pero no rellena huecos anteriores. Requeriría
  procesar los ZIP mensuales que publica Hacienda.
- **La fecha límite de presentación no viene en el feed.** Está en el
  pliego, que es el objeto del paso 3.
- **El filtro de procedencia catalana es heurístico**, basado en dominios y
  nombres de órgano. Puede dejar escapar entidades con dominio propio.
- **El estado solo se refresca en lo que reaparece** en la ventana
  adaptativa. Las filas antiguas conservan el estado del día en que se
  vieron hasta que su expediente vuelva a moverse.
- **El prefijo CPV `925` arrastra destrucción documental**, porque incluye
  archivos. El cribado lo rechaza bien, pero desperdicia llamadas.
- **Algunos títulos catalanes son el encabezado del pliego**, no una
  descripción. El cribado clasifica esos casos con muy poca señal.

---

## Próximos pasos

1. Paso 3: descarga de pliegos.
2. Paso 4: análisis con LLM y extracción de plazos.
3. Paso 5: alerta.
4. Recuperación de histórico vía ZIP mensuales.

Deuda técnica conocida y aplazada conscientemente: ver el final de
`DECISIONES.md`.
