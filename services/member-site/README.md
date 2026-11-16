# Member-Site — training site with real member logins and on-site payment

Deployed on the audited AWS host as part of the live-servers set, behind the
nginx gateway at **https://members.911transedu.net/members/** (also
http://52.41.84.116:443/members/, which redirects to the HTTPS name;
host port 18109, loopback only; the gateway is the public entry point).

- Python 3 stdlib only: `http.server`, `sqlite3`, `hashlib.pbkdf2_hmac`,
  `urllib` (BTC rate quote). No pip installs.
- Sign-up / sign-in / sign-out with salted PBKDF2 password hashes (never
  plaintext) and 30-day HttpOnly session cookies. Duplicate emails and
  wrong passwords are rejected. Members can delete their own account.
- **Preview** (`/members/preview`, public): course outline and descriptive
  material only — CCCE "Transgender Emergency Medicine" (OHA CCCE approved
  Aug 5, 2026 – Aug 5, 2029; Oregon EMT renewal category hours) and EMSPR.
  No protected files are exposed.
- **Card payments (GoDaddy Pay Links)**: per-product Pay Links the
  owner saves on `/members/owner/paylinks`; the buy page renders a
  Pay by card section for each saved link, the buyer records the
  order on the site, and the owner's roster confirmation unlocks.
  Pay Links are one-time per period (no auto-renewal).
- **Recurring subscriptions (Braintree)**: card, Apple Pay,
  Google Pay, and ACH subscriptions run on the SEPARATE Braintree
  service at `/subs/` (`services/braintree-subscriptions`), whose
  webhooks grant and revoke access in this site's database.
- **Stripe: removed completely (2026-11-15)** at the owner's
  direction. An earlier built-in Stripe Checkout integration was
  deleted wholesale — no Stripe code, keys, or routes remain in
  this app.
- **Bitcoin**: offered
  alongside Cash App cash. The app quotes BTC from the USD price
  (CoinGecko), creates a pending order, and instructs the buyer to send
  that BTC amount to Taylor's cashtag `$taylorsprecher2026` INSIDE Cash
  App with their account email in the payment note; the owner confirms
  receipt in the roster to unlock. There is NO on-chain deposit address
  — Taylor confirmed 2026-11-12 that Cash App shows none ("There isn't
  one") — so no address env var exists and none is ever displayed.
- **Cash App — the operative purchase path (2026-11-12)**: same manual
  pattern — a pending order shows the USD amount and Taylor's cashtag
  `$taylorsprecher2026` (public receive handle; override via
  `CASHAPP_CASHTAG`); the owner confirms receipt in the roster to
  unlock. Manual orders (Bitcoin + Cash App + GoDaddy) share one
  log, and Cash App is the standing no-fee payment method.
- **Purchase pages** (`/members/buy/ccce`, `/members/buy/emspr`,
  public): stable per-product buy pages showing the price, the
  cashtag, the note guidance (put your account email in the Cash App
  payment note), and the owner-confirmation unlock; signed-in members
  get the order button on the page. The public training site's buy
  buttons point at these URLs (2026-11-12 sales decision).
- **Keys**: this app holds no payment-processor keys at all. The
  Braintree service keeps its own credentials in its own
  environment (see `services/braintree-subscriptions/README.md`).
- **Course contents**: served only to signed-in members with access, from
  `/data/content/` on the `members-data` volume — never a public URL.
- **Course-file upload** (`/members/admin/upload`, owner session only,
  fail-closed 403 for everyone else): the owner stages bundles through
  the upload page (multipart form) or a raw zip POST with `?product=`
  (`ccce` or `emspr`) and `X-Filename`. Zip-only (magic bytes + zipfile
  check), 100 MB cap, written to `/data/content/<product>/` on the
  volume — never into the repo, never committed to git. Uploads are
  logged in SQLite (`uploads` table: owner email, product, filename,
  bytes, timestamp); re-uploading the same filename replaces it.
  Downloads come from `/data/content/<product>/` (legacy top-level
  bundle location still honored) and require sign-in + product access.
  The real bundle is staged by the owner after go-live from the master
  on file (`~/workspace/your_files/CCCE_Course_Files.zip`, MD5
  d456b0c6bf3a20718b02a64313d6a224) — no Drive download needed.
- **Teach-Back collection** (`teachback` upload target): individual
  teach-back documents (.zip/.pdf/.md/.txt/.docx) staged under
  `/data/content/teachback/` and served file-by-file at
  `/members/content/teachback[/<filename>]`. Design decision
  (2026-11-11): the collection unlocks with **CCCE course access** —
  no separate purchase or access row — so the owner's roster
  grant/revoke of CCCE controls it. Teach-backs are never folded into
  `CCCE_Course_Files.zip`; the bundle stays byte-identical.
- **Member uploads — department inbox** (`/members/upload`, any
  signed-in member; anonymous users are redirected to sign-in):
  members send training-related documents (PDF, .doc/.docx, .xls/.xlsx,
  PNG/JPG, ZIP — extension allowlist plus magic-byte check), 25 MB per
  file, filenames sanitized. Files land in a per-member inbox at
  `/data/uploads/<member_id>/` on the `members-data` volume — outside
  `/data/content/`, never in git, never at a public URL. Each member
  sees and downloads only their own uploads
  (`/members/upload/download/<id>`, fail-closed 403 for anyone else);
  the owner sees all uploads grouped by member, with download access,
  in the roster area of the member desk. Uploading the same filename
  twice keeps both copies (`name-2.ext`, …). Deleting an account
  removes its upload rows and files.
- **Connection step (Taylor's app standard, 2026-11-08)**: account
  creation asks for Cellular, Wi-Fi (Local Network), and Bluetooth up
  front. A web page cannot raise iOS native permission prompts, so the
  signup flow and the upload page present this as an explicit
  connection checklist naming all three, with live status only where
  the browser genuinely reports it (online state, connection type,
  Bluetooth availability) — no fake system prompts.
- **Owner** = the member account registered with
  `taylorsprecher@gmail.com`. The roster (grant/revoke, order
  confirmation) is fail-closed to everyone else. Until the owner
  registers, no manual grants are possible.
- Persistence: SQLite at `/data/members.db` on the `members-data` named
  Docker volume — survives container recreation and redeploys.
- History: mirrors the Muse `training-site-member-logins` app (which
  saves accounts but cannot be shared publicly). Taylor chose 2026-11-11
  to host this version on the AWS server for a public address, then moved
  payment on-site ("Take on that site itself and remove GoDaddy" — that
  change applied to this member site only at the time). On 2026-11-12
  Taylor extended the decision to ALL sales ("Sales on our own site —
  buyers pay through members.911transedu.net, no GoDaddy links"): this
  member site is now the sales channel for the CCCE course and EMSPR,
  with Cash App as the operative payment path, and the public training
  site's buy buttons repoint to the `/members/buy/<key>` pages here.
