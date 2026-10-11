"""Maturity-aware illustrative yield calculation for synthetic fixtures.

This is a reference implementation, not production-certified. The default observation
window is an example only and must be approved/configured per client.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta

def calculate_yield(rows, *, cohort_start, cohort_end, as_of_date,
                    observation_days=90, unknown_source_label="unknown"):
    """Calculate join yield for applications in a defined cohort.

    Cohort membership is based on applied_at date (inclusive). An application is
    considered mature when as_of_date >= applied_at.date() + observation_days.
    Mature rejected/withdrawn applications enter the denominator as non-joins.
    Confirmed joined applications enter numerator and denominator only when the
    join timestamp is present. Immature or unresolved applications are reported
    separately and excluded from the mature-cohort denominator.

    IMPORTANT: observation_days=90 is illustrative, not a universal business rule.
    Real implementations must define whether the observation window means calendar
    days since application, requisition closure, or another client-approved policy.
    """
    start = date.fromisoformat(cohort_start)
    end = date.fromisoformat(cohort_end)
    as_of = date.fromisoformat(as_of_date)
    if end < start:
        raise ValueError("cohort_end must be on or after cohort_start")
    if observation_days < 0:
        raise ValueError("observation_days must be >= 0")

    stats = defaultdict(lambda: {
        "all_applications": 0,
        "mature_applications": 0,
        "joined_applications": 0,
        "mature_non_joined_applications": 0,
        "pending_or_immature_applications": 0,
        "unknown_source_applications": 0
    })

    for row in rows:
        applied_text = (row.get("applied_at") or "").strip()
        if not applied_text:
            continue  # required-field validator should report this separately
        applied_at = datetime.fromisoformat(applied_text)
        applied_day = applied_at.date()
        if not (start <= applied_day <= end):
            continue

        channel = (row.get("source_channel") or "").strip() or unknown_source_label
        g = stats[channel]
        g["all_applications"] += 1

        if channel.casefold() == unknown_source_label.casefold():
            g["unknown_source_applications"] += 1
            # Do not attribute unknown-source applications to a named channel.
            continue

        mature = as_of >= applied_day + timedelta(days=observation_days)
        outcome = (row.get("application_outcome") or "unknown").strip().lower()
        joined_at = (row.get("joined_at") or "").strip()

        if outcome == "joined" and joined_at:
            # A confirmed join is an outcome, but for a strict mature-cohort yield
            # include it only if the application itself has reached maturity.
            if mature:
                g["mature_applications"] += 1
                g["joined_applications"] += 1
            else:
                g["pending_or_immature_applications"] += 1
        elif mature and outcome in ("rejected", "withdrawn"):
            g["mature_applications"] += 1
            g["mature_non_joined_applications"] += 1
        else:
            g["pending_or_immature_applications"] += 1

    results = []
    for channel, g in sorted(stats.items()):
        denom = g["mature_applications"]
        result = dict(g)
        result.update({
            "channel": channel,
            "yield_percent": round(g["joined_applications"] / denom * 100, 2) if denom else None,
            "yield_status": "available" if denom else "not_applicable_no_mature_denominator"
        })
        results.append(result)
    return results

if __name__ == "__main__":
    import csv, json
    with open("applications.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(json.dumps(calculate_yield(
        rows, cohort_start="2026-01-01", cohort_end="2026-06-30",
        as_of_date="2026-10-11", observation_days=90
    ), indent=2))
