/**
 * WhatsApp Personal Bridge — Job Agent
 *
 * Connects to your personal WhatsApp via WhatsApp Web, monitors the groups
 * you configure, extracts job URLs, and forwards them to the Job Agent API
 * for automatic processing (extraction → scoring → cover letter → dashboard).
 *
 * How it works:
 *   1. Run this script once — a QR code appears in the terminal
 *   2. Open WhatsApp on your phone → Linked Devices → Link a Device
 *   3. Scan the QR code — session is saved locally (no re-scan needed)
 *   4. The bridge runs in the background and watches your groups
 *   5. Any message containing a URL in a watched group is forwarded
 *
 * Configuration (bridge/.env):
 *   JOB_AGENT_URL       — URL of your running job-agent API
 *   JOB_AGENT_TOKEN     — API secret (matches SECRET_KEY in main .env)
 *   WATCH_ALL_GROUPS    — "true" to watch every group, "false" to filter
 *   GROUP_KEYWORDS      — comma-separated keywords; only groups whose name
 *                         contains one of these are watched (e.g. "jobs,hiring,careers")
 *   JOB_URL_ONLY        — "true" to only forward likely job-board URLs
 *   SESSION_DIR         — where to store the WhatsApp session (default: ./.wwebjs_auth)
 *   LOG_LEVEL           — "info" | "verbose" | "silent"
 *   ENABLE_SEND         — "true" to expose the local POST /send outbound endpoint
 *   SEND_PORT           — port for the outbound send server (default: 8100)
 *   FORWARD_TEXT_POSTS  — "true" to forward keyword-matching group text posts
 *                         (no URL) to the agent's /api/ingest-text endpoint
 *   FORWARD_DIRECT_MESSAGES — "true" to process only 1:1 chats listed in
 *                         DIRECT_CHAT_NUMBERS
 *   DIRECT_CHAT_NUMBERS — comma-separated phone numbers for opted-in 1:1 chats
 *   AGENT_REQUEST_TIMEOUT_MS — API forwarding timeout (default: 60000)
 *   ALLOW_NONLOCAL_AGENT_URL — explicit opt-in for a non-loopback API (default: false)
 *   ARCHIVE_RESCAN_INTERVAL_MS — bounded cache-only archive poll interval
 *                               (default: 600000; minimum: 60000; zero disables)
 */

'use strict';

const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const fetch = require('node-fetch');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { boundedReasonCode, createArchiveScanScheduler } = require('./archive_scan_scheduler');
const {
  archiveRescanIntervalDefault,
  parseArchiveRescanInterval,
} = require('./archive_scan_config');
const { createBoundedDeduper } = require('./archive_dedup');
const { ARCHIVE_SCAN_MODE, archiveScanAllowsPagination } = require('./archive_scan_modes');

// ── Load config ──────────────────────────────────────────────────────────────
require('dotenv').config({ path: path.join(__dirname, '.env') });

const archiveRescanAttemptsRaw = process.env.ARCHIVE_RESCAN_ATTEMPTS;
const archiveRescanIntervalDefaultValue = archiveRescanIntervalDefault({
  attemptsRaw: archiveRescanAttemptsRaw,
  intervalRaw: process.env.ARCHIVE_RESCAN_INTERVAL_MS,
});

const CONFIG = {
  agentUrl: (process.env.JOB_AGENT_URL || 'http://localhost:8000').replace(/\/$/, ''),
  agentToken: process.env.JOB_AGENT_TOKEN || '',
  allowNonLocalAgentUrl: process.env.ALLOW_NONLOCAL_AGENT_URL === 'true',
  watchAllGroups: process.env.WATCH_ALL_GROUPS !== 'false',   // default: watch all
  watchArchivedOnly: process.env.WATCH_ARCHIVED_ONLY === 'true', // take only archived groups
  groupKeywords: (process.env.GROUP_KEYWORDS || 'jobs,hiring,careers,work,job,tech,remote,vacancy,recruitment')
    .split(',').map(s => s.trim().toLowerCase()).filter(Boolean),
  jobUrlOnly: process.env.JOB_URL_ONLY !== 'false',   // default: job URLs only
  sessionDir: process.env.SESSION_DIR || path.join(__dirname, '.wwebjs_auth'),
  logLevel: process.env.LOG_LEVEL || 'info',     // info | verbose | silent
  forwardOwnerDocs: process.env.FORWARD_OWNER_DOCS === 'true', // forward CV PDFs sent in 1:1 chat
  forwardDirectMessages: process.env.FORWARD_DIRECT_MESSAGES === 'true', // opt-in 1:1 job messages
  directChatNumbers: (process.env.DIRECT_CHAT_NUMBERS || '')
    .split(',').map(s => s.replace(/[^\d]/g, '')).filter(Boolean),
  enableSend: process.env.ENABLE_SEND === 'true', // expose local POST /send outbound endpoint
  sendPort: parseInt(process.env.SEND_PORT || '8100', 10),
  forwardTextPosts: process.env.FORWARD_TEXT_POSTS === 'true', // forward keyword text posts (no URL)
  // URL ingestion can trigger a browser fetch and (in local eager mode) a
  // bounded extraction chain. Ten seconds made the bridge report a false
  // failure while the API was still processing the accepted message.
  agentRequestTimeoutMs: Math.min(
    120000,
    Math.max(5000, parseInt(process.env.AGENT_REQUEST_TIMEOUT_MS || '60000', 10) || 60000),
  ),
  // Keep the cached WhatsApp Web page configurable. The repository default is
  // the last known-good snapshot for this adapter; operators may pin another
  // tested version for a qualification run. Never silently switch to an
  // unqualified live snapshot in production.
  waWebVersion: process.env.WA_WEB_VERSION || '',
  waWebVersionRemotePath: process.env.WA_WEB_VERSION_REMOTE_PATH
    || 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.2412.54.html',
  archiveScanOnStart: process.env.ARCHIVE_SCAN_ON_START === 'true',
  archiveScanLimit: Math.min(
    500,
    Math.max(1, parseInt(process.env.ARCHIVE_SCAN_LIMIT || '100', 10) || 100),
  ),
  // WhatsApp may hydrate archived message windows shortly after the session
  // reports ready. A couple of delayed, bounded startup rescans can inspect
  // that local cache; periodic passes below are explicitly cache-only.
  archiveRescanDelayMs: Math.min(
    300000,
    Math.max(5000, parseInt(process.env.ARCHIVE_RESCAN_DELAY_MS || '30000', 10) || 30000),
  ),
  archiveRescanAttempts: Math.min(
    3,
    Math.max(0, parseInt(process.env.ARCHIVE_RESCAN_ATTEMPTS || '2', 10) || 0),
  ),
  // Continue checking only hydrated archive caches after the startup passes.
  // Zero disables the periodic pass; no scan ever requests unbounded history.
  archiveRescanIntervalMs: parseArchiveRescanInterval(
    process.env.ARCHIVE_RESCAN_INTERVAL_MS,
    { defaultValue: archiveRescanIntervalDefaultValue },
  ),
};

// ── Known job board hostnames (mirrors ingestion/url_utils.py) ───────────────
const JOB_HOSTS = [
  'greenhouse.io', 'lever.co', 'myworkdayjobs.com', 'workday.com',
  'linkedin.com', 'indeed.com', 'glassdoor.com', 'ziprecruiter.com',
  'angel.co', 'wellfound.com', 'otta.com', 'remote.co', 'weworkremotely.com',
  'jobvite.com', 'icims.com', 'smartrecruiters.com', 'ashbyhq.com',
  'workable.com', 'recruitee.com', 'teamtailor.com', 'bamboohr.com',
  'dover.com', 'amazon.jobs', 'careers.google.com', 'careers.microsoft.com',
  'jobs.apple.com', 'efinancialcareers.com', 'totaljobs.com', 'reed.co.uk',
  'cwjobs.co.uk', 'jobsite.co.uk', 'monster.co.uk', 'cityjobs.com',
  'comeet.com', 'comeet.co', 'rippling.com', 'dover.io',
];

const SHORT_HOSTS = [
  'bit.ly', 't.co', 'goo.gl', 'tinyurl.com', 'ow.ly', 'lnkd.in',
  'rb.gy', 'cutt.ly', 'buff.ly', 'tiny.cc', 'is.gd', 's.id',
];

// ── URL extraction ────────────────────────────────────────────────────────────
const URL_RE = /https?:\/\/[^\s<>"')\]},;|\u200b\u200c\u200d\ufeff]+/gi;
const TRAIL = /[.,;:!?)\]]+$/;

function extractUrls(text) {
  if (!text) return [];
  // Strip WhatsApp bold/italic/code markers
  const clean = text.replace(/[*_~`]/g, ' ');
  const raw = clean.match(URL_RE) || [];
  const seen = new Set();
  return raw
    .map(u => u.replace(TRAIL, ''))
    .filter(u => u && !seen.has(u) && seen.add(u));
}

function isJobUrl(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    if (JOB_HOSTS.some(h => host.includes(h))) return true;
    if (/\/(jobs?|careers?|apply|job-openings?)\//i.test(url)) return true;
    return false;
  } catch { return false; }
}

function isShortUrl(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, '');
    return SHORT_HOSTS.includes(host);
  } catch { return false; }
}

// ── Text-post keyword check (mirrors ingestion/text_post_parser.py) ──────────
const TEXT_POST_KEYWORDS = [
  'hiring', 'vacancy', 'vacancies', 'send cv', 'send resume', 'looking for',
  'we are recruiting', 'job opening', 'apply', 'position',
  'مطلوب', 'توظيف', 'وظيفة', 'شاغر', // Arabic: required / hiring / job / vacancy
];

function looksLikeJobText(text) {
  const low = (text || '').toLowerCase();
  return TEXT_POST_KEYWORDS.some(kw => low.includes(kw));
}

// ── Logging ───────────────────────────────────────────────────────────────────
function log(level, ...args) {
  if (CONFIG.logLevel === 'silent') return;
  if (level === 'verbose' && CONFIG.logLevel !== 'verbose') return;
  const ts = new Date().toISOString().slice(11, 19);
  const prefix = { info: '✓', verbose: '·', warn: '⚠', error: '✗' }[level] || '?';
  console.log(`[${ts}] ${prefix}`, ...args);
}

// ── Group filter ──────────────────────────────────────────────────────────────
function shouldWatchGroup(chat) {
  if (CONFIG.watchArchivedOnly && !chat.archived) return false;
  if (CONFIG.watchAllGroups) return true;
  const lower = (chat.name || '').toLowerCase();
  return CONFIG.groupKeywords.some(kw => lower.includes(kw));
}

function isLoopbackAgentUrl(value) {
  try {
    const parsed = new URL(value);
    if (!['http:', 'https:'].includes(parsed.protocol)) return false;
    const host = parsed.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    return ['localhost', '127.0.0.1', '::1', 'host.docker.internal'].includes(host);
  } catch {
    return false;
  }
}

function validateAgentUrl() {
  if (CONFIG.allowNonLocalAgentUrl || isLoopbackAgentUrl(CONFIG.agentUrl)) {
    return true;
  }
  log(
    'error',
    `Refusing non-local JOB_AGENT_URL (${CONFIG.agentUrl}). `
      + 'Point the bridge at http://127.0.0.1:8000; a Vercel URL cannot run the private worker. '
      + 'Set ALLOW_NONLOCAL_AGENT_URL=true only for an intentionally secured private network.',
  );
  return false;
}

function chatNumber(chat) {
  const raw = chat?.id?.user || chat?.id?._serialized || '';
  return String(raw).replace(/[^\d]/g, '');
}

function shouldWatchDirectChat(chat) {
  if (!CONFIG.forwardDirectMessages) return false;
  const number = chatNumber(chat);
  // Direct-message forwarding is deliberately opt-in and allowlisted. An
  // empty list must never turn a personal inbox into an ingestion source.
  return Boolean(number) && CONFIG.directChatNumbers.includes(number);
}

// WhatsApp Web occasionally rejects one chat while serializing its group
// metadata. `client.getChats()` uses Promise.all, so that single rejection
// used to prevent every archived group from being inspected. Keep a small,
// read-only metadata fallback that never asks WhatsApp for participant data.
async function listChatMetadataFallback() {
  if (!client.pupPage) return [];
  return client.pupPage.evaluate(() => {
    const models = window.Store?.Chat?.getModelsArray?.() || [];
    return models.map(chat => {
      const id = chat?.id?._serialized || '';
      const name = chat?.formattedTitle || chat?.name || '';
      const archived = Boolean(
        chat?.archived || chat?.isArchived || chat?.archive || chat?.__x_isArchived,
      );
      const isGroup = Boolean(chat?.isGroup || chat?.groupMetadata || /@g\.us$/.test(id));
      return { id, name, archived, isGroup };
    }).filter(chat => chat.id);
  });
}

// Fetch only the recent text metadata needed for URL extraction. This avoids
// the adapter's group-metadata serializer and never returns media, cookies,
// participant lists, or page content to the Node process.
async function fetchRawChatMessages(chatId, limit, { allowPagination = true } = {}) {
  if (!client.pupPage || !chatId) return [];
  return client.pupPage.evaluate(async ({ id, max, allowPagination: shouldPaginate }) => {
    const wid = window.Store?.WidFactory?.createWid?.(id);
    const chat = wid ? window.Store?.Chat?.get?.(wid) : null;
    if (!chat?.msgs?.getModelsArray) {
      return { messages: [], cachedCount: 0, loadedCount: 0, loadError: 'CHAT_MESSAGES_UNAVAILABLE' };
    }

    let messages = chat.msgs.getModelsArray();
    const chatCachedCount = messages.length;
    let cacheSource = 'chat';

    // Some WhatsApp Web builds keep more history in the global message
    // collection than in an archived chat's `msgs` window.  Use that
    // already-cached data as a read-only fallback before attempting the
    // version-sensitive pagination hook.  This never asks WhatsApp to fetch
    // new data and never serializes message bodies outside this process.
    const serializeChatId = value => {
      if (typeof value === 'string') return value;
      if (!value || typeof value !== 'object') return '';
      if (typeof value._serialized === 'string') return value._serialized;
      const user = typeof value.user === 'string' ? value.user : '';
      const server = typeof value.server === 'string' ? value.server : '';
      return user && server ? `${user}@${server}` : '';
    };
    const messageBelongsToChat = message => {
      const candidates = [
        message?.chatId,
        message?.id?.remote,
        message?.from,
        message?.to,
      ].map(serializeChatId);
      return candidates.includes(id);
    };
    const globalMessages = window.Store?.Msg?.getModelsArray?.() || [];
    const globalMatches = globalMessages.filter(messageBelongsToChat);
    if (globalMatches.length > messages.length) {
      messages = globalMatches;
      cacheSource = 'global';
    }
    const cachedCount = messages.length;
    let loadError = '';
    if (shouldPaginate) {
      try {
        while (messages.length < max) {
          const loaded = await window.Store.ConversationMsgs.loadEarlierMsgs(chat, chat.msgs);
          if (!loaded || !loaded.length) break;
          messages = [...loaded, ...messages];
        }
      } catch (error) {
        // Some archived conversations refuse historical pagination. The already
        // cached messages are still safe to inspect.
        loadError = String(error);
      }
    } else {
      // Periodic passes must never request older messages. Keep the limitation
      // visible so operators do not mistake a cache refresh for full history.
      loadError = 'HISTORICAL_PAGINATION_UNAVAILABLE';
    }

    const serializeParticipant = value => {
      if (typeof value === 'string') return value;
      if (!value || typeof value !== 'object') return '';
      if (typeof value._serialized === 'string') return value._serialized;
      const user = typeof value.user === 'string' ? value.user : '';
      const server = typeof value.server === 'string' ? value.server : '';
      return user && server ? `${user}@${server}` : '';
    };

    const result = messages
      .filter(message => !message?.isNotification)
      .sort((a, b) => Number(a?.t || 0) - Number(b?.t || 0))
      .slice(-max)
      .map(message => ({
        body: message?.body || '',
        quotedBody: message?.quotedMsg?.body || '',
        // Store models expose `from`/`author` as Wid objects on some
        // WhatsApp Web builds.  Keep the bridge/API boundary JSON-safe and
        // bounded instead of forwarding the internal model object (which
        // FastAPI correctly rejects as a non-string sender with HTTP 422).
        from: serializeParticipant(message?.from),
        author: serializeParticipant(message?.author),
      }));
    return {
      messages: result,
      cachedCount,
      chatCachedCount,
      globalCachedCount: globalMatches.length,
      cacheSource,
      loadedCount: messages.length,
      loadError,
    };
  }, { id: chatId, max: limit, allowPagination });
}

async function processArchiveMessage(message, chat) {
  const fullText = `${message?.body || ''}\n${message?.quotedBody || ''}`.trim();
  const urls = extractUrls(fullText);
  const sender = message?.author || message?.from || 'whatsapp-archive';
  const sourceName = chat?.isGroup
    ? `group:${chat.name || 'archived'}`
    : `direct:${chatNumber(chat)}`;

  for (const url of urls) {
    if (CONFIG.jobUrlOnly && !isJobUrl(url) && !isShortUrl(url)) continue;
    if (alreadySeen(url)) continue;
    markSeen(url);
    await forwardUrl(url, sender, sourceName);
  }

  if (!urls.length && CONFIG.forwardTextPosts && fullText && looksLikeJobText(fullText)) {
    const dedupKey = `text:${fullText}`;
    if (!alreadySeen(dedupKey)) {
      markSeen(dedupKey);
      await forwardTextPost(fullText, sender, sourceName);
    }
  }
}

let _archiveRescanScheduler = null;
let _archiveSchedulerGeneration = 0;
let _archiveScanState = {
  enabled: CONFIG.archiveScanOnStart,
  active: false,
  running: false,
  phase: 'idle',
  mode: 'hydrated_cache_only',
  lastStartedAt: null,
  lastFinishedAt: null,
  lastGroupCount: 0,
  lastMessageCount: 0,
  lastErrorCode: null,
  lastPaginationAvailable: null,
};

function archiveLoadErrorCode(value) {
  if (!value) return null;
  const text = String(value);
  if (/HISTORICAL_PAGINATION_UNAVAILABLE|waitForChatLoading|loadEarlier|ConversationMsgs/i.test(text)) {
    return 'HISTORICAL_PAGINATION_UNAVAILABLE';
  }
  return boundedReasonCode('ARCHIVE_HISTORY_LOAD_FAILED');
}

async function scanArchiveGroup(
  chat,
  { mode = ARCHIVE_SCAN_MODE.STARTUP, stateGeneration = null } = {},
) {
  if (!CONFIG.archiveScanOnStart || !chat?.id) {
    return { messageCount: 0, paginationAvailable: true };
  }
  try {
    const allowPagination = archiveScanAllowsPagination(mode);
    const scan = await fetchRawChatMessages(
      chat.id,
      CONFIG.archiveScanLimit,
      { allowPagination },
    );
    const messages = Array.isArray(scan) ? scan : (scan.messages || []);
    for (const message of messages) await processArchiveMessage(message, chat);
    const errorCode = Array.isArray(scan) ? null : archiveLoadErrorCode(scan.loadError);
    if (stateGeneration === null || stateGeneration === _archiveSchedulerGeneration) {
      _archiveScanState = {
        ..._archiveScanState,
        lastMessageCount: messages.length,
        lastErrorCode: errorCode,
        lastPaginationAvailable: !errorCode,
      };
    }
    const diagnostics = Array.isArray(scan)
      ? ''
      : ` (cached=${scan.cachedCount}, loaded=${scan.loadedCount}`
        + `${scan.chatCachedCount !== undefined ? `, chat=${scan.chatCachedCount}` : ''}`
        + `${scan.globalCachedCount !== undefined ? `, global=${scan.globalCachedCount}` : ''}`
        + `${scan.cacheSource ? `, source=${scan.cacheSource}` : ''}`
        + `${scan.loadError ? `, pagination=${scan.loadError}` : ''})`;
    log('info', `Scanned ${messages.length} recent message(s) from one eligible archive group${diagnostics}.`);
    return {
      messageCount: messages.length,
      paginationAvailable: !errorCode,
      ...(errorCode ? { errorCode } : {}),
    };
  } catch (err) {
    log('warn', `Archive group scan skipped: ${err.message}`);
    return {
      messageCount: 0,
      paginationAvailable: false,
      errorCode: 'ARCHIVE_SCAN_FAILED',
    };
  }
}

function invalidateArchiveRescans() {
  _archiveSchedulerGeneration += 1;
  const scheduler = _archiveRescanScheduler;
  _archiveRescanScheduler = null;
  if (scheduler) scheduler.stop();
  _archiveScanState = {
    ..._archiveScanState,
    active: false,
    running: false,
    phase: 'idle',
  };
}

function scheduleArchiveRescans() {
  invalidateArchiveRescans();
  const generation = _archiveSchedulerGeneration;
  _archiveRescanScheduler = createArchiveScanScheduler({
    enabled: CONFIG.archiveScanOnStart,
    initialDelayMs: CONFIG.archiveRescanDelayMs,
    initialAttempts: CONFIG.archiveRescanAttempts,
    intervalMs: CONFIG.archiveRescanIntervalMs,
    listGroups: listChatMetadataFallback,
    shouldWatchGroup: chat => chat.isGroup && shouldWatchGroup(chat),
    scanGroup: async group => {
      if (!client.pupPage) {
        return { messageCount: 0, paginationAvailable: false, errorCode: 'WHATSAPP_PAGE_UNAVAILABLE' };
      }
      return scanArchiveGroup(group, {
        mode: ARCHIVE_SCAN_MODE.PERIODIC_CACHE,
        stateGeneration: generation,
      });
    },
    onState: next => {
      if (generation !== _archiveSchedulerGeneration) return;
      _archiveScanState = { ..._archiveScanState, ...next, mode: 'hydrated_cache_only' };
    },
  });
  _archiveScanState = {
    ..._archiveScanState,
    enabled: CONFIG.archiveScanOnStart,
  };
  _archiveRescanScheduler.start();
}

function messageChatId(message) {
  const value = message?.fromMe ? message?.to : message?.from;
  if (typeof value === 'string') return value;
  return value?._serialized || '';
}

async function fallbackChatForMessage(message) {
  const id = messageChatId(message);
  if (!id) return null;
  try {
    const chats = await listChatMetadataFallback();
    const matched = chats.find(chat => chat.id === id);
    if (matched) return { ...matched, id: { _serialized: matched.id } };
  } catch (_) {
    // The store may still be syncing; the message will be ignored safely.
  }
  return null;
}

// ── Forward URL to Job Agent ──────────────────────────────────────────────────
async function forwardUrl(url, senderPhone, sourceName) {
  const endpoint = `${CONFIG.agentUrl}/api/ingest`;
  const headers = { 'Content-Type': 'application/json' };
  if (CONFIG.agentToken) headers['Authorization'] = `Bearer ${CONFIG.agentToken}`;

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        url,
        sender: senderPhone || 'whatsapp-bridge',
        source: sourceName,
      }),
      timeout: CONFIG.agentRequestTimeoutMs,
    });

    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      log('info', `Forwarded → ${url.slice(0, 70)}`);
      if (data.added > 0) log('verbose', `  ↳ added: ${data.added}`);
      if (data.skipped > 0) log('verbose', `  ↳ duplicate, skipped`);
      return true;
    } else {
      log('warn', `Agent rejected ${url} — HTTP ${res.status}`);
      return false;
    }
  } catch (err) {
    log('error', `Failed to forward ${url}: ${err.message}`);
    return false;
  }
}

// ── Forward a keyword-matching group TEXT post (no URL) to the Job Agent ─────
async function forwardTextPost(text, senderPhone, sourceName) {
  const endpoint = `${CONFIG.agentUrl}/api/ingest-text`;
  const headers = { 'Content-Type': 'application/json' };
  if (CONFIG.agentToken) headers['Authorization'] = `Bearer ${CONFIG.agentToken}`;

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        text,
        sender: senderPhone || 'whatsapp-bridge',
        source: sourceName,
      }),
      timeout: CONFIG.agentRequestTimeoutMs,
    });

    if (res.ok) {
      log('info', `Forwarded text post → [${sourceName || 'whatsapp'}]`);
      return true;
    } else {
      log('warn', `Agent rejected text post — HTTP ${res.status}`);
      return false;
    }
  } catch (err) {
    log('error', `Failed to forward text post: ${err.message}`);
    return false;
  }
}

// ── Forward a CV PDF sent directly in the owner's 1:1 chat ────────────────────
async function forwardDocument(msg) {
  const endpoint = `${CONFIG.agentUrl}/api/profile/resume`;
  try {
    const media = await msg.downloadMedia();
    if (!media || !media.data) return;

    const form = new FormData();
    const buffer = Buffer.from(media.data, 'base64');
    form.append('file', new Blob([buffer], { type: media.mimetype || 'application/pdf' }),
      media.filename || 'resume.pdf');

    const headers = {};
    if (CONFIG.agentToken) headers['Authorization'] = `Bearer ${CONFIG.agentToken}`;

    // Use Node's built-in fetch (not node-fetch) so the native FormData/Blob
    // multipart encoding is handled correctly.
    const res = await globalThis.fetch(endpoint, { method: 'POST', body: form, headers });
    if (res.ok) {
      log('info', `Forwarded CV document → ${media.filename || 'resume.pdf'}`);
    } else {
      log('warn', `Agent rejected resume upload — HTTP ${res.status}`);
    }
  } catch (err) {
    log('error', `Failed to forward document: ${err.message}`);
  }
}

// ── Heartbeat — lets the dashboard know the bridge is alive ──────────────────
let _heartbeatTimer = null;
let _watchedGroupCount = 0;

async function sendHeartbeat() {
  if (!CONFIG.agentUrl) return;
  const headers = { 'Content-Type': 'application/json' };
  if (CONFIG.agentToken) headers['Authorization'] = `Bearer ${CONFIG.agentToken}`;
  try {
    await fetch(`${CONFIG.agentUrl}/api/bridge/heartbeat`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        id: 'whatsapp-web',
        groups_watched: _watchedGroupCount,
        archive_scan: {
          enabled: Boolean(_archiveScanState.enabled),
          active: Boolean(_archiveScanState.active),
          running: Boolean(_archiveScanState.running),
          phase: _archiveScanState.phase === 'scanning' ? 'scanning' : 'idle',
          mode: 'hydrated_cache_only',
          last_started_at: _archiveScanState.lastStartedAt || null,
          last_finished_at: _archiveScanState.lastFinishedAt || null,
          last_group_count: Number.isFinite(_archiveScanState.lastGroupCount)
            ? Math.max(0, Math.min(10000, Math.floor(_archiveScanState.lastGroupCount))) : 0,
          last_message_count: Number.isFinite(_archiveScanState.lastMessageCount)
            ? Math.max(0, Math.min(500, Math.floor(_archiveScanState.lastMessageCount))) : 0,
          last_error_code: _archiveScanState.lastErrorCode
            ? boundedReasonCode(_archiveScanState.lastErrorCode) : null,
          last_pagination_available: _archiveScanState.lastPaginationAvailable === null
            ? null : Boolean(_archiveScanState.lastPaginationAvailable),
        },
      }),
      timeout: 5000,
    });
    log('verbose', `Heartbeat sent (groups: ${_watchedGroupCount})`);
  } catch (err) {
    log('verbose', `Heartbeat failed: ${err.message}`);
  }
}

function startHeartbeat() {
  if (_heartbeatTimer) clearInterval(_heartbeatTimer);
  sendHeartbeat();  // send immediately on connect
  _heartbeatTimer = setInterval(sendHeartbeat, 60_000);  // then every 60 s
}

function stopHeartbeat() {
  if (_heartbeatTimer) { clearInterval(_heartbeatTimer); _heartbeatTimer = null; }
}

// ── Outbound send server — lets the Python worker request WhatsApp sends ─────
// POST http://127.0.0.1:<SEND_PORT>/send   { to, text, pdf_base64? }
// Guarded by ENABLE_SEND=true + an `Authorization: Bearer <JOB_AGENT_TOKEN>` header.
function toChatId(to) {
  return String(to).includes('@') ? to : `${String(to).replace(/[^\d]/g, '')}@c.us`;
}

function sendJson(res, status, obj) {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(obj));
}

async function handleSendRequest(req, res, body) {
  let payload;
  try {
    payload = JSON.parse(body || '{}');
  } catch {
    sendJson(res, 400, { error: 'invalid JSON body' });
    return;
  }

  const { to, text, pdf_base64: pdfBase64 } = payload;
  if (!to) {
    sendJson(res, 400, { error: 'missing "to"' });
    return;
  }

  try {
    const chatId = toChatId(to);
    if (pdfBase64) {
      const media = new MessageMedia('application/pdf', pdfBase64, 'CV.pdf');
      await client.sendMessage(chatId, media, { caption: text || '' });
    } else {
      await client.sendMessage(chatId, text || '');
    }
    log('info', `Sent WhatsApp message → ${to}${pdfBase64 ? ' (+pdf)' : ''}`);
    sendJson(res, 200, { ok: true });
  } catch (err) {
    log('error', `Send failed for ${to}: ${err.message}`);
    sendJson(res, 500, { error: err.message });
  }
}

function startSendServer() {
  if (!CONFIG.enableSend) {
    log('verbose', 'Send endpoint disabled (ENABLE_SEND != true)');
    return;
  }

  const server = http.createServer((req, res) => {
    if (req.method !== 'POST' || req.url !== '/send') {
      sendJson(res, 404, { error: 'not found' });
      return;
    }

    const auth = req.headers['authorization'] || '';
    const token = auth.startsWith('Bearer ') ? auth.slice(7) : '';
    if (!CONFIG.agentToken || token !== CONFIG.agentToken) {
      sendJson(res, 401, { error: 'unauthorized' });
      return;
    }

    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => { handleSendRequest(req, res, body).catch(err => {
      log('error', `Send handler error: ${err.message}`);
      sendJson(res, 500, { error: err.message });
    }); });
  });

  server.on('error', err => log('error', `Send server error: ${err.message}`));
  server.listen(CONFIG.sendPort, '127.0.0.1', () => {
    log('info', `Send endpoint listening on 127.0.0.1:${CONFIG.sendPort}/send`);
  });
}

// ── Deduplication (bounded hashes, resets on restart) ─────────────────────────
// The index never retains URL/query strings or message bodies. Durable URL
// deduplication is enforced by the local API; this short-lived index only
// prevents repeated forwarding during a bridge session.
const seenArchiveItems = createBoundedDeduper();

function markSeen(value) { seenArchiveItems.add(value); }
function alreadySeen(value) { return seenArchiveItems.has(value); }

// ── WhatsApp client ───────────────────────────────────────────────────────────
const client = new Client({
  ...(CONFIG.waWebVersion ? { webVersion: CONFIG.waWebVersion } : {}),
  authStrategy: new LocalAuth({ dataPath: CONFIG.sessionDir }),
  puppeteer: {
    headless: true,
    protocolTimeout: 60000,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
    ],
  },
  webVersionCache: {
    type: 'remote',
    remotePath: CONFIG.waWebVersionRemotePath,
  },
});

// QR code — shown once on first run
client.on('qr', qr => {
  console.log('\n════════════════════════════════════════════');
  console.log('  Scan this QR code with your WhatsApp:');
  console.log('  Phone → Linked Devices → Link a Device');
  console.log('════════════════════════════════════════════\n');
  qrcode.generate(qr, { small: true });
});

let readyEventReceived = false;
let syncFallbackTriggered = false;
let syncFallbackTimer = null;

client.on('authenticated', () => log('info', 'WhatsApp session authenticated'));
client.on('loading_screen', (percent, message) => {
  log('verbose', `WhatsApp sync ${percent}%${message ? ` (${String(message).slice(0, 40)})` : ''}`);
});
client.on('change_state', state => log('verbose', `WhatsApp connection state: ${String(state)}`));
client.once('ready', () => {
  readyEventReceived = true;
  if (syncFallbackTimer) {
    clearInterval(syncFallbackTimer);
    syncFallbackTimer = null;
  }
  log('verbose', 'WhatsApp library ready event received');
});
client.on('ready', async () => {
  log('info', '─────────────────────────────────────────────');
  log('info', 'WhatsApp bridge is READY');
  log('info', `  Agent URL   : ${CONFIG.agentUrl}`);
  log('info', `  Watch all   : ${CONFIG.watchAllGroups}`);
  log('info', `  Archived only: ${CONFIG.watchArchivedOnly}`);
  log('info', `  Direct chats: ${CONFIG.forwardDirectMessages ? CONFIG.directChatNumbers.length + ' allowlisted' : 'disabled'}`);
  if (!CONFIG.watchAllGroups) {
    log('info', `  Keywords    : ${CONFIG.groupKeywords.join(', ')}`);
  }
  log('info', `  Job URLs only: ${CONFIG.jobUrlOnly}`);
  log('info', '─────────────────────────────────────────────');

  // List currently-watched groups on startup
  try {
    const chats = await client.getChats();
    const groups = chats.filter(c => c.isGroup);
    const watched = groups.filter(g => shouldWatchGroup(g));
    const directChats = chats.filter(c => !c.isGroup && shouldWatchDirectChat(c));
    _watchedGroupCount = watched.length;
    log('info', `Monitoring ${watched.length} / ${groups.length} groups:`);
    for (const g of watched) {
      log('info', `  • ${g.name}`);
      // Process last 5 messages from each group on startup
      if (CONFIG.archiveScanOnStart) {
        await scanArchiveGroup(g, { mode: ARCHIVE_SCAN_MODE.STARTUP });
      } else {
        const messages = await g.fetchMessages({ limit: 5 });
        for (const m of messages) await processMessage(m);
      }
    }
    if (directChats.length) {
      log('info', `Monitoring ${directChats.length} allowlisted direct chat(s)`);
      for (const chat of directChats) {
        const messages = await chat.fetchMessages({ limit: 5 });
        for (const message of messages) {
          await processMessage(message);
        }
      }
    }
  } catch (err) {
    log('warn', `Could not list groups through the normal adapter: ${err.message}`);
    try {
      const fallbackChats = await listChatMetadataFallback();
      const fallbackGroupCount = fallbackChats.filter(c => c.isGroup).length;
      const fallbackArchivedCount = fallbackChats.filter(c => c.isGroup && c.archived).length;
      const fallbackGroups = fallbackChats.filter(c => c.isGroup && shouldWatchGroup(c));
      _watchedGroupCount = fallbackGroups.length;
      log(
        'info',
        `Read-only metadata fallback found ${fallbackGroups.length} eligible group(s)`
          + ` (${fallbackArchivedCount}/${fallbackGroupCount} marked archived).`,
      );
      for (const group of fallbackGroups) {
        await scanArchiveGroup(group, { mode: ARCHIVE_SCAN_MODE.STARTUP });
      }
    } catch (fallbackError) {
      log('warn', `Read-only chat metadata fallback unavailable: ${fallbackError.message}`);
    }
  }

  startHeartbeat();
  scheduleArchiveRescans();
});

client.on('disconnected', reason => {
  log('warn', `Disconnected: ${reason}`);
  // Invalidate any in-flight/queued cache scan before reconnecting. A slow
  // pass may finish asynchronously, so lifecycle generation checks prevent it
  // from publishing state into the next authenticated session.
  invalidateArchiveRescans();
  stopHeartbeat();
  // Attempt reconnect after 10 seconds
  setTimeout(() => {
    log('info', 'Attempting to reconnect…');
    client.initialize().catch(e => log('error', `Reconnect failed: ${e.message}`));
  }, 10_000);
});

// ── Main message handler ──────────────────────────────────────────────────────
async function processMessage(msg) {
  try {
    let chat;
    try {
      chat = await msg.getChat();
    } catch (err) {
      // A single broken group serializer must not drop live URL messages from
      // every other chat. Fall back to the lightweight, read-only metadata
      // path and continue only when the chat can be identified and passes the
      // same allowlist/archive filters.
      chat = await fallbackChatForMessage(msg);
      if (!chat) {
        log('warn', `Ignoring message from an unavailable WhatsApp chat (${String(err)}).`);
        return;
      }
    }

    // ── Forward CV PDFs sent directly in the owner's 1:1 chat ──────────────
    if (CONFIG.forwardOwnerDocs && !chat.isGroup && msg.hasMedia && msg.type === 'document'
        && msg.mimetype === 'application/pdf') {
      await forwardDocument(msg);
      return;
    }

    const isGroup = Boolean(chat.isGroup);
    if (isGroup) {
      if (!shouldWatchGroup(chat)) return;
    } else if (!shouldWatchDirectChat(chat)) {
      // Personal chats are ignored unless an explicit phone allowlist is
      // configured. This keeps private conversations out of the pipeline by
      // default while allowing an operator to forward links from a known
      // self/recruiter chat.
      return;
    }

    const body = msg.body || '';

    // Also check quoted/forwarded message body
    const quotedBody = msg.hasQuotedMsg
      ? (await msg.getQuotedMessage().catch(() => null))?.body || ''
      : '';

    const fullText = `${body}\n${quotedBody}`.trim();
    const urls = extractUrls(fullText);

    if (!urls.length) {
      // No URL in this message — optionally forward it as a text job-post
      // candidate (e.g. "Hiring RF Engineer, WhatsApp 05xxxxxxx").
      if (CONFIG.forwardTextPosts && fullText && looksLikeJobText(fullText)) {
        const dedupKey = `text:${fullText}`;
        if (!alreadySeen(dedupKey)) {
          markSeen(dedupKey);
          const contact = await msg.getContact().catch(() => null);
          const sender = contact?.number || msg.author || msg.from;
          const sourceName = isGroup ? `group:${chat.name}` : `direct:${chatNumber(chat)}`;
          log('info', `[${sourceName}] ${sender} → text post`);
          await forwardTextPost(fullText, sender, sourceName);
        }
      }
      return;
    }

    const contact = await msg.getContact().catch(() => null);
    const sender = contact?.number || msg.author || msg.from;

    for (const url of urls) {
      // Filter: only forward job-board URLs if jobUrlOnly is set
      if (CONFIG.jobUrlOnly && !isJobUrl(url) && !isShortUrl(url)) {
        continue;
      }

      if (alreadySeen(url)) {
        continue;
      }

      markSeen(url);
      const sourceName = isGroup ? `group:${chat.name}` : `direct:${chatNumber(chat)}`;
      log('info', `[${sourceName}] ${sender} → ${url.slice(0, 70)}`);
      await forwardUrl(url, sender, sourceName);
    }

  } catch (err) {
    log('error', `Message handler error: ${err?.message || String(err)}`);
  }
}

client.on('message', processMessage);

// ── Graceful shutdown ─────────────────────────────────────────────────────────
process.on('SIGINT', () => { log('info', 'Shutting down…'); stopHeartbeat(); client.destroy().then(() => process.exit(0)); });
process.on('SIGTERM', () => { log('info', 'Shutting down…'); stopHeartbeat(); client.destroy().then(() => process.exit(0)); });

// ── Start ─────────────────────────────────────────────────────────────────────
if (!validateAgentUrl()) {
  process.exit(1);
}
log('info', 'Starting WhatsApp bridge…');
startSendServer();
client.initialize().then(() => {
  // whatsapp-web.js resolves initialize() before the page's asynchronous
  // `hasSynced` event fires.  On a warm LocalAuth profile that event can be
  // emitted just before the listener is attached, leaving the bridge stuck
  // after authentication.  Probe only the boolean connection state and ask
  // the library to run its own callback once when it is already complete.
  const probeSyncState = async () => {
    if (readyEventReceived || syncFallbackTriggered || !client.pupPage) return;
    try {
      const synced = await client.pupPage.evaluate(() => Boolean(
        window.AuthStore?.AppState?.hasSynced
          && typeof window.onAppStateHasSyncedEvent === 'function',
      ));
      if (!synced) return;
      syncFallbackTriggered = true;
      await client.pupPage.evaluate(() => window.onAppStateHasSyncedEvent());
    } catch (error) {
      log('verbose', `WhatsApp sync probe deferred: ${String(error).slice(0, 120)}`);
    }
  };
  syncFallbackTimer = setInterval(probeSyncState, 1000);
  probeSyncState();
}).catch(err => {
  log('error', `Failed to start: ${err?.message || String(err)}`);
  if (err?.stack) log('error', err.stack);
  process.exit(1);
});
