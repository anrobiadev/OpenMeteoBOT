'use strict';
/*
 * WhatsApp <-> weather-bot bridge using Baileys (UNOFFICIAL WhatsApp Web).
 *
 * - Receives WhatsApp messages, forwards them to the Python service (/incoming),
 *   and sends back the reply.
 * - Exposes POST /send so the Python alert loop can push proactive alerts.
 *
 * WARNING: logs in as a WhatsApp Web client tied to YOUR number. Against
 * WhatsApp's ToS; the number can be banned. Prefer a secondary number.
 *
 * Env:
 *   PY_URL       Python /incoming URL (default http://127.0.0.1:5000/incoming)
 *   WA_PORT      port for the /send endpoint (default 3000)
 *   WA_ALLOWED   comma-separated phone numbers allowed to use the bot
 *                (digits only, e.g. 407xxxxxxxx). Empty = allow everyone.
 */
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason,
        fetchLatestBaileysVersion } =
  require('@whiskeysockets/baileys');
const express = require('express');
const axios = require('axios');
const P = require('pino');
const qrcode = require('qrcode-terminal');
const fs = require('fs');

const PY_URL = process.env.PY_URL || 'http://127.0.0.1:5000/incoming';
const PORT = parseInt(process.env.WA_PORT || '3000', 10);
const ALLOWED = (process.env.WA_ALLOWED || '')
  .split(',').map(s => s.trim()).filter(Boolean);

// Heartbeat file the Telegram bot reads to know the WhatsApp link is healthy.
const STATUS_FILE = process.env.WA_STATUS_FILE || 'wa_status.json';
function writeStatus() {
  try {
    fs.writeFileSync(STATUS_FILE, JSON.stringify({
      ts: Math.floor(Date.now() / 1000),
      connected: connected,
      loggedOut: loggedOut,
    }));
  } catch (_) { /* ignore */ }
}
setInterval(writeStatus, 30000);   // refresh the heartbeat every 30s

let sock = null;
let connected = false;
let starting = false;
let loggedOut = false;                        // fatal: needs a fresh QR scan
let lastActivity = Date.now();
let reconnectTimer = null;

// Idle time after which the watchdog actively probes the socket (ms).
const STALE_MS = parseInt(process.env.WA_STALE_MS || '180000', 10);   // 3 min

function markActivity() { lastActivity = Date.now(); }

function scheduleReconnect(delay) {
  if (loggedOut || reconnectTimer) return;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; start(); }, delay);
}

async function start() {
  if (starting || loggedOut) return;
  starting = true;
  try {
    if (sock) { try { sock.end(); } catch (_) {} }   // drop any stale socket first
    const { state, saveCreds } = await useMultiFileAuthState('wa_auth');
    const { version, isLatest } = await fetchLatestBaileysVersion();
    console.log(`Using WhatsApp Web v${version.join('.')} (latest: ${isLatest})`);
    sock = makeWASocket({
      version,                               // use the CURRENT WA Web protocol
      auth: state,
      logger: P({ level: 'silent' }),
      browser: ['MeteoBot', 'Chrome', '1.0'],
    });
    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (u) => {
      const { connection, lastDisconnect, qr } = u;
      if (qr) {
        console.log('Scan this QR in WhatsApp > Linked devices:');
        qrcode.generate(qr, { small: true });
      }
      if (connection === 'open') { connected = true; markActivity(); writeStatus(); console.log('WhatsApp connected.'); }
      if (connection === 'close') {
        connected = false;
        const err = lastDisconnect && lastDisconnect.error;
        const code = err && err.output && err.output.statusCode;
        console.log(`Connection closed. statusCode=${code} reason=${err && err.message}`);
        const stop = code === DisconnectReason.loggedOut ||
                     code === DisconnectReason.connectionReplaced;
        if (stop) {
          loggedOut = true;
          writeStatus();
          console.log('Not reconnecting (logged out or session replaced). '
                      + 'Delete the wa_auth folder and restart to re-scan.');
        } else {
          writeStatus();
          scheduleReconnect(3000);           // back off before retrying
        }
      }
    });

    sock.ev.on('messages.upsert', handleUpsert);
  } catch (e) {
    console.error('start() error:', e.message);
    scheduleReconnect(5000);
  } finally {
    starting = false;
  }
}

// Watchdog: catch a silently-dead ("zombie") connection that never fires 'close'.
setInterval(async () => {
  if (loggedOut) return;
  if (!connected) { scheduleReconnect(1000); return; }
  if (Date.now() - lastActivity > STALE_MS) {
    try {
      await sock.sendPresenceUpdate('available');   // active probe + keepalive
      markActivity();
    } catch (e) {
      console.log('[watchdog] keepalive failed -> reconnect:', e.message);
      connected = false;
      try { sock.end(); } catch (_) {}
      scheduleReconnect(1000);
    }
  }
}, 60000);

async function handleUpsert({ messages, type }) {
    if (type !== 'notify') return;
    markActivity();
    for (const m of messages) {
      if (!m.message || m.key.fromMe) continue;
      const jid = m.key.remoteJid || '';
      if (jid.endsWith('@g.us') || jid.endsWith('@broadcast')) continue; // no groups/status
      const number = jid.split('@')[0];
      const text = m.message.conversation ||
                   (m.message.extendedTextMessage && m.message.extendedTextMessage.text) || '';
      const allowed = !ALLOWED.length || ALLOWED.includes(number);
      console.log(`[in] from=${number} allowed=${allowed} text=${JSON.stringify(text)}`);
      if (!allowed) continue;
      if (!text.trim()) continue;
      try {
        // Map/radar/sat build an image on the Python side, which can be slow and
        // returns a large base64 body -> disable axios size limits + long timeout.
        const res = await axios.post(PY_URL, { from: jid, text }, {
          timeout: 60000,
          maxContentLength: Infinity,
          maxBodyLength: Infinity,
        });
        const data = res.data || {};
        if (data.image) {                        // Photo reply (map/radar/sat)
          const buf = Buffer.from(data.image, 'base64');
          console.log(`[out] image bytes=${buf.length}`);
          try {
            await sock.sendMessage(jid, {
              image: buf,
              mimetype: 'image/png',
              caption: data.caption || '',
            });
          } catch (imgErr) {                     // fall back to text so something arrives
            console.error('Image send failed:', imgErr.message);
            if (data.caption) await sock.sendMessage(jid, { text: data.caption });
          }
        } else if (data.reply) {                 // text reply
          console.log(`[out] replyLen=${data.reply.length}`);
          await sock.sendMessage(jid, { text: data.reply });
        } else {
          console.log('[out] empty response from Python:', JSON.stringify(data).slice(0, 200));
        }
      } catch (e) {
        console.error('Python error:', e.message, e.response ? `(status ${e.response.status})` : '');
      }
    }
}

// HTTP endpoint so the Python alert loop can push messages to WhatsApp
const app = express();
app.use(express.json());
app.post('/send', async (req, res) => {
  const { to, text } = req.body || {};
  if (!to || !text) return res.status(400).json({ error: 'to and text required' });
  if (!sock) return res.status(503).json({ error: 'not connected' });
  try {
    await sock.sendMessage(to, { text });
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});
app.listen(PORT, '127.0.0.1', () => console.log(`Bridge send API on http://127.0.0.1:${PORT}`));

start().catch(err => console.error('Fatal:', err));
