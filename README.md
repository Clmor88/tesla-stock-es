# Tesla Stock España

Monitor automático del inventario Tesla en España:

- Vehículos **nuevos y de demostración** disponibles para entrega inmediata.
- Vehículos **de ocasión** vendidos por Tesla.

Revisa Model 3, Model Y, Model S y Model X en cualquier punto de España. Envía
los avisos al mismo chat de Telegram cuando aparece una unidad nueva y, en el
caso de los vehículos de ocasión, también cuando deja de estar disponible.

[![Comprobar stock Tesla España](https://github.com/Clmor88/tesla-stock-es/actions/workflows/monitor.yml/badge.svg)](https://github.com/Clmor88/tesla-stock-es/actions/workflows/monitor.yml)

## Cómo funciona

1. GitHub Actions lanza una comprobación por hora en Linux y, si Tesla bloquea sus tres sesiones, activa un respaldo en macOS.
2. Un Chrome real obtiene las cookies que Tesla exige contra tráfico automatizado.
3. Ese mismo Chrome consulta la API oficial de inventario para España.
4. El Worker de Cloudflare compara los identificadores con la comprobación anterior.
5. Telegram recibe los avisos en el chat configurado.

La primera ejecución de cada inventario crea una línea base y envía un mensaje
de activación. Las unidades que ya estaban disponibles en ese momento no
generan avisos individuales.

En los vehículos de ocasión, una desaparición se confirma en dos comprobaciones
consecutivas antes de avisar. Esto reduce falsas retiradas si Tesla devuelve
temporalmente un resultado incompleto. Una retirada requiere dos comprobaciones
válidas, por lo que el tiempo exacto depende de cuándo Tesla permita cada consulta.

## Archivos

- `monitor.py`: consulta y normaliza ambos inventarios para los cuatro modelos.
- `cloudflare-worker.js`: relé autenticado, comparación de altas y bajas, estado en KV y avisos por Telegram.
- `.github/workflows/monitor.yml`: ejecución automática y manual.
- `requirements.txt`: dependencias de Python.

## Seguridad

El token del bot de Telegram permanece como secreto cifrado en Cloudflare.
GitHub solo guarda `MONITOR_API_KEY` como secreto cifrado de Actions. Ningún
token, chat ID ni clave privada está incluido en este repositorio público.

## Frecuencia

El workflow solicita una comprobación por hora, en el minuto 17. Cada ejecución
añade una espera aleatoria de 45 a 180 segundos y puede recrear hasta tres sesiones para
evitar un patrón rígido. Si Linux agota sus tres sesiones, macOS realiza hasta tres sesiones nuevas desde otra red. GitHub puede introducir retrasos en horas de alta demanda.

## Créditos

El método inicial de adquisición de cookies de Akamai con `nodriver` está adaptado de
[TeslaWebScrape](https://github.com/JumpBearCode/TeslaWebScrape), publicado bajo
licencia MIT.

Proyecto independiente, no afiliado ni respaldado por Tesla, Inc.
