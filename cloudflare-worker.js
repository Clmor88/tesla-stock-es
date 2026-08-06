const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

const STATE_KEY = "github-monitor:cars:v1";
const INITIALIZED_KEY = "github-monitor:initialized:v1";

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

function normalizeCars(value) {
  if (!Array.isArray(value)) {
    throw new Error("cars debe ser una lista");
  }

  const seen = new Set();
  const cars = [];
  for (const raw of value.slice(0, 500)) {
    const id = String(raw?.id ?? "").trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    cars.push({
      id: id.slice(0, 200),
      model: String(raw?.model ?? "Tesla").slice(0, 100),
      trim: String(raw?.trim ?? "").slice(0, 180),
      year: String(raw?.year ?? "").slice(0, 10),
      price: raw?.price ?? 0,
      location: String(raw?.location ?? "España").slice(0, 180),
      demo: Boolean(raw?.demo),
      url: String(raw?.url ?? "https://www.tesla.com/es_ES/inventory/new/m3").slice(0, 500),
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

function describeCar(car) {
  const title = [car.model, car.trim].filter(Boolean).join(" — ");
  const year = car.year ? " (" + escapeHtml(car.year) + ")" : "";
  const demo = car.demo ? "\n🧪 Vehículo de demostración" : "";
  const safeUrl = car.url.startsWith("https://www.tesla.com/")
    ? escapeHtml(car.url)
    : "https://www.tesla.com/es_ES/inventory/new/m3";

  return (
    "🚗 <b>" + escapeHtml(title) + "</b>" + year + "\n" +
    "💶 " + escapeHtml(formatPrice(car.price)) + "\n" +
    "📍 " + escapeHtml(car.location) + demo + "\n" +
    '🔗 <a href="' + safeUrl + '">Ver y reservar en Tesla</a>'
  );
}

async function notifyNewCars(env, cars) {
  let current =
    "⚡️ <b>" + cars.length + " Tesla nuevos en entrega inmediata</b>\n\n";
  const messages = [];

  for (const car of cars) {
    const block = describeCar(car);
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

async function handleReport(request, env) {
  const expected = env.MONITOR_API_KEY;
  const supplied = request.headers.get("authorization");
  if (!expected || supplied !== "Bearer " + expected) {
    return json({ok: false, error: "No autorizado"}, 401);
  }

  const body = await request.json();
  const cars = normalizeCars(body?.cars);
  const currentIds = cars.map((car) => car.id);
  const previousRaw = await env.INVENTORY_STATE.get(STATE_KEY);
  const previousIds = new Set(previousRaw ? JSON.parse(previousRaw) : []);
  const initialized = await env.INVENTORY_STATE.get(INITIALIZED_KEY);

  if (!initialized) {
    await sendTelegram(
      env,
      "✅ <b>Monitor Tesla España activado</b>\n" +
        "Revisaré los cuatro modelos cada cinco minutos. Inventario inicial: " +
        cars.length +
        " vehículos.",
    );
    await env.INVENTORY_STATE.put(STATE_KEY, JSON.stringify(currentIds));
    await env.INVENTORY_STATE.put(INITIALIZED_KEY, "1");
    return json({ok: true, initialized: true, current: cars.length, new: 0});
  }

  const newCars = cars.filter((car) => !previousIds.has(car.id));
  if (newCars.length) {
    await notifyNewCars(env, newCars);
  }

  await env.INVENTORY_STATE.put(STATE_KEY, JSON.stringify(currentIds));
  return json({
    ok: true,
    initialized: false,
    current: cars.length,
    new: newCars.length,
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
