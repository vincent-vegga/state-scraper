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
| 4a | Cribado semántico sobre título, órgano y CPV | ⬜ Siguiente |
| 5 | Alerta al usuario | ⬜ Pendiente |
| 3 | Descarga de documentos, **solo si la solvencia remite al pliego** | ⬜ Mejora |
| 4b | Extracción de requisitos del pliego | ⬜ Mejora |

El orden de ejecución **no** es 1→2→3→4→5. El cribado semántico se adelanta
a la descarga, y la descarga es condicional. Ver Decisiones 15 y 19 en
`DECISIONES.md`:

```
1 → 2 → 4a (cribado) → 5 (alerta completa)
                 └── ¿la solvencia remite al pliego? ──sí──→ 3 → 4b
```

Los pasos 1 y 2 se ejecutan solos cada mañana y escriben en base de datos.

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
| Fecha límite de presentación | 95,0 % |
| Email de contacto del órgano | 92,6 % |
| Con al menos un documento referenciado | 90,5 % |
| Con criterios de adjudicación | 86,5 % |
| Con requisitos de solvencia | 83,7 % |
| Garantía definitiva | 40,1 % |

**Criterios de adjudicación:** 4,5 de media por licitación, el 100 % con
ponderación numérica, y en el 85 % de los casos las ponderaciones suman
100. **Requisitos de solvencia:** el 76,8 % de los campos trae contenido
real; solo el 23,2 % remite al pliego en PDF.

Medido sobre 317 licitaciones adicionales (ventana de 3 días):
**3,6 documentos de media** por licitación, con pliego administrativo y
pliego técnico presentes en prácticamente todas las que traen documentos.

**Reparto del volumen por prefijo CPV:**

| Prefijo | Volumen |
|---|---|
| `923` (entretenimiento) | 51,2 % |
| `7995` (eventos y ferias) | 50,0 % |
| `925` (bibliotecas y museos) | 11,6 % |

Tres consecuencias de producto:

1. La cobertura del código postal por encima del 80 % hace viable el
   filtrado por provincia.
2. Con plazo, criterios de adjudicación ponderados y solvencia, **el feed
   no da para una alerta: da para un informe**, sin descargar nada.
3. El email del órgano en el 92,6 % de los casos convierte la alerta en
   acción inmediata: no solo "existe esta oportunidad", sino "y este es el
   correo de quien la convoca".

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
- **El filtro de procedencia catalana es heurístico**, basado en dominios y
  nombres de órgano. Puede dejar escapar entidades con dominio propio.
- **Los pliegos son PDF.** El XML de la plataforma es el anuncio
  estructurado, no el pliego: lo referencia y da su huella, pero no lo
  contiene. El paso 4b necesitará extracción de PDF.
- **La solvencia remite al pliego en el 23 % de los campos.** Ese es el
  contenido que solo existe en PDF.

---

## Próximos pasos

1. Paso 3: descarga de pliegos.
2. Paso 4: análisis con LLM y extracción de plazos.
3. Paso 5: alerta.
4. Recuperación de histórico vía ZIP mensuales.

Deuda técnica conocida y aplazada conscientemente: ver el final de
`DECISIONES.md`.
