"""The SQL predicates that decide which reviews are published and weighed.

One definition, shared by the web service and crowdcode-cron. Both decide
"which reviews count" for the same service, and cron deliberately does not
import the MCP app, so the predicates live here rather than in server.py —
two copies of this answer would drift, and the one that drifted would publish
evidence the other had ruled out.

Every builder takes an optional table alias so a join (`reviews r`) and a
single-table query share the same text. Counts are `count(alias.id)`, never
`count(*)`: the service listing left-joins reviews, where `count(*)` would
score a service with no reviews as having one.
"""

from __future__ import annotations

# weight_exclusion values. None means the review carries ranking weight.
EXCLUSION_COMMUNITY = "community"
EXCLUSION_SELF_DEALING = "self_dealing"
EXCLUSION_DUST_PAYMENT = "dust_payment"
EXCLUSION_UNSIGNED_PLACEHOLDER = "unsigned_placeholder"
# The review describes a failure CrowdCode caused, not the provider's. Our
# gateway misread the provider's own valid 402 offer, or the agent held no
# funds on the network the provider asked to be paid on: the provider was
# never given the chance to succeed or fail, so the rating is not evidence
# about the provider. The row and its rating are kept verbatim — the mistake
# was ours and deleting the record of it would hide it — with the reason in
# `reviews.exclusion_note`, and are never counted, summarized or weighed.
# Only an operator writes this, from scripts/exclude_platform_defect_reviews.py;
# the review write path cannot produce it.
EXCLUSION_PLATFORM_DEFECT = "platform_defect"

WEIGHT_EXCLUSIONS = (
    EXCLUSION_COMMUNITY,
    EXCLUSION_SELF_DEALING,
    EXCLUSION_DUST_PAYMENT,
    EXCLUSION_UNSIGNED_PLACEHOLDER,
    EXCLUSION_PLATFORM_DEFECT,
)


def _prefix(alias: str) -> str:
    return f"{alias}." if alias else ""


def public_reviews(alias: str = "") -> str:
    """Reviews that may be counted and summarized publicly.

    Their text is never published: raw reviews stay in our tables, and only
    counts, scores and the generated summary (pros, cons, things to watch out
    for) leave the backend. Two kinds of row are held back:

    * The legacy stripe/manual admission path accepted unsigned placeholder
      rows (payments.verify_payment_reference). Nothing attributable stands
      behind them, so they are retained for continuity and never counted.
    * Reviews of a failure CrowdCode caused (EXCLUSION_PLATFORM_DEFECT).
    """
    p = _prefix(alias)
    return (
        f"(({p}signature_verified or {p}verified_source = 'hosted_receipt')"
        f" and {p}weight_exclusion is distinct from '{EXCLUSION_PLATFORM_DEFECT}')"
    )


def weighted_reviews(alias: str = "") -> str:
    """The ranking-weighted subset (docs/SCORING.md v4 §3.2): a verified hosted
    gateway receipt, or a paid review verified on chain, whose payer was
    neither self-dealing nor paying dust."""
    p = _prefix(alias)
    return f"({p}weight_basis in ('hosted', 'onchain') and {p}weight_exclusion is null)"


def legacy_reviews(alias: str = "") -> str:
    """The pre-cutover corpus, which keeps the weight it earned under v2 (§3.7)."""
    return f"({_prefix(alias)}weight_basis = 'legacy')"


def review_counts(alias: str = "") -> str:
    """The evidence counters every score payload carries."""
    p = _prefix(alias)
    public = public_reviews(alias)
    weighted = weighted_reviews(alias)
    legacy = legacy_reviews(alias)
    return f"""
              count({p}id) filter (where {public})::int as num_reviews,
              avg({p}rating) filter (where {public})::float as avg_rating,
              count({p}id) filter (where {public} and {p}payment_verified)::int
                as num_verified_reviews,
              avg({p}rating) filter (where {public} and {p}payment_verified)::float
                as verified_avg_rating,
              count({p}id) filter (where {public} and {p}payment_verification_level
                in ('onchain_verified', 'response_attested'))::int
                as num_onchain_verified_reviews,
              count({p}id) filter (where {public} and {p}payment_verification_level
                in ('unverified', 'signature_only'))::int
                as num_signature_only_reviews,
              count({p}id) filter (where {weighted})::int as num_weighted_reviews,
              avg({p}rating) filter (where {weighted})::float as weighted_avg_rating,
              count({p}id) filter (where {legacy})::int as num_legacy_reviews,
              count({p}id) filter (where {public} and not {weighted}
                and not {legacy})::int as num_community_reviews"""


def directory_listed(alias: str = "services") -> str:
    """Which services rows the public directory and search show.

    The services index (crowdcode.index) creates a row for every tracked
    resource that had none, so its evidence has a service to attach to. Until
    one of those rows is reviewed it stays out of the directory, the rankings
    and search: the index tracks thousands of resources, and which of them
    agents should see is the reliability rule's call, not the crawler's.
    """
    return (
        f"not (coalesce({alias}.metadata->>'listing_source', '') = 'index'"
        f" and not exists (select 1 from reviews listed_r where listed_r.service_id = {alias}.id))"
    )
