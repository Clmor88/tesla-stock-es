"""Monitor del inventario Tesla nuevo de España.

Obtiene cookies anti-bot con Chrome/nodriver, consulta la API oficial con la
huella TLS de Chrome y envía el inventario normalizado al relé de Cloudflare.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import nodriver as uc
from curl_cffi import requests as curl_requests

API_URL = "https://www.tesla.com/inventory/api/v4/inventory-results"
INVENTORY_URL = (
    "https://www.tesla.com/es_es/inventory/new/my"
    "?arrangeby=plh&zip=28001&range=0"
)
RELAY_URL = os.getenv(
    "MONITOR_RELAY_URL",
    "https://tesla-stock-es.clementmoreno.workers.dev/report",
)
MODELS = {
    "m3": "Model 3",
    "my": "Model Y",
    "ms": "Model S",
    "mx": "Model X",
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


async def acquire_cookies() -> dict[str, str]:
    """Abre un Chrome real y devuelve las cookies creadas por Akamai."""
    log("Abriendo Chrome para obtener las cookies de Tesla…")
    browser = await uc.start(
        headless=False,
        no_sandbox=True,
        lang="es-ES",
        browser_args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--window-size=1440,1000",
        ],
    )
    try:
        page = await browser.get("https://www.tesla.com/es_es")
        await asyncio.sleep(6)
        page = await browser.get(INVENTORY_URL)
        await asyncio.sleep(12)

        title = str(await page.evaluate("document.title"))
        if "Access Denied" in title:
            raise RuntimeError("Tesla ha bloqueado la sesión del navegador")

        cdp_cookies = await page.send(uc.cdp.network.get_cookies())
        cookies = {
            cookie.name: cookie.value
            for cookie in cdp_cookies
            if "tesla.com" in (cookie.domain or "")
        }
        if "_abck" not in cookies:
            raise RuntimeError("Tesla no ha entregado la cookie _abck")
        log(f"Cookies obtenidas correctamente ({len(cookies)} en total).")
        return cookies
    finally:
        browser.stop()


def build_query(model: str, offset: int) -> dict[str, Any]:
    return {
        "query": {
            "model": model,
            "condition": "new",
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            "market": "ES",
            "language": "es",
            "super_region": "north america",
            "lng": -3.7038,
            "lat": 40.4168,
            "zip": "28001",
            "range": 0,
            "region": "ES",
        },
        "offset": offset,
        "count": 50,
        "outsideOffset": 0,
        "outsideSearch": False,
    }


def fetch_model(model: str, cookies: dict[str, str]) -> list[dict[str, Any]]:
    """Descarga todas las páginas disponibles de un modelo."""
    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    results: list[dict[str, Any]] = []
    offset = 0
    total = 1

    while offset < total and offset < 500:
        response = curl_requests.get(
            API_URL,
            params={"query": json.dumps(build_query(model, offset), separators=(",", ":"))},
            impersonate="chrome",
            timeout=30,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "es-ES,es;q=0.9",
                "Cookie": cookie_header,
                "Referer": INVENTORY_URL,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            },
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Tesla devolvió HTTP {response.status_code} para {model}"
            )

        data = response.json()
        batch = data.get("results", [])
        if not isinstance(batch, list):
            batch = []

        try:
            total = int(data.get("total_matches_found", len(batch)) or 0)
        except (TypeError, ValueError):
            total = len(batch)

        results.extend(batch)
        log(f"{MODELS[model]}: {len(results)}/{total} vehículos.")
        if not batch:
            break
        offset += len(batch)

    return results


def first_value(vehicle: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = vehicle.get(name)
        if value not in (None, ""):
            return value
    return default


def normalize_vehicle(model: str, vehicle: dict[str, Any]) -> dict[str, Any]:
    vin = str(first_value(vehicle, "VIN", "Vin", "vin"))
    identifier = vin or str(
        first_value(
            vehicle,
            "InventoryLotCode",
            "ReferenceNumber",
            "VehicleConfigId",
            default="",
        )
    )
    if not identifier:
        raise ValueError("Vehículo sin identificador estable")

    price = first_value(
        vehicle,
        "TotalPrice",
        "PurchasePrice",
        "Price",
        "InventoryPrice",
        default=0,
    )
    city = str(first_value(vehicle, "City", "MetroName", default="")).strip()
    province = str(
        first_value(vehicle, "StateProvince", "Province", "Region", default="")
    ).strip()
    location = ", ".join(part for part in (city, province) if part)

    return {
        "id": identifier,
        "vin": vin,
        "model": MODELS[model],
        "trim": str(first_value(vehicle, "TrimName", "Trim", "Title", default="")),
        "year": first_value(vehicle, "Year", default=""),
        "price": price,
        "currency": "EUR",
        "location": location or "España",
        "demo": bool(vehicle.get("IsDemo", False)),
        "url": (
            f"https://www.tesla.com/es_ES/{model}/order/{vin}"
            "?titleStatus=new&redirect=no"
            if vin
            else f"https://www.tesla.com/es_ES/inventory/new/{model}"
        ),
    }


def send_report(cars: list[dict[str, Any]]) -> None:
    secret = os.environ.get("MONITOR_API_KEY")
    if not secret:
        raise RuntimeError("Falta el secreto MONITOR_API_KEY")

    response = curl_requests.post(
        RELAY_URL,
        json={
            "cars": cars,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"El relé devolvió HTTP {response.status_code}")
    log(f"Relé actualizado: {response.text[:300]}")


async def main() -> None:
    cookies = await acquire_cookies()
    cars: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in MODELS:
        raw_results = fetch_model(model, cookies)
        for raw_vehicle in raw_results:
            try:
                car = normalize_vehicle(model, raw_vehicle)
            except ValueError as exc:
                log(f"Ignorado: {exc}")
                continue
            if car["id"] not in seen:
                seen.add(car["id"])
                cars.append(car)

    cars.sort(key=lambda car: (str(car["model"]), float(car["price"] or 0)))
    log(f"Inventario español total: {len(cars)} vehículos.")
    send_report(cars)


if __name__ == "__main__":
    asyncio.run(main())
