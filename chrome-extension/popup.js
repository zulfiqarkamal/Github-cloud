// TradingView Webhook Wirer — popup.js
// Deploys a Cloudflare Worker and wires env vars so TradingView alerts
// can trigger GitHub Actions via the worker.

const WORKER_NAME = 'tv-webhook';
const GITHUB_OWNER = 'zulfiqarkamal';
const GITHUB_REPO = 'github-cloud';
const WORKER_JS_URL =
  `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/main/cloudflare-worker/worker.js`;

// Fallback worker script if the GitHub file doesn't exist yet.
// Validates WEBHOOK_SECRET, then dispatches a repository_dispatch event
// to GitHub Actions with the TradingView alert payload.
const FALLBACK_WORKER_JS = `
export default {
  async fetch(request, env) {
    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }

    // Verify the shared secret sent in the X-Webhook-Secret header
    const secret = request.headers.get('X-Webhook-Secret');
    if (!secret || secret !== env.WEBHOOK_SECRET) {
      return new Response('Unauthorized', { status: 401 });
    }

    let body;
    try { body = await request.json(); }
    catch { return new Response('Bad JSON', { status: 400 }); }

    // In DRY_RUN mode log the payload but don't call GitHub
    if (env.DRY_RUN === 'true') {
      console.log('[DRY_RUN] payload:', JSON.stringify(body));
      return new Response(JSON.stringify({ dry_run: true, received: body }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }

    // Dispatch a repository_dispatch event so GitHub Actions can pick it up
    const ghResp = await fetch(
      \`https://api.github.com/repos/\${env.GITHUB_OWNER}/\${env.GITHUB_REPO}/dispatches\`,
      {
        method: 'POST',
        headers: {
          Authorization: \`Bearer \${env.GITHUB_TOKEN}\`,
          Accept: 'application/vnd.github+json',
          'Content-Type': 'application/json',
          'User-Agent': 'tv-webhook-worker/1.0',
        },
        body: JSON.stringify({ event_type: 'tradingview_alert', client_payload: body }),
      }
    );

    if (!ghResp.ok) {
      const txt = await ghResp.text();
      return new Response(\`GitHub error \${ghResp.status}: \${txt}\`, { status: 502 });
    }
    return new Response(JSON.stringify({ ok: true }), {
      headers: { 'Content-Type': 'application/json' },
    });
  },
};
`.trim();

// ── helpers ──────────────────────────────────────────────────────────────────

function $(id) { return document.getElementById(id); }

const log = $('log');

function step(msg, state = '') {
  const el = document.createElement('div');
  el.className = 'step' + (state ? ' ' + state : '');
  el.textContent = msg;
  log.appendChild(el);
  el.scrollIntoView({ block: 'nearest' });
  return el;
}

/** Generate a cryptographically random hex secret. */
function randomSecret(bytes = 32) {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  return Array.from(arr).map(b => b.toString(16).padStart(2, '0')).join('');
}

/** Cloudflare Workers API base. */
function cfBase(accountId) {
  return `https://api.cloudflare.com/client/v4/accounts/${accountId}/workers/scripts/${WORKER_NAME}`;
}

/** Wrapper around fetch that throws a readable error on non-2xx. */
async function apiFetch(url, opts, label) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let detail = '';
    try { const j = await r.json(); detail = JSON.stringify(j.errors || j); } catch {}
    throw new Error(`${label} failed (HTTP ${r.status}): ${detail}`);
  }
  return r;
}

// ── main setup flow ───────────────────────────────────────────────────────────

$('setup').addEventListener('click', async () => {
  const cfAccountId = $('cfAccountId').value.trim();
  const cfToken = $('cfToken').value.trim();
  const ghToken = $('ghToken').value.trim();
  const dryRun = $('dryRun').checked;

  if (!cfAccountId || !cfToken || !ghToken) {
    step('Please fill in all three fields.', 'err');
    return;
  }

  $('setup').disabled = true;
  log.innerHTML = '';
  $('results').style.display = 'none';

  try {
    // ── Step 1: generate a random webhook secret ───────────────────────────
    const s1 = step('Generating webhook secret…');
    const webhookSecret = randomSecret();
    s1.textContent = '✓ Webhook secret generated';
    s1.className = 'step ok';

    // ── Step 2: fetch worker.js from GitHub ────────────────────────────────
    const s2 = step('Fetching worker.js from GitHub…');
    let workerJs;
    try {
      const ghRaw = await fetch(WORKER_JS_URL);
      if (ghRaw.ok) {
        workerJs = await ghRaw.text();
        s2.textContent = '✓ worker.js fetched from GitHub';
      } else {
        workerJs = FALLBACK_WORKER_JS;
        s2.textContent = '⚠ worker.js not in repo yet — using built-in fallback';
      }
    } catch {
      workerJs = FALLBACK_WORKER_JS;
      s2.textContent = '⚠ Could not reach GitHub — using built-in fallback';
    }
    s2.className = 'step ok';

    // ── Step 3: deploy worker script to Cloudflare ─────────────────────────
    const s3 = step(`Deploying worker "${WORKER_NAME}" to Cloudflare…`);

    // The CF Workers Upload API uses multipart/form-data with the script as
    // an ES module part and a JSON metadata part specifying the module format.
    const metadata = JSON.stringify({
      main_module: 'worker.js',
      compatibility_date: '2024-01-01',
    });

    const formData = new FormData();
    formData.append(
      'metadata',
      new Blob([metadata], { type: 'application/json' }),
      'metadata.json'
    );
    formData.append(
      'worker.js',
      new Blob([workerJs], { type: 'application/javascript+module' }),
      'worker.js'
    );

    await apiFetch(
      cfBase(cfAccountId),
      {
        method: 'PUT',
        headers: { Authorization: `Bearer ${cfToken}` },
        // Note: do NOT set Content-Type header — browser sets it with boundary automatically
        body: formData,
      },
      'Worker deploy'
    );

    s3.textContent = `✓ Worker "${WORKER_NAME}" deployed`;
    s3.className = 'step ok';

    // ── Step 4: set environment variables on the worker ────────────────────
    const s4 = step('Setting environment variables on the worker…');

    // CF Workers env vars are set via the "Settings" bindings API.
    // We PUT a bindings payload to /settings.
    const bindings = [
      { type: 'secret_text', name: 'WEBHOOK_SECRET', text: webhookSecret },
      { type: 'secret_text', name: 'GITHUB_TOKEN',   text: ghToken },
      { type: 'plain_text',  name: 'GITHUB_OWNER',   text: GITHUB_OWNER },
      { type: 'plain_text',  name: 'GITHUB_REPO',    text: GITHUB_REPO },
      { type: 'plain_text',  name: 'DRY_RUN',        text: dryRun ? 'true' : 'false' },
    ];

    const settingsFormData = new FormData();
    settingsFormData.append(
      'settings',
      new Blob([JSON.stringify({ bindings })], { type: 'application/json' }),
      'settings.json'
    );

    await apiFetch(
      `${cfBase(cfAccountId)}/settings`,
      {
        method: 'PATCH',
        headers: { Authorization: `Bearer ${cfToken}` },
        // No Content-Type header — browser sets it with boundary automatically
        body: settingsFormData,
      },
      'Set env vars'
    );

    s4.textContent = '✓ Environment variables set (WEBHOOK_SECRET, GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO, DRY_RUN)';
    s4.className = 'step ok';

    // ── Step 5: derive and display the Worker URL ──────────────────────────
    const s5 = step('Retrieving your Cloudflare subdomain…');

    // Fetch the account subdomain (e.g. "myaccount") from CF API
    let subdomain = cfAccountId.substring(0, 8); // safe fallback
    try {
      const subR = await fetch(
        `https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/workers/subdomain`,
        { headers: { Authorization: `Bearer ${cfToken}` } }
      );
      if (subR.ok) {
        const subJ = await subR.json();
        subdomain = subJ.result?.subdomain || subdomain;
      }
    } catch {}

    const workerUrl = `https://${WORKER_NAME}.${subdomain}.workers.dev`;
    s5.textContent = `✓ Worker URL: ${workerUrl}`;
    s5.className = 'step ok';

    // ── Done: show results ─────────────────────────────────────────────────
    $('workerUrl').value = workerUrl;
    $('webhookSecret').value = webhookSecret;
    $('results').style.display = 'block';

    step('All done! Paste the Worker URL and secret into your TradingView alert.', 'ok');

  } catch (err) {
    step(`Error: ${err.message}`, 'err');
  } finally {
    $('setup').disabled = false;
  }
});

// ── copy helper (called from inline onclick in HTML) ─────────────────────────
function copyVal(id) {
  const el = $(id);
  el.select();
  navigator.clipboard.writeText(el.value).catch(() => document.execCommand('copy'));
}
