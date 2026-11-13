# TradingView → GitHub Actions webhook setup

This guide wires TradingView alerts to your trading agent in ~15 minutes.
No server required — a free Cloudflare Worker acts as the proxy.

## How it works

```
TradingView alert
      │  POST JSON body
      ▼
Cloudflare Worker  (validates secret, free tier)
      │  POST /repos/.../dispatches
      ▼
GitHub Actions  workflow_dispatch
      │  runs autotrader.py --signal-rule=... --signal-ticker=...
      ▼
alpaca paper/live account
```

## Step 1 — Create a webhook secret

Generate a random string (e.g. `openssl rand -hex 20` in a terminal).
Keep it — you will use it in steps 2 and 4.

## Step 2 — Deploy the Cloudflare Worker

1. Sign up at <https://dash.cloudflare.com> (free).
2. Go to **Workers & Pages → Create application → Create Worker**.
3. Replace the default code with the contents of `cloudflare-worker/worker.js`.
4. Click **Deploy**.
5. Note the worker URL — it looks like `https://your-worker-name.your-subdomain.workers.dev`.

Then add these **Environment Variables** under the worker's **Settings → Variables**:

| Variable | Value |
|---|---|
| `WEBHOOK_SECRET` | the random string from step 1 |
| `GITHUB_TOKEN` | a GitHub PAT (see below) |
| `GITHUB_OWNER` | `zulfiqarkamal` |
| `GITHUB_REPO` | `github-cloud` |
| `DRY_RUN` | `true` _(change to `false` only when you are ready to trade live)_ |

### Creating the GitHub PAT

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Set **Repository access** to `zulfiqarkamal/github-cloud` only.
3. Under **Permissions → Repository permissions**, grant **Actions → Read and write**.
4. Copy the token and paste it as `GITHUB_TOKEN` in Cloudflare.

## Step 3 — Update the Pine script

In `quality-momentum/tradingview_alerts.pine`, replace both occurrences of:
```
YOUR_WEBHOOK_SECRET
```
with your actual secret from step 1.  
The secret travels inside the JSON body — it never appears in the URL.

## Step 4 — Set up TradingView alerts

For each alert you want:

1. Open the chart (e.g. SPY daily).
2. Add the **Quality Momentum alerts** indicator.
3. Click the **Alerts** (clock) icon → **Create alert**.
4. Set **Condition** to the indicator, pick the alert condition  
   (e.g. "Closed below 200-day").
5. Under **Notifications**, enable **Webhook URL** and paste:
   ```
   https://your-worker-name.your-subdomain.workers.dev
   ```
6. Leave the **Message** field exactly as the Pine script generates it  
   (it already contains the JSON with ticker, close, rule, and secret).
7. Save the alert.

## Step 5 — Test end-to-end

### Manual test via curl
```bash
curl -X POST https://your-worker.workers.dev \
  -H "Content-Type: application/json" \
  -d '{"rule":"below_200d","ticker":"SPY","close":530.0,"secret":"YOUR_WEBHOOK_SECRET"}'
```
Expected response: `{"ok":true,"rule":"below_200d","ticker":"SPY","close":"530.0"}`

### Check GitHub Actions
Go to <https://github.com/zulfiqarkamal/github-cloud/actions> and confirm
a **TradingView webhook signal** run appeared.

### Check Alpaca
With `DRY_RUN=true`, the run logs what it *would* do — no real orders.

## Signal rules handled by autotrader.py

| `rule` value | Meaning | Autotrader action |
|---|---|---|
| `below_200d` | SPY crossed below 200-day MA | Applies crash-brake (reduce equity exposure) |
| `above_200d` | SPY crossed above 200-day MA | Removes crash-brake (restore full allocation) |
| `stop` | A holding hit the 20% stop level | Triggers exit check for that ticker |

## Security notes

- The shared secret is **not** in the URL — it is in the POST body.
  TradingView sends the message body verbatim, so the secret stays server-side.
- The Cloudflare Worker never logs the secret value.
- Rotate the secret by updating the CF environment variable and the Pine script
  simultaneously.
- Keep `DRY_RUN=true` until you have verified at least two full end-to-end tests.
