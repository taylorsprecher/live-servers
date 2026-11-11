# Member-Site — training site with real member logins and on-site payment

Deployed on the audited AWS host as part of the live-servers set, behind the
nginx gateway at **http://52.41.84.116:443/members/** (host port 18109,
loopback only; the gateway is the public entry point).

- Python 3 stdlib only: `http.server`, `sqlite3`, `hashlib.pbkdf2_hmac`,
  `urllib` (Stripe REST API + BTC rate quote). No pip installs.
- Sign-up / sign-in / sign-out with salted PBKDF2 password hashes (never
  plaintext) and 30-day HttpOnly session cookies. Duplicate emails and
  wrong passwords are rejected. Members can delete their own account.
- **Preview** (`/members/preview`, public): course outline and descriptive
  material only — CCCE "Transgender Emergency Medicine" (OHA CCCE approved
  Aug 5, 2026 – Aug 5, 2029; Oregon EMT renewal category hours) and EMSPR.
  No protected files are exposed.
- **Card / wallet checkout (Stripe)**: Stripe-hosted Checkout pages.
  `payment_method_types` is not restricted, so card, Google Pay and Apple
  Pay surface when enabled in the Stripe Dashboard and supported by the
  buyer's device (Apple Card pays as a card / via Apple Pay; hosted
  Checkout means no Apple Pay domain verification on this host). Payment
  unlocks automatically via the signature-verified webhook
  `POST /members/stripe/webhook` (`checkout.session.completed`).
- **Bitcoin**: separate path (Stripe does not process BTC). The app quotes
  BTC from the USD price (CoinGecko), creates a pending order showing the
  receive address, and the owner confirms receipt in the roster to unlock.
  Receive address comes from the host env (`BTC_RECEIVE_ADDRESS`); while
  unset, Bitcoin shows as "not yet available".
- **Cash App**: same manual pattern — a pending order shows the USD amount
  and Taylor's cashtag `$taylorsprecher2026` (public receive handle;
  override via `CASHAPP_CASHTAG`); the owner confirms receipt in the
  roster to unlock. Manual orders (Bitcoin + Cash App) share one log.
- **Keys are environment-only** (host `/home/deploy/live-servers/.env`,
  passed through docker-compose): `STRIPE_SECRET_KEY`,
  `STRIPE_WEBHOOK_SECRET`, `BTC_RECEIVE_ADDRESS`. Never hardcoded, never
  committed. Without the Stripe secret key, the site runs normally and
  card checkout shows as "being switched on".
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
  `taylorsprecher@gmail.com`. The roster (grant/revoke, BTC confirmation,
  Stripe payment log) is fail-closed to everyone else. Until the owner
  registers, no manual grants are possible (automatic Stripe unlocks and
  the fail-closed default still work).
- Persistence: SQLite at `/data/members.db` on the `members-data` named
  Docker volume — survives container recreation and redeploys.
- History: mirrors the Muse `training-site-member-logins` app (which
  saves accounts but cannot be shared publicly). Taylor chose 2026-11-11
  to host this version on the AWS server for a public address, then moved
  payment on-site ("Take on that site itself and remove GoDaddy" — that
  change applies to this member site only, not the public training site).
