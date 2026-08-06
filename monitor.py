"""Monitor del inventario Tesla nuevo de España.

Obtiene cookies anti-bot con Chrome/nodriver, consulta la API oficial con la
huella TLS de Chrome y envía el inventario normalizado al relé de Cloudflare.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import nodriver as uc
from nodriver.core.util import free_port
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


async def fetch_inventory_in_browser() -> dict[str, list[dict[str, Any]]]:
    """Arranca Chrome y consulta todo el inventario dentro del navegador."""
    log("Abriendo Chrome para obtener las cookies de Tesla…")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    profile = tempfile.TemporaryDirectory(prefix="tesla-chrome-")
    port = free_port()
    chrome_process = await asyncio.create_subprocess_exec(
        "/usr/bin/google-chrome",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--remote-allow-origins=*",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile.name}",
        "--window-size=1440,1000",
        "about:blank",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    browser = None

    def read_debug_version() -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{port}/json/version", timeout=2
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Puerto de Chrome devolvió HTTP {response.status}")

    try:
        last_error: Exception | None = None
        for _ in range(40):
            if chrome_process.returncode is not None:
                stderr = (await chrome_process.stderr.read()).decode(
                    "utf-8", errors="replace"
                )
                raise RuntimeError(
                    f"Chrome terminó antes de abrir su puerto: {stderr[-1500:]}"
                )
            try:
                await asyncio.to_thread(read_debug_version)
                break
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError(
                f"Chrome no abrió el puerto de depuración: {last_error}"
            )

        log("Chrome está listo; conectando el monitor…")
        browser = await uc.start(host="127.0.0.1", port=port)
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
        inventory: dict[str, list[dict[str, Any]]] = {}
        for model in MODELS:
            inventory[model] = await fetch_model_in_page(page, model)
            await asyncio.sleep(2)
        return inventory
    finally:
        if browser is not None:
            try:
                await browser.aclose()
            except Exception:
                pass
        if chrome_process.returncode is None:
            chrome_process.terminate()
            try:
                await asyncio.wait_for(chrome_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                chrome_process.kill()
                await chrome_process.wait()
        profile.cleanup()


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



async def fetch_model_in_page(page: Any, model: str) -> list[dict[str, Any]]:
    """Consulta un modelo mediante fetch dentro del Chrome validado por Tesla."""
    results: list[dict[str, Any]] = []
    offset = 0
    total = 1

    while offset < total and offset < 500:
        query = json.dumps(build_query(model, offset), separators=(",", ":"))
        request_url = f"{API_URL}?query={urllib.parse.quote(query, safe='')}"
        script = f"""
            (async () => {{
                const response = await fetch({json.dumps(request_url)}, {{
                    method: "GET",
                    credentials: "include",
                    headers: {{
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "es-ES,es;q=0.9"
                    }}
                }});
                const body = await response.text();
                return JSON.stringify({{status: response.status, body}});
            }})()
        """

        payload: dict[str, Any] | None = None
        for attempt in range(2):
            raw_payload = await page.evaluate(
                script, await_promise=True, return_by_value=True
            )
            payload = json.loads(str(raw_payload))
            status = int(payload.get("status", 0))
            if status == 200:
                break
            if attempt == 0 and status in (403, 412):
                log(f"{MODELS[model]} bloqueado temporalmente; recargando…")
                await page.reload()
                await asyncio.sleep(6)
                continue
            raise RuntimeError(f"Tesla devolvió HTTP {status} para {model}")

        if payload is None:
            raise RuntimeError(f"Tesla no devolvió respuesta para {model}")

        data = json.loads(str(payload.get("body", "{}")))
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
        await asyncio.sleep(2)

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
    inventory_by_model = await fetch_inventory_in_browser()
    cars: list[dict[str, Any]] = []
    seen: set[str] = set()

    for model in MODELS:
        raw_results = inventory_by_model[model]
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
