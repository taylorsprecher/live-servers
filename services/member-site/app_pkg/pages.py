"""HTML rendering — every page re-skinned onto the Fleet design system.
Body markup, copy, and logic are the monolith's, moved function-for-function;
the PAGE shell now links the vendored fleet.css and uses the Fleet
header/footer pattern."""
import html
import json
import os
import time
import uuid

from .catalog import COLLECTIONS, COURSES, price_line
from .config import (
    ACCEPT_JS_URL,
    ACH_CHECKOUT_READY,
    AUTHNET_API_LOGIN_ID,
    AUTHNET_CLIENT_KEY,
    RENEWAL_OFFER_READY,
    BASE,
    CASHAPP_CASHTAG,
    CONTENT_DIR,
    MAX_UPLOAD_BYTES,
    MEMBER_UPLOAD_MAX_BYTES,
    OWNER_EMAIL,
)
from .store import (
    _db,
    _lock,
    all_member_uploads,
    any_godaddy_link,
    btc_quote,
    collection_files,
    content_available,
    godaddy_link,
    has_content_access,
    member_access,
    member_uploads,
    staged_files,
)


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{base}/assets/fleet.css">
<style>
 .card {{ background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); padding: 18px; margin: 14px 0; }}
 label {{ display: block; margin: 10px 0 4px; font-weight: 600; font-size: .95rem; }}
 input, select {{ width: 100%; box-sizing: border-box; min-height: 44px; padding: 9px 12px; border-radius: var(--radius-sm); border: 1px solid var(--line); background: var(--surface); color: var(--text); font: inherit; }}
 button, .btn {{ display: inline-flex; align-items: center; justify-content: center; margin-top: 14px; min-height: 44px; padding: 10px 18px; border: 1px solid var(--accent); border-radius: var(--radius-sm); background: var(--accent); color: #fff; font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; }}
 button:hover, .btn:hover {{ background: var(--accent-strong); color: #fff; }}
 button.secondary, .btn.secondary {{ background: var(--surface); border-color: var(--line); color: var(--text); }}
 button.secondary:hover, .btn.secondary:hover {{ border-color: var(--accent); color: var(--accent-strong); }}
 button:disabled {{ opacity: .55; cursor: not-allowed; }}
 .msg {{ border: 1px solid var(--line); border-left: 4px solid var(--warn); background: var(--warn-soft); color: var(--text); padding: 12px 14px; border-radius: var(--radius-sm); }}
 .msg.ok, .ok {{ border-left-color: var(--ok); background: var(--ok-soft); }}
 table {{ width: 100%; border-collapse: collapse; font-size: .94rem; }}
 td, th {{ padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
 .small {{ font-size: .85rem; color: var(--muted); }}
 code {{ font-family: var(--mono); word-break: break-all; }}
 form.inline {{ display: inline; }}
 form.inline button {{ margin-top: 0; min-height: 36px; padding: 5px 10px; font-size: .85rem; }}
</style></head><body>
<header class="fl-header"><div class="fl-header-inner">
<a class="fl-wordmark" href="{base}/">911TransEdu</a>
<span class="fl-service">Members</span>
<nav class="fl-nav"><a href="{base}/">Home</a><a href="{base}/preview">Preview</a><a href="{base}/apps">Apps Hub</a><a href="{base}/desk">Member Desk</a></nav>
</div></header>
<main class="fl-main">
{body}
</main>
<footer class="fl-footer"><div class="fl-footer-inner">
<span>Taylor's Transgender Patient Awareness, LLC</span>
<a href="https://911transedu.net">911transedu.net</a>
</div></footer>
</body></html>"""


def esc(s):
    return html.escape(str(s), quote=True)


def catalog_cards():
    out = ""
    for c in COURSES:
        out += (
            f'<div class="card"><strong>{esc(c["name"])}</strong><br>'
            f'<span class="small">{esc(price_line(c))}</span><br>'
            f'<a class="btn" href="{BASE}/buy/{c["key"]}">Buy — {esc(c["price"])}</a> '
            f'<a class="btn secondary" href="{BASE}/preview">Preview the course</a></div>'
        )
    return out


def connection_step_html():
    """Taylor's standing app standard (2026-11-08): account creation in
    Taylor's apps asks for Cellular, Wi-Fi (Local Network) and Bluetooth
    up front. This is a web app: it cannot raise iOS native permission
    prompts, so the flow presents the step as an explicit checklist
    naming all three, with live status only where the browser genuinely
    reports it (online state, connection type, Bluetooth availability).
    No fake system dialogs."""
    return """
<div class="card"><h2>Connection step — please allow all three</h2>
<p class="small">When you create an account, this training app asks for
the connections it uses up front. A web page cannot open your phone's
system permission dialogs, so when your phone or browser asks, please
allow all three:</p>
<table>
<tr><td><strong>Cellular</strong></td><td>keeps the site, downloads and
uploads working away from Wi-Fi</td></tr>
<tr><td><strong>Wi-Fi (Local Network)</strong></td><td>reaches the
training pages and files on your local network</td></tr>
<tr><td><strong>Bluetooth</strong></td><td>pairs training devices and
accessories</td></tr>
</table>
<p class="small" id="conn-status">Connection status: checking…</p>
<script>
(function () {
  var parts = [navigator.onLine ? "online" : "offline"];
  if (navigator.connection && navigator.connection.effectiveType) {
    parts.push("connection: " + navigator.connection.effectiveType);
  }
  var el = document.getElementById("conn-status");
  function done(bt) {
    if (bt !== null) { parts.push("Bluetooth: " + bt); }
    el.textContent = "Connection status (as reported by this browser): "
      + parts.join(" · ");
  }
  if (navigator.bluetooth && navigator.bluetooth.getAvailability) {
    navigator.bluetooth.getAvailability().then(function (a) {
      done(a ? "available" : "not available on this device/browser");
    }, function () { done(null); });
  } else { done(null); }
})();
</script></div>
"""


def home_page(message="", ok=False):
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    if any_godaddy_link():
        pay_note = (
            "Or pay by credit or debit card through the GoDaddy Pay "
            "Link on each product's buy page — access unlocks when "
            "the owner confirms your payment. "
            "You can also pay with Bitcoin inside Cash App — each "
            "course's buy page shows the Bitcoin amount to send."
        )
    else:
        pay_note = (
            "Or pay with Bitcoin inside Cash App — each course's buy "
            "page shows the exact Bitcoin amount to send to the same "
            "cashtag, with the same email note and owner confirmation."
        )
    body = f"""
<h1>Training Site — Member Sign-In</h1>
<p class="small">Taylor's Transgender Patient Awareness, LLC · 911transedu.net</p>
{msg}
<div class="card"><h2>Sign in</h2>
<form method="post" action="{BASE}/signin">
<label>Email</label><input type="email" name="email" required>
<label>Password</label><input type="password" name="password" required>
<button type="submit">Sign in</button></form></div>
<div class="card"><h2>Create an account</h2>
<form method="post" action="{BASE}/signup">
<label>Full name</label><input type="text" name="name" required>
<label>Email</label><input type="email" name="email" required>
<label>Password (8+ characters)</label><input type="password" name="password" minlength="8" required>
<button type="submit">Create account</button></form></div>
{connection_step_html()}
<h2>Courses</h2>
{catalog_cards()}
<p><a class="btn secondary" href="{BASE}/apps">Member &amp; Server Apps — open the apps hub</a></p>
<p class="small">Sold only on this site. Buy with Cash App: send the
price to {esc(CASHAPP_CASHTAG)} and put your account email in the
payment note — access unlocks once the owner confirms receipt.
{pay_note}</p>
"""
    return PAGE.format(base=BASE, title="Training Site — Members", body=body)


def preview_page():
    if any_godaddy_link():
        card_line = (
            "You can also pay by credit or debit card through the "
            "GoDaddy Pay Link on each product's buy page — access "
            "unlocks when the owner confirms your payment."
        )
    else:
        card_line = (
            "Cash App is the way to buy — follow the four steps above."
        )
    body = f"""
<h1>Course Preview</h1>
<p class="small">Taylor's Transgender Patient Awareness, LLC</p>
<div class="card"><h2>Cultural Competence Continuing Education:
Transgender Emergency Medicine</h2>
<p><strong>Price: $15 USD — every two years, per provider</strong></p>
<p>OHA Cultural Competence Continuing Education (CCCE) approved course —
"Transgender Emergency Medicine", approved August 5, 2026 through
August 5, 2029.</p>
<p>The course covers all Oregon EMT renewal categories (OAR 333-265-0110
Appendix 1):</p>
<table>
<tr><th>Category</th><th>Hours</th></tr>
<tr><td>Trauma</td><td>3</td></tr>
<tr><td>Medical</td><td>3</td></tr>
<tr><td>Pediatric &amp; Obstetric</td><td>3</td></tr>
<tr><td>Airway / Breathing / Cardiology</td><td>4</td></tr>
<tr><td>Culture / Race / Gender / Ethnicity</td><td>2</td></tr>
<tr><td>Miscellaneous</td><td>9</td></tr>
<tr><td><strong>Total</strong></td><td><strong>24</strong></td></tr>
</table>
<p class="small">This preview shows the outline only. The full course
contents are released to member accounts after payment.</p>
<p><a class="btn" href="{BASE}/buy/ccce">Buy the CCCE Course — $15</a></p></div>
<div class="card"><h2>EMSPR Protocols</h2>
<p><strong>Price: $6,000 USD — every year</strong></p>
<p>The EMSPR protocol set for EMS agencies and training programs.
Full contents are released to member accounts after payment.</p>
<p><a class="btn" href="{BASE}/buy/emspr">Buy EMSPR Protocols — $6,000</a></p></div>
<div class="card"><h2>How to buy</h2>
<p>Both products are sold only on this site, through Cash App:</p>
<ol>
<li>Create your free member account with your email.</li>
<li>In Cash App, send the product price to
<strong><code>{esc(CASHAPP_CASHTAG)}</code></strong>.</li>
<li>Put <strong>your account email</strong> in the Cash App payment
note — that is how your payment is matched to your account.</li>
<li>Access unlocks as soon as the owner confirms receipt — your
download then appears on your Member Desk.</li>
</ol>
<p class="small">You can also pay with <strong>Bitcoin inside Cash
App</strong>: each product's buy page shows the exact Bitcoin amount
to send to the same cashtag, with the same account-email note and
owner confirmation.</p>
<p class="small">{card_line}</p></div>
<p><a class="btn" href="{BASE}/">Sign in / Create account</a></p>
"""
    return PAGE.format(base=BASE, title="Course Preview", body=body)


def apps_account_card(member):
    """User accounts on the Apps Hub (Taylor, 2026-11-15: "User
    accounts needs to be on the app" / "They need to be able to sign
    into their own profile on app"). Signed-out visitors get the
    sign-in and create-account forms (the site's existing endpoints);
    signed-in members get their own profile: name, email, course
    access, pending orders, and links to their desk and uploads."""
    if member is None:
        return f"""
      <section class="acct" aria-labelledby="acct-h">
        <div class="acct-card">
          <h2 id="acct-h">Your Account</h2>
          <p class="acct-note">Sign in to your own profile — your courses,
          downloads and orders — right here on the app.</p>
          <form method="post" action="{BASE}/signin">
            <label>Email</label>
            <input type="email" name="email" required autocomplete="email">
            <label>Password</label>
            <input type="password" name="password" required autocomplete="current-password">
            <button type="submit">Sign in</button>
          </form>
          <details>
            <summary>New here? Create your account</summary>
            <form method="post" action="{BASE}/signup">
              <label>Full name</label>
              <input type="text" name="name" required autocomplete="name">
              <label>Email</label>
              <input type="email" name="email" required autocomplete="email">
              <label>Password (8+ characters)</label>
              <input type="password" name="password" minlength="8" required autocomplete="new-password">
              <button type="submit">Create account</button>
            </form>
            <p class="acct-note">Account setup standard: when you create an
            account, this app asks for Cellular, Wi-Fi (Local Network) and
            Bluetooth up front — please allow all three when your phone asks.</p>
          </details>
        </div>
      </section>
"""
    granted = member_access(member["id"])
    with _lock:
        pending = _db.execute(
            "SELECT COUNT(*) AS n FROM manual_orders WHERE member_id = ? "
            "AND status = 'pending'",
            (member["id"],),
        ).fetchone()["n"]
    rows = ""
    for c in COURSES:
        if c["key"] in granted:
            if content_available(c):
                right = f'<a href="{BASE}/content/{c["key"]}">Download</a>'
            else:
                right = "<strong>Access granted</strong>"
        else:
            right = f'<a href="{BASE}/buy/{c["key"]}">Buy — {esc(c["price"])}</a>'
        rows += (
            f'<li><span>{esc(c["short"])}</span>{right}</li>'
        )
    pending_note = (
        f'<p class="acct-note">{pending} payment(s) pending owner '
        "confirmation.</p>"
        if pending
        else ""
    )
    owner_link = (
        f'<a class="acct-btn ghost" href="{BASE}/owner/paylinks">Pay Links</a>'
        if member["email"].lower() == OWNER_EMAIL
        else ""
    )
    return f"""
      <section class="acct" aria-labelledby="acct-h">
        <div class="acct-card">
          <h2 id="acct-h">Your Profile</h2>
          <p class="acct-note">Signed in as <strong>{esc(member["name"])}</strong>
          · {esc(member["email"])}</p>
          <ul class="acct-courses">{rows}</ul>
          {pending_note}
          <div class="acct-actions">
            <a class="acct-btn" href="{BASE}/desk">Open my Member Desk</a>
            <a class="acct-btn ghost" href="{BASE}/upload">My uploads</a>
            {owner_link}
            <form method="post" action="{BASE}/signout">
              <button type="submit" class="ghost">Sign out</button>
            </form>
          </div>
        </div>
      </section>
"""


def apps_page(member=None):
    """Member & Server Apps hub — a standalone page (its own design;
    its live service checks run in the visitor's browser against the
    public service subdomains). Served from apps.html beside this
    file so the markup stays out of the code; the Dockerfile copies
    it into the image next to app.py. The ACCOUNT-CARD marker in the
    markup is replaced with the visitor's account/profile card, and
    the PAY-NOTE marker with the payment note for the card state in
    force: when the owner has saved a GoDaddy Pay Link, the note
    points buyers at it; otherwise it says Cash App works today."""
    try:
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps.html"),
            encoding="utf-8",
        ) as fh:
            page = fh.read()
    except OSError:
        return (
            "<h1>Apps hub unavailable</h1>"
            "<p>The apps page is missing on the server.</p>"
        )
    if any_godaddy_link():
        pay_note = (
            "Or pay by credit or debit card through the GoDaddy Pay "
            "Link on each product's buy page — access unlocks when "
            "the owner confirms your payment."
        )
    else:
        pay_note = (
            "To buy by card, look for the GoDaddy Pay Link on each "
            "product's buy page — Cash App works today."
        )
    return page.replace(
        "<!-- ACCOUNT-CARD -->", apps_account_card(member)
    ).replace("<!-- PAY-NOTE -->", pay_note).replace(
        "<!-- FLEET-CSS -->",
        f'<link rel="stylesheet" href="{BASE}/assets/fleet.css">',
    )


def connectivity_page():
    """Connectivity — the phone app that keeps Cellular, Wi-Fi and
    Bluetooth in one place: a live reachability watch against this
    server's /health, guided radio tests, and a timestamped timeline
    kept on the phone. Served from connectivity.html beside this
    file, same pattern as apps_page; public like the buy pages so
    the phone can install it to the Home Screen without a login."""
    try:
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "connectivity.html"),
            encoding="utf-8",
        ) as fh:
            page = fh.read()
    except OSError:
        return (
            "<h1>Connectivity unavailable</h1>"
            "<p>The connectivity page is missing on the server.</p>"
        )
    return page.replace(
        "<!-- FLEET-CSS -->",
        f'<link rel="stylesheet" href="{BASE}/assets/fleet.css">',
    )


def ssk_page():
    """SSK Python — the phone app that finds the SSK portable cloud
    drive: a local-network sweep for the drive's web UI / FTP / SMB
    services plus the USB-C disk-alive steps, and the download for
    its BeeWare Briefcase project (built 2026-11-22 at Taylor's
    direction; the iOS packaging phases run on the Mac). Served
    from ssk.html beside this file, same pattern as
    connectivity_page; public like the buy pages."""
    try:
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ssk.html"),
            encoding="utf-8",
        ) as fh:
            page = fh.read()
    except OSError:
        return (
            "<h1>SSK Python unavailable</h1>"
            "<p>The SSK Python page is missing on the server.</p>"
        )
    return page.replace(
        "<!-- FLEET-CSS -->",
        f'<link rel="stylesheet" href="{BASE}/assets/fleet.css">',
    )


def cport_page():
    """C-Port Python — the installable CPython package for the
    C-Port serial protocol (C extension cserialproto + pipeline),
    extracted from Taylor's C-Port_Python.docx and built, tested
    and installed 2026-11-22 at Taylor's direction. Served from
    cport.html beside this file, same pattern as ssk_page; public
    like the buy pages. The fleet cport service keeps running the
    pure-Python pipeline — this page changes nothing live."""
    try:
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cport.html"),
            encoding="utf-8",
        ) as fh:
            page = fh.read()
    except OSError:
        return (
            "<h1>C-Port Python unavailable</h1>"
            "<p>The C-Port Python page is missing on the server.</p>"
        )
    return page.replace(
        "<!-- FLEET-CSS -->",
        f'<link rel="stylesheet" href="{BASE}/assets/fleet.css">',
    )


def words_page():
    """Words Generator — the Python tool that drafts words from
    Taylor's own notes (order-2 Markov chain over the notes'
    vocabulary), built 2026-11-22 at Taylor's direction toward
    the 95,000-word Ulysses writing goal. Served from words.html
    beside this file, same pattern as ssk_page; public like the
    buy pages. The tool itself downloads at
    /members/words/words_generator.py."""
    try:
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "words.html"),
            encoding="utf-8",
        ) as fh:
            page = fh.read()
    except OSError:
        return (
            "<h1>Words Generator unavailable</h1>"
            "<p>The Words Generator page is missing on the server.</p>"
        )
    return page.replace(
        "<!-- FLEET-CSS -->",
        f'<link rel="stylesheet" href="{BASE}/assets/fleet.css">',
    )


def _ach_section(course, member, amount):
    """ACH (eCheck) section for the buy page — "" unless the
    checkout is configured (config.ACH_CHECKOUT_READY: the eCheck
    switch plus the Accept.js identifiers), the same
    exists-only-while-configured pattern as the GoDaddy section.

    The bank fields carry NO name attributes and sit outside any
    form: nothing on this page can submit them to our server. The
    inline script hands them to Accept.js (Accept.dispatchData
    with bankData), which exchanges them at Authorize.net for a
    one-time payment nonce; only the nonce, the product key, the
    member's account email, an idempotency key, and the
    authorization assertion are posted — to the payment ledger's
    public checkout route, which charges the server-side catalog
    amount. The pay button stays disabled until the WEB debit
    authorization box is checked.

    When config.RENEWAL_OFFER_READY holds (the renewal switch is on
    as well), the section also renders the optional auto-renew
    checkbox — the recurring debit authorization — and the script
    posts auto_renew=true with the checkout; the ledger enrolls the
    buyer from the completed charge and the result message shows
    the one-time cancel code."""
    if not ACH_CHECKOUT_READY:
        return ""
    if member is not None and course["key"] in member_access(member["id"]):
        return ""
    if member is None:
        return (
            '<div class="card"><h2>Or pay by bank account — ACH</h2>'
            '<p class="small">You can also pay by bank account (ACH) '
            "once you sign in — your account comes first, because "
            "your account email is how the payment is matched.</p></div>"
        )
    script = """<script>
(function () {
  var btn = document.getElementById("ach-pay");
  var auth = document.getElementById("ach-auth");
  var msg = document.getElementById("ach-msg");
  function show(text, ok) {
    msg.textContent = text;
    msg.className = ok ? "msg ok" : "msg";
    msg.style.display = "block";
  }
  auth.addEventListener("change", function () { btn.disabled = !auth.checked; });
  btn.addEventListener("click", function () {
    if (!auth.checked) { return; }
    btn.disabled = true;
    var bankData = {
      accountNumber: document.getElementById("ach-account").value,
      routingNumber: document.getElementById("ach-routing").value,
      nameOnAccount: document.getElementById("ach-name").value,
      accountType: document.getElementById("ach-type").value
    };
    var secureData = {
      authData: { apiLoginID: __LOGIN__, clientKey: __CLIENT_KEY__ },
      bankData: bankData
    };
    Accept.dispatchData(secureData, function (response) {
      if (response.messages.resultCode === "Error") {
        var parts = [];
        for (var i = 0; i < response.messages.message.length; i++) {
          parts.push(response.messages.message[i].text);
        }
        show("Your bank details were not accepted: " + parts.join(" "), false);
        btn.disabled = false;
        return;
      }
      document.getElementById("ach-account").value = "";
      document.getElementById("ach-routing").value = "";
      var payload = {
        product: __PRODUCT__,
        nonce: response.opaqueData.dataValue,
        descriptor: response.opaqueData.dataDescriptor,
        email: __EMAIL__,
        authorization: true,
        auto_renew: (document.getElementById("ach-renew") || {}).checked === true,
        account_type: bankData.accountType,
        idempotency_key: __IDEM__
      };
      fetch("/ledger/checkout/echeck", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(function (r) {
        return r.json().then(function (doc) { return { doc: doc }; });
      }).then(function (res) {
        var doc = res.doc || {};
        if (doc.error) { show(doc.error, false); btn.disabled = false; return; }
        var out = doc.echeck_checkout || {};
        if (out.approved) {
          var text = "Payment submitted — thank you. eCheck payments take " +
               "several business days to settle; your access unlocks " +
               "as soon as the payment is confirmed.";
          if (payload.auto_renew) {
            var rn = out.auto_renew || {};
            if (rn.enrolled && rn.cancel_token) {
              text += " Auto-renewal is ON. Your cancel code is " +
                rn.cancel_token + " — save it: with your account email " +
                "and this code you can cancel renewal anytime.";
            } else if (rn.enrolled) {
              text += " Auto-renewal is ON for this purchase.";
            } else {
              text += " Auto-renewal could not be set up — your purchase " +
                "is complete; email us to enable it.";
            }
          }
          show(text, true);
        } else if (out.deduplicated) {
          show(out.message || "This payment was already submitted.", true);
        } else {
          show(out.message || "The payment was not approved.", false);
          btn.disabled = false;
        }
      }).catch(function () {
        show("Something went wrong sending the payment. Please try " +
             "again, or use Cash App above.", false);
        btn.disabled = false;
      });
    });
  });
})();
</script>"""
    script = (
        script.replace("__LOGIN__", json.dumps(AUTHNET_API_LOGIN_ID))
        .replace("__CLIENT_KEY__", json.dumps(AUTHNET_CLIENT_KEY))
        .replace("__PRODUCT__", json.dumps(course["key"]))
        .replace("__EMAIL__", json.dumps(member["email"]))
        .replace("__IDEM__", json.dumps(uuid.uuid4().hex))
    )
    renew_block = ""
    if RENEWAL_OFFER_READY:
        # The recurring WEB debit authorization — the payment
        # ledger's renewal.AUTHORIZATION_VERSION "recurring-web-v1";
        # wording changes must land in both places together.
        # Separate from, and optional beside, the one-time
        # authorization below: the ledger enrolls the buyer only
        # when the checkout posts auto_renew=true.
        renew_block = f"""
<label style="font-weight:400"><input id="ach-renew" type="checkbox" style="width:auto;min-height:0;margin-right:8px">Auto-renew: I also authorize Taylor's Transgender Patient Awareness, LLC to debit the same bank account {amount} {course['cadence']} to renew this purchase automatically, starting when the current term ends, until I cancel. I can cancel anytime before a renewal date — with the cancel code shown after checkout, or by emailing taylor_sprecher@911transedu.net.</label>"""
    return f"""
<div class="card"><h2>Or pay by bank account — ACH</h2>
<p>Pay {amount} straight from your bank account (eCheck). Your
routing and account numbers go only to Authorize.net, the payment
processor — never to this site: your browser exchanges them for a
one-time payment token, and that token is all this site ever sees.</p>
<label for="ach-name">Name on account</label>
<input id="ach-name" type="text" autocomplete="off" maxlength="22">
<label for="ach-routing">Routing number</label>
<input id="ach-routing" type="text" inputmode="numeric" autocomplete="off" maxlength="9">
<label for="ach-account">Account number</label>
<input id="ach-account" type="text" inputmode="numeric" autocomplete="off" maxlength="17">
<label for="ach-type">Account type</label>
<select id="ach-type"><option value="checking">Checking</option><option value="savings">Savings</option></select>
<label style="font-weight:400"><input id="ach-auth" type="checkbox" style="width:auto;min-height:0;margin-right:8px">I authorize Taylor's Transgender Patient Awareness, LLC to debit my bank account for this purchase — a one-time eCheck debit of {amount}.</label>{renew_block}
<p class="small">eCheck payments take several business days to
settle. Access unlocks as soon as the payment is confirmed.</p>
<button id="ach-pay" type="button" disabled>Pay {amount} by bank account</button>
<p id="ach-msg" style="display:none"></p>
</div>
<script src="{esc(ACCEPT_JS_URL)}" charset="utf-8"></script>
{script}"""


def buy_page(course, member=None, message="", ok=False):
    """Stable public purchase page for one product: /members/buy/<key>.

    Cash App is the operative way to buy (2026-11-12 sales decision).
    The page always shows the price, the cashtag, the note guidance
    (the buyer's account email), and the owner-confirmation unlock.
    Bitcoin is offered alongside Cash App cash (Taylor, 2026-11-12):
    sent inside Cash App to the same cashtag at the live-quoted BTC
    amount — no on-chain address exists or is shown.
    Card subscriptions (card, Apple Pay, Google Pay, ACH) run on
    this page presents the manual rails — Cash App, Bitcoin inside Cash App, and a
    GoDaddy Pay Link when the owner has saved one. Signed-in
    members get the order buttons on this page; everyone else
    gets the sign-in/create-account step first (their account
    email is the identity the payment note must carry)."""
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    amount = f"${course['cents'] / 100:,.2f}"
    if member is not None and course["key"] in member_access(member["id"]):
        action = (
            "<p><strong>You already have access to this product.</strong></p>"
            f'<p><a class="btn" href="{BASE}/content/{course["key"]}">'
            "Download the full contents</a></p>"
        )
    elif member is not None:
        with _lock:
            pending = _db.execute(
                "SELECT * FROM manual_orders WHERE member_id = ? AND "
                "course_key = ? AND method = 'cashapp' AND status = 'pending' "
                "ORDER BY created_at",
                (member["id"], course["key"]),
            ).fetchall()
        if pending:
            o = pending[-1]
            action = (
                f'<p>Your Cash App order #{o["id"]} is recorded: send '
                f"<strong>{esc(o['amount_text'])}</strong> to "
                f"<code>{esc(CASHAPP_CASHTAG)}</code> in Cash App with your "
                "account email in the payment note. Access unlocks as soon "
                "as the owner confirms receipt.</p>"
            )
        else:
            action = (
                f'<form method="post" action="{BASE}/cashapp/order">'
                f'<input type="hidden" name="course_key" value="{course["key"]}">'
                f'<button type="submit">Record my Cash App order — {amount}</button></form>'
                '<p class="small">Record the order when you send the '
                "payment — it is what the owner confirms against.</p>"
            )
    else:
        action = (
            f'<p><a class="btn" href="{BASE}/">Sign in / Create your free '
            "account</a></p>"
            '<p class="small">Your account comes first — the email on it '
            "is what you put in the Cash App note, and it is the account "
            "that receives the access.</p>"
        )
    per_person = (
        "<p>Course access is <strong>$15 per person</strong> — each "
        "buyer needs their own account and payment.</p>"
        if course["key"] == "ccce"
        else ""
    )
    # Card via GoDaddy Pay Link (Taylor, 2026-11-15: "Use GoDaddy
    # Pay Links for cards"). The section exists only while the
    # owner has saved a link for this product on the owner Pay
    # Links page — the buy page is built around the link. The
    # buyer pays on GoDaddy's page, records the order here, and
    # access unlocks when the owner confirms the payment in the
    # roster: the Cash App flow, on a card rail. Pay Links are
    # one-time payments, so the cadence price is paid per period
    # and does not auto-renew — the copy says so.
    godaddy_section = ""
    gd_link = godaddy_link(course["key"])
    if gd_link and (
        member is None or course["key"] not in member_access(member["id"])
    ):
        if member is not None:
            with _lock:
                gd_pending = _db.execute(
                    "SELECT * FROM manual_orders WHERE member_id = ? AND "
                    "course_key = ? AND method = 'godaddy' AND "
                    "status = 'pending' ORDER BY created_at",
                    (member["id"], course["key"]),
                ).fetchall()
            if gd_pending:
                o = gd_pending[-1]
                gd_action = (
                    f'<p>Your card order #{o["id"]} is recorded. Access '
                    "unlocks as soon as the owner confirms your GoDaddy "
                    "payment.</p>"
                )
            else:
                gd_action = (
                    f'<form method="post" action="{BASE}/godaddy/order">'
                    f'<input type="hidden" name="course_key" value="{course["key"]}">'
                    f'<button class="secondary" type="submit">Record my '
                    f"card order — {amount}</button></form>"
                    '<p class="small">Record the order when you pay — '
                    "it is what the owner confirms against.</p>"
                )
            gd_body = (
                f"<p>Pay {amount} by credit or debit card on GoDaddy's "
                "secure pay page. Use the same email as your account "
                "here so the payment can be matched. This is a "
                f"one-time card payment for the current period "
                f"({esc(course['cadence'])}); it does not auto-renew.</p>"
                f'<p><a class="btn" href="{esc(gd_link)}" '
                f'target="_blank" rel="noopener">Pay by card — '
                f"{amount}</a></p>"
                f"{gd_action}"
            )
        else:
            gd_body = (
                '<p class="small">You can also pay by credit or debit '
                "card through this product's GoDaddy Pay Link once "
                "you sign in — your account comes first, because your "
                "account email is how the payment is matched.</p>"
            )
        godaddy_section = (
            '<div class="card"><h2>Or pay by card — GoDaddy Pay '
            f"Link</h2>{gd_body}</div>"
        )
    # Bitcoin alongside Cash App cash (Taylor, 2026-11-12: "Offer
    # bitcoin alongside"): paid INSIDE Cash App to the cashtag — Cash
    # App shows Taylor no on-chain deposit address ("There isn't
    # one"), so no address is shown or implied; the buyer sends the
    # quoted BTC amount to the cashtag with their account email in
    # the note and records the order here, exactly like a Cash App
    # cash order. The quote is live from CoinGecko; if the rate is
    # unreachable the option stays named with a check-back note
    # rather than a wrong amount.
    btc_section = ""
    if member is None or course["key"] not in member_access(member["id"]):
        btc_amount, _rate = btc_quote(course["cents"])
        if btc_amount is None:
            btc_section = (
                '<div class="card"><h2>Or pay with Bitcoin — inside '
                "Cash App</h2>"
                '<p class="small">The Bitcoin rate is unavailable right '
                "now — please check back shortly. Cash App cash above "
                "works as usual.</p></div>"
            )
        else:
            if member is not None:
                with _lock:
                    btc_pending = _db.execute(
                        "SELECT * FROM manual_orders WHERE member_id = ? AND "
                        "course_key = ? AND method = 'bitcoin' AND "
                        "status = 'pending' ORDER BY created_at",
                        (member["id"], course["key"]),
                    ).fetchall()
                if btc_pending:
                    o = btc_pending[-1]
                    btc_action = (
                        f'<p>Your Bitcoin order #{o["id"]} is recorded: send '
                        f"<strong>{esc(o['amount_text'])}</strong> to "
                        f"<code>{esc(CASHAPP_CASHTAG)}</code> inside Cash App "
                        "with your account email in the payment note. Access "
                        "unlocks as soon as the owner confirms receipt.</p>"
                    )
                else:
                    btc_action = (
                        f'<form method="post" action="{BASE}/bitcoin/order">'
                        f'<input type="hidden" name="course_key" value="{course["key"]}">'
                        f'<button class="secondary" type="submit">Record my '
                        f"Bitcoin order — {btc_amount} BTC</button></form>"
                        '<p class="small">Record the order when you send the '
                        "payment — it is what the owner confirms against.</p>"
                    )
            else:
                btc_action = (
                    f'<p><a class="btn secondary" href="{BASE}/">Sign in / '
                    "Create your free account</a></p>"
                    '<p class="small">Your account comes first — the email '
                    "on it is what you put in the Cash App note.</p>"
                )
            btc_section = f"""
<div class="card"><h2>Or pay with Bitcoin — inside Cash App</h2>
<ol>
<li>In Cash App, send exactly <strong>{btc_amount} BTC</strong>
(the Bitcoin amount for {amount} at the current rate) to
<strong><code>{esc(CASHAPP_CASHTAG)}</code></strong> using Cash App's
Bitcoin send to the cashtag — there is no separate wallet address.</li>
<li>In the Cash App payment note, put
<strong>your account email</strong> — that is how your payment is
matched to your account.</li>
<li>Access unlocks as soon as the owner confirms receipt. Your
download then appears on your Member Desk.</li>
</ol>
{btc_action}
</div>"""
    ach_section = _ach_section(course, member, amount)
    body = f"""
<h1>Buy — {esc(course["name"])}</h1>
<p class="small">Taylor's Transgender Patient Awareness, LLC · sold only on this site</p>
{msg}
<div class="card"><h2>Price: {esc(price_line(course))}</h2>
<p>One-time payment. The files are released to your member account
only — access unlocks here on this site.</p>
{per_person}
<h2>How to buy — Cash App</h2>
<ol>
<li>Create your member account (or sign in) with your email.</li>
<li>In Cash App, send exactly <strong>{amount}</strong> to
<strong><code>{esc(CASHAPP_CASHTAG)}</code></strong>.</li>
<li>In the Cash App payment note, put
<strong>your account email</strong> — that is how your payment is
matched to your account.</li>
<li>Access unlocks as soon as the owner confirms receipt. Your
download then appears on your Member Desk.</li>
</ol>
{action}
</div>
{godaddy_section}
{ach_section}
{btc_section}
<p><a class="btn secondary" href="{BASE}/preview">Preview the courses</a>
<a class="btn secondary" href="{BASE}/desk">Member Desk</a></p>
"""
    return PAGE.format(base=BASE, title=f"Buy — {course['short']}", body=body)


def upload_page(member, message="", ok=False):
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    options = "".join(
        f'<option value="{c["key"]}">{esc(c["short"])} — {esc(c["name"])}</option>'
        for c in COURSES
    ) + "".join(
        f'<option value="{c["key"]}">{esc(c["short"])} — {esc(c["name"])}</option>'
        for c in COLLECTIONS
    )
    sections = ""
    for c in COURSES:
        files = staged_files(c["key"])
        legacy = os.path.join(CONTENT_DIR, c["file"])
        legacy_note = ""
        if os.path.isfile(legacy):
            legacy_note = (
                f'<tr><td>{esc(c["file"])} (top level)</td>'
                f"<td>{os.path.getsize(legacy):,} bytes</td></tr>"
            )
        if files:
            trs = "".join(
                f"<tr><td>{esc(name)}</td><td>{size:,} bytes</td></tr>"
                for name, size, _mtime in files
            )
        elif not legacy_note:
            trs = '<tr><td colspan="2" class="small">No files staged yet.</td></tr>'
        else:
            trs = ""
        sections += (
            f'<div class="card"><h2>Staged files — {esc(c["short"])}</h2>'
            f"<table><tr><th>File</th><th>Size</th></tr>{trs}{legacy_note}</table></div>"
        )
    for c in COLLECTIONS:
        files = collection_files(c["key"])
        if files:
            trs = "".join(
                f"<tr><td>{esc(name)}</td><td>{size:,} bytes</td></tr>"
                for name, size, _mtime in files
            )
        else:
            trs = '<tr><td colspan="2" class="small">No files staged yet.</td></tr>'
        sections += (
            f'<div class="card"><h2>Staged files — {esc(c["short"])} collection</h2>'
            f"<table><tr><th>File</th><th>Size</th></tr>{trs}</table>"
            '<p class="small">Teach-Back documents unlock with CCCE course '
            "access and are served individually — they are not added to "
            "the course bundle zip.</p></div>"
        )
    with _lock:
        log_rows = _db.execute(
            "SELECT * FROM uploads ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    log = ""
    if log_rows:
        log = (
            '<div class="card"><h2>Recent uploads</h2><table>'
            "<tr><th>When (UTC)</th><th>Product</th><th>File</th><th>Size</th></tr>"
            + "".join(
                f'<tr><td class="small">{esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"])))}</td>'
                f'<td>{esc(r["course_key"])}</td><td>{esc(r["filename"])}</td>'
                f'<td>{r["bytes"]:,} bytes</td></tr>'
                for r in log_rows
            )
            + "</table></div>"
        )
    body = f"""
<h1>Course File Upload</h1>
<p class="small">Owner only. Files go to the protected content area on the
server volume — they are never public and never part of the site code.</p>
{msg}
<div class="card"><h2>Upload course content</h2>
<form method="post" action="{BASE}/admin/upload" enctype="multipart/form-data">
<label>Target</label>
<select name="product">{options}</select>
<label>File (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)</label>
<input type="file" name="file" required>
<button type="submit">Upload</button></form>
<p class="small">Uploading a file with the same name replaces the staged
copy. Products take a .zip bundle: the bundle for both products is
CCCE_Course_Files.zip — upload it once under CCCE and once under EMSPR.
The Teach-Back collection takes individual documents
(.zip, .pdf, .md, .txt, .docx).</p></div>
{sections}
{log}
<p><a class="btn secondary" href="{BASE}/desk">Back to Member Desk</a></p>
"""
    return PAGE.format(base=BASE, title="Course File Upload", body=body)


def member_upload_page(member, message="", ok=False):
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    rows = member_uploads(member["id"])
    if rows:
        trs = "".join(
            f'<tr><td><a href="{BASE}/upload/download/{r["id"]}">{esc(r["filename"])}</a></td>'
            f'<td>{r["bytes"]:,} bytes</td>'
            f'<td class="small">{esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"])))} UTC</td></tr>'
            for r in rows
        )
        listing = (
            "<table><tr><th>File</th><th>Size</th><th>Uploaded</th></tr>"
            f"{trs}</table>"
        )
    else:
        listing = '<p class="small">You have not uploaded anything yet.</p>'
    body = f"""
<h1>Upload Documents</h1>
<p class="small">Send training-related documents for your department to
Taylor's Transgender Patient Awareness, LLC. Your uploads go to your
private member inbox on the server — only you and the site owner can
see them, and they are never public.</p>
{msg}
{connection_step_html()}
<div class="card"><h2>Upload a document</h2>
<form method="post" action="{BASE}/upload" enctype="multipart/form-data">
<label>File — PDF, Word (.doc/.docx), Excel (.xls/.xlsx), image
(.png/.jpg), or ZIP; max {MEMBER_UPLOAD_MAX_BYTES // (1024 * 1024)} MB</label>
<input type="file" name="file" required>
<button type="submit">Upload</button></form></div>
<div class="card"><h2>Your uploads</h2>
{listing}</div>
<p><a class="btn secondary" href="{BASE}/desk">Back to Member Desk</a></p>
"""
    return PAGE.format(base=BASE, title="Upload Documents", body=body)


def owner_paylinks_page(member, message="", ok=False):
    """Owner-only GoDaddy Pay Links page (Taylor, 2026-11-15: "Use
    GoDaddy Pay Links for cards"). The owner pastes each product's
    Pay Link; the links live in the settings table and each buy
    page renders its card section around the link it has — no
    link, no section. Saving a blank box clears that product's
    link. Links are shown back in full: they are public payment
    URLs, not secrets."""
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    states = ""
    for c in COURSES:
        v = godaddy_link(c["key"])
        states += (
            f'<p class="small">{esc(c["short"])} '
            f"({esc(price_line(c))}): "
            f'{"<strong>SET</strong> — " + esc(v) if v else "Not set"}'
            "</p>"
        )
    body = f"""
<h1>GoDaddy Pay Links — Cards</h1>
<p class="small">Owner only. Paste each product's GoDaddy Pay Link
and that product's buy page offers Pay by card right away — nothing
else to deploy. Buyers pay on GoDaddy's page, record the order on
the buy page, and access unlocks when you confirm the payment in
the roster.</p>
{msg}
<div class="card"><h2>Status</h2>
{states}</div>
<div class="card"><h2>Save the links</h2>
<form method="post" action="{BASE}/owner/paylinks">
<label>CCCE Course pay link — $15 every two years, per provider</label>
<p class="small">GoDaddy app → Payments → Pay Links. Leave the box blank to take the card button off this product.</p>
<input type="url" name="godaddy_link_ccce" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…">
<label>EMSPR Protocols pay link — $6,000 every year</label>
<p class="small">The link's amount should be $6,000. Leave the box blank to take the card button off this product.</p>
<input type="url" name="godaddy_link_emspr" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="https://…">
<button type="submit">Save pay links</button></form>
<p class="small">Pay Links are one-time payments: the cadence
price is paid per period and does not auto-renew.</p></div>
<p><a class="btn secondary" href="{BASE}/desk">Back to Member Desk</a></p>
"""
    return PAGE.format(base=BASE, title="GoDaddy Pay Links — Owner", body=body)


def desk_page(member, message="", ok=False):
    is_owner = member["email"].lower() == OWNER_EMAIL
    granted = member_access(member["id"])
    msg = f'<p class="msg{" ok" if ok else ""}">{esc(message)}</p>' if message else ""
    with _lock:
        my_pending = _db.execute(
            "SELECT * FROM manual_orders WHERE member_id = ? AND status = 'pending' "
            "ORDER BY created_at",
            (member["id"],),
        ).fetchall()
    pending_by_course = {}
    for o in my_pending:
        pending_by_course.setdefault(o["course_key"], []).append(o)

    rows = ""
    for c in COURSES:
        if c["key"] in granted:
            if content_available(c):
                status = (
                    f'<strong>Access granted.</strong><br>'
                    f'<a class="btn" href="{BASE}/content/{c["key"]}">'
                    f'Download full course contents</a>'
                )
            else:
                status = (
                    "<strong>Access granted</strong> — your access is saved. "
                    "The course files are being added to the site; they "
                    "will appear here to download once added."
                )
        else:
            parts = []
            for o in pending_by_course.get(c["key"], []):
                if o["method"] == "bitcoin":
                    parts.append(
                        f'<p class="small">Bitcoin order #{o["id"]} pending: send '
                        f'<strong>{esc(o["amount_text"])}</strong> to '
                        f'<code>{esc(CASHAPP_CASHTAG)}</code> inside Cash App — '
                        "put your account email in the payment note so it can "
                        "be matched.<br>Access unlocks when the owner confirms "
                        "receipt.</p>"
                    )
                elif o["method"] == "godaddy":
                    parts.append(
                        f'<p class="small">Card order #{o["id"]} pending '
                        "(GoDaddy Pay Link): pay "
                        f'<strong>{esc(o["amount_text"])}</strong> through '
                        "the Pay by card button on the buy page if you "
                        "have not paid yet.<br>Access unlocks when the "
                        "owner confirms your payment.</p>"
                    )
                else:
                    parts.append(
                        f'<p class="small">Cash App order #{o["id"]} pending: send '
                        f'<strong>{esc(o["amount_text"])}</strong> to '
                        f'<code>{esc(CASHAPP_CASHTAG)}</code> in Cash App — put '
                        "your account email in the payment note so it can be "
                        "matched.<br>Access unlocks when the owner confirms "
                        "receipt.</p>"
                    )
            # Cash App is the operative purchase path (2026-11-12 sales
            # decision) and is presented first, as the primary action,
            # with the full instructions inline: exact amount, cashtag,
            # account email in the note, owner-confirmation unlock.
            amount = f"${c['cents'] / 100:,.2f}"
            parts.append(
                '<p><strong>Buy with Cash App:</strong> send '
                f"<strong>{amount}</strong> to "
                f"<code>{esc(CASHAPP_CASHTAG)}</code> in Cash App and put "
                "your account email in the payment note, then record "
                "your order:</p>"
                f'<form class="inline" method="post" action="{BASE}/cashapp/order">'
                f'<input type="hidden" name="course_key" value="{c["key"]}">'
                f'<button type="submit">Pay with Cash App — {amount}</button></form>'
                '<p class="small">Access unlocks when the owner confirms '
                "receipt.</p>"
            )
            # Bitcoin alongside Cash App (Taylor, 2026-11-12): sent
            # inside Cash App to the cashtag; the order records the
            # live-quoted BTC amount and the owner confirms receipt,
            # exactly like a Cash App cash order.
            parts.append(
                '<p><strong>Buy with Bitcoin:</strong> send the Bitcoin '
                "amount quoted on the course buy page to "
                f"<code>{esc(CASHAPP_CASHTAG)}</code> inside Cash App "
                "and put your account email in the payment note, then "
                "record your order:</p>"
                f'<form class="inline" method="post" action="{BASE}/bitcoin/order">'
                f'<input type="hidden" name="course_key" value="{c["key"]}">'
                f'<button class="secondary" type="submit">Pay with Bitcoin</button></form> '
                '<p class="small">Access unlocks when the owner confirms '
                "receipt.</p>"
            )
            # The desk offers the manual rails (Cash App, Bitcoin,
            # and a recorded GoDaddy Pay Link order when a link is
            # set). Recurring card/wallet/bank subscriptions live on
            # (No subscription rail at present.)
            status = manual = "".join(parts)
        rows += (
            f'<div class="card"><strong>{esc(c["name"])}</strong><br>'
            f'<span class="small">{esc(price_line(c))}</span><br>{status}</div>'
        )

    roster = ""
    if is_owner:
        with _lock:
            all_members = _db.execute(
                "SELECT * FROM members ORDER BY created_at"
            ).fetchall()
            pend = _db.execute(
                "SELECT o.*, m.name, m.email FROM manual_orders o "
                "JOIN members m ON m.id = o.member_id "
                "WHERE o.status = 'pending' ORDER BY o.created_at"
            ).fetchall()
        trs = ""
        for m in all_members:
            m_access = member_access(m["id"])
            cells = ""
            for c in COURSES:
                if c["key"] in m_access:
                    action, label = "revoke", "Revoke"
                else:
                    action, label = "grant", "Grant"
                cells += (
                    f'<td><form class="inline" method="post" action="{BASE}/access">'
                    f'<input type="hidden" name="member_id" value="{m["id"]}">'
                    f'<input type="hidden" name="course_key" value="{c["key"]}">'
                    f'<input type="hidden" name="action" value="{action}">'
                    f'<button type="submit">{label} {esc(c["short"])}</button></form></td>'
                )
            trs += f'<tr><td>{esc(m["name"])}</td><td>{esc(m["email"])}</td>{cells}</tr>'
        btc_rows = ""
        for o in pend:
            btc_rows += (
                f'<tr><td>#{o["id"]}</td><td>{esc(o["name"])} ({esc(o["email"])})</td>'
                f'<td>{esc(o["course_key"])}</td><td>{esc(o["method"])}</td>'
                f'<td>{esc(o["amount_text"])}</td>'
                f'<td><form class="inline" method="post" action="{BASE}/manual/confirm">'
                f'<input type="hidden" name="order_id" value="{o["id"]}">'
                f'<button type="submit">Confirm receipt — unlock</button></form></td></tr>'
            )
        if not btc_rows:
            btc_rows = '<tr><td colspan="6" class="small">No pending manual-payment orders.</td></tr>'
        up_rows = all_member_uploads()
        if up_rows:
            grouped = {}
            for r in up_rows:
                grouped.setdefault((r["member_name"], r["member_email"]), []).append(r)
            up_sections = ""
            for (mname, memail), items in grouped.items():
                item_rows = "".join(
                    f'<tr><td class="small">{esc(time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["created_at"])))} UTC</td>'
                    f'<td><a href="{BASE}/upload/download/{r["id"]}">{esc(r["filename"])}</a></td>'
                    f'<td>{r["bytes"]:,} bytes</td></tr>'
                    for r in items
                )
                up_sections += (
                    f'<p><strong>{esc(mname)}</strong> '
                    f'<span class="small">{esc(memail)}</span></p>'
                    "<table><tr><th>When (UTC)</th><th>File</th><th>Size</th></tr>"
                    f"{item_rows}</table>"
                )
        else:
            up_sections = '<p class="small">No member uploads received yet.</p>'
        roster = f"""
<div class="card"><h2>Owner roster — grant / revoke course access</h2>
<table><tr><th>Member</th><th>Email</th><th>CCCE</th><th>EMSPR</th></tr>{trs}</table>
<p class="small">Only the owner account sees this roster.</p></div>
<div class="card"><h2>Pending manual-payment orders (Bitcoin / Cash App)</h2>
<table><tr><th>#</th><th>Member</th><th>Product</th><th>Method</th><th>Amount</th><th></th></tr>{btc_rows}</table></div>
<div class="card"><h2>Course files</h2>
<p class="small">Stage or replace the protected course bundles.</p>
<a class="btn secondary" href="{BASE}/admin/upload">Open the upload page</a></div>
<div class="card"><h2>GoDaddy Pay Links</h2>
<p class="small">Card payments through GoDaddy Pay Links: save or clear
the link for each product. Buy pages show the Pay by card button for
each link that is set.</p>
<a class="btn secondary" href="{BASE}/owner/paylinks">Open the Pay Links page</a></div>
<div class="card"><h2>Member uploads received</h2>
<p class="small">Documents members sent from the upload page, grouped by
member. Only you can see other members' uploads.</p>
{up_sections}</div>
"""

    # Teach-Back collection card: unlocks with CCCE access (see the
    # COLLECTIONS design decision near the top of this file).
    if has_content_access(member["id"], "teachback"):
        teachback = (
            '<div class="card"><strong>Teach-Back Collection</strong><br>'
            '<span class="small">Included with your CCCE course access.</span><br>'
            f'<a class="btn" href="{BASE}/content/teachback">'
            "Browse teach-back documents</a></div>"
        )
    else:
        teachback = (
            '<div class="card"><strong>Teach-Back Collection</strong><br>'
            '<span class="small">Unlocks with the CCCE course.</span></div>'
        )

    card_li = (
        "<li>your card payment is confirmed — automatically when a "
        "owner for a GoDaddy Pay Link payment, or</li>"
    )

    body = f"""
<h1>Member Desk</h1>
{msg}
<div class="card"><strong>{esc(member["name"])}</strong><br>
<span class="small">{esc(member["email"])}</span>
<form class="inline" method="post" action="{BASE}/signout" style="float:right">
<button class="secondary" type="submit">Sign out</button></form></div>
<div class="card"><h2>How access works</h2>
<p>Course access is <strong>$15 per person</strong>.</p>
<p>Your account gets access — it becomes <strong>entitled</strong> — when:</p>
<ul>
<li>your Cash App payment is confirmed by the owner, or</li>
{card_li}
<li>the owner grants your account access from the roster (for example,
teach-back access for departments, or past buyers honored).</li>
</ul>
<p>Until then, you can log in and use the site, but course downloads
stay locked.</p></div>
<h2>Your courses</h2>
{rows}
{teachback}
<div class="card"><strong>Upload documents</strong><br>
<span class="small">Send training-related documents for your
department.</span><br>
<a class="btn" href="{BASE}/upload">Open the upload page</a></div>
<div class="card"><strong>Member &amp; Server Apps</strong><br>
<span class="small">Every live server and phone app in one place,
with a live check from your device.</span><br>
<a class="btn" href="{BASE}/apps">Open the apps hub</a></div>
{roster}
<div class="card"><h2>Delete account</h2>
<form method="post" action="{BASE}/delete-account">
<label>Password (required to delete your own account)</label>
<input type="password" name="password" required>
<button class="secondary" type="submit">Delete my account</button></form></div>
"""
    return PAGE.format(base=BASE, title="Member Desk", body=body)
