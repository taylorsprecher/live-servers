"""Portal — page data and rendering.

The COMPANIES map, the per-company intro blocks, and the per-company
Teachbacks blocks are carried over from the original app.py unchanged
in content (names, links, and text identical); only the card markup
classes were re-skinned onto the Fleet design system (fl-grid /
fl-card, with .card-top / .tag styled by the templates' per-page
<style> block). The template mechanics are the original ones:
index.html is served as the index page, and company.html is rendered
per company by substituting __COMPANY_NAME__, __INTRO_SECTION__, and
__TEACHBACK_SECTION__ (plus __BASE_PATH__, added so the vendored
fleet.css link follows the configured base path).

Company pages carry the company name and public platform links only —
no individual names, emails, or phone numbers.
"""
from pathlib import Path

from .config import BASE_PATH

_SERVICE_ROOT = Path(__file__).parent.parent
INDEX_HTML = (
    (_SERVICE_ROOT / "index.html")
    .read_text(encoding="utf-8")
    .replace("__BASE_PATH__", BASE_PATH)
    .encode()
)
_COMPANY_TEMPLATE = (_SERVICE_ROOT / "company.html").read_text(encoding="utf-8")
FLEET_CSS = (_SERVICE_ROOT / "assets" / "fleet.css").read_bytes()

# slug -> company display name (pre-escaped for HTML substitution).
COMPANIES = {
    "hillsboro": "Hillsboro Fire &amp; Rescue",
    "woodburn": "Woodburn Ambulance",
    "monitor": "Monitor RFPD",
    "oha": "OHA EMS",
    "maine": "State of Maine",
}

_TRAINING_SITE = (
    "https://muse.ai/s/911transedu-net-website-xkk6xaxm0ib1ak/"
    "-hQ_ySKdwECtNsqmEyusrdqJDfrhiRX9-5oxqk2dnI4"
)

# slug -> optional intro block (HTML, no trailing newline; empty for
# companies with no intro on record). Substituted for
# __INTRO_SECTION__ in company.html, followed by a newline.
COMPANY_INTRO_SECTIONS = {
    "maine": (
        '    <h2>For Maine EMS Providers &amp; Agencies</h2>\n'
        '    <div class="fl-grid">\n'
        '      <a class="fl-card" href="https://members.911transedu.net/members/">'
        '<div class="card-top"><span>Register on the Member Site</span></div>'
        '<span class="tag">This portal is for Maine EMS providers and '
        'agencies — register with your agency email and access is '
        'granted from the owner roster</span></a>\n'
        '      <a class="fl-card" href="https://members.911transedu.net/members/buy/emspr">'
        '<div class="card-top"><span>EMSPR Protocols — $6,000</span></div>'
        '<span class="tag">The protocols are covered with every provider, '
        'and it&rsquo;s on the counties or hospitals to pay for '
        'them</span></a>\n'
        '    </div>\n'
    ),
}

# slug -> optional Teachbacks block (HTML, no trailing newline).
# Companies with a company-specific teachback link that article;
# companies without one carry a generic "All Teachbacks" block linking
# the training site's #all-teachbacks collection section (the umbrella
# anchor was created on the training site 2026-11-13 at Taylor's
# direction, "Create the all teachbacks"; before that the cards linked
# the bare training-site URL). Substituted for
# __TEACHBACK_SECTION__ in company.html, followed by a newline.
COMPANY_TEACHBACK_SECTIONS = {
    "hillsboro": (
        '    <h2>Teachbacks</h2>\n'
        '    <div class="fl-grid">\n'
        f'      <a class="fl-card" href="{_TRAINING_SITE}#hillsboro-fire-teachback">'
        '<div class="card-top"><span>Hillsboro Fire Teach-Back</span></div>'
        '<span class="tag">This company&rsquo;s own teach-back — the 2019 '
        'Hillsboro Fire training session and delivery record</span></a>\n'
        '    </div>\n'
    ),
    "maine": (
        '    <h2>Teachbacks</h2>\n'
        '    <div class="fl-grid">\n'
        f'      <a class="fl-card" href="{_TRAINING_SITE}#kptv-city-of-bend-teachback">'
        '<div class="card-top"><span>Maine &sect;14 Teach-Back</span></div>'
        '<span class="tag">The State of Maine Expansion Section '
        '(&sect;14) on the training site, inside the KPTV to City of '
        'Bend Teachback</span></a>\n'
        '    </div>\n'
    ),
    "woodburn": (
        '    <h2>Teachbacks</h2>\n'
        '    <div class="fl-grid">\n'
        f'      <a class="fl-card" href="{_TRAINING_SITE}#all-teachbacks">'
        '<div class="card-top"><span>All Teachbacks</span></div>'
        '<span class="tag">Every teach-back — on the training site</span></a>\n'
        '    </div>\n'
    ),
    "monitor": (
        '    <h2>Teachbacks</h2>\n'
        '    <div class="fl-grid">\n'
        f'      <a class="fl-card" href="{_TRAINING_SITE}#all-teachbacks">'
        '<div class="card-top"><span>All Teachbacks</span></div>'
        '<span class="tag">Every teach-back — on the training site</span></a>\n'
        '    </div>\n'
    ),
    "oha": (
        '    <h2>Teachbacks</h2>\n'
        '    <div class="fl-grid">\n'
        f'      <a class="fl-card" href="{_TRAINING_SITE}#all-teachbacks">'
        '<div class="card-top"><span>All Teachbacks</span></div>'
        '<span class="tag">Every teach-back — on the training site</span></a>\n'
        '    </div>\n'
    ),
}


def company_page(slug):
    page = _COMPANY_TEMPLATE.replace("__BASE_PATH__", BASE_PATH)
    page = page.replace("__COMPANY_NAME__", COMPANIES[slug])
    intro = COMPANY_INTRO_SECTIONS.get(slug, "")
    page = page.replace("__INTRO_SECTION__", intro)
    section = COMPANY_TEACHBACK_SECTIONS.get(slug, "")
    return page.replace("__TEACHBACK_SECTION__", section).encode()
