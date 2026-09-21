"""Pulls open job postings from a Personio job board.

Personio is the only platform here whose public feed is XML rather than
JSON. The board a human sees at

    https://{slug}.jobs.personio.com/

publishes every open position at

    GET https://{slug}.jobs.personio.com/xml

as a <workzag-jobs> document (the element name is a leftover from
Personio's original product name — it is not a typo, and matching on
<positions> instead returns nothing).

Two host facts, both confirmed against live boards rather than assumed:

  - Tenants answer on both .com and .de, but not all of them: the tld is
    part of the tenant's configuration, not a mirror. So both are tried,
    .com first, and only a failure on both is a real failure.
  - A tenant that does not exist does NOT 404. It answers 307 with
    Location: https://personio.com — the vendor's marketing site, which
    answers 429. Following that is actively harmful: the shared retry
    session in http_client.py retries 429 five times with growing backoff,
    so one wrong slug turns into a slow retry storm against a site that
    was never going to have the answer, and the Scrape Note ends up
    reading "too many 429 error responses" with no mention of the board.
    So redirects are not followed, and a 3xx is read as "no such board".
  - The per-posting page is /job/{id} on the same host. The feed carries
    no URL field at all, so this is constructed — and it has to be, since
    a posting with no URL is one the Notion sync cannot dedupe on.

Parsed with xml.etree from the standard library rather than adding a
dependency. ElementTree does not resolve external entities, so a hostile
feed cannot use this to read local files or make onward requests; the
residual entity-expansion risk is accepted for a feed served by a known
ATS vendor over TLS.
"""
import xml.etree.ElementTree as ET

from http_client import session

from ats_finder.find_ats import slugify

# .com first: every tenant in the target list answers there today, so the
# .de attempt is a fallback that normally costs nothing.
HOSTS = ("jobs.personio.com", "jobs.personio.de")


def _description(position):
    """Flatten Personio's sectioned description into one HTML blob.

    The feed splits a posting into named <jobDescription> sections ("About
    Us", "Role Summary", "What You'll Do"). Scoring wants one string, and
    the section headings carry real signal about seniority and scope, so
    they are kept as headings rather than dropped.
    """
    sections = position.find("jobDescriptions")
    if sections is None:
        return None
    parts = []
    for section in sections:
        name = (section.findtext("name") or "").strip()
        value = (section.findtext("value") or "").strip()
        if not value:
            continue
        parts.append(f"<h3>{name}</h3>{value}" if name else value)
    return "\n".join(parts) or None


def _location(position):
    """Personio's <office> is the location. <subcompany> is the legal
    entity, which is not the same thing and reads as noise in a location
    column, so it is deliberately not folded in."""
    office = (position.findtext("office") or "").strip()
    return office or None


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)

    last_error = None
    for host in HOSTS:
        try:
            # allow_redirects=False is load-bearing, not a default being
            # restated — see the module docstring on the 307 to the
            # vendor's marketing site.
            resp = session.get(f"https://{slug}.{host}/xml", timeout=20,
                               allow_redirects=False)
            if resp.is_redirect or resp.is_permanent_redirect:
                raise RuntimeError(
                    f"no Personio board at {slug}.{host} (it redirects to "
                    f"{resp.headers.get('Location')}, which is how Personio "
                    f"answers for a tenant that does not exist)")
            resp.raise_for_status()
            # .content, not .text: the document declares its own encoding
            # in the XML prolog, and handing ElementTree a str that
            # requests already decoded by guesswork is how accented office
            # names ("Münster") turn into mojibake or a ValueError.
            root = ET.fromstring(resp.content)
        except Exception as e:
            last_error = e
            continue

        jobs = []
        for position in root.findall("position"):
            job_id = (position.findtext("id") or "").strip()
            title = (position.findtext("name") or "").strip()
            if not job_id or not title:
                continue  # a feed row with neither cannot be deduped or scored
            jobs.append({
                "company": company_name,
                "title": title,
                "location": _location(position),
                "url": f"https://{slug}.{host}/job/{job_id}",
                "posted_at": (position.findtext("createdAt") or "").strip() or None,
                "description": _description(position),
                "source_ats": "Personio",
            })
        return jobs

    raise last_error
