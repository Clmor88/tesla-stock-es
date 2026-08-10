"""Monitor del inventario Tesla nuevo y de ocasión de España.

Usa Chrome/nodriver para consultar la API oficial dentro de una sesión validada
y envía ambos catálogos normalizados al relé de Cloudflare.
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
CONDITIONS = {
    "new": "Nuevo",
    "used": "Ocasión",
}
MAX_BROWSER_ATTEMPTS = max(
    1, min(3, int(os.getenv("MAX_BROWSER_ATTEMPTS", "3")))
)
BROWSER_RETRY_DELAYS = (8, 18)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def chrome_executable() -> str:
    """Localiza Chrome en los runners Linux, macOS y Windows."""
    candidates = [
        os.getenv("CHROME_BIN", ""),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
        ),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            log(f"Chrome localizado en {candidate}")
            return candidate
    raise RuntimeError("No se encontró Google Chrome en el runner")


async def fetch_inventory_in_browser() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Consulta Tesla, recreando toda la sesión si aparece un bloqueo temporal."""
    errors: list[str] = []

    for attempt in range(1, MAX_BROWSER_ATTEMPTS + 1):
        log(f"Intento de navegador {attempt}/{MAX_BROWSER_ATTEMPTS}.")
        try:
            return await fetch_inventory_session(attempt)
        except Exception as exc:
            errors.append(str(exc))
            if attempt >= MAX_BROWSER_ATTEMPTS:
                detail = " | ".join(errors)
                raise RuntimeError(
                    f"Tesla no permitió completar la consulta tras "
                    f"{MAX_BROWSER_ATTEMPTS} sesiones: {detail}"
                ) from exc

            delay = BROWSER_RETRY_DELAYS[attempt - 1]
            log(
                f"Sesión {attempt} descartada: {exc}. "
                f"Nuevo perfil limpio en {delay} segundos…"
            )
            await asyncio.sleep(delay)

    raise RuntimeError("No se pudo iniciar ninguna sesión de Tesla")


async def fetch_inventory_session(
    attempt: int,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Arranca un Chrome con perfil efímero y consulta ambos inventarios."""
    log("Abriendo Chrome con un perfil limpio para obtener cookies de Tesla…")
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    profile = tempfile.TemporaryDirectory(prefix="tesla-chrome-")
    port = free_port()
    window_size = "1440,1000" if attempt % 2 else "1365,900"
    chrome_args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=Translate,MediaRouter",
        "--lang=es-ES",
        "--no-sandbox",
        "--remote-allow-origins=*",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile.name}",
        f"--window-size={window_size}",
        "about:blank",
    ]
    if sys.platform in ("darwin", "win32"):
        chrome_args.insert(0, "--headless=new")

    chrome_process = await asyncio.create_subprocess_exec(
        chrome_executable(),
        *chrome_args,
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
        await asyncio.sleep(5 + attempt * 2)
        page = await browser.get(INVENTORY_URL)
        await asyncio.sleep(10 + attempt * 2)

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

        inventory: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for condition in CONDITIONS:
            inventory[condition] = {}
            for model in MODELS:
                inventory[condition][model] = await fetch_model_in_page(
                    page, model, condition
                )
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


def build_query(model: str, condition: str, offset: int) -> dict[str, Any]:
    return {
        "query": {
            "model": model,
            "condition": condition,
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


async def fetch_model_in_page(
    page: Any, model: str, condition: str
) -> list[dict[str, Any]]:
    """Consulta un modelo mediante fetch dentro del Chrome validado por Tesla."""
    results: list[dict[str, Any]] = []
    offset = 0
    total = 1

    while offset < total and offset < 500:
        query = json.dumps(
            build_query(model, condition, offset), separators=(",", ":")
        )
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
        for api_attempt in range(3):
            raw_payload = await page.evaluate(
                script, await_promise=True, return_by_value=True
            )
            payload = json.loads(str(raw_payload))
            status = int(payload.get("status", 0))
            if status == 200:
                break
            if api_attempt < 2 and status in (403, 412):
                wait_seconds = 6 + api_attempt * 4
                log(
                    f"{CONDITIONS[condition]} {MODELS[model]} bloqueado "
                    f"temporalmente; reintento API {api_attempt + 2}/3 "
                    f"en {wait_seconds} segundos…"
                )
                await page.reload()
                await asyncio.sleep(wait_seconds)
                continue
            raise RuntimeError(
                f"Tesla devolvió HTTP {status} para {condition}/{model}"
            )

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
        log(
            f"{CONDITIONS[condition]} {MODELS[model]}: "
            f"{len(results)}/{total} vehículos."
        )
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


def normalize_vehicle(
    model: str, condition: str, vehicle: dict[str, Any]
) -> dict[str, Any]:
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

    mileage = first_value(
        vehicle,
        "Odometer",
        "OdometerKM",
        "OdometerKm",
        "Mileage",
        "MileageKM",
        "MileageKm",
        default="",
    )

    return {
        "id": identifier,
        "condition": condition,
        "vin": vin,
        "model": MODELS[model],
        "trim": str(first_value(vehicle, "TrimName", "Trim", "Title", default="")),
        "year": first_value(vehicle, "Year", default=""),
        "price": price,
        "currency": "EUR",
        "location": location or "España",
        "mileage": mileage,
        "demo": bool(vehicle.get("IsDemo", False)),
        "url": (
            f"https://www.tesla.com/es_ES/{model}/order/{vin}"
            f"?titleStatus={condition}&redirect=no"
            if vin
            else f"https://www.tesla.com/es_ES/inventory/{condition}/{model}"
        ),
    }


def send_report(
    new_cars: list[dict[str, Any]], used_cars: list[dict[str, Any]]
) -> None:
    secret = os.environ.get("MONITOR_API_KEY")
    if not secret:
        raise RuntimeError("Falta el secreto MONITOR_API_KEY")

    response = curl_requests.post(
        RELAY_URL,
        json={
            "cars": new_cars,
            "used_cars": used_cars,
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
    inventory = await fetch_inventory_in_browser()
    cars_by_condition: dict[str, list[dict[str, Any]]] = {
        condition: [] for condition in CONDITIONS
    }
    seen_by_condition: dict[str, set[str]] = {
        condition: set() for condition in CONDITIONS
    }

    for condition in CONDITIONS:
        for model in MODELS:
            for raw_vehicle in inventory[condition][model]:
                try:
                    car = normalize_vehicle(model, condition, raw_vehicle)
                except ValueError as exc:
                    log(f"Ignorado: {exc}")
                    continue
                if car["id"] not in seen_by_condition[condition]:
                    seen_by_condition[condition].add(car["id"])
                    cars_by_condition[condition].append(car)

    for cars in cars_by_condition.values():
        cars.sort(key=lambda car: (str(car["model"]), float(car["price"] or 0)))

    new_cars = cars_by_condition["new"]
    used_cars = cars_by_condition["used"]
    log(
        f"Inventario español total: {len(new_cars)} nuevos y "
        f"{len(used_cars)} de ocasión."
    )
    send_report(new_cars, used_cars)


if __name__ == "__main__":
    asyncio.run(main())
