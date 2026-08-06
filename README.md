# Tesla Stock España

Monitor automático de vehículos Tesla **nuevos y de demostración** disponibles
para entrega inmediata en cualquier punto de España. Revisa Model 3, Model Y,
Model S y Model X y avisa por Telegram cuando aparece una unidad nueva.

[![Comprobar stock Tesla España](https://github.com/Clmor88/tesla-stock-es/actions/workflows/monitor.yml/badge.svg)](https://github.com/Clmor88/tesla-stock-es/actions/workflows/monitor.yml)

## Cómo funciona

1. GitHub Actions ejecuta el monitor cada cinco minutos.
2. Un Chrome real obtiene las cookies que Tesla exige contra tráfico automatizado.
3. Ese mismo Chrome consulta la API oficial de inventario para España.
4. El Worker de Cloudflare compara los identificadores con la comprobación anterior.
5. Telegram recibe un mensaje únicamente cuando aparece un vehículo nuevo.

La primera ejecución crea una línea base y envía un mensaje de activación. Las
unidades que ya estaban disponibles en ese momento no generan avisos
individuales.

## Archivos

- `monitor.py`: consulta y normaliza el inventario de los cuatro modelos.
- `cloudflare-worker.js`: relé autenticado, estado en KV y avisos por Telegram.
- `.github/workflows/monitor.yml`: ejecución automática y manual.
- `requirements.txt`: dependencias de Python.

## Seguridad

El token del bot de Telegram permanece como secreto cifrado en Cloudflare.
GitHub solo guarda `MONITOR_API_KEY` como secreto cifrado de Actions. Ningún
token, chat ID ni clave privada está incluido en este repositorio público.

## Frecuencia

El workflow solicita una comprobación cada cinco minutos. GitHub puede introducir
un pequeño retraso en horas de alta demanda.

## Créditos

El método inicial de adquisición de cookies de Akamai con `nodriver` está adaptado de
[TeslaWebScrape](https://github.com/JumpBearCode/TeslaWebScrape), publicado bajo
licencia MIT.

Proyecto independiente, no afiliado ni respaldado por Tesla, Inc.
