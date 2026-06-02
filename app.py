# app.py
from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

# Google Ads
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


# -------------------- App & MCP basics --------------------
APP_NAME = "mcp-google-ads"
APP_VER = "0.3.0"
MCP_PROTO_DEFAULT = "2024-11-05"

app = FastAPI()


# -------------------- Env & Ads client --------------------
DEV_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
LOGIN_CUSTOMER_ID = (os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "") or "").replace("-", "").strip()


def _require_env() -> None:
    missing = [k for k, v in [
        ("GOOGLE_ADS_DEVELOPER_TOKEN", DEV_TOKEN),
        ("GOOGLE_ADS_CLIENT_ID", CLIENT_ID),
        ("GOOGLE_ADS_CLIENT_SECRET", CLIENT_SECRET),
        ("GOOGLE_ADS_REFRESH_TOKEN", REFRESH_TOKEN),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing required env: {', '.join(missing)}")


def _new_ads_client(login_cid: Optional[str] = None) -> GoogleAdsClient:
    _require_env()
    cfg = {
        "developer_token": DEV_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "use_proto_plus": True,
    }
    final_login = (login_cid or LOGIN_CUSTOMER_ID or "").replace("-", "").strip()
    if final_login:
        cfg["login_customer_id"] = final_login
    return GoogleAdsClient.load_from_dict(cfg)


def _money(micros: int | None) -> float:
    return round((micros or 0) / 1_000_000, 6)


def _where_time(args: Dict[str, Any]) -> str:
    """Return a GAQL WHERE date fragment from date_preset or time_range."""
    date_preset = (args.get("date_preset") or "").upper().strip()
    tr = args.get("time_range") or {}
    if tr.get("since") and tr.get("until"):
        return f" segments.date BETWEEN '{tr['since']}' AND '{tr['until']}' "
    if date_preset in {"TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_30_DAYS", "THIS_MONTH", "LAST_MONTH"}:
        return f" segments.date DURING {date_preset} "
    return " segments.date DURING LAST_30_DAYS "


def _err_from_gax(e: GoogleAdsException) -> Dict[str, Any]:
    status = e.error.code().name if hasattr(e, "error") else "UNKNOWN"
    rid = getattr(e, "request_id", None)
    details: Dict[str, Any] = {"status": status, "request_id": rid}
    try:
        if getattr(e, "failure", None) and e.failure.errors:
            details["errors"] = [{"message": er.message} for er in e.failure.errors]
    except Exception:
        pass
    return details


# -------------------- Minimal tools --------------------
def tool_ping(_args: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True}


def tool_debug_login_header(_args: Dict[str, Any]) -> Dict[str, Any]:
    return {"env_LOGIN_CUSTOMER_ID": LOGIN_CUSTOMER_ID}


def tool_echo_short(args: Dict[str, Any]) -> Dict[str, Any]:
    m = (args.get("msg") or "").strip()
    if not m:
        return {"error": {"detail": "msg required"}}
    return {"msg": m}


def tool_noop_ok(_args: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True}


def tool_list_resources(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("CustomerService")
        resp = svc.list_accessible_customers()
        customers: List[Dict[str, str]] = []
        for rn in resp.resource_names:
            customers.append({"resource_name": rn, "customer_id": rn.split("/")[-1]})
        return {"count": len(customers), "customers": customers}
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}


# -------------------- Campaign summary (with min_spend) --------------------
def tool_fetch_campaign_summary(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inputs (customer_id recommended):
      customer_id: "1234567890"
      login_customer_id: MCC header override (recommended for permissioned clients)
      date_preset: TODAY|YESTERDAY|LAST_7_DAYS|LAST_30_DAYS|THIS_MONTH|LAST_MONTH
      time_range: {"since":"YYYY-MM-DD","until":"YYYY-MM-DD"}  # overrides date_preset
      min_spend: number >= 1.0 (account currency), default 1.0
    """
    login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
    customer_id = (args.get("customer_id") or "").replace("-", "") or ""
    if not customer_id:
        return {"error": {"detail": "customer_id required"}}

    where_time = _where_time(args)
    min_spend = max(1.0, float(args.get("min_spend", 1.0)))
    min_cost_micros = int(min_spend * 1_000_000)

    q = f"""
    SELECT
      campaign.id, campaign.name, campaign.status,
      metrics.impressions, metrics.clicks, metrics.cost_micros,
      metrics.conversions, metrics.conversions_value
    FROM campaign
    WHERE {where_time}
      AND metrics.cost_micros >= {min_cost_micros}
    ORDER BY metrics.cost_micros DESC
    """

    try:
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("GoogleAdsService")
        resp = svc.search(request={"customer_id": customer_id, "query": q})

        out: List[Dict[str, Any]] = []
        for r in resp:
            cost = _money(getattr(r.metrics, "cost_micros", 0))
            imps = int(getattr(r.metrics, "impressions", 0) or 0)
            clicks = int(getattr(r.metrics, "clicks", 0) or 0)
            conv = float(getattr(r.metrics, "conversions", 0.0) or 0.0)
            conv_val = float(getattr(r.metrics, "conversions_value", 0.0) or 0.0)
            ctr = (clicks / imps * 100) if imps else 0.0
            cpc = (cost / clicks) if clicks else 0.0
            cpa = (cost / conv) if conv else 0.0
            roas = (conv_val / cost) if cost > 0 else 0.0
            out.append({
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "status": r.campaign.status.name,
                "impressions": imps,
                "clicks": clicks,
                "cost": round(cost, 2),
                "conversions": round(conv, 2),
                "conv_value": round(conv_val, 2),
                "ctr_pct": round(ctr, 2),
                "cpc": round(cpc, 2),
                "cpa": round(cpa, 2),
                "roas": round(roas, 2),
            })
        return {"query": q, "rows": out}
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}


# -------------------- Generic metrics (with optional min_spend) --------------------
ENTITY_FROM = {
    "account": "customer",
    "campaign": "campaign",
    "ad_group": "ad_group",
    "ad": "ad_group_ad",
}

def tool_fetch_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inputs:
      customer_id (required)
      entity: account|campaign|ad_group|ad (default campaign)
      ids: ["123","456"] optional
      fields: GAQL fields list (default common set)
      date_preset OR time_range like above
      min_spend: number >= 1.0 (optional; when provided filters by spend)
      login_customer_id optional
    """
    login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
    customer_id = (args.get("customer_id") or "").replace("-", "") or ""
    if not customer_id:
        return {"error": {"detail": "customer_id required"}}

    entity = (args.get("entity") or "campaign").lower()
    if entity not in ENTITY_FROM:
        return {"error": {"detail": f"invalid entity '{entity}'"}}

    fields = args.get("fields") or [
        "metrics.cost_micros",
        "metrics.clicks",
        "metrics.impressions",
        "metrics.conversions",
        "metrics.conversions_value",
    ]
    ids = [str(x).replace("-", "") for x in (args.get("ids") or [])]

    where_time = _where_time(args)

    id_col = {
        "account": "customer.id",
        "campaign": "campaign.id",
        "ad_group": "ad_group.id",
        "ad": "ad_group_ad.ad.id",
    }[entity]
    id_clause = f" AND {id_col} IN ({','.join(ids)}) " if ids else ""

    # Optional spend filter
    min_spend = args.get("min_spend", None)
    spend_clause = ""
    if min_spend is not None:
        try:
            ms = max(1.0, float(min_spend))
            spend_clause = f" AND metrics.cost_micros >= {int(ms * 1_000_000)} "
        except Exception:
            pass  # ignore invalid and do not filter

    base_cols = {
        "account": ["customer.id", "customer.descriptive_name"],
        "campaign": ["campaign.id", "campaign.name", "campaign.status"],
        "ad_group": ["ad_group.id", "ad_group.name", "ad_group.status", "campaign.id", "campaign.name"],
        "ad": ["ad_group_ad.ad.id", "ad_group.id", "ad_group.name", "campaign.id", "campaign.name"],
    }[entity]

    select_cols = base_cols + fields
    frm = ENTITY_FROM[entity]
    q = f"""
    SELECT {', '.join(select_cols)}
    FROM {frm}
    WHERE {where_time}{id_clause}{spend_clause}
    """

    try:
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("GoogleAdsService")
        resp = svc.search(request={"customer_id": customer_id, "query": q})

        out: List[Dict[str, Any]] = []
        for r in resp:
            row: Dict[str, Any] = {}
            if entity == "account":
                row["customer_id"] = str(r.customer.id)
                row["customer_name"] = r.customer.descriptive_name
            elif entity == "campaign":
                row["campaign_id"] = str(r.campaign.id)
                row["campaign_name"] = r.campaign.name
                row["campaign_status"] = r.campaign.status.name
            elif entity == "ad_group":
                row["ad_group_id"] = str(r.ad_group.id)
                row["ad_group_name"] = r.ad_group.name
                row["ad_group_status"] = r.ad_group.status.name
                row["campaign_id"] = str(r.campaign.id)
                row["campaign_name"] = r.campaign.name
            else:
                row["ad_id"] = str(r.ad_group_ad.ad.id)
                row["ad_group_id"] = str(r.ad_group.id)
                row["ad_group_name"] = r.ad_group.name
                row["campaign_id"] = str(r.campaign.id)
                row["campaign_name"] = r.campaign.name

            m = r.metrics
            row.update({
                "cost": _money(getattr(m, "cost_micros", 0)),
                "impressions": int(getattr(m, "impressions", 0) or 0),
                "clicks": int(getattr(m, "clicks", 0) or 0),
                "conversions": float(getattr(m, "conversions", 0.0) or 0.0),
                "conversions_value": float(getattr(m, "conversions_value", 0.0) or 0.0),
            })
            out.append(row)

        return {"query": q, "rows": out}
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}


# -------------------- Search terms (top spend) --------------------
def tool_fetch_search_terms(args: Dict[str, Any]) -> Dict[str, Any]:
    login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
    customer_id = (args.get("customer_id") or "").replace("-", "") or ""
    if not customer_id:
        return {"error": {"detail": "customer_id required"}}

    where_time = _where_time(args)

    min_spend = max(1.0, float(args.get("min_spend", 1.0)))
    min_cost_micros = int(min_spend * 1_000_000)
    min_clicks = int(args.get("min_clicks", 0))

    cids = [c.replace("-", "") for c in (args.get("campaign_ids") or [])]
    agids = [g.replace("-", "") for g in (args.get("ad_group_ids") or [])]

    filters = [where_time, f" AND metrics.cost_micros >= {min_cost_micros} "]
    if min_clicks > 0:
        filters.append(f" AND metrics.clicks >= {min_clicks} ")
    if cids:
        filters.append(f" AND campaign.id IN ({','.join(cids)}) ")
    if agids:
        filters.append(f" AND ad_group.id IN ({','.join(agids)}) ")

    limit = max(1, min(int(args.get("limit", 100)), 1000))

    q = f"""
    SELECT
      search_term_view.search_term,
      campaign.id, campaign.name,
      ad_group.id, ad_group.name,
      metrics.impressions,
      metrics.clicks,
      metrics.cost_micros,
      metrics.conversions,
      metrics.conversions_value
    FROM search_term_view
    WHERE {''.join(filters)}
    ORDER BY metrics.cost_micros DESC
    LIMIT {limit}
    """

    try:
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("GoogleAdsService")
        rows = svc.search(request={"customer_id": customer_id, "query": q})

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "search_term": r.search_term_view.search_term,
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "ad_group_id": str(r.ad_group.id),
                "ad_group_name": r.ad_group.name,
                "impressions": int(r.metrics.impressions or 0),
                "clicks": int(r.metrics.clicks or 0),
                "cost": _money(r.metrics.cost_micros),
                "conversions": float(r.metrics.conversions or 0.0),
                "conv_value": float(r.metrics.conversions_value or 0.0),
            })
        return {"query": q, "rows": out}
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}


# -------------------- Change history --------------------
def tool_fetch_change_history(args: Dict[str, Any]) -> Dict[str, Any]:
    login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
    customer_id = (args.get("customer_id") or "").replace("-", "") or ""
    if not customer_id:
        return {"error": {"detail": "customer_id required"}}

    tr = args.get("time_range") or {}
    since = tr.get("since")
    until = tr.get("until")
    if not (since and until):
        return {"error": {"detail": "time_range.since and time_range.until are required"}}

    limit = max(1, min(int(args.get("limit", 200)), 1000))

    types = args.get("resource_types") or []
    type_filter = ""
    if types:
        safe = ",".join([f"'{t}'" for t in types])
        type_filter = f" AND change_event.resource_type IN ({safe}) "

    q = f"""
    SELECT
      change_event.change_date_time,
      change_event.resource_type,
      change_event.client_type,
      change_event.user_email,
      change_event.change_resource_name
    FROM change_event
    WHERE change_event.change_date_time BETWEEN '{since} 00:00:00' AND '{until} 23:59:59'
      {type_filter}
    ORDER BY change_event.change_date_time DESC
    LIMIT {limit}
    """

    try:
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("GoogleAdsService")
        rows = svc.search(request={"customer_id": customer_id, "query": q})

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "time": r.change_event.change_date_time,
                "resource_type": r.change_event.resource_type.name,
                "client_type": r.change_event.client_type.name,
                "user": r.change_event.user_email,
                "change_resource_name": r.change_event.change_resource_name,
            })
        return {"query": q, "changes": out}
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}


# -------------------- Budget pacing --------------------
def tool_fetch_budget_pacing(args: Dict[str, Any]) -> Dict[str, Any]:
    login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
    customer_id = (args.get("customer_id") or "").replace("-", "") or ""
    if not customer_id:
        return {"error": {"detail": "customer_id required"}}

    month = args.get("month")
    target = args.get("target_spend")
    if not (month and target is not None):
        return {"error": {"detail": "month and target_spend are required"}}

    target = float(target)
    year, mon = map(int, month.split("-"))
    start = datetime.date(year, mon, 1)
    today = datetime.date.today()

    if today.year == year and today.month == mon:
        end = today
        days_elapsed = (end - start).days + 1
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        days_in_month = (next_month - start).days
    else:
        next_month = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end = next_month - datetime.timedelta(days=1)
        days_in_month = (next_month - start).days
        days_elapsed = days_in_month

    q = f"""
    SELECT
      segments.date,
      metrics.cost_micros
    FROM customer
    WHERE segments.date BETWEEN '{start:%Y-%m-%d}' AND '{end:%Y-%m-%d}'
    """

    try:
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("GoogleAdsService")
        rows = svc.search(request={"customer_id": customer_id, "query": q})

        mtd_cost = 0.0
        for r in rows:
            mtd_cost += _money(r.metrics.cost_micros)

        avg_per_day = (mtd_cost / days_elapsed) if days_elapsed else 0.0
        projected_eom = round(avg_per_day * days_in_month, 2)

        pace_status = "on_track"
        if projected_eom > target * 1.05:
            pace_status = "over"
        elif projected_eom < target * 0.95:
            pace_status = "under"

        return {
            "month": month,
            "target": round(target, 2),
            "mtd_spend": round(mtd_cost, 2),
            "projected_eom": projected_eom,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "pace_status": pace_status,
            "query": q
        }
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}

# -------------------- Geo performance --------------------
def tool_fetch_geo_performance(args: Dict[str, Any]) -> Dict[str, Any]:
    login = (args.get("login_customer_id") or LOGIN_CUSTOMER_ID or "").replace("-", "") or None
    customer_id = (args.get("customer_id") or "").replace("-", "") or ""
    if not customer_id:
        return {"error": {"detail": "customer_id required"}}

    level = (args.get("level") or "city").lower().strip()
    level_map = {
        "city": ("geo_target_city", "city"),
        "region": ("geo_target_region", "region"),
        "country": ("geo_target_country", "country"),
    }
    if level not in level_map:
        return {"error": {"detail": f"invalid level '{level}' (use city|region|country)"}}
    geo_attr, geo_key = level_map[level]

    view = (args.get("view") or "geographic").lower().strip()
    if view not in {"geographic", "user_location"}:
        return {"error": {"detail": f"invalid view '{view}' (use geographic|user_location)"}}
    from_view = "geographic_view" if view == "geographic" else "user_location_view"

    where_time = _where_time(args)

    cids = [str(c).replace("-", "").strip() for c in (args.get("campaign_ids") or []) if str(c).strip()]
    cid_clause = f" AND campaign.id IN ({','.join(cids)}) " if cids else ""

    # Optional spend filter (>= 0 allowed to surface zeros if the caller wants it)
    spend_clause = ""
    if args.get("min_spend") is not None:
        try:
            ms = max(0.0, float(args.get("min_spend", 0.0)))
            spend_clause = f" AND metrics.cost_micros >= {int(ms * 1_000_000)} "
        except Exception:
            pass

    select_cols = [
        "campaign.id",
        "campaign.name",
        f"segments.{geo_attr}",
        "metrics.impressions",
        "metrics.clicks",
        "metrics.cost_micros",
        "metrics.conversions",
        "metrics.conversions_value",
    ]

    q = f"""
    SELECT
      {', '.join(select_cols)}
    FROM {from_view}
    WHERE {where_time}{cid_clause}{spend_clause}
    ORDER BY metrics.cost_micros DESC
    """

    try:
        client = _new_ads_client(login_cid=login)
        svc = client.get_service("GoogleAdsService")
        rows = svc.search(request={"customer_id": customer_id, "query": q})

        out: List[Dict[str, Any]] = []
        totals_by_campaign: Dict[str, Dict[str, float]] = {}

        for r in rows:
            cost = _money(getattr(r.metrics, "cost_micros", 0))
            imps = int(getattr(r.metrics, "impressions", 0) or 0)
            clicks = int(getattr(r.metrics, "clicks", 0) or 0)
            conv = float(getattr(r.metrics, "conversions", 0.0) or 0.0)
            conv_val = float(getattr(r.metrics, "conversions_value", 0.0) or 0.0)

            # Geo label from segments
            geo_label = getattr(r.segments, geo_attr, None)
            # Some client versions return None or empty; normalize to ""
            geo_label = str(geo_label) if geo_label is not None else ""

            row = {
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                geo_key: geo_label,
                "impressions": imps,
                "clicks": clicks,
                "cost": round(cost, 2),
                "conversions": round(conv, 2),
                "conv_value": round(conv_val, 2),
            }
            out.append(row)

            # Totals per campaign (handy for QA)
            key = str(r.campaign.id)
            if key not in totals_by_campaign:
                totals_by_campaign[key] = {"cost": 0.0, "clicks": 0.0, "impressions": 0.0, "conversions": 0.0, "conv_value": 0.0}
            totals_by_campaign[key]["cost"] += cost
            totals_by_campaign[key]["clicks"] += clicks
            totals_by_campaign[key]["impressions"] += imps
            totals_by_campaign[key]["conversions"] += conv
            totals_by_campaign[key]["conv_value"] += conv_val

        # Round totals for readability
        totals = {
            cid: {
                "cost": round(v["cost"], 2),
                "clicks": int(v["clicks"]),
                "impressions": int(v["impressions"]),
                "conversions": round(v["conversions"], 2),
                "conv_value": round(v["conv_value"], 2),
            }
            for cid, v in totals_by_campaign.items()
        }

        return {"query": q, "view": from_view, "level": level, "rows": out, "totals_by_campaign": totals}
    except GoogleAdsException as e:
        return {"error": _err_from_gax(e)}
    except Exception as e:
        return {"error": {"detail": str(e)}}



# ---------- TOOLS (schemas) ----------
TOOLS = [
    {
        "name": "fetch_campaign_summary",
        "description": "Per-campaign KPIs with computed ctr/cpc/cpa/roas. Supports min_spend to filter by spend in the date range.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "customer_id":   {"type": "string", "maxLength": 20, "pattern": "^[0-9-]*$"},
                "date_preset":   {"type": "string", "enum": ["TODAY","YESTERDAY","LAST_7_DAYS","LAST_30_DAYS","THIS_MONTH","LAST_MONTH"]},
                "time_range": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "since": {"type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                        "until": {"type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}
                    }
                },
                "min_spend": {
                    "type": "number",
                    "description": "Minimum spend (account currency) in the selected time range.",
                    "minimum": 1,
                    "default": 1.0
                },
                "login_customer_id": {"type": "string", "maxLength": 20, "pattern": "^[0-9-]*$"}
            }
        }
    },
    {
        "name": "fetch_metrics",
        "description": "Generic metrics for account/campaign/ad_group/ad. Optional min_spend filter.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "customer_id":   {"type": "string", "maxLength": 20, "pattern": "^[0-9-]*$"},
                "entity": {"type": "string", "enum": ["account","campaign","ad_group","ad"], "default": "campaign"},
                "ids": {
                    "type": "array", "maxItems": 200,
                    "items": {"type": "string", "maxLength": 30, "pattern": "^[0-9-]*$"}
                },
                "fields": {
                    "type": "array", "maxItems": 100,
                    "items": {"type": "string", "maxLength": 64},
                    "default": ["metrics.cost_micros","metrics.clicks","metrics.impressions","metrics.conversions","metrics.conversions_value"]
                },
                "date_preset": {"type": "string", "enum": ["TODAY","YESTERDAY","LAST_7_DAYS","LAST_30_DAYS","THIS_MONTH","LAST_MONTH"]},
                "time_range": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "since": {"type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                        "until": {"type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}
                    }
                },
                "min_spend": {"type": "number", "minimum": 1},
                "login_customer_id": {"type": "string", "maxLength": 20, "pattern": "^[0-9-]*$"}
            }
        }
    },
    {
        "name": "fetch_search_terms",
        "description": "Top search terms by spend (and optional filters).",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "customer_id":   { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" },
                "date_preset":   { "type": "string", "enum": ["TODAY","YESTERDAY","LAST_7_DAYS","LAST_30_DAYS","THIS_MONTH","LAST_MONTH"] },
                "time_range": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "since": { "type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
                        "until": { "type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$" }
                    }
                },
                "min_spend":     { "type": "number", "minimum": 1, "default": 1.0 },
                "min_clicks":    { "type": "integer", "minimum": 0, "default": 0 },
                "campaign_ids":  { "type": "array", "maxItems": 200, "items": { "type": "string", "maxLength": 30, "pattern": "^[0-9-]*$" } },
                "ad_group_ids":  { "type": "array", "maxItems": 200, "items": { "type": "string", "maxLength": 30, "pattern": "^[0-9-]*$" } },
                "limit":         { "type": "integer", "minimum": 1, "maximum": 1000, "default": 100 },
                "login_customer_id": { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" }
            }
        }
    },
    {
        "name": "fetch_change_history",
        "description": "Change events within a date range (ordered by most recent).",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "customer_id":   { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" },
                "time_range": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "since": { "type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
                        "until": { "type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$" }
                    }
                },
                "resource_types": {
                    "type": "array", "maxItems": 50,
                    "items": { "type": "string", "maxLength": 64 }
                },
                "limit": { "type": "integer", "minimum": 1, "maximum": 1000, "default": 200 },
                "login_customer_id": { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" }
            },
            "required": ["time_range"]
        }
    },
    {
        "name": "fetch_budget_pacing",
        "description": "Month-to-date spend and projected EOM vs target.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "customer_id":   { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" },
                "month": { "type": "string", "description": "YYYY-MM", "maxLength": 7, "pattern": "^\\d{4}-\\d{2}$" },
                "target_spend": { "type": "number", "description": "Target for the month in account currency" },
                "login_customer_id": { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" }
            },
            "required": ["month", "target_spend"]
        }
    },
    {
        "name": "list_resources",
        "description": "List accessible Google Ads customer accounts for the authenticated user.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "login_customer_id": {
                    "type": "string",
                    "description": "Optional manager (MCC) header override",
                    "maxLength": 20,
                    "pattern": "^[0-9-]*$"
                }
            }
        }
    },
    {
        "name": "fetch_geo_performance",
        "description": "Geo performance (city/region/country) for selected campaigns using geographic_view or user_location_view.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "customer_id":   { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" },
                "date_preset":   { "type": "string", "enum": ["TODAY","YESTERDAY","LAST_7_DAYS","LAST_30_DAYS","THIS_MONTH","LAST_MONTH"] },
                "time_range": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "since": { "type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
                        "until": { "type": "string", "maxLength": 10, "pattern": "^\\d{4}-\\d{2}-\\d{2}$" }
                    }
                },
                "campaign_ids":  { "type": "array", "maxItems": 200, "items": { "type": "string", "maxLength": 30, "pattern": "^[0-9-]*$" } },
                "level":         { "type": "string", "enum": ["city","region","country"], "default": "city" },
                "view":          { "type": "string", "enum": ["geographic","user_location"], "default": "geographic" },
                "min_spend":     { "type": "number", "minimum": 0 },
                "login_customer_id": { "type": "string", "maxLength": 20, "pattern": "^[0-9-]*$" }
            }
        }
    },
    {
        "name": "ping",
        "description": "Health check (public).",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}
    },
    {
        "name": "debug_login_header",
        "description": "Show which login_customer_id (MCC) the server will use.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}
    },
    {
        "name": "echo_short",
        "description": "Echo a short string. Use only for debugging tool calls.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"msg": {"type": "string", "maxLength": 80}},
            "required": ["msg"]
        }
    },
    {
        "name": "noop_ok",
        "description": "Returns a tiny fixed JSON object.",
        "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}
    }
]


# -------------------- Discovery (minimal) --------------------
@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
def root(request: Request):
    if request.method == "HEAD":
        return PlainTextResponse("")
    return PlainTextResponse("ok")


@app.get("/.well-known/mcp.json")
def mcp_discovery():
    return JSONResponse({
        "mcpVersion": MCP_PROTO_DEFAULT,
        "name": APP_NAME,
        "version": APP_VER,
        "auth": {"type": "none"},
        "capabilities": {"tools": {"listChanged": True}},
        "endpoints": {"rpc": "/"},
        "tools": TOOLS,
    })


# -------------------- JSON-RPC (initialize, tools/list, tools/call) --------------------
def _pack_text(data: Any) -> Dict[str, Any]:
    # Text-only response (works with strict MCP clients)
    try:
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    except Exception:
        text = str(data)
    return {"content": [{"type": "text", "text": text}]}


def _call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "ping":
        return _pack_text(tool_ping(args))
    if name == "debug_login_header":
        return _pack_text(tool_debug_login_header(args))
    if name == "echo_short":
        return _pack_text(tool_echo_short(args))
    if name == "noop_ok":
        return _pack_text(tool_noop_ok(args))
    if name == "list_resources":
        return _pack_text(tool_list_resources(args))
    if name == "fetch_campaign_summary":
        return _pack_text(tool_fetch_campaign_summary(args))
    if name == "fetch_metrics":
        return _pack_text(tool_fetch_metrics(args))
    if name == "fetch_search_terms":
        return _pack_text(tool_fetch_search_terms(args))
    if name == "fetch_change_history":
        return _pack_text(tool_fetch_change_history(args))
    if name == "fetch_budget_pacing":
        return _pack_text(tool_fetch_budget_pacing(args))
    if name == "fetch_geo_performance":
        return _pack_text(tool_fetch_geo_performance(args))
    return {"error": {"code": -32601, "message": f"Unknown tool: {name}"}}


@app.post("/")
async def rpc(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}})

    def handle(obj: Dict[str, Any]) -> Dict[str, Any] | None:
        if not isinstance(obj, dict):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        _id = obj.get("id")
        method = (obj.get("method") or "").lower()

        if method == "initialize":
            client_proto = (obj.get("params") or {}).get("protocolVersion") or MCP_PROTO_DEFAULT
            result = {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": APP_NAME, "version": APP_VER},
                "tools": TOOLS,
            }
            return {"jsonrpc": "2.0", "id": _id, "result": result}

        if method in ("initialized", "notifications/initialized"):
            return {"jsonrpc": "2.0", "id": _id, "result": {"ok": True}}

        if method in ("tools/list", "tools.list", "list_tools", "tools.index"):
            return {"jsonrpc": "2.0", "id": _id, "result": {"tools": TOOLS}}

        if method == "tools/call":
            params = obj.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            res = _call_tool(name, args)
            if "error" in res and "content" not in res:
                return {"jsonrpc": "2.0", "id": _id, "error": res["error"]}
            return {"jsonrpc": "2.0", "id": _id, "result": res}

        # unknown
        return {"jsonrpc": "2.0", "id": _id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    # batch support
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for entry in payload:
            resp = handle(entry)
            if resp is not None:
                out.append(resp)
        return JSONResponse(out if out else [], status_code=200)

    # single
    resp = handle(payload)
    return JSONResponse(resp if resp is not None else {}, status_code=200)


# -------------------- Local dev --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
