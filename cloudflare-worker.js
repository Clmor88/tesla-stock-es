const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

const NEW_STATE_KEY = "github-monitor:cars:v1";
const NEW_INITIALIZED_KEY = "github-monitor:initialized:v1";
const USED_STATE_KEY = "github-monitor:used-cars:v2";
const USED_INITIALIZED_KEY = "github-monitor:used-initialized:v2";

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: JSON_HEADERS,
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatPrice(value) {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount <= 0) {
    return "Precio no indicado";
  }
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(amount);
}

function formatMileage(value) {
  const amount = Number(String(value ?? "").replace(/\D/g, ""));
  if (!Number.isFinite(amount) || amount <= 0) {
    return "";
  }
  return new Intl.NumberFormat("es-ES", {
    maximumFractionDigits: 0,
  }).format(amount) + " km";
}

function fallbackInventoryUrl(condition) {
  return "https://www.tesla.com/es_ES/inventory/" + condition + "/m3";
}

function normalizeCars(value, condition) {
  if (!Array.isArray(value)) {
    throw new Error(condition + " cars debe ser una lista");
  }

  const seen = new Set();
  const cars = [];
  for (const raw of value.slice(0, 500)) {
    const id = String(raw?.id ?? "").trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    cars.push({
      id: id.slice(0, 200),
      condition,
      model: String(raw?.model ?? "Tesla").slice(0, 100),
      trim: String(raw?.trim ?? "").slice(0, 180),
      year: String(raw?.year ?? "").slice(0, 10),
      price: raw?.price ?? 0,
      location: String(raw?.location ?? "España").slice(0, 180),
      mileage: raw?.mileage ?? "",
      demo: Boolean(raw?.demo),
      url: String(raw?.url ?? fallbackInventoryUrl(condition)).slice(0, 500),
    });
  }
  return cars;
}

async function sendTelegram(env, text) {
  const chatId = await env.INVENTORY_STATE.get("chat");
  if (!chatId) {
    throw new Error("No hay chat de Telegram registrado");
  }

  const response = await fetch(
    "https://api.telegram.org/bot" + env.TELEGRAM_BOT_TOKEN + "/sendMessage",
    {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: false,
      }),
    },
  );

  if (!response.ok) {
    throw new Error("Telegram devolvió HTTP " + response.status);
  }
}

function describeCar(car, removed = false) {
  const title = [car.model, car.trim].filter(Boolean).join(" — ");
  const year = car.year ? " (" + escapeHtml(car.year) + ")" : "";
  const mileage = formatMileage(car.mileage);
  const mileageLine = mileage ? "\n🛣 " + escapeHtml(mileage) : "";
  const demo = car.demo ? "\n🧪 Vehículo de demostración" : "";
  const removedLine = removed ? "\n❌ Ya no aparece en el inventario" : "";
  const fallback = fallbackInventoryUrl(car.condition);
  const safeUrl = removed
    ? fallback
    : car.url.startsWith("https://www.tesla.com/")
      ? escapeHtml(car.url)
      : fallback;
  const linkText = removed ? "Consultar inventario de Tesla" : "Ver en Tesla";

  return (
    "🚗 <b>" + escapeHtml(title) + "</b>" + year + "\n" +
    "💶 " + escapeHtml(formatPrice(car.price)) + "\n" +
    "📍 " + escapeHtml(car.location) + mileageLine + demo + removedLine + "\n" +
    '🔗 <a href="' + safeUrl + '">' + linkText + "</a>"
  );
}

async function notifyCars(env, cars, title, removed = false) {
  let current = title + "\n\n";
  const messages = [];

  for (const car of cars) {
    const block = describeCar(car, removed);
    if (current.length + block.length + 2 > 3500) {
      messages.push(current.trim());
      current = "";
    }
    current += block + "\n\n";
  }
  if (current.trim()) messages.push(current.trim());

  for (const message of messages) {
    await sendTelegram(env, message);
  }
}

async function handleNewInventory(env, cars) {
  const currentIds = cars.map((car) => car.id);
  const previousRaw = await env.INVENTORY_STATE.get(NEW_STATE_KEY);
  const previousIds = new Set(previousRaw ? JSON.parse(previousRaw) : []);
  const initialized = await env.INVENTORY_STATE.get(NEW_INITIALIZED_KEY);

  if (!initialized) {
    await sendTelegram(
      env,
      "✅ <b>Monitor Tesla España activado</b>\n" +
        "Revisaré los cuatro modelos cada cinco minutos. Inventario inicial: " +
        cars.length +
        " vehículos.",
    );
    await env.INVENTORY_STATE.put(NEW_STATE_KEY, JSON.stringify(currentIds));
    await env.INVENTORY_STATE.put(NEW_INITIALIZED_KEY, "1");
    return {initialized: true, current: cars.length, added: 0};
  }

  const added = cars.filter((car) => !previousIds.has(car.id));
  if (added.length) {
    await notifyCars(
      env,
      added,
      "⚡️ <b>" + added.length + " Tesla nuevos en entrega inmediata</b>",
    );
  }

  await env.INVENTORY_STATE.put(NEW_STATE_KEY, JSON.stringify(currentIds));
  return {initialized: false, current: cars.length, added: added.length};
}

function readUsedState(raw) {
  if (!raw) return {active: [], pending: []};

  try {
    const parsed = JSON.parse(raw);
    const active = normalizeCars(parsed?.active ?? [], "used");
    const pending = [];
    for (const item of Array.isArray(parsed?.pending) ? parsed.pending : []) {
      const car = normalizeCars([item?.car], "used")[0];
      if (!car) continue;
      pending.push({
        car,
        misses: Math.max(1, Number(item?.misses) || 1),
      });
    }
    return {active, pending};
  } catch {
    return {active: [], pending: []};
  }
}

async function handleUsedInventory(env, cars) {
  const initialized = await env.INVENTORY_STATE.get(USED_INITIALIZED_KEY);

  if (!initialized) {
    await sendTelegram(
      env,
      "✅ <b>Monitor Tesla de ocasión activado</b>\n" +
        "Inventario inicial: " + cars.length + " vehículos. " +
        "Te avisaré cuando aparezca uno y cuando deje de estar disponible.",
    );
    await env.INVENTORY_STATE.put(
      USED_STATE_KEY,
      JSON.stringify({active: cars, pending: []}),
    );
    await env.INVENTORY_STATE.put(USED_INITIALIZED_KEY, "1");
    return {
      initialized: true,
      current: cars.length,
      added: 0,
      removed: 0,
      pending_removed: 0,
    };
  }

  const previous = readUsedState(
    await env.INVENTORY_STATE.get(USED_STATE_KEY),
  );
  const currentIds = new Set(cars.map((car) => car.id));
  const knownIds = new Set([
    ...previous.active.map((car) => car.id),
    ...previous.pending.map((item) => item.car.id),
  ]);
  const added = cars.filter((car) => !knownIds.has(car.id));
  const pendingById = new Map();
  const removed = [];

  for (const item of previous.pending) {
    if (currentIds.has(item.car.id)) continue;
    const misses = item.misses + 1;
    if (misses >= 2) {
      removed.push(item.car);
    } else {
      pendingById.set(item.car.id, {car: item.car, misses});
    }
  }

  for (const car of previous.active) {
    if (!currentIds.has(car.id) && !pendingById.has(car.id)) {
      pendingById.set(car.id, {car, misses: 1});
    }
  }

  if (added.length) {
    await notifyCars(
      env,
      added,
      "🟢 <b>" + added.length + " Tesla de ocasión añadidos</b>",
    );
  }
  if (removed.length) {
    await notifyCars(
      env,
      removed,
      "🔴 <b>" + removed.length + " Tesla de ocasión retirados</b>",
      true,
    );
  }

  const pending = [...pendingById.values()];
  await env.INVENTORY_STATE.put(
    USED_STATE_KEY,
    JSON.stringify({active: cars, pending}),
  );

  return {
    initialized: false,
    current: cars.length,
    added: added.length,
    removed: removed.length,
    pending_removed: pending.length,
  };
}

async function handleReport(request, env) {
  const expected = env.MONITOR_API_KEY;
  const supplied = request.headers.get("authorization");
  if (!expected || supplied !== "Bearer " + expected) {
    return json({ok: false, error: "No autorizado"}, 401);
  }

  const body = await request.json();
  const newCars = normalizeCars(body?.cars, "new");
  const newInventory = await handleNewInventory(env, newCars);

  let usedInventory = null;
  if (Array.isArray(body?.used_cars)) {
    const usedCars = normalizeCars(body.used_cars, "used");
    usedInventory = await handleUsedInventory(env, usedCars);
  }

  return json({
    ok: true,
    new_inventory: newInventory,
    used_inventory: usedInventory,
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return json({
        ready: true,
        service: "tesla-stock-es",
        scheduler: "GitHub Actions",
        monitors: ["new", "used"],
      });
    }

    if (request.method === "POST" && url.pathname === "/report") {
      try {
        return await handleReport(request, env);
      } catch (error) {
        console.error("Report error:", error?.message ?? "unknown");
        return json({ok: false, error: "No se pudo procesar el informe"}, 500);
      }
    }

    return json({ok: false, error: "Ruta no encontrada"}, 404);
  },
};
