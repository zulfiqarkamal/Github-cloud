/**
 * Cloudflare Worker — TradingView → GitHub Actions webhook proxy
 *
 * Deploy once at:
 *   https://dash.cloudflare.com → Workers & Pages → Create Worker
 *
 * Required Worker environment variables (set in CF dashboard under Settings → Variables):
 *   WEBHOOK_SECRET   — a random string you also put into the TradingView alert message
 *   GITHUB_TOKEN     — a GitHub PAT with `repo` scope (or fine-grained: actions:write on this repo)
 *   GITHUB_OWNER     — zulfiqarkamal
 *   GITHUB_REPO      — github-cloud
 *   DRY_RUN          — "true" or "false" (controls whether the autotrader places orders)
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }

    // Validate shared secret embedded in the alert payload.
    if (!env.WEBHOOK_SECRET || body.secret !== env.WEBHOOK_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const rule = body.rule ?? "unknown";
    const ticker = body.ticker ?? "UNKNOWN";
    const close = String(body.close ?? "0");
    const dryRun = env.DRY_RUN !== "false"; // default to dry-run for safety

    // Trigger GitHub Actions workflow_dispatch
    const ghUrl = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/tradingview-webhook.yml/dispatches`;

    const ghResponse = await fetch(ghUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "TradingView-Webhook-Proxy/1.0",
      },
      body: JSON.stringify({
        ref: "main",
        inputs: {
          rule,
          ticker,
          close,
          dry_run: String(dryRun),
        },
      }),
    });

    if (!ghResponse.ok) {
      const text = await ghResponse.text();
      console.error(`GitHub API error ${ghResponse.status}: ${text}`);
      return new Response(`GitHub API error: ${ghResponse.status}`, {
        status: 502,
      });
    }

    // GitHub returns 204 No Content on success
    return new Response(
      JSON.stringify({ ok: true, rule, ticker, close }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  },
};
