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

const PY_URL = process.env.PY_URL || 'http://127.0.0.1:5000/incoming';
const PORT = parseInt(process.env.WA_PORT || '3000', 10);
const ALLOWED = (process.env.WA_ALLOWED || '')
  .split(',').map(s => s.trim()).filter(Boolean);

let sock = null;

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState('wa_auth');
  const { version, isLatest } = await fetchLatestBaileysVersion();
  console.log(`Using WhatsApp Web v${version.join('.')} (latest: ${isLatest})`);
  sock = makeWASocket({
    version,                                 // use the CURRENT WA Web protocol
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
    if (connection === 'open') console.log('WhatsApp connected.');
    if (connection === 'close') {
      const err = lastDisconnect && lastDisconnect.error;
      const code = err && err.output && err.output.statusCode;
      console.log(`Connection closed. statusCode=${code} reason=${err && err.message}`);
      const stop = code === DisconnectReason.loggedOut ||
                   code === DisconnectReason.connectionReplaced;
      if (stop) {
        console.log('Not reconnecting (logged out or session replaced). '
                    + 'Delete the wa_auth folder and restart to re-scan.');
      } else {
        setTimeout(start, 3000);             // back off before retrying
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
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
        // Map/radar/sat build an image on the Python side, which can be slow.
        const res = await axios.post(PY_URL, { from: jid, text }, { timeout: 60000 });
        const data = res.data || {};
        if (data.image) {                        // Photo reply (map/radar/sat)
          const buf = Buffer.from(data.image, 'base64');
          console.log(`[out] image bytes=${buf.length}`);
          await sock.sendMessage(jid, { image: buf, caption: data.caption || '' });
        } else if (data.reply) {                 // text reply
          console.log(`[out] replyLen=${data.reply.length}`);
          await sock.sendMessage(jid, { text: data.reply });
        }
      } catch (e) {
        console.error('Python error:', e.message);
      }
    }
  });
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
