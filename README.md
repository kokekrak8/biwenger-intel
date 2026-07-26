# Biwenger Intel

App para el móvil con el **dinero estimado** y la **puja máxima** de cada rival de
tu liga Biwenger, sus **ajustes** y los **jugadores** con dueño. Se instala en la
pantalla de inicio como una app.

## Cómo meter tu liga (con el capturador de 2 pasos)

Biwenger no deja que otra web lea tus datos, así que se capturan con dos pequeños
scripts que se ejecutan **dentro** de Biwenger (en la consola de DevTools, o como
marcadores). Graban lo que la propia web de Biwenger carga, que siempre es correcto.

1. Abre **Biwenger** en el navegador, entra en tu liga (sesión iniciada) y abre
   **DevTools → Console** (tecla F12).
2. Pega el contenido de **`capturador-PASO1-escuchar.txt`** y Enter → "Capturador ACTIVADO".
3. **Sin cerrar**, navega para que Biwenger cargue los datos ricos:
   - abre **Clasificación** (carga el *valor de equipo* de todos),
   - abre el **Tablón/Actividad** (carga los movimientos de dinero: cláusulas, abonos…),
   - entra en **2–3 equipos de rivales** (carga sus plantillas),
   - pasa por **Mercado**.
4. Pega el contenido de **`capturador-PASO2-descargar.txt`** y Enter → se descarga
   **`biwenger-todo.json`**.
5. Abre la app → **Tú → Actualizar / reimportar mi liga**. Pon el **presupuesto
   inicial** (40.000.000), deja el modo **"Plantilla aleatoria + 40M − Valor de
   Equipo"**, pulsa **Elegir archivo** y selecciona `biwenger-todo.json`.

Verás la clasificación por dinero, la puja máxima de cada uno y sus jugadores.

## Cómo se calcula

Tu liga usa el modo *"Plantilla aleatoria + 40 Millones menos el Valor de Equipo"*,
así que:

    dinero ≈ 40.000.000 − Valor de Equipo
    puja máxima = dinero + Valor de Equipo / 4     (fórmula de Biwenger)

A esto se le suman los movimientos del Tablón que son dinero puro y no se ven en el valor de equipo (cláusulas, abonos de jornada). Las compras/ventas ya van dentro del valor de equipo. **Tu** saldo se lee exacto de Biwenger; el de los rivales es estimación (Biwenger lo oculta). Al principio de temporada es muy fiel. Según avance puede desviarse algo por las
subidas/bajadas de precio de los jugadores y por los premios de jornada (eso no se
puede saber sin el histórico de precios día a día).

## Instalar en el móvil

Abre la app en el navegador del teléfono y añádela a la pantalla de inicio (iPhone:
Compartir → *Añadir a pantalla de inicio*; Android: menú → *Instalar aplicación*).
Para llevar tus datos al móvil: en el ordenador, tras importar, pulsa **Descargar
data.json**, sube la carpeta (con ese data.json) a un hosting estático
(p. ej. app.netlify.com/drop) y abre esa URL en el móvil.

## Actualizar

Repite los 2 pasos del capturador cuando quieras datos frescos y vuelve a cargar el
archivo. (Un móvil sin cuentas no puede actualizarse solo a diario porque Biwenger
bloquea el acceso externo.)

## Archivos

    index.html                     La app
    capturador-PASO1-escuchar.txt  Paso 1 del capturador
    capturador-PASO2-descargar.txt Paso 2 del capturador
    manifest.webmanifest, sw.js    Instalación + offline
    icon-*.png                     Iconos
    biwenger_agent.py, ...         Recolector Python opcional (modo automático)
