"""Courses and content collections — data carried over verbatim."""


COURSES = [
    {
        "key": "ccce",
        "name": "Cultural Competence Continuing Education: Transgender Emergency Medicine",
        "short": "CCCE Course",
        "price": "$15",
        "cents": 1500,
        "file": "CCCE_Course_Files.zip",
        # Billing cadence (Taylor, 2026-11-15: "$15 every two years
        # for provider") — the course is priced per provider on the
        # two-year Oregon EMT renewal cycle.
        "cadence": "every two years",
        "per": "per provider",
        "interval": "year",
        "interval_count": 2,
    },
    {
        "key": "emspr",
        "name": "EMSPR Protocols",
        "short": "EMSPR",
        "price": "$6,000",
        "cents": 600000,
        "file": "CCCE_Course_Files.zip",
        # Billing cadence (Taylor, 2026-11-15: "EMS protocols every
        # year") — the protocols are licensed per year.
        "cadence": "every year",
        "per": "",
        "interval": "year",
        "interval_count": 1,
    },
]


COURSE_KEYS = {c["key"] for c in COURSES}


COURSE_BY_KEY = {c["key"]: c for c in COURSES}


def price_line(course):
    """'$15 USD — every two years, per provider' / '$6,000 USD —
    every year' — the price with its billing cadence."""
    line = f'{course["price"]} USD — {course["cadence"]}'
    if course.get("per"):
        line += f', {course["per"]}'
    return line


COLLECTIONS = [
    {
        "key": "teachback",
        "name": "Teach-Back Collection — similar-topic teach-back documents",
        "short": "Teach-Back",
        "unlock_course": "ccce",
    },
]


COLLECTION_BY_KEY = {c["key"]: c for c in COLLECTIONS}


UPLOAD_TARGETS = COURSE_KEYS | set(COLLECTION_BY_KEY)


COLLECTION_EXTS = (".zip", ".pdf", ".md", ".txt", ".docx")


COLLECTION_CONTENT_TYPES = {
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
