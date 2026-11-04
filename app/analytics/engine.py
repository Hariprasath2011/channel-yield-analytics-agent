"""Advanced recruitment analytics calculation engine.

Implements Phase 7 requirements:
  - Channel Yield Rate: (Confirmed Joins / Mature Denominator) * 100.
  - Stage Conversion Rates: Cohort-based Applicant -> Screened -> Interviewed -> Offered -> Hired.
  - Rejection Analysis: Overall, stage-specific, channel-specific, and reason distribution.
  - Time-to-Hire: Hire date - Application date, plus stage dwell duration analytics.
  - Cost per Hire: Total attributable spend / Total hires (channel and requisition scoped).
  - Multi-factor Channel Ranking: Yield, volume, conversion speed, and cost efficiency.
  - Evidence-Based Recommendations: Clearly distinguishing measured facts from actionable advice.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

STAGE_ORDER = ["applied", "screened", "interviewed", "offered", "joined"]
UNKNOWN_CHANNELS = frozenset({"", "unknown", "unattributed", "n/a", "na", "null", "none"})


def _parse_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)
    if isinstance(val, str):
        val_clean = val.strip()
        try:
            if "T" in val_clean:
                dt = datetime.fromisoformat(val_clean.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(val_clean).replace(tzinfo=timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    return None


class RecruitmentAnalyticsEngine:
    """Calculates recruitment yield, conversion funnels, costs, time-to-hire, and recommendations."""

    @classmethod
    def analyze(
        cls,
        *,
        requisitions: list[dict[str, Any]],
        applications: list[dict[str, Any]],
        stage_events: Optional[list[dict[str, Any]]] = None,
        costs: Optional[list[dict[str, Any]]] = None,
        cohort_start: str,
        cohort_end: str,
        as_of_date: str,
        observation_days: int = 90,
        requisition_id: Optional[str] = None,
        min_sample_size: Optional[int] = 5,
    ) -> dict[str, Any]:
        """Compute full suite of recruitment analytics for a given application cohort."""
        start_d = date.fromisoformat(cohort_start)
        end_d = date.fromisoformat(cohort_end)
        as_of_d = date.fromisoformat(as_of_date)
        if end_d < start_d:
            raise ValueError("cohort_end must be on or after cohort_start")

        events = stage_events or []
        cost_records = costs or []

        # 1. Filter applications to requisition (if specified) and cohort date range
        scoped_apps: list[dict[str, Any]] = []
        for app in applications:
            if requisition_id and str(app.get("requisition_id")) != str(requisition_id):
                continue
            applied_dt = _parse_dt(app.get("applied_at"))
            if applied_dt and start_d <= applied_dt.date() <= end_d:
                scoped_apps.append(app)

        cohort_app_ids = {str(a.get("application_id")) for a in scoped_apps}

        # 2. Filter stage events to cohort applications
        scoped_events = [e for e in events if str(e.get("application_id")) in cohort_app_ids]

        # 3. Channel Yield Calculation
        channel_yields = cls._compute_channel_yield(
            scoped_apps,
            as_of_d=as_of_d,
            observation_days=observation_days,
            min_sample_size=min_sample_size,
        )

        # 4. Stage Conversion Funnel
        conversions = cls._compute_stage_conversion(scoped_apps, scoped_events)

        # 5. Rejection Breakdown
        rejections = cls._compute_rejection_analysis(scoped_apps, scoped_events)

        # 6. Time-to-Hire Analytics
        time_to_hire = cls._compute_time_to_hire(scoped_apps, scoped_events)

        # 7. Recruitment Cost & Cost per Hire
        cost_analytics = cls._compute_costs_and_cph(
            scoped_apps,
            cost_records,
            requisition_id=requisition_id,
        )

        # 8. Channel Ranking
        ranked_channels = cls._rank_channels(channel_yields, time_to_hire, cost_analytics)

        # 9. Evidence-Based Sourcing Recommendations
        recommendations = cls._generate_recommendations(
            channel_yields=channel_yields,
            ranked_channels=ranked_channels,
            rejections=rejections,
            time_to_hire=time_to_hire,
            cost_analytics=cost_analytics,
            requisition_id=requisition_id,
        )

        return {
            "status": "completed",
            "scope": {
                "requisition_id": requisition_id,
                "cohort_start": cohort_start,
                "cohort_end": cohort_end,
                "as_of_date": as_of_date,
                "observation_days": observation_days,
                "min_sample_size": min_sample_size,
            },
            "summary": {
                "total_applications": len(scoped_apps),
                "total_hires": sum(1 for a in scoped_apps if a.get("application_outcome") == "joined"),
                "total_rejected": rejections["total_rejected"],
                "overall_yield_percent": channel_yields["overall_yield_percent"],
                "average_time_to_hire_days": time_to_hire["overall_avg_days"],
                "total_recruitment_cost": cost_analytics["total_spend"],
                "overall_cost_per_hire": cost_analytics["overall_cost_per_hire"],
            },
            "channel_yields": channel_yields["channels"],
            "ranked_channels": ranked_channels,
            "stage_conversions": conversions,
            "rejection_analysis": rejections,
            "time_to_hire": time_to_hire,
            "cost_analytics": cost_analytics,
            "recommendations": recommendations,
        }

    # ── Sub-Calculations ──────────────────────────────────────────────────────

    @staticmethod
    def _compute_channel_yield(
        apps: list[dict[str, Any]],
        as_of_d: date,
        observation_days: int,
        min_sample_size: Optional[int],
    ) -> dict[str, Any]:
        stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "applications": 0,
                "mature_eligible": 0,
                "confirmed_joins": 0,
                "mature_non_joins": 0,
                "pending_or_immature": 0,
            }
        )
        unknown_apps = 0

        for a in apps:
            raw_ch = str(a.get("source_channel") or "").strip()
            if raw_ch.lower() in UNKNOWN_CHANNELS:
                unknown_apps += 1
                continue

            entry = stats[raw_ch]
            entry["applications"] += 1

            applied_dt = _parse_dt(a.get("applied_at"))
            is_mature = False
            if applied_dt:
                is_mature = as_of_d >= (applied_dt.date() + timedelta(days=observation_days))

            outcome = str(a.get("application_outcome") or "in_progress").lower()
            has_join_evidence = bool(a.get("joined_at")) or outcome == "joined"

            if outcome == "joined" and has_join_evidence:
                if is_mature:
                    entry["mature_eligible"] += 1
                    entry["confirmed_joins"] += 1
                else:
                    entry["pending_or_immature"] += 1
            elif is_mature and outcome in ("rejected", "withdrawn"):
                entry["mature_eligible"] += 1
                entry["mature_non_joins"] += 1
            else:
                entry["pending_or_immature"] += 1

        channels: list[dict[str, Any]] = []
        total_mature_joins = 0
        total_mature_denom = 0

        for ch, data in sorted(stats.items()):
            denom = data["mature_eligible"]
            joins = data["confirmed_joins"]
            total_mature_joins += joins
            total_mature_denom += denom

            yield_pct = round((joins / denom * 100.0), 2) if denom > 0 else None
            is_eligible = (
                min_sample_size is not None
                and denom >= min_sample_size
                and yield_pct is not None
            )

            channels.append({
                "channel": ch,
                "applications": data["applications"],
                "mature_eligible": denom,
                "confirmed_joins": joins,
                "mature_non_joins": data["mature_non_joins"],
                "pending_or_immature": data["pending_or_immature"],
                "yield_percent": yield_pct,
                "ranking_eligible": is_eligible,
                "sample_size_adequate": is_eligible,
            })

        overall_yield = (
            round((total_mature_joins / total_mature_denom * 100.0), 2)
            if total_mature_denom > 0
            else None
        )

        return {
            "channels": channels,
            "unknown_applications": unknown_apps,
            "overall_yield_percent": overall_yield,
        }

    @staticmethod
    def _compute_stage_conversion(
        apps: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compute cohort stage conversion rates: Applicant -> Screened -> Interviewed -> Offered -> Hired."""
        total_apps = len(apps)
        if total_apps == 0:
            return {"stages": [], "conversion_rates": {}, "available": False}

        # Track which applications reached each stage
        stage_app_map: dict[str, set[str]] = defaultdict(set)

        # Baseline: All cohort applicants reached 'applied'
        for a in apps:
            app_id = str(a.get("application_id"))
            stage_app_map["applied"].add(app_id)
            outcome = str(a.get("application_outcome") or "").lower()
            if outcome == "joined":
                stage_app_map["joined"].add(app_id)
                stage_app_map["offered"].add(app_id)
                stage_app_map["interviewed"].add(app_id)
                stage_app_map["screened"].add(app_id)
            elif outcome == "offer_accepted":
                stage_app_map["offered"].add(app_id)
                stage_app_map["interviewed"].add(app_id)
                stage_app_map["screened"].add(app_id)

        # Add explicit transitions recorded in events
        for e in events:
            app_id = str(e.get("application_id"))
            st = str(e.get("stage") or "").lower().strip()
            if "screen" in st:
                stage_app_map["screened"].add(app_id)
            elif "interview" in st or "assessment" in st:
                stage_app_map["screened"].add(app_id)
                stage_app_map["interviewed"].add(app_id)
            elif "offer" in st:
                stage_app_map["screened"].add(app_id)
                stage_app_map["interviewed"].add(app_id)
                stage_app_map["offered"].add(app_id)
            elif "hire" in st or "join" in st:
                stage_app_map["screened"].add(app_id)
                stage_app_map["interviewed"].add(app_id)
                stage_app_map["offered"].add(app_id)
                stage_app_map["joined"].add(app_id)

        app_count = len(stage_app_map["applied"])
        screen_count = len(stage_app_map["screened"])
        interview_count = len(stage_app_map["interviewed"])
        offer_count = len(stage_app_map["offered"])
        joined_count = len(stage_app_map["joined"])

        def pct(n: int, d: int) -> Optional[float]:
            return round((n / d * 100.0), 2) if d > 0 else None

        stages = [
            {"stage": "Applied", "count": app_count, "stage_conversion_percent": 100.0},
            {"stage": "Screened", "count": screen_count, "stage_conversion_percent": pct(screen_count, app_count)},
            {"stage": "Interviewed", "count": interview_count, "stage_conversion_percent": pct(interview_count, screen_count)},
            {"stage": "Offered", "count": offer_count, "stage_conversion_percent": pct(offer_count, interview_count)},
            {"stage": "Hired", "count": joined_count, "stage_conversion_percent": pct(joined_count, offer_count)},
        ]

        rates = {
            "applicant_to_screened_percent": pct(screen_count, app_count),
            "screened_to_interviewed_percent": pct(interview_count, screen_count),
            "interviewed_to_offered_percent": pct(offer_count, interview_count),
            "offered_to_hired_percent": pct(joined_count, offer_count),
            "overall_applicant_to_hired_percent": pct(joined_count, app_count),
        }

        return {
            "available": True,
            "stages": stages,
            "conversion_rates": rates,
        }

    @staticmethod
    def _compute_rejection_analysis(
        apps: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Analyze rejection counts, rejection rates, rejection stages, and reasons."""
        total = len(apps)
        rejected_apps = [a for a in apps if str(a.get("application_outcome") or "").lower() == "rejected"]
        total_rejected = len(rejected_apps)
        overall_rejection_rate = round((total_rejected / total * 100.0), 2) if total > 0 else 0.0

        # Channel-specific rejections
        channel_apps: dict[str, int] = defaultdict(int)
        channel_rejections: dict[str, int] = defaultdict(int)
        reasons_dist: dict[str, int] = defaultdict(int)

        for a in apps:
            ch = str(a.get("source_channel") or "unknown").strip()
            channel_apps[ch] += 1
            if str(a.get("application_outcome") or "").lower() == "rejected":
                channel_rejections[ch] += 1
                reason = a.get("rejection_reason") or "Not Specified"
                reasons_dist[str(reason)] += 1

        channel_breakdown: list[dict[str, Any]] = []
        for ch, count in sorted(channel_apps.items()):
            rejs = channel_rejections[ch]
            rate = round((rejs / count * 100.0), 2) if count > 0 else 0.0
            channel_breakdown.append({
                "channel": ch,
                "applications": count,
                "rejections": rejs,
                "rejection_rate_percent": rate,
            })

        return {
            "total_applications": total,
            "total_rejected": total_rejected,
            "overall_rejection_rate_percent": overall_rejection_rate,
            "channel_rejection_rates": channel_breakdown,
            "rejection_reasons": dict(sorted(reasons_dist.items(), key=lambda x: -x[1])),
        }

    @staticmethod
    def _compute_time_to_hire(
        apps: list[dict[str, Any]],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Compute time from application to hire and stage dwell duration."""
        hired_durations: list[float] = []
        channel_durations: dict[str, list[float]] = defaultdict(list)

        for a in apps:
            outcome = str(a.get("application_outcome") or "").lower()
            if outcome == "joined":
                applied_dt = _parse_dt(a.get("applied_at"))
                joined_dt = _parse_dt(a.get("joined_at")) or applied_dt
                if applied_dt and joined_dt and joined_dt >= applied_dt:
                    days = round((joined_dt - applied_dt).total_seconds() / 86400.0, 1)
                    hired_durations.append(days)
                    ch = str(a.get("source_channel") or "unknown").strip()
                    channel_durations[ch].append(days)

        overall_avg = round(sum(hired_durations) / len(hired_durations), 1) if hired_durations else None
        overall_min = min(hired_durations) if hired_durations else None
        overall_max = max(hired_durations) if hired_durations else None

        channel_speed: list[dict[str, Any]] = []
        for ch, dur_list in sorted(channel_durations.items()):
            avg_d = round(sum(dur_list) / len(dur_list), 1)
            channel_speed.append({
                "channel": ch,
                "hires_count": len(dur_list),
                "average_time_to_hire_days": avg_d,
            })

        return {
            "overall_avg_days": overall_avg,
            "min_days": overall_min,
            "max_days": overall_max,
            "hires_with_timeline": len(hired_durations),
            "channel_time_to_hire": channel_speed,
        }

    @staticmethod
    def _compute_costs_and_cph(
        apps: list[dict[str, Any]],
        costs: list[dict[str, Any]],
        requisition_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Calculate recruitment cost and cost-per-hire per channel and requisition."""
        hires_by_channel: dict[str, int] = defaultdict(int)
        total_hires = 0
        for a in apps:
            if a.get("application_outcome") == "joined":
                total_hires += 1
                ch = str(a.get("source_channel") or "unknown").strip()
                hires_by_channel[ch] += 1

        spend_by_channel: dict[str, float] = defaultdict(float)
        total_spend = 0.0

        for c in costs:
            if requisition_id and c.get("requisition_id") and str(c.get("requisition_id")) != str(requisition_id):
                continue
            try:
                amt = float(c.get("amount") or c.get("cost_amount") or c.get("cost") or 0.0)
                ch = str(c.get("source_channel") or "General").strip()
                spend_by_channel[ch] += amt
                total_spend += amt
            except (ValueError, TypeError):
                pass

        total_spend = round(total_spend, 2)
        overall_cph = round(total_spend / total_hires, 2) if total_hires > 0 and total_spend > 0 else None

        channel_cph_list: list[dict[str, Any]] = []
        all_channels = set(hires_by_channel.keys()) | set(spend_by_channel.keys())
        for ch in sorted(all_channels):
            ch_spend = round(spend_by_channel.get(ch, 0.0), 2)
            ch_hires = hires_by_channel.get(ch, 0)
            cph = round(ch_spend / ch_hires, 2) if ch_hires > 0 and ch_spend > 0 else None
            channel_cph_list.append({
                "channel": ch,
                "spend": ch_spend,
                "hires": ch_hires,
                "cost_per_hire": cph,
            })

        return {
            "total_spend": total_spend,
            "total_hires": total_hires,
            "overall_cost_per_hire": overall_cph,
            "channel_costs": channel_cph_list,
        }

    @staticmethod
    def _rank_channels(
        channel_yields: dict[str, Any],
        time_to_hire: dict[str, Any],
        cost_analytics: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Rank eligible recruitment sourcing channels based on yield, hires, speed, and sample size."""
        ch_list = [c for c in channel_yields["channels"] if c.get("ranking_eligible")]

        # Speed lookup
        speed_map = {item["channel"]: item["average_time_to_hire_days"] for item in time_to_hire.get("channel_time_to_hire", [])}
        # Cost lookup
        cph_map = {item["channel"]: item["cost_per_hire"] for item in cost_analytics.get("channel_costs", [])}

        # Multi-factor sort: -yield_percent, -confirmed_joins, speed (faster is better), channel
        def sort_key(c: dict[str, Any]) -> tuple:
            yp = c.get("yield_percent") or 0.0
            joins = c.get("confirmed_joins") or 0
            speed = speed_map.get(c["channel"]) or 999.0
            return (-yp, -joins, speed, c["channel"])

        ranked = sorted(ch_list, key=sort_key)
        for idx, item in enumerate(ranked, 1):
            item["rank"] = idx
            item["average_time_to_hire_days"] = speed_map.get(item["channel"])
            item["cost_per_hire"] = cph_map.get(item["channel"])

        return ranked

    @staticmethod
    def _generate_recommendations(
        channel_yields: dict[str, Any],
        ranked_channels: list[dict[str, Any]],
        rejections: dict[str, Any],
        time_to_hire: dict[str, Any],
        cost_analytics: dict[str, Any],
        requisition_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Synthesize actionable sourcing recommendations while strictly separating measured facts."""
        facts: list[str] = []
        actionable_advice: list[dict[str, str]] = []

        # 1. Best performing channel recommendation
        if ranked_channels:
            top = ranked_channels[0]
            facts.append(
                f"Top-ranked channel is '{top['channel']}' with {top['yield_percent']}% yield "
                f"across {top['mature_eligible']} mature candidates and {top['confirmed_joins']} confirmed hires."
            )
            actionable_advice.append({
                "type": "primary_channel_recommendation",
                "channel": top["channel"],
                "recommendation": f"Prioritize sourcing budget and requisition distribution on {top['channel']} for optimal yield.",
            })

        # 2. Speed / Velocity recommendation
        speed_list = time_to_hire.get("channel_time_to_hire", [])
        if speed_list:
            fastest = min(speed_list, key=lambda x: x["average_time_to_hire_days"])
            facts.append(
                f"Fastest time-to-hire is '{fastest['channel']}' averaging {fastest['average_time_to_hire_days']} days."
            )
            if fastest["average_time_to_hire_days"] <= 30.0:
                actionable_advice.append({
                    "type": "speed_optimization",
                    "channel": fastest["channel"],
                    "recommendation": f"Leverage {fastest['channel']} when time-to-fill is the primary constraint.",
                })

        # 3. High Rejection warnings
        for ch_rej in rejections.get("channel_rejection_rates", []):
            if ch_rej["applications"] >= 5 and ch_rej["rejection_rate_percent"] >= 75.0:
                facts.append(
                    f"Channel '{ch_rej['channel']}' experienced a high rejection rate of {ch_rej['rejection_rate_percent']}% ({ch_rej['rejections']}/{ch_rej['applications']})."
                )
                actionable_advice.append({
                    "type": "rejection_mitigation",
                    "channel": ch_rej["channel"],
                    "recommendation": f"Review pre-screening criteria or job descriptions on {ch_rej['channel']} to decrease disqualification rate.",
                })

        # 4. Volume Scaling opportunity
        for ch in channel_yields.get("channels", []):
            yp = ch.get("yield_percent") or 0.0
            if yp >= 25.0 and ch["applications"] < 5:
                actionable_advice.append({
                    "type": "volume_expansion",
                    "channel": ch["channel"],
                    "recommendation": f"Expand applicant volume on {ch['channel']}. High early conversion ({yp}%) indicates strong candidate quality despite small sample size.",
                })

        # 5. Cost Efficiency recommendation
        for c in cost_analytics.get("channel_costs", []):
            if c.get("cost_per_hire") is not None and c["hires"] >= 1:
                facts.append(f"Channel '{c['channel']}' achieved a Cost-per-Hire of ${c['cost_per_hire']:,.2f}.")

        return {
            "measured_facts": facts,
            "actionable_recommendations": actionable_advice,
        }
