"""Whether a value saved in `Careers URL` is usable as one at all.

The pipeline's "have we researched this company" test has always been
"is the field blank" — invariant 8: blank means never researched, so every
outcome writes something non-blank. That works for a field whose wrong
answers are still *answers*. It has no defence against a field holding
something that was never an answer in the first place.

That gap was found the hard way. Twenty rows carried the same
`app.notion.com/p/...` link in `Careers URL`, pasted into the wrong
column at some point. Because the field was non-blank,
`get_companies_missing_url()` never revisited them, so the scrape retried
them every run, guessed a slug from the company name, failed, and stamped
Needs Manual Check — forever, with the one pass that could have repaired
it skipping them every time. It also propagated: `derive_website()` reads
the Careers URL, so eighteen of those companies had their `Website` set to
`https://app.notion.com` and their page icon stamped with Notion's
favicon, which invariant 16 then reads as "already checked".

So this module answers a deliberately narrow question: is this string
*structurally capable* of being a careers destination? It makes no network
call and forms no opinion about whether the page is any good — an
unreachable or stale URL is a scrape failure, which `sync_scrape_status()`
already surfaces, and judging liveness here would collide with invariant 1
(a fetch that fails to run is not evidence the URL is wrong).
"""
from urllib.parse import urlparse

# Hosts that can never be any kind of careers destination: document and
# file-storage products. A link to one of these is always a paste error,
# never a considered answer, which is what makes it safe to treat as
# "never researched" and overwrite.
#
# Note what is deliberately NOT here: google.com. A pre-filled Google
# search is the documented last resort of fill_missing_careers_urls() when
# no careers page and no homepage can be found — a real, clickable,
# deliberately-chosen answer (invariant 8 exists so those rows stop being
# re-researched forever). Rejecting it would re-open exactly the loop that
# invariant is there to close. The docs/drive subdomains below are
# documents rather than searches, so they are listed individually.
UNUSABLE_CAREERS_HOSTS = (
    "notion.com", "notion.so", "notion.site",
    "docs.google.com", "drive.google.com",
    "dropbox.com", "box.com", "sharepoint.com", "onedrive.live.com",
    "airtable.com", "figma.com",
)


def is_usable_careers_url(url):
    """True if `url` could plausibly be a careers destination.

    False covers: empty, unparseable, a non-http scheme, a host with no dot
    in it (a bare word, or a whole sentence saved by mistake), and any host
    in UNUSABLE_CAREERS_HOSTS or a subdomain of one.

    False means "treat this row as never researched", so a caller acting on
    it will overwrite the value. That is why the bar is structural
    impossibility rather than mere suspicion: a URL a human deliberately
    chose must always pass.
    """
    if not url or not isinstance(url, str):
        return False

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    host = (parsed.netloc or "").lower()
    host = host.split("@")[-1].split(":")[0]  # strip userinfo@ / :port
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return False

    return not any(host == h or host.endswith("." + h)
                   for h in UNUSABLE_CAREERS_HOSTS)
