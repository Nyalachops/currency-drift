# Currency Drift

A weekly, one-glance read on ten currencies from an SGD base. Every Saturday
morning (05:30 Singapore) it re-scores the basket on **trend**, **carry** and
**value**, redraws the dashboard, emails the digest, and — only when something
actually shifts — pings WhatsApp.

This is a drift gauge, not a trading signal, and it never touches your
holdings or the Value Gate verdicts.

---

## Install — three steps, once

**1 · Create the repo.**
On GitHub: *New repository* → name it `currency-drift` → Public → Create.
Then *Add file → Upload files* and drag in everything from this folder
(including the `.github` and `data` folders — keep the structure). Commit.

**2 · Turn on the page.**
*Settings → Pages* → under **Build and deployment** choose
**Deploy from a branch**, branch **main**, folder **/ (root)**. Save.
A minute later your dashboard lives at
`https://YOUR-USERNAME.github.io/currency-drift/`.
Paste that address into `config.json` as `page_url` (edit the file right in
GitHub) so the emails and WhatsApp pings can link to it.

**3 · Give it keys.**
*Settings → Secrets and variables → Actions → New repository secret*, five times:

| Secret | Value |
| --- | --- |
| `EMAIL_USER` | the Gmail address that sends (same one the screener uses) |
| `EMAIL_PASS` | that account's Gmail **app password** (same as the screener) |
| `EMAIL_TO` | where the digest lands (optional — defaults to EMAIL_USER) |
| `CALLMEBOT_PHONE` | your WhatsApp number from the creds file |
| `CALLMEBOT_APIKEY` | your CallMeBot key from the creds file |

Then: *Actions* tab → **weekly** → **Run workflow**. That maiden run pulls
live ECB rates, tries the BIS feeds for real REER and policy rates, sends the
first digest, and commits a fresh page. After that it runs itself every
Saturday.

---

## Reading the page

- **Three verdict tiles** — Increase / Watch / Reduce, with a one-line market
  reason. Pegged HKD never occupies a tile.
- **The wheel** — spoke length = composite strength vs the basket; dot size =
  carry; dot colour = valuation (teal cheap · grey fair · coral rich); dashed
  ghost = pegged. The core is the **drift meter**: how much the ranking
  churned this week (calm · lively · volatile).
- **The table** — lens scores 0–10, the week's move vs SGD, and a real
  12-week composite sparkline.
- **Alerts** fire on: entering/leaving the top or bottom 3 · a composite
  flipping sign · a weekly move beyond ±1.5% vs SGD. Email goes out every
  Saturday regardless; WhatsApp only speaks when an alert fires.

## Honest small print

- Until the maiden run, the shipped page is built from the bundled ECB
  snapshot and is stamped with its data date in the footer.
- **Value** uses BIS real effective exchange rates when reachable; otherwise
  a 10-year nominal proxy. The footer always says which mode you're looking at.
- **Carry** uses each currency's policy rate (SGD uses 3M SORA) — refreshed
  from BIS when reachable, else the seeded table in `config.json` (seeded
  08 Aug 2026; GBP/CNY/ZAR/HKD marked medium-confidence there). HKD's posted
  base rate overstates what HKD cash actually earns when HIBOR trades soft —
  treat its carry dot generously.
- Backfilled sparkline history assumes today's carry; from launch onwards the
  robot records true history week by week.
- Weights (40 value / 30 trend / 30 carry), the ±1.5% band and the drift
  bands all live in `config.json`. Costs $0/month to run.
