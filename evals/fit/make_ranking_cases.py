"""Writes postings_ranking.yaml and labels_ranking.yaml next to this file. The output is
committed; re-run after editing: `python evals/fit/make_ranking_cases.py`."""

import sys
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
P = ["first-year-intern", "intl-sponsorship", "newgrad-analyst", "senior-backend"]
G = "g"  # gated

# category -> (grades per persona, titles)
CATS = {
    "swe_intern": (
        (1, 1, G, G),
        [
            "Software Engineer Intern",
            "Software Engineering Intern - Summer 2027",
            "SWE Intern",
            "Software Developer Intern",
            "Backend Engineering Intern",
            "Software Engineer Intern (Summer 2027)",
            "Full Stack Engineering Intern",
        ],
    ),
    "data_intern": (
        (1, 1, G, G),
        ["Data Engineering Intern", "Data Science Intern", "Data Platform Intern"],
    ),
    "ml_intern": (
        (1, 1, G, G),
        ["Machine Learning Intern", "ML Engineer Intern", "Machine Learning Engineering Intern"],
    ),
    "analyst_intern": ((0, 0, G, G), ["Data Analyst Intern", "Business Analyst Intern"]),
    "other_intern": (
        (0, 0, G, G),
        [
            "Marketing Intern",
            "Finance Intern",
            "Human Resources Intern",
            "Mechanical Engineering Intern",
            "Product Design Intern",
            "Sales Intern",
            "Communications Intern",
            "Supply Chain Intern",
        ],
    ),
    "analyst_ng": (
        (G, G, 1, G),
        [
            "Data Analyst, New Grad",
            "Business Intelligence Analyst, New Grad",
            "Analytics Analyst, New Grad 2027",
        ],
    ),
    "analyst": (
        (0, 0, 1, 0),
        ["Data Analyst", "Business Intelligence Analyst", "Reporting Analyst", "Product Analyst"],
    ),
    "senior_backend": (
        (G, G, G, 1),
        [
            "Senior Backend Engineer",
            "Senior Platform Engineer",
            "Staff Software Engineer, Infrastructure",
            "Senior Software Engineer, Distributed Systems",
        ],
    ),
    "backend": ((0, 0, 0, 1), ["Backend Engineer", "Platform Engineer", "Infrastructure Engineer"]),
    "nontech": (
        (0, 0, 0, 0),
        [
            "Account Executive",
            "Recruiter",
            "Customer Success Manager",
            "Sales Development Representative",
            "Office Coordinator",
            "Registered Nurse",
            "Accountant",
            "Account Manager",
            "Paralegal",
            "Store Associate",
            "Field Marketing Manager",
            "Technical Recruiter",
        ],
    ),
}
COMPANIES = [
    "Aldermoor",
    "Basaltline",
    "Cobaltwing",
    "Dunmere",
    "Emberlake",
    "Foxglove Systems",
    "Greyheron",
    "Hollowpine",
    "Ironbark",
    "Juniper Row",
    "Kelpforge",
    "Larkspur Data",
    "Marrowstone",
    "Nettlefield",
    "Oakhaven",
    "Pebblecreek",
    "Quartzridge",
    "Rookwood",
    "Saffronvale",
    "Thistledown",
    "Umberfall",
    "Vantablue",
    "Willowmere",
    "Yarrowby",
    "Zephyrine",
    "Ashcombe",
    "Briarwood Labs",
    "Cindertrail",
    "Dovecote",
    "Elderglen",
]
CITIES = [
    "Chicago, IL",
    "New York, NY",
    "Boston, MA",
    "San Francisco, CA",
    "Austin, TX",
    "Seattle, WA",
]


BOILER = """
About Quillstack
Quillstack is a financial infrastructure platform for businesses. Millions of companies, from
the world's largest enterprises to the most ambitious startups, use Quillstack to accept
payments, grow their revenue, and accelerate new business opportunities. Our mission is to
increase the GDP of the internet, and we have a staggering amount of work ahead.
Who we are looking for
We look for people who are rigorous, curious and kind, who take ownership of outcomes and who
want to grow with a fast-moving company. You will collaborate with people across product,
design, operations, legal and finance in offices around the world.
In-office expectations
Quillstack employees in most locations are currently expected to spend at least 50% of the
time in a given month in their local office or with users. This hits a balance between
bringing people together for in-person collaboration and learning from each other, while
supporting flexibility about how to do this in a way that makes sense for individuals and
their teams.
Pay and benefits
The annual US base salary range for this role is listed by location. This salary range may
be inclusive of several career levels and will be narrowed during the interview process
based on a number of factors, including the candidate's experience, qualifications, and
location. Applicants interested in this role and who are not located in the US may request
the annual salary range for their location during the interview process. Specific benefits
and details about what compensation is included in the salary range listed above will vary
depending on the applicant's location and can be discussed in more detail during the
interview process. Benefits and other compensation include equity, health insurance,
retirement plans, wellness stipends and paid parental leave.
Equal opportunity
Quillstack is proud to be an equal opportunity employer. We are committed to providing equal
employment opportunity regardless of race, color, ancestry, religion, sex, national origin,
sexual orientation, age, citizenship, marital status, disability, gender identity or veteran
status. We also consider qualified applicants regardless of criminal histories, consistent
with legal requirements. If you need assistance or an accommodation due to a disability, you
may contact us. We will never ask for payment during the hiring process.
"""

posts, labels = [], {p: {} for p in P}
lines = [
    "# Ranking cases for the fit eval set: title-only rows like Simplify's, watchlist jobs, and the",  # noqa: E501
    "# two ranking failures found in the 2026-11-06 system check. Every company here is fictional.",
    "# posted_days_ago: null means the source gives no date (first_seen_days_ago says when jobhunt",
    "# first saw it); deadline_days is days from manifest `today`.",
    "",
]


def emit(d, desc=None):
    lines.append(f"- id: {d['id']}")
    for k in (
        "company",
        "title",
        "locations",
        "work_mode",
        "employment_type",
        "posted_days_ago",
        "first_seen_days_ago",
        "deadline_days",
    ):
        if k in d:
            v = d[k]
            if isinstance(v, list):
                v = "[" + ", ".join(f'"{x}"' for x in v) + "]"
            elif v is None:
                v = "null"
            lines.append(f"  {k}: {v}")
    if desc:
        lines.append("  description: |")
        lines.extend("    " + x for x in desc.strip().splitlines())
    lines.append("")


def label(pid, grades):
    for p, g in zip(P, grades, strict=True):
        labels[p][pid] = {"gated": True} if g == G else {"grade": g}


lines.append(
    "# --- a watchlist company: a fresh sales job vs an evergreen internship (the Stripe case). Both"  # noqa: E501
)
lines.append(
    "# carry the full-length text live postings have; long text is what loses to title-only rows. ---\n"  # noqa: E501
)
emit(
    {
        "id": "r01",
        "company": "Quillstack",
        "title": "Account Executive",
        "locations": ["San Francisco, CA"],
        "posted_days_ago": 1,
    },
    """
Quillstack is hiring an Account Executive to grow our mid-market book of business.
You will own the full sales cycle from prospecting to close and partner with solutions engineers.
Requirements
- 2+ years of quota-carrying sales experience
- Excellent written and verbal communication
- Experience with Salesforce
"""
    + BOILER,
)
label("r01", (0, 0, 0, G))  # in-office: out for the remote-only persona
emit(
    {
        "id": "r02",
        "company": "Quillstack",
        "title": "Software Engineer, Intern",
        "locations": ["San Francisco, CA", "Seattle, WA"],
        "employment_type": "Intern",
        "posted_days_ago": 400,
    },
    """
Quillstack hires software engineering interns throughout the year. Interns join a product team,
write production code in Python, Java or Go, and ship to users with a mentor.
Requirements
- Currently pursuing a degree in Computer Science or a related field
- Experience with Python, Java, or Go
- Familiarity with Git
"""
    + BOILER,
)
label("r02", (2, 2, G, G))

lines.append(
    "# --- a watchlist company whose jobs have no date and no text (the Ramp all-tied case) ---\n"
)
for pid, title, g in [
    ("r03", "Data Analyst, New Grad", (G, G, 2, G)),
    ("r04", "Analytics Engineer", (0, 0, 1, 0)),
    ("r05", "Business Operations Associate", (G, G, 1, G)),
    ("r06", "Software Engineer, New Grad", (G, G, 0, G)),
    ("r07", "Account Executive, Mid-Market", (0, 0, 0, 0)),
]:
    emit(
        {
            "id": pid,
            "company": "Ledgerline Financial",
            "title": title,
            "locations": ["New York, NY"],
            "posted_days_ago": None,
            "first_seen_days_ago": 3,
        }
    )
    label(pid, g)

lines.append("# --- internships with and without an application deadline ---\n")
emit(
    {
        "id": "r08",
        "company": "Copperfield Transit",
        "title": "Software Engineering Intern, Summer 2027",
        "locations": ["Chicago, IL"],
        "work_mode": "hybrid",
        "employment_type": "Intern",
        "posted_days_ago": 20,
        "deadline_days": 10,
    },
    """
Build trip-planning tools used by riders across the city. Applications close on the deadline.
Requirements
- Pursuing a Bachelor's degree in Computer Science
- Experience with Python or Java
""",
)
label("r08", (2, G, G, G))  # bachelor's only: the intl persona is a master's student
emit(
    {
        "id": "r09",
        "company": "Marlowe Learning",
        "title": "Software Engineering Intern, Summer 2027",
        "locations": ["Chicago, IL"],
        "work_mode": "hybrid",
        "employment_type": "Intern",
        "posted_days_ago": 2,
    },
    """
Build classroom tools for teachers. Interns work in Python and React with a mentor.
Requirements
- Pursuing a Bachelor's degree in Computer Science
- Experience with Python or Java
""",
)
label("r09", (2, G, G, G))

lines.append("# --- title-only rows, like most Simplify listings ---\n")
n = 10
i = 0
for cat, (grades, titles) in CATS.items():
    for t in titles:
        pid = f"r{n:02d}"
        company = COMPANIES[i % len(COMPANIES)]
        loc = "Remote - US" if cat in ("senior_backend",) else CITIES[i % len(CITIES)]
        emit(
            {
                "id": pid,
                "company": company,
                "title": t,
                "locations": [loc],
                "posted_days_ago": (i * 3) % 19,
            }
        )
        g = list(grades)
        if "Manager" in t:
            g[2] = G  # the new-grad persona excludes "manager" (Must)
        if t == "Store Associate":
            g = [G, G, 0, G]  # an entry-level role: out for intern-only and senior-only personas
        label(pid, tuple(g))
        n += 1
        i += 1

lines.append(
    "# --- bulk title-only rows: live lists hold thousands, enough to push recall past top K ---\n"
)
SUFFIX = ["Labs", "Works", "Group", "Systems", "Co", "Technologies"]
BULK = [
    (
        [
            "Software Engineer Intern",
            "Software Engineering Intern",
            "Software Developer Intern",
            "Data Engineering Intern",
            "Machine Learning Intern",
            "Backend Engineering Intern",
        ],
        60,
        (1, 1, G, G),
    ),
    (
        [
            "Data Analyst, New Grad",
            "Business Intelligence Analyst, New Grad",
            "Analytics Analyst, New Grad",
            "Data Analyst I, New Grad",
            "Business Analyst, New Grad",
        ],
        50,
        (G, G, 1, G),
    ),
]
k = 0
for titles, count, grades in BULK:
    for j in range(count):
        pid = f"b{k + 1:03d}"
        company = f"{COMPANIES[k % len(COMPANIES)]} {SUFFIX[(k // len(COMPANIES)) % len(SUFFIX)]}"
        emit(
            {
                "id": pid,
                "company": company,
                "title": titles[j % len(titles)],
                "locations": [CITIES[k % len(CITIES)]],
                "posted_days_ago": (k * 7) % 23,
            }
        )
        label(pid, grades)
        k += 1

(root / "postings_ranking.yaml").write_text("\n".join(lines).rstrip() + "\n")
out = [
    "# Labels for postings_ranking.yaml (grade only: no band, so band agreement stays comparable).",
    '# Title-only rows grade 1 at best: without the posting text nobody can say "strong fit".',
    "",
]
for p in P:
    out.append(f"{p}:")
    for pid, lab in labels[p].items():
        out.append(
            f"  {pid}: " + ("{gated: true}" if lab.get("gated") else f"{{grade: {lab['grade']}}}")
        )
    out.append("")
(root / "labels_ranking.yaml").write_text("\n".join(out).rstrip() + "\n")
print(n - 1 + k, "postings")
