#!/usr/bin/env node
// Health-area food-photo bot (health-bcr.1).
// Long-polls Telegram, runs meal photos/descriptions through a FROZEN Claude
// vision prompt, stores rows in data/health.sqlite, replies with the estimate.
// The prompt and model stay fixed for the whole cut — the weekly TDEE
// correction absorbs a consistent bias but not a drifting one.

import { execFile } from "node:child_process";
import { createServer } from "node:http";
import { promisify } from "node:util";
import { DatabaseSync } from "node:sqlite";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const execFileAsync = promisify(execFile);
const ROOT = dirname(fileURLToPath(import.meta.url));
const DATA = join(ROOT, "data");
const PHOTOS = join(DATA, "photos");
const SCANS = join(DATA, "scans");
mkdirSync(PHOTOS, { recursive: true });
mkdirSync(SCANS, { recursive: true });

// --- env ---------------------------------------------------------------
const env = {};
for (const line of readFileSync(join(ROOT, ".env"), "utf8").split("\n")) {
  const m = line.match(/^([A-Z_]+)=(.*)$/);
  if (m) env[m[1]] = m[2];
}
const TOKEN = env.TELEGRAM_BOT_TOKEN;
const MODEL = env.HEALTH_VISION_MODEL || "claude-sonnet-5";
if (!TOKEN) throw new Error("TELEGRAM_BOT_TOKEN missing in .env");
const API = `https://api.telegram.org/bot${TOKEN}`;
const CLAUDE = env.CLAUDE_BIN || "claude";

// --- db ----------------------------------------------------------------
const db = new DatabaseSync(join(DATA, "health.sqlite"));
db.exec(`
  CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,            -- ISO UTC
    date TEXT NOT NULL,          -- local date (Europe/Helsinki)
    chat_id INTEGER, message_id INTEGER,
    kind TEXT,                   -- photo | text
    photo_path TEXT, caption TEXT,
    items TEXT, kcal REAL, kcal_low REAL, kcal_high REAL,
    protein_g REAL, carbs_g REAL, fat_g REAL,
    confidence TEXT, question TEXT, raw TEXT,
    corrected_kcal REAL
  );
  CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
  CREATE TABLE IF NOT EXISTS metrics (
    name TEXT NOT NULL,
    ts TEXT NOT NULL,
    date TEXT NOT NULL,
    qty REAL, units TEXT, raw TEXT,
    PRIMARY KEY (name, ts) ON CONFLICT REPLACE
  );
`);
try { db.exec("ALTER TABLE meals ADD COLUMN bot_msg_id INTEGER"); } catch { /* exists */ }
db.exec("CREATE TABLE IF NOT EXISTS foods (name TEXT PRIMARY KEY COLLATE NOCASE, facts TEXT NOT NULL, created TEXT)");
const kvGet = (k) => db.prepare("SELECT v FROM kv WHERE k=?").get(k)?.v;
const kvSet = (k, v) =>
  db.prepare("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v").run(k, String(v));

const localDate = (d = new Date()) =>
  new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Helsinki" }).format(d);
const localStamp = (d = new Date()) =>
  new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Helsinki", dateStyle: "short", timeStyle: "medium" })
    .format(d).replace(/[ :]/g, "-");

// --- telegram ----------------------------------------------------------
async function tg(method, params = {}) {
  // Hard deadline on every call: a silently-dropped connection must throw,
  // not hang the poll loop forever (long-poll waits 50s server-side → 65s cap).
  const deadline = method === "getUpdates" ? 65_000 : 30_000;
  const res = await fetch(`${API}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(params),
    signal: AbortSignal.timeout(deadline),
  });
  const j = await res.json();
  if (!j.ok) throw new Error(`${method}: ${j.description}`);
  return j.result;
}
const reply = (chat_id, text, message_id) =>
  tg("sendMessage", { chat_id, text, reply_to_message_id: message_id }).catch((e) => { log(`reply failed: ${e.message}`); return null; });

// A clarifying question opens a 15-minute window in which a plain text
// message is treated as the ANSWER (amending that meal), not a new meal.
const setPending = (mealId) => kvSet("pending_q", JSON.stringify({ meal: mealId, until: Date.now() + 15 * 60 * 1000 }));
const clearPending = () => kvSet("pending_q", "");
function pendingMealId() {
  try {
    const p = JSON.parse(kvGet("pending_q") || "null");
    if (p && p.until > Date.now()) return p.meal;
  } catch { /* none */ }
  return null;
}

async function downloadPhoto(fileId, dest) {
  const f = await tg("getFile", { file_id: fileId });
  const res = await fetch(`https://api.telegram.org/file/bot${TOKEN}/${f.file_path}`);
  writeFileSync(dest, Buffer.from(await res.arrayBuffer()));
}

// --- vision (FROZEN — do not edit mid-cut; see baseline doc) ------------
const knownFoodsBlock = () => {
  const rows = db.prepare("SELECT name, facts FROM foods ORDER BY name LIMIT 40").all();
  if (!rows.length) return "";
  return `\nUser's verified staple foods (label data — when the meal contains one of these, use these numbers instead of estimating):\n${rows.map((r) => `- ${r.name}: ${r.facts}`).join("\n")}\n`;
};

const VISION_PROMPT = (photoPath, caption) => `You are a nutrition estimator inside an automated food log. Subject: 37-year-old male, 164 cm, ~68 kg, living in Finland (Finnish/Nordic foods common).

${photoPath ? `Read the meal photo at ${photoPath} with the Read tool.` : "No photo — estimate from the text description alone."}
${caption ? `User's note: "${caption}"` : ""}
${knownFoodsBlock()}

Estimate the meal. Be realistic about hidden energy (cooking oil, butter, dressings, sugar in drinks). Portion size is the main error source — use plate/cutlery/hand cues for scale. If the user's note gives portion or preparation facts, they override visual guesses.

Respond with ONLY a JSON object, no prose, no fences:
{"items":[{"name":"...","est_g":0}],"kcal":0,"kcal_low":0,"kcal_high":0,"protein_g":0,"carbs_g":0,"fat_g":0,"confidence":"high|medium|low","question":"one short clarifying question ONLY if it would materially change the estimate, else empty string"}`;

function extractJson(stdout) {
  let text = stdout;
  try {
    const e = JSON.parse(stdout);
    if (e && typeof e.result === "string") text = e.result;
  } catch { /* raw */ }
  const s = text.indexOf("{");
  const e = text.lastIndexOf("}");
  if (s === -1 || e === -1) throw new Error("no JSON in vision output");
  return JSON.parse(text.slice(s, e + 1));
}

async function estimateMeal(photoPath, caption) {
  const { stdout } = await execFileAsync(
    CLAUDE,
    ["-p", VISION_PROMPT(photoPath, caption), "--output-format", "json", "--model", MODEL, "--allowedTools", "Read"],
    { cwd: ROOT, timeout: 180_000, maxBuffer: 8 * 1024 * 1024 },
  );
  return extractJson(stdout);
}

// --- reply intent (NOT part of the frozen estimator — routing only) ------
// Text in the pending window / a reply can be a correction to that meal OR a
// separate food eaten in addition. Amending with separate food used to
// overwrite the meal and silently drop calories.
const INTENT_PROMPT = (meal, text) => {
  let items = [];
  try { items = (JSON.parse(meal.items || "[]") || []).map((i) => i.name); } catch { /* none */ }
  return `A food-log bot has logged this meal:
Items: ${items.join(", ") || "(unknown)"}
User's note: "${meal.caption || ""}"
${meal.question ? `Bot's open question to the user: "${meal.question}"` : ""}

The user then sent: "${text}"

Is the user (a) correcting or answering about that same logged meal (portion, food identity, preparation, answering the question), or (b) reporting separate food/drink consumed in addition (a snack, another meal, "I also had...")?
Rules of thumb:
- Corrections usually contain corrective wording ("actually", "it was", "no,", "only", a quantity/kcal fix) or directly answer the bot's question.
- A message that is just the NAME of a food, with no corrective wording, is a NEW entry unless it plausibly names the same dish that was logged.
- If the text does both, treat it as a correction.
Respond with ONLY JSON, no prose: {"intent":"amend"} or {"intent":"new_meal"}`;
};

async function classifyReply(meal, text) {
  try {
    const { stdout } = await execFileAsync(
      CLAUDE,
      ["-p", INTENT_PROMPT(meal, text), "--output-format", "json", "--model", MODEL],
      { cwd: ROOT, timeout: 60_000, maxBuffer: 1024 * 1024 },
    );
    const intent = extractJson(stdout)?.intent;
    if (intent === "new_meal" || intent === "amend") return intent;
  } catch (e) {
    log(`intent classify failed: ${e.message}`);
  }
  return "amend"; // on failure, keep the pre-fix behavior
}

// --- formatting ---------------------------------------------------------
const n0 = (x) => (x == null ? "?" : Math.round(x));
function estimateText(a) {
  const items = (a.items || []).map((i) => `${i.name}${i.est_g ? ` ~${n0(i.est_g)}g` : ""}`).join(", ");
  const lines = [
    `~${n0(a.kcal)} kcal (${n0(a.kcal_low)}–${n0(a.kcal_high)}) · ${a.confidence} confidence`,
    `P ${n0(a.protein_g)}g · C ${n0(a.carbs_g)}g · F ${n0(a.fat_g)}g`,
    items ? `→ ${items}` : "",
    a.question ? `❓ ${a.question} (answer with "fix <kcal>" or just tell me — I'll adjust)` : "",
  ];
  return lines.filter(Boolean).join("\n");
}

function todaySummary() {
  const rows = db.prepare(
    "SELECT COALESCE(corrected_kcal,kcal) k, protein_g p FROM meals WHERE date=? AND COALESCE(corrected_kcal,kcal) IS NOT NULL",
  ).all(localDate());
  const kcal = rows.reduce((s, r) => s + (r.k || 0), 0);
  const prot = rows.reduce((s, r) => s + (r.p || 0), 0);
  return `Today: ${rows.length} logged · ~${Math.round(kcal)} kcal · ~${Math.round(prot)}g protein`;
}

// --- handlers -----------------------------------------------------------
const log = (m) => console.log(`[${new Date().toISOString()}] ${m}`);

async function handleMessage(msg) {
  const chatId = msg.chat.id;

  // First contact locks the bot to this chat; everyone else is ignored.
  const owner = kvGet("owner_chat_id");
  if (!owner) {
    kvSet("owner_chat_id", chatId);
    log(`owner locked to chat ${chatId} (${msg.from?.username || msg.from?.first_name || "?"})`);
  } else if (String(chatId) !== owner) {
    log(`ignoring message from non-owner chat ${chatId}`);
    return;
  }

  const text = (msg.text || "").trim();
  const caption = (msg.caption || "").trim();

  if (text === "/start") {
    return reply(chatId, "Locked to you. Send meal photos (a short caption helps: portion, oil, drink). Commands: /today, fix <kcal>. Reply to an estimate (or just answer my question) to amend it — new foods can just be sent as their own message, I can tell the difference. Caption a photo \"scan\" to archive a body-scan sheet.");
  }
  if (text === "/today") return reply(chatId, todaySummary());

  // Staple-food dictionary: "remember <name>: <label facts>", /foods, "forget <name>"
  const rem = text.match(/^\/?remember\s+([^:=]+)[:=]\s*(.+)$/is);
  if (rem) {
    const name = rem[1].trim();
    db.prepare("INSERT INTO foods (name,facts,created) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET facts=excluded.facts").run(name, rem[2].trim(), new Date().toISOString());
    return reply(chatId, `Remembered "${name}". It'll be used whenever it shows up in a meal.`);
  }
  if (text === "/foods") {
    const rows = db.prepare("SELECT name,facts FROM foods ORDER BY name").all();
    return reply(chatId, rows.length ? rows.map((r) => `• ${r.name}: ${r.facts}`).join("\n") : "No remembered foods yet. Teach me: remember <name>: <label facts>");
  }
  const fg = text.match(/^\/?forget\s+(.+)$/i);
  if (fg) {
    const gone = db.prepare("DELETE FROM foods WHERE name=?").run(fg[1].trim());
    return reply(chatId, gone.changes ? `Forgot "${fg[1].trim()}".` : `Don't know "${fg[1].trim()}" — see /foods.`);
  }

  // Which meal is this message about? Telegram reply-to wins; otherwise an
  // open clarifying-question window claims plain text as its answer.
  const refId = msg.reply_to_message?.message_id;
  const targetMeal = refId
    ? db.prepare("SELECT * FROM meals WHERE bot_msg_id=? OR message_id=? ORDER BY id DESC LIMIT 1").get(refId, refId)
    : null;

  const fix = text.match(/^fix\s+(\d{2,5})\s*(?:kcal)?$/i);
  if (fix) {
    const meal = targetMeal || db.prepare("SELECT id,kcal FROM meals ORDER BY id DESC LIMIT 1").get();
    if (!meal) return reply(chatId, "Nothing to fix yet.");
    db.prepare("UPDATE meals SET corrected_kcal=? WHERE id=?").run(Number(fix[1]), meal.id);
    clearPending();
    return reply(chatId, `Corrected ${n0(meal.kcal)} → ${fix[1]} kcal. ${todaySummary()}`);
  }

  // Amendment path: text replying to an estimate, or answering an open question.
  // The text may instead describe NEW food (a snack sent inside the pending
  // window) — classify first so additions don't overwrite the previous meal.
  if (text && !text.startsWith("/")) {
    const meal = targetMeal || (pendingMealId() && db.prepare("SELECT * FROM meals WHERE id=?").get(pendingMealId()));
    if (meal) {
      if ((await classifyReply(meal, text)) === "new_meal") {
        clearPending();
        return logMeal(msg, "text", null, text);
      }
      clearPending();
      return amendMeal(msg, meal, text);
    }
  }

  // Photo message
  if (msg.photo?.length) {
    const best = msg.photo[msg.photo.length - 1];
    if (/^scan/i.test(caption)) {
      const dest = join(SCANS, `${localStamp()}-telegram.jpg`);
      await downloadPhoto(best.file_id, dest);
      return reply(chatId, `Scan sheet archived (${dest.split("/").pop()}).`, msg.message_id);
    }
    const dest = join(PHOTOS, `${localStamp()}-${msg.message_id}.jpg`);
    await downloadPhoto(best.file_id, dest);
    return logMeal(msg, "photo", dest, caption);
  }

  // Plain-text meal description (anything that isn't a command)
  if (text && !text.startsWith("/")) return logMeal(msg, "text", null, text);
}

async function logMeal(msg, kind, photoPath, caption) {
  const chatId = msg.chat.id;
  let a = null;
  try {
    a = await estimateMeal(photoPath, caption);
  } catch (e) {
    log(`vision failed: ${e.message}`);
  }
  const info = db.prepare(
    `INSERT INTO meals (ts,date,chat_id,message_id,kind,photo_path,caption,items,kcal,kcal_low,kcal_high,protein_g,carbs_g,fat_g,confidence,question,raw)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  ).run(
    new Date().toISOString(), localDate(), chatId, msg.message_id, kind, photoPath, caption || null,
    a ? JSON.stringify(a.items || []) : null,
    a?.kcal ?? null, a?.kcal_low ?? null, a?.kcal_high ?? null,
    a?.protein_g ?? null, a?.carbs_g ?? null, a?.fat_g ?? null,
    a?.confidence ?? null, a?.question || null, a ? JSON.stringify(a) : null,
  );
  const mealId = Number(info.lastInsertRowid);
  const sent = await (a
    ? reply(chatId, `${estimateText(a)}\n\n${todaySummary()}`, msg.message_id)
    : reply(chatId, "Logged the entry but couldn't estimate it right now — I'll flag it for review. (You can send \"fix <kcal>\" if you know it.)", msg.message_id));
  if (sent?.message_id) db.prepare("UPDATE meals SET bot_msg_id=? WHERE id=?").run(sent.message_id, mealId);
  setPending(mealId); // any text in the next 15 min amends this meal, question or not
}

async function amendMeal(msg, meal, correction) {
  const chatId = msg.chat.id;
  const combined = [meal.caption, `Correction/answer from the user: ${correction}`].filter(Boolean).join("\n");
  let a = null;
  try {
    a = await estimateMeal(meal.photo_path, combined);
  } catch (e) {
    log(`amend vision failed: ${e.message}`);
  }
  if (!a) return reply(chatId, 'Couldn\'t re-estimate — try "fix <kcal>" instead.', msg.message_id);
  db.prepare(
    `UPDATE meals SET caption=?, items=?, kcal=?, kcal_low=?, kcal_high=?, protein_g=?, carbs_g=?, fat_g=?, confidence=?, question=?, raw=?, corrected_kcal=NULL WHERE id=?`,
  ).run(
    combined, JSON.stringify(a.items || []),
    a.kcal ?? null, a.kcal_low ?? null, a.kcal_high ?? null,
    a.protein_g ?? null, a.carbs_g ?? null, a.fat_g ?? null,
    a.confidence ?? null, a.question || null, JSON.stringify(a), meal.id,
  );
  const sent = await reply(chatId, `Amended ⤴\n${estimateText(a)}\n\n${todaySummary()}`, msg.message_id);
  if (sent?.message_id) db.prepare("UPDATE meals SET bot_msg_id=? WHERE id=?").run(sent.message_id, meal.id);
  setPending(meal.id); // corrections can chain
}

// --- Health Auto Export ingest (health-bcr.2) ----------------------------
// iPhone (HAE app) POSTs Apple Health metrics as JSON over the tailnet.
// Weight arrives via Garmin scale → Garmin Connect → Apple Health → HAE.
const INGEST_TOKEN = env.HAE_INGEST_TOKEN;
const INGEST_BIND = env.HAE_BIND || "127.0.0.1";
const INGEST_PORT = Number(env.HAE_PORT || 3210);

function saveMetrics(payload) {
  const metrics = payload?.data?.metrics || [];
  const ins = db.prepare("INSERT INTO metrics (name,ts,date,qty,units,raw) VALUES (?,?,?,?,?,?)");
  let saved = 0;
  for (const m of metrics) {
    for (const p of m.data || []) {
      const ts = p.date || p.timestamp;
      if (!ts) continue;
      const qty = typeof p.qty === "number" ? p.qty : (typeof p.avg === "number" ? p.avg : null);
      const extras = JSON.stringify(p);
      ins.run(m.name, ts, localDate(new Date(ts)), qty, m.units || null, extras);
      saved++;
    }
  }
  return saved;
}

if (INGEST_TOKEN) {
  const srv = createServer((req, res) => {
    const deny = (code, msg) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify({ ok: false, error: msg })); };
    if (req.method === "GET" && req.url === "/health") { res.writeHead(200); return res.end("ok"); }
    if (req.method !== "POST" || req.url !== "/ingest/hae") return deny(404, "not found");
    if ((req.headers.authorization || "") !== `Bearer ${INGEST_TOKEN}`) return deny(401, "unauthorized");
    let body = "";
    req.on("data", (c) => { body += c; if (body.length > 64 * 1024 * 1024) req.destroy(); });
    req.on("end", () => {
      try {
        const saved = saveMetrics(JSON.parse(body));
        log(`ingest: saved ${saved} metric points`);
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, saved }));
      } catch (e) {
        log(`ingest error: ${e.message}`);
        deny(400, e.message);
      }
    });
  });
  srv.listen(INGEST_PORT, INGEST_BIND, () => log(`ingest listening on http://${INGEST_BIND}:${INGEST_PORT}/ingest/hae`));
  srv.on("error", (e) => log(`ingest server error: ${e.message}`));
} else {
  log("ingest disabled (no HAE_INGEST_TOKEN in .env)");
}

// --- main loop ----------------------------------------------------------
log(`health bot starting — model ${MODEL}, db ${join(DATA, "health.sqlite")}`);
let offset = Number(kvGet("update_offset") || 0);
for (;;) {
  try {
    const updates = await tg("getUpdates", { offset, timeout: 50, allowed_updates: ["message"] });
    for (const u of updates) {
      offset = u.update_id + 1;
      kvSet("update_offset", offset);
      if (u.message) {
        try {
          await handleMessage(u.message);
        } catch (e) {
          log(`handler error: ${e.message}`);
          reply(u.message.chat.id, "Something broke handling that — it's logged, I'll survive.").catch(() => {});
        }
      }
    }
  } catch (e) {
    log(`poll error: ${e.message} — retrying in 5s`);
    await new Promise((r) => setTimeout(r, 5000));
  }
}
