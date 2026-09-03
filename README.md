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

**La interfaz está publicada y se actualiza sola cada mañana:**
https://vincent-vegga.github.io/state-scraper/

---

## Estado del proyecto

El sistema está concebido como un pipeline de cinco pasos:

| Paso | Descripción | Estado |
|---|---|---|
| 1 | Conexión a feeds ATOM oficiales | ✅ Operativo |
| 2 | Filtrado por CPV y control de estado | ✅ Operativo |
| 2b | Filtro por estado del expediente (solo `PUB`) | ✅ Operativo |
| 4a | Cribado semántico con LLM | ✅ Operativo |
| — | Interfaz web pública, actualizada a diario | ✅ Operativo |
| 5 | Alerta diaria por correo | ✅ Operativo |
| 3 | Descarga de documentos, si la solvencia remite al pliego | ⬜ Mejora |
| 4b | Extracción de requisitos del PDF | ⬜ Mejora |

**El pipeline está completo.** El orden de ejecución **no** es 1→2→3→4→5: el
cribado semántico se adelanta a la descarga de documentos, y la descarga es
condicional. Ver Decisiones 15 y 19 en `DECISIONES.md`:

```
1 → 2 → 2b → 4a (cribado) → web + correo
                      └── ¿la solvencia remite al pliego? ──sí──→ 3 → 4b
```

Todo se ejecuta solo cada mañana.

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
            ├──▶ cribador.py ──▶ veredicto de viabilidad
            │
            ├──▶ generar_interfaz.py ──▶ GitHub Pages
            │
            └──▶ alertador.py ──▶ correo diario
```

Publicar la web y enviar el correo son *jobs* separados que dependen del
scraper pero no entre sí. Si el correo falla, la web se publica igual: dos
resultados que pueden fallar por motivos distintos no deben compartir
destino.

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
| `generar_interfaz.py` | Construye la web a partir de la base de datos |
| `alertador.py` | Paso 5: alerta diaria por correo |
| `.gitignore` | Impide que los datos generados acaben en el repositorio |
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
  - `alerta_prueba`: compone el correo y lo imprime, **sin enviarlo**.
- **cpv_prefijos**: prefijos solo para esa ejecución. Vacío usa los del
  workflow.
- **dias_solape** y **max_paginas**: para una *pasada profunda* puntual,
  que relee semanas atrás y refresca estados y plazos. Valores típicos: 20
  y 120. Vacíos, se usan los de siempre — así no hay que acordarse de
  revertir la configuración.

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
| `Correo aceptado por Resend` | La alerta salió |
| `Sin novedades. No se envía` | Correcto: no había nada que contar |

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
| `HORAS_NOVEDAD` | `6` | Ventana que define qué es "nuevo" |
| `URL_INTERFAZ` | (la web) | Enlace incluido en el correo |

Credenciales, como *secrets* del repositorio: `SUPABASE_URL`,
`SUPABASE_KEY`, `OPENAI_API_KEY`, `RESEND_API_KEY` y
`DESTINATARIOS_ALERTA`.

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
| `estado_licitacion` | text | `PUB`, `EV`, `ADJ`, `RES`, `ANUL`, `PRE` |
| `estado_nombre` | text | Etiqueta oficial en español |
| `fecha_actualizacion` | timestamptz | **Interna.** Ordena la paginación |
| `fecha_publicacion` | timestamptz | Cuánto lleva en la calle |
| `fecha_limite` | timestamptz | Plazo de presentación |
| `estado_pipeline` | text | `pendiente_analisis` → ... → `alertado` |
| `fecha_deteccion` | timestamptz | Cuándo lo vio el scraper |
| `cribado_veredicto` | text | `si` / `quizas` / `no` |
| `cribado_motivo` | text | Una frase del modelo |
| `cribado_version`, `cribado_modelo` | text | Trazabilidad del prompt |

**Tres fechas distintas**, y confundirlas fue un error real. La de
actualización ordena la paginación y no se muestra; la de publicación mide
la ventaja que lleva la competencia; la límite dice si aún se puede
ofertar.

Todas son `timestamptz`. El script asume hora peninsular cuando CODICE
omite la zona, y la interfaz muestra siempre en `Europe/Madrid`: asumir UTC
desplazaba un vencimiento de las 23:59 al día siguiente.

**Vistas:**

| Vista | Para qué |
|---|---|
| `licitaciones_por_cribar` | Cola del paso 4a |
| `licitaciones_pendientes` | Todo lo vivo, cribado o no |
| `oportunidades` | Lo que ven la web y el correo |

RLS activado sin políticas: nada es accesible con claves públicas.

---

## Métricas observadas

Medidas sobre licitaciones reales de agosto y septiembre de 2026, filtro
`7995,923,925`.

**Cobertura de campos:**

| Campo | Cobertura |
|---|---|
| Presupuesto | 100 % |
| Órgano | 100 % |
| Expediente | 100 % |
| Fecha límite de presentación | 95,0 % |
| Email de contacto del órgano | 92,6 % |
| Documentos referenciados | 90,5 % |
| Código postal | 84,9 % |
| Criterios de adjudicación | 86,5 % |
| Fecha real de publicación | 26,8 % |

**Reparto del volumen por prefijo CPV:**

| Prefijo | Volumen |
|---|---|
| `923` (entretenimiento) | 51,2 % |
| `7995` (eventos y ferias) | 50,0 % |
| `925` (bibliotecas y museos) | 11,6 % |

**Calidad del cribado.** Evaluado contra 20 casos clasificados a mano y
auditando 30 rechazos al azar: **cero falsos negativos**, que es el único
error que hace daño. Ver Decisión 22.

Tres consecuencias de producto: el código postal por encima del 80 % hace
viable el filtrado por provincia; con plazo, criterios de adjudicación
ponderados y solvencia, el feed no da para una alerta sino para un informe;
y el email del órgano convierte la alerta en acción inmediata.

---

## La interfaz

`generar_interfaz.py` lee la vista `oportunidades` y escribe un **único
fichero HTML con los datos incrustados dentro**. Sin servidor, sin
credenciales en el navegador y sin depender de la red al abrirlo.

Se publica en GitHub Pages **desde un artefacto de la ejecución**, no desde
una rama. Es la diferencia que permite que el repositorio siga sin contener
datos: el HTML se sirve sin llegar a guardarse en ningún commit.

El despliegue va en un job aparte del workflow, con permisos propios de
escritura sobre Pages. Así el job que descarga XML de terceros conserva
únicamente permisos de lectura.

La provincia se calcula al vuelo desde los dos primeros dígitos del código
postal. Es un cálculo, no un dato: guardarlo sería duplicar información que
ya está en la tabla.

**Dos pestañas.** *Contratos de hoy* enseña lo detectado en la última pasada
del robot; *Todos los abiertos*, el resto. Los filtros se conservan al
cambiar de pestaña, pero los contadores de cada una ignoran los filtros: si
mostraran el resultado filtrado, cambiar de pestaña sería un salto a ciegas.

**El filtro geográfico** es un campo con etiquetas y desplegable buscable.
Acepta comunidades autónomas además de provincias —elegir Andalucía añade
sus ocho de golpe— y busca sin tildes. Escribir "Sevilla" ofrece también
"Andalucía", que es lo que hace descubrible la función.

**Los títulos se recortan.** Algunos órganos ponen como título el encabezado
entero del pliego; el preámbulo se elimina y el original se muestra completo
al desplegar.

**El repositorio es público.** Los datos son de contratación pública y ya
estaban publicados; las credenciales viven en los *secrets* de GitHub, que
siguen siendo privados. Hacerlo público es además requisito de GitHub Pages
en el plan gratuito.

---

## La alerta diaria

`alertador.py` envía por correo lo que el robot ha detectado en su última
pasada, a través de Resend.

**Solo se envía si hay novedades.** Un correo que a veces dice "hoy no hay
nada" enseña a ignorar al remitente, y con él los días en que sí importa.

**Novedad es lo de la última pasada**, no lo de hoy según el calendario. Si
el robot se cae un día, al volver detecta lo acumulado y todo eso se envía:
una avería no hace perder ninguna oportunidad. Es la misma definición que
usa la pestaña de la web, para que nadie reciba por correo algo que ya vio
marcado como nuevo.

Se manda en HTML y en texto plano a la vez, y cada oportunidad lleva plazo
con hora exacta. Si el envío falla, la ejecución termina en rojo: un envío
fallido que acabara en verde sería indistinguible de un día sin novedades.

---

## El embudo

Medido sobre datos reales de agosto de 2026:

| Etapa | Filas | Filtro |
|---|---|---|
| Capturado de los feeds | ~2.000 | prefijo CPV |
| Vivo | 124 | estado `PUB` |
| Relevante | 53 | cribado semántico |
| **Mostrado** | **34** | regla de vigencia |

Cada etapa elimina una clase distinta de ruido, y ninguna podría hacer el
trabajo de las otras: el CPV no distingue un concierto de una corrida de
toros, el estado no distingue lo relevante de lo irrelevante, el modelo no
sabría por sí solo si un expediente sigue abierto, y la regla de vigencia
descarta lo que no se puede respaldar con evidencia.

Del volumen vivo, **una de cada tres licitaciones del nicho resulta
relevante**. Eso es una alerta útil, no una lista.

---

## Límites conocidos

- **Los canales utilizados excluyen los contratos menores** (servicios por
  debajo de 15.000 €). Existe un canal específico para ellos, aún no
  integrado.
- **El CPV no discrimina por sí solo.** Ver Decisión 11. El filtrado fino
  corresponde al cribado semántico.
- **El estado solo se refresca en lo que reaparece** en la ventana
  adaptativa. Un expediente que se adjudique y no vuelva al feed conservaría
  su plazo futuro y seguiría mostrándose. Con ciclos de quince días, que es
  lo normal en el sector, el refresco lo alcanza; es una probabilidad baja,
  no una imposibilidad.
- **No hay recuperación de histórico.** La ventana adaptativa mantiene la
  continuidad hacia delante pero no rellena huecos anteriores. Requeriría
  procesar los ZIP mensuales que publica Hacienda.
- **La fecha real de publicación llega en el 26,8 % de los casos.** El feed
  transporta `IssueDate` de forma irregular; obtenerla siempre exigiría
  descargar el documento del expediente.
- **La solvencia remite al pliego en el 23 % de los campos.** Ese contenido
  solo existe en PDF, y es el que responde a "¿puedo presentarme?".
- **El filtro de procedencia catalana es heurístico**, basado en dominios y
  nombres de órgano. Puede dejar escapar entidades con dominio propio.
- **El prefijo CPV `925` arrastra destrucción documental**, porque incluye
  archivos. El cribado lo rechaza bien, pero desperdicia llamadas.
- **El cron de GitHub no garantiza puntualidad.** Ver Decisión 14. La
  ventana adaptativa absorbe las ejecuciones perdidas.
- **Un solo destinatario y un solo filtro.** No hay usuarios: el sector y las
  provincias son configuración global, no preferencias de cada persona.

---

## Hoja de ruta

El pipeline está terminado. Lo que sigue convierte una herramienta en un
producto, y está estimado pero no construido: hacerlo antes de validar con
usuarios reales sería apostar sobre supuestos.

**Sin esto no hay producto**

| | Horas |
|---|---|
| Usuarios y suscripciones: cada uno elige sector y provincias | 40-60 |
| Criterios de relevancia configurables, no escritos en el prompt | 8-12 |
| Envío de correo con reputación, rebotes y baja | 6-10 |

**Sin esto el producto miente**

| | Horas |
|---|---|
| Verificar un expediente concreto sin esperar al feed | 3-4 |
| Histórico de cambios de estado, en vez de sobrescribir | 6-10 |
| Recuperación por ZIP mensuales al ampliar criterios | 4-6 |
| Integrar el canal de contratos menores | 4-6 |

**Lo que decide si es negocio**

| | Horas |
|---|---|
| Extracción de solvencia desde el pliego (pasos 3 y 4b) | 8-12 |
| Métrica de calidad continua del cribado | 4-6 |
| Cobro | 15-25 |

**Total: 100-150 horas.**

El orden real depende de una respuesta que aún no se tiene: si el problema
del usuario es la relevancia, la solvencia o el plazo. La extracción de
solvencia podría ser lo más valioso de la lista —responde a "¿puedo
presentarme?" en lugar de "¿me interesa?"— pero es una sospecha, no un dato.

Deuda técnica conocida y aplazada conscientemente: ver el final de
`DECISIONES.md`.
