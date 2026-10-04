#!/usr/bin/env python3
"""
================================================================================
 Aptech Limited Weekly Status Report - Automated Word (.docx) Generator
================================================================================
 USER MANUAL & QUICK START GUIDE:
 -------------------------------------------------------------------------------
 1. SINGLE STANDALONE FILE:
    - This script is 100% self-contained. No other python files are required.
    - Upload ONLY this file ('generate_weekly_doc.py') to AWS CloudShell or EC2.

 2. AUTOMATIC DEPENDENCY INSTALLATION:
    - Installs boto3, python-docx and matplotlib via pip on first run if missing.

 3. TEST ON A SINGLE ACCOUNT FIRST (recommended):
    - By Account ID:
      python3 generate_weekly_doc.py --account-id 654654548701 --start 2026-09-14 --end 2026-09-20
    - By Account Name:
      python3 generate_weekly_doc.py --account-id "Aptech Avalon" --start 2026-09-14 --end 2026-09-20

 4. GENERATE THE COMPLETE REPORT (ALL 20 ACCOUNTS):
    python3 generate_weekly_doc.py --start 2026-09-14 --end 2026-09-20
    (Assumes the 'operisoft-readonly' role in every account via STS.)

 5. LOCAL DEMO / MOCK REPORT (no AWS credentials needed):
    python3 generate_weekly_doc.py --mock --start 2026-09-14 --end 2026-09-20

 6. SAVE THE TWO COST IMAGES AS PNG FILES TOO (for checking):
    python3 generate_weekly_doc.py --account-id 654654548701 --start 2026-09-14 --end 2026-09-20 --save-images

 -------------------------------------------------------------------------------
 BRANDING (cover page, per-page header, watermark):
   The generated .docx is fully branded:
     * A branded COVER PAGE (page 1): large Operisoft logo, the "Weekly Status
       Report" / "Aptech Limited" title, the Aptech banner, the "Submitted By"
       block, and the AWS Advanced Tier Partner badge.
     * A PER-PAGE HEADER on every page: the AWS partner badge/cluster image on
       the left and the Operisoft logo on the right.
     * A diagonal "CONFIDENTIAL" WATERMARK behind the content on every page.
   The cover DATE is the GENERATION DATE (today, the day you run the script),
   shown as DD/MM/YYYY.
   Branding images are read from the 'assets/branding/' directory resolved
   relative to this script (works regardless of the current directory). The
   expected PNG files are:
       aptech_logo.png            operisoft_logo_large.png
       operisoft_logo_header.png  aws_partner_badge.png
       aws_partner_cluster.png
   If any asset is missing the script logs a warning and simply skips that
   image, so it never crashes when branding files are absent.

 -------------------------------------------------------------------------------
 HOW THE TWO COST IMAGES ARE PRODUCED:
   AWS has no API that returns a screenshot of the Cost Explorer console.
   The script calls Cost Explorer GetCostAndUsage (DAILY, grouped by SERVICE)
   and draws both images from that real data:
     * Image 1 - "Cost and usage overview" + stacked daily cost graph
     * Image 2 - "Cost and usage breakdown" table (service x day)
   Cost Explorer API price: $0.01 per request. The script makes ONE request
   per account (current + previous week together), about $0.20 for 20 accounts.

 REQUIRED IAM PERMISSIONS ON 'operisoft-readonly' IN EVERY ACCOUNT:
   ce:GetCostAndUsage, cloudwatch:DescribeAlarms, cloudwatch:DescribeAlarmHistory,
   ec2:DescribeRegions, ec2:DescribeInstances  (AWS 'ReadOnlyAccess' covers these)

 WORD (.DOCX) REPORT STRUCTURE:
   - Page 1: branded COVER PAGE (large Operisoft logo, "Weekly Status Report" /
     "Aptech Limited" title, Aptech banner, "Submitted By" block with today's
     generation date, AWS Advanced Tier Partner badge).
   - Every page carries the branded header (AWS partner badges + Operisoft logo)
     and a diagonal CONFIDENTIAL watermark behind the content.
   - Title: Aptech Limited Weekly Status Report (date range)
   - Cost Summary bullets + Master Billing Table (account names link to sections)
   - Security Best Practices Links table
   - EXACTLY ONE page per account (images are auto-sized so nothing spills to a 2nd page):
       * Summary back-link banner + Account Name - Account ID
       * Billing and Cost Overview (total, daily average)
       * Image 1: Cost and usage overview  |  Image 2: Cost and usage breakdown
       * Tax, cost remarks, activity note
       * Resource Utilization & Alarms table
================================================================================
"""

import argparse
import io
import json
import math
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone, date
from xml.sax.saxutils import escape as xml_escape


# ----------------------------------------------------------- auto dependency --
def ensure_dependencies():
    required = {"boto3": "boto3", "docx": "python-docx", "matplotlib": "matplotlib"}
    missing = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print(f"[INFO] Missing dependencies detected: {', '.join(missing)}. Auto-installing via pip...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet"] + missing)
            print("[INFO] Dependencies installed successfully!")
        except Exception as e:
            sys.exit(f"[ERROR] Automatic pip installation failed: {e}. Please run: pip install {' '.join(missing)}")


ensure_dependencies()

import boto3
from botocore.config import Config
from botocore.exceptions import NoCredentialsError

import docx
from docx.shared import Inches, Pt, Emu, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import parse_xml, OxmlElement
from docx.oxml.ns import nsdecls, qn

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ============================================================== CONFIG ========
DEFAULT_TZ = "Asia/Kolkata"
ROLE_NAME_DEFAULT = "operisoft-readonly"

DEFAULT_ACCOUNTS = [
    {"no": 1, "name": "Aptrack 1.0 - DC [Aptech]", "account_id": "654654548701"},
    {"no": 2, "name": "Aptrack 2.0 - NonProd UAT [MEL]", "account_id": "767397805488"},
    {"no": 3, "name": "Aptrack 2.0 OldManagement A/c [MEL]/Production", "account_id": "015747350560"},
    {"no": 4, "name": "Aptrack 1.0 - DR [Aptrack5.0]", "account_id": "590183710228"},
    {"no": 5, "name": "Aptrack - ProAlle", "account_id": "617172754330"},
    {"no": 6, "name": "Aptrack - 2.0 - Shared Service [MEL]", "account_id": "533267037955"},
    {"no": 7, "name": "Aptrack-SAP-Dev and Quality", "account_id": "528757792293"},
    {"no": 8, "name": "Aptrack-SAP-Production", "account_id": "195275643918"},
    {"no": 9, "name": "Aptrack – LCMS", "account_id": "771423618181"},
    {"no": 10, "name": "Aptech-ProConnect", "account_id": "108782065415"},
    {"no": 11, "name": "Aptrack - QnA Solution", "account_id": "211125374996"},
    {"no": 12, "name": "Aptech-LAPA", "account_id": "343218180162"},
    {"no": 13, "name": "Aptrack-Arena", "account_id": "034362049944"},
    {"no": 14, "name": "Aptech Avalon", "account_id": "985539791413"},
    {"no": 15, "name": "Aptech AI Applications", "account_id": "166185345052"},
    {"no": 16, "name": "Aptrack AR_VR_MAAC", "account_id": "377555974222"},
    {"no": 17, "name": "Aptrack - 2.0 - Log archive [MEL]", "account_id": "637423329781"},
    {"no": 18, "name": "Aptech-Domains", "account_id": "622050225451"},
    {"no": 19, "name": "Aptrack - 2.0 - Audit [MEL]", "account_id": "851725245759"},
    {"no": 20, "name": "Aptech International", "account_id": "470684059456"},
]

SECURITY_LINKS = [
    ("Best Practices for AWS root users", "https://docs.aws.amazon.com/accounts/latest/reference/best-practices-root-user.html"),
    ("Best Practices for AWS Access Keys", "https://docs.aws.amazon.com/accounts/latest/reference/credentials-access-keys-best-practices.html"),
    ("Shared Responsibility Model", "https://aws.amazon.com/compliance/shared-responsibility-model/"),
    ("AWS Cloudtrail", "https://aws.amazon.com/cloudtrail/"),
    ("Trusted Advisor", "https://aws.amazon.com/premiumsupport/trustedadvisor/"),
    ("Creating Billing alarms", "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/gs_monitor_estimated_charges_with_cloudwatch.html#gs_creating_billing_alarm"),
    ("Enable MFA", "https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_mfa.html"),
    ("GIT Secrets", "https://github.com/awslabs/git-secrets"),
]

METRIC_MAP = {
    "CPU": [
        "cpuutilization", "cpu_usage_active", "cpu_usage_user", "cpu_usage_system",
        "cpu_usage_idle", "processor % processor time", "% processor time", "cpu",
    ],
    "Memory": [
        "mem_used_percent", "memoryutilization", "memory % committed bytes in use",
        "memory available mbytes", "available mbytes", "mem_available_percent",
        "windows_memory", "memory", "mem",
    ],
    "Disk": [
        "disk_used_percent", "diskspaceutilization", "logicaldisk % free space",
        "% free space", "used_percent", "disk_free", "freestoragespace", "disk",
    ],
}

INVERTED_METRICS = (
    "% free space", "free space", "mem_available_percent", "available mbytes", "cpu_usage_idle",
)

# Colours used by the AWS Cost Explorer console graph (in series order)
AWS_SERIES_COLORS = [
    "#688AE8", "#C33D69", "#2EA597", "#8456CE", "#E07941",
    "#3759CE", "#962249", "#096F64", "#6237A7", "#A84401",
]
OTHERS_COLOR = "#A0A0A0"
# Cost Explorer API returns long service names; the console shows these short ones
SHORT_NAMES = {
    "Amazon Virtual Private Cloud": "VPC", "AWS Network Firewall": "Network Firewall",
    "EC2 - Other": "EC2-Other", "Amazon Elastic Compute Cloud - Compute": "EC2-Instances",
    "AmazonCloudWatch": "CloudWatch", "AWS CloudTrail": "CloudTrail",
    "Amazon Simple Storage Service": "S3", "AWS Cost Explorer": "Cost Explorer",
    "AWS Config": "Config", "AWS Key Management Service": "Key Management Service",
    "AWS Secrets Manager": "Secrets Manager", "Amazon Simple Notification Service": "SNS",
    "Amazon Simple Queue Service": "SQS", "AWS Glue": "Glue", "AWS CloudShell": "CloudShell",
    "Amazon Relational Database Service": "Relational Database Service",
    "Amazon CloudFront": "CloudFront", "Amazon Route 53": "Route 53",
    "Amazon Elastic Load Balancing": "Elastic Load Balancing", "Elastic Load Balancing": "Elastic Load Balancing",
    "Amazon Elastic Container Service": "Elastic Container Service", "AWS Lambda": "Lambda",
    "Amazon OpenSearch Service": "OpenSearch Service", "Amazon Bedrock": "Bedrock",
    "AWS WAF": "WAF", "AWS Backup": "Backup", "Amazon Elastic File System": "EFS",
    "AWS Systems Manager": "Systems Manager", "AWS Security Hub": "Security Hub",
    "Amazon GuardDuty": "GuardDuty", "Amazon DynamoDB": "DynamoDB",
}


def short_name(service):
    return SHORT_NAMES.get(service, service)


MAX_GRAPH_SERIES = 9     # graph shows top 9 services + "Others" (like the console)
MAX_TABLE_ROWS = 16      # breakdown table shows top 16 services

# ---- Page layout (US Letter). Every account section is auto-sized to fit on ONE page.
PAGE_WIDTH_IN, PAGE_HEIGHT_IN = 8.5, 11.0
MARGIN_TOP_BOTTOM_IN, MARGIN_LEFT_RIGHT_IN = 0.5, 0.75
CONTENT_WIDTH_IN = PAGE_WIDTH_IN - 2 * MARGIN_LEFT_RIGHT_IN      # 7.0 in
CONTENT_HEIGHT_IN = PAGE_HEIGHT_IN - 2 * MARGIN_TOP_BOTTOM_IN    # 10.0 in
PAGE_SAFETY_IN = 0.45            # spare room so Word / LibreOffice / Google Docs keep it on one page
IMG_MAX_WIDTH_IN = 6.9           # normal image width
IMG_MIN_WIDTH_IN = 5.0           # shrink images down to this first ...
IMG_FLOOR_WIDTH_IN = 4.4         # ... absolute smallest before giving up on one page
MIN_TABLE_ROWS = 8               # ... then show fewer breakdown rows (like console page 1), not fewer than this

MASTER_COL_WIDTHS_IN = [0.35, 1.3, 0.95, 0.85, 0.65, 0.95, 1.95]   # sums to 7.0 in
SECURITY_COL_WIDTHS_IN = [2.4, 4.6]
ALARM_COL_WIDTHS_IN = [1.9, 0.95, 1.45, 2.7]
ALARM_HEADERS = ("Server Name", "Region", "Memory/Disk/CPU", "Alert date and no. of trigger")


# ------------------------------------------------------------------ helpers --
def log(msg):
    print(msg, flush=True)


# ------------------------------------------------------- branding assets --
# Resolve the branding asset directory relative to THIS script file (not the
# current working directory) so the report builds the same way from CloudShell,
# EC2 or any CWD. We try a few sensible locations and fall back gracefully.
def _branding_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "assets", "branding"),
        os.path.join(here, "..", "assets", "branding"),
        os.path.join(os.getcwd(), "assets", "branding"),
    ]
    for d in candidates:
        if os.path.isdir(d):
            return os.path.abspath(d)
    # default to the script-relative path even if it does not exist yet
    return os.path.abspath(candidates[0])


BRANDING_DIR = _branding_dir()


def asset_path(name):
    """Return the absolute path to a branding asset, or None if it is missing.

    Degrades gracefully: a missing asset logs a warning and returns None so the
    caller can skip the image instead of crashing the whole report.
    """
    path = os.path.join(BRANDING_DIR, name)
    if os.path.isfile(path):
        return path
    log(f"[WARN] Branding asset not found, skipping: {path}")
    return None


def _add_centered_image(doc, name, width_in):
    """Add a centered picture to a new paragraph. Returns the paragraph (or None)."""
    path = asset_path(name)
    if not path:
        return None
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_spacing(p, before=0, after=6, single=True)
    try:
        p.add_run().add_picture(path, width=Inches(width_in))
    except Exception as e:
        log(f"[WARN] Could not embed image {name}: {e}")
    return p


def get_tz(name):
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone(timedelta(hours=5, minutes=30))


def boto_config():
    return Config(retries={"max_attempts": 10, "mode": "adaptive"}, connect_timeout=15, read_timeout=60)


def date_range(start_d, end_d):
    out = []
    d = start_d
    while d <= end_d:
        out.append(d)
        d += timedelta(days=1)
    return out


def money(v):
    return f"${v:,.2f}"


def assume_role_session(base_session, target_account_id, role_name):
    try:
        sts = base_session.client("sts", config=boto_config())
        res = sts.assume_role(
            RoleArn=f"arn:aws:iam::{target_account_id}:role/{role_name}",
            RoleSessionName=f"WeeklyReport-{target_account_id}",
            DurationSeconds=3600,
        )
        c = res["Credentials"]
        return boto3.Session(
            aws_access_key_id=c["AccessKeyId"],
            aws_secret_access_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"],
        )
    except Exception as e:
        log(f"   [ERROR] AssumeRole failed for {target_account_id} ({role_name}): {e}")
        return None


# ------------------------------------------------------- alarm discovery ------
def list_regions(session):
    try:
        ec2 = session.client("ec2", region_name="us-east-1", config=boto_config())
        resp = ec2.describe_regions(AllRegions=False)
        return sorted(r["RegionName"] for r in resp["Regions"])
    except Exception:
        return ["us-east-1", "us-east-2", "us-west-1", "us-west-2", "ap-south-1",
                "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "eu-west-1", "eu-central-1"]


def get_instance_names(session, region):
    names = {}
    try:
        ec2 = session.client("ec2", region_name=region, config=boto_config())
        for page in ec2.get_paginator("describe_instances").paginate():
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    tag = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), None)
                    names[inst["InstanceId"]] = tag or inst["InstanceId"]
    except Exception:
        pass
    return names


def classify_metric(namespace, metric_name):
    blob = f"{(namespace or '').lower()} {(metric_name or '').lower()}"
    for category, keys in METRIC_MAP.items():
        if any(k in blob for k in keys):
            return category
    return "Other"


def is_inverted(metric_name, comparison):
    name = (metric_name or "").lower()
    return any(k in name for k in INVERTED_METRICS) or bool(comparison and comparison.startswith("LessThan"))


def threshold_label(category, metric_name, threshold, comparison, unit):
    cat = category if category != "Other" else (metric_name or "Metric")
    if threshold is None:
        return f"{cat} (no threshold)"
    mn = (metric_name or "").lower()
    pct_like = not (unit and unit.lower() in ("bytes", "megabytes", "gigabytes", "count", "seconds"))
    if "mbytes" in mn or "freestoragespace" in mn:
        pct_like = False
    if not pct_like:
        return f"{cat} {threshold:g} {unit or ''}".strip()
    val = threshold
    if is_inverted(metric_name, comparison) and 0 <= threshold <= 100:
        val = 100 - threshold
    if float(val).is_integer():
        val = int(val)
    return f"{cat} {val}%"


def extract_metric_info(alarm):
    ns, mn, dims = alarm.get("Namespace"), alarm.get("MetricName"), alarm.get("Dimensions") or []
    if not mn and alarm.get("Metrics"):
        for m in alarm["Metrics"]:
            stat = m.get("MetricStat")
            if stat and stat.get("Metric"):
                ns = stat["Metric"].get("Namespace")
                mn = stat["Metric"].get("MetricName")
                dims = stat["Metric"].get("Dimensions") or []
                break
    return ns, mn, dims


def instance_id_from_dims(dims):
    for d in dims or []:
        if d.get("Name") in ("InstanceId", "instanceId", "instance_id"):
            return d.get("Value")
    return None


def path_from_dims(dims):
    for d in dims or []:
        if d.get("Name") in ("path", "device", "instance"):
            return d.get("Value")
    return None


def count_alarm_triggers(cw, alarm_name, start_utc, end_utc, tz):
    day_counts = defaultdict(int)
    try:
        pages = cw.get_paginator("describe_alarm_history").paginate(
            AlarmName=alarm_name, AlarmTypes=["MetricAlarm"], HistoryItemType="StateUpdate",
            StartDate=start_utc, EndDate=end_utc, ScanBy="TimestampAscending",
        )
        for page in pages:
            for item in page.get("AlarmHistoryItems", []):
                try:
                    data = json.loads(item.get("HistoryData", "{}"))
                    new_state = data.get("newState", {}).get("stateValue")
                    old_state = data.get("oldState", {}).get("stateValue")
                except (ValueError, AttributeError):
                    new_state = "ALARM" if "to alarm" in (item.get("HistorySummary") or "").lower() else None
                    old_state = None
                if new_state == "ALARM" and old_state != "ALARM":
                    ts = item["Timestamp"]
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    day_counts[ts.astimezone(tz).date()] += 1
    except Exception:
        pass
    return dict(day_counts)


def process_region(account_info, session, region, start_utc, end_utc, tz, threads=8):
    rows = []
    try:
        cw = session.client("cloudwatch", region_name=region, config=boto_config())
        alarms = []
        for page in cw.get_paginator("describe_alarms").paginate(AlarmTypes=["MetricAlarm"]):
            alarms.extend(page.get("MetricAlarms", []))
    except Exception:
        return rows   # disabled / not opted-in region

    selected = []
    for a in alarms:
        ns, mn, dims = extract_metric_info(a)
        cat = classify_metric(ns, mn)
        if cat != "Other":
            selected.append((a, ns, mn, dims, cat))
    if not selected:
        return rows

    names = get_instance_names(session, region)

    def work(bundle):
        return bundle, count_alarm_triggers(cw, bundle[0]["AlarmName"], start_utc, end_utc, tz)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        for fut in as_completed([pool.submit(work, b) for b in selected]):
            try:
                (a, ns, mn, dims, cat), counts = fut.result()
            except Exception:
                continue
            iid = instance_id_from_dims(dims)
            label = threshold_label(cat, mn, a.get("Threshold"), a.get("ComparisonOperator"), a.get("Unit"))
            disk_path = path_from_dims(dims) if cat == "Disk" else None
            if disk_path:
                label = f"{label}  [{disk_path}]"
            rows.append({
                "account_id": account_info["account_id"],
                "region": region,
                "server": (names.get(iid, iid) if iid else a.get("AlarmName")) or "-",
                "instance_id": iid or "-",
                "label": label,
                "counts": counts,
                "total": sum(counts.values()),
            })
    return rows


# -------------------------------------------------------- cost explorer API --
def fetch_daily_service_costs(session, start_str, end_str_exclusive):
    """
    ONE Cost Explorer call for the whole 14-day window (previous + current week).
    Returns {date: {service: cost}}  or None on error.
    """
    ce = session.client("ce", region_name="us-east-1", config=boto_config())
    daily = defaultdict(dict)
    kwargs = {
        "TimePeriod": {"Start": start_str, "End": end_str_exclusive},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    try:
        while True:
            resp = ce.get_cost_and_usage(**kwargs)
            for day in resp.get("ResultsByTime", []):
                d = datetime.strptime(day["TimePeriod"]["Start"], "%Y-%m-%d").date()
                for group in day.get("Groups", []):
                    cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    srv = group["Keys"][0]
                    daily[d][srv] = daily[d].get(srv, 0.0) + cost
            token = resp.get("NextPageToken")
            if not token:
                break
            kwargs["NextPageToken"] = token
        return dict(daily)
    except Exception as e:
        log(f"   [WARN] Cost Explorer error: {e}")
        return None


def build_week_detail(daily, days):
    """Week-level structure used for the table numbers and both images."""
    per_day = {d: dict(daily.get(d, {})) for d in days}
    service_totals = defaultdict(float)
    for d in days:
        for srv, c in per_day[d].items():
            service_totals[srv] += c
    total = sum(service_totals.values())
    tax = sum(c for s, c in service_totals.items() if s.lower() == "tax" or "tax" in s.lower())
    return {
        "days": days,
        "per_day": per_day,
        "service_totals": dict(service_totals),
        "total": total,
        "tax": tax,
        "service_count": len([s for s, c in service_totals.items() if round(c, 2) != 0 or s in service_totals]),
    }


def summarise_account(account_info, cur, prev):
    diff = cur["total"] - prev["total"]
    avg_daily = cur["total"] / max(1, len(cur["days"]))

    # services that caused the change (largest per-service delta in the same direction)
    services = (set(cur["service_totals"]) | set(prev["service_totals"])) - {"Tax"}   # tax is not a usage
    deltas = {s: cur["service_totals"].get(s, 0.0) - prev["service_totals"].get(s, 0.0) for s in services}
    if diff > 0:
        drivers = [s for s, dv in sorted(deltas.items(), key=lambda x: x[1], reverse=True) if dv > 0.5][:3]
    else:
        drivers = [s for s, dv in sorted(deltas.items(), key=lambda x: x[1]) if dv < -0.5][:3]
    srv_str = ", ".join(short_name(x) for x in drivers) if drivers else "EC2-Instances"

    if abs(diff) < 0.5:
        remark = "The cost remains same."
    elif diff > 0:
        remark = f"The costs increased by ${diff:,.2f} due to usage of following services: {srv_str}."
    else:
        remark = f"The costs decreased by ${abs(diff):,.2f} due to usage of following services: {srv_str}."

    return {
        "no": account_info["no"], "name": account_info["name"], "account_id": account_info["account_id"],
        "prev_cost": prev["total"], "tax_cost": cur["tax"], "cur_cost": cur["total"],
        "avg_daily": avg_daily, "remark": remark, "diff": diff, "cur_detail": cur,
    }


def failed_account(account_info, reason):
    return {
        "no": account_info["no"], "name": account_info["name"], "account_id": account_info["account_id"],
        "prev_cost": 0.0, "tax_cost": 0.0, "cur_cost": 0.0, "avg_daily": 0.0,
        "remark": reason, "diff": 0.0, "cur_detail": None,
    }


# ------------------------------------------------- Cost Explorer style images --
FIG_WIDTH_IN = 10.2
OVERVIEW_FIG_H_IN = 4.0
BREAKDOWN_ROW_H_IN, BREAKDOWN_HEAD_IN, BREAKDOWN_FOOT_IN = 0.26, 0.50, 0.12
TEXT_DARK, TEXT_GREY, AXIS_GREY = "#000716", "#5F6B7A", "#414D5C"
BORDER_GREY, GRID_GREY = "#C6C6CD", "#E9EBED"


def breakdown_fig_height_in(services_shown):
    """+2 rows = column-header row + 'Total costs' row."""
    return BREAKDOWN_HEAD_IN + BREAKDOWN_ROW_H_IN * (services_shown + 2) + BREAKDOWN_FOOT_IN


def _ordered_services(detail):
    return [s for s, c in sorted(detail["service_totals"].items(), key=lambda x: x[1], reverse=True)]


def _service_count(detail):
    return len(detail["service_totals"])


def _png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def _card(fig, fig_h, top_in, bottom_in):
    """Console-style rounded container from top_in to bottom_in (inches measured from the top)."""
    fig.patches.append(FancyBboxPatch(
        (0.008, 1 - bottom_in / fig_h), 0.984, (bottom_in - top_in) / fig_h, transform=fig.transFigure,
        boxstyle="round,pad=0,rounding_size=0.012", linewidth=1, edgecolor=BORDER_GREY,
        facecolor="none", zorder=0))


def render_cost_overview_png(detail):
    """Image 1: 'Cost and usage overview' card + stacked daily 'Cost and usage graph'.
    Returns (png_buffer, width_in, height_in)."""
    days = detail["days"]
    ordered = _ordered_services(detail)
    top = ordered[:MAX_GRAPH_SERIES]
    rest = ordered[MAX_GRAPH_SERIES:]

    series = [(s, [detail["per_day"][d].get(s, 0.0) for d in days], AWS_SERIES_COLORS[i % len(AWS_SERIES_COLORS)])
              for i, s in enumerate(top)]
    if rest:
        series.append(("Others", [sum(detail["per_day"][d].get(s, 0.0) for s in rest) for d in days], OTHERS_COLOR))

    W, H = FIG_WIDTH_IN, OVERVIEW_FIG_H_IN
    fig = plt.figure(figsize=(W, H), dpi=150)
    fig.patch.set_facecolor("white")

    def y(inch_from_top):
        return 1 - inch_from_top / H

    # ---- overview card
    _card(fig, H, 0.04, 0.92)
    fig.text(0.022, y(0.13), "Cost and usage overview", fontsize=11, fontweight="bold", color=TEXT_DARK, va="top")
    cards = [("Total cost", money(detail["total"])),
             ("Average daily cost", money(detail["total"] / max(1, len(days)))),
             ("Service count", str(_service_count(detail)))]
    for i, (lbl, val) in enumerate(cards):
        x = 0.022 + i * 0.325
        if i:
            fig.add_artist(plt.Line2D([x - 0.012, x - 0.012], [y(0.42), y(0.84)], transform=fig.transFigure,
                                      color=BORDER_GREY, linewidth=1))
        fig.text(x, y(0.42), lbl, fontsize=8.5, fontweight="bold", color=TEXT_DARK, va="top")
        fig.text(x, y(0.60), val, fontsize=13, color=TEXT_DARK, va="top")

    # ---- graph card
    _card(fig, H, 1.0, 3.96)
    fig.text(0.022, y(1.09), "Cost and usage graph", fontsize=11, fontweight="bold", color=TEXT_DARK, va="top")
    fig.text(0.022, y(1.36), "Costs ($)", fontsize=8, fontweight="bold", color=TEXT_DARK, va="top")

    ax_top, ax_bottom = 1.58, 3.10
    ax = fig.add_axes([0.055, y(ax_bottom), 0.925, (ax_bottom - ax_top) / H])
    xs = list(range(len(days)))
    bottom = [0.0] * len(days)
    for name, vals, color in series:
        ax.bar(xs, vals, bottom=bottom, color=color, width=0.68, label=short_name(name), zorder=3)
        bottom = [b + v for b, v in zip(bottom, vals)]

    ax.set_xticks(xs)
    ax.set_xticklabels([d.strftime("%b %d") for d in days], fontsize=8, color=AXIS_GREY)
    ax.tick_params(axis="y", labelsize=8, colors=AXIS_GREY, length=0)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=GRID_GREY, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BORDER_GREY)
    ymax = max(bottom) if bottom and max(bottom) > 0 else 1
    ax.set_ylim(0, ymax * 1.15)
    ax.set_xlim(-0.6, len(days) - 0.4)

    if series:
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=min(len(series), 5),
                  frameon=False, fontsize=7.6, handlelength=0.9, handleheight=0.9, columnspacing=1.4,
                  labelspacing=0.35)
    else:
        ax.text(0.5, 0.5, "No cost data for this period", transform=ax.transAxes,
                ha="center", va="center", fontsize=10, color=TEXT_GREY)

    return _png(fig), W, H


def render_cost_breakdown_png(detail, max_rows=MAX_TABLE_ROWS):
    """Image 2: 'Cost and usage breakdown (N)' table - Service | Service total | each day.
    Shows the top `max_rows` services (highest cost first, like page 1 of the console table).
    Returns (png_buffer, width_in, height_in)."""
    days = detail["days"]
    ordered = _ordered_services(detail)[:max_rows]

    def cell(v, present):
        return money(v) if present else "-"

    rows = [("Total costs", money(detail["total"]),
             [money(sum(detail["per_day"][d].values())) for d in days])]
    for s in ordered:
        rows.append((s, money(detail["service_totals"][s]),
                     [cell(detail["per_day"][d].get(s, 0.0), s in detail["per_day"][d]) for d in days]))

    n = len(rows)
    row_h, head_in, foot_in = BREAKDOWN_ROW_H_IN, BREAKDOWN_HEAD_IN, BREAKDOWN_FOOT_IN
    W = FIG_WIDTH_IN
    H = breakdown_fig_height_in(len(ordered))
    fig = plt.figure(figsize=(W, H), dpi=150)
    fig.patch.set_facecolor("white")
    _card(fig, H, 0.02, H - 0.02)

    title = fig.text(0.022, 1 - 0.13 / H, "Cost and usage breakdown", fontsize=11, fontweight="bold",
                     color=TEXT_DARK, va="top")
    title_right = title.get_window_extent(renderer=fig.canvas.get_renderer()).x1 / (W * fig.dpi)
    fig.text(title_right + 0.006, 1 - 0.13 / H, f"({_service_count(detail)})", fontsize=11, color=TEXT_GREY, va="top")

    ax = fig.add_axes([0.022, foot_in / H, 0.96, row_h * (n + 1) / H])
    ax.set_xlim(0, 1)
    ax.set_ylim(n + 1, 0)
    ax.axis("off")

    col_service, col_total = 0.0, 0.19
    day_start, day_end = 0.31, 1.0
    step = (day_end - day_start) / max(1, len(days))
    day_cols = [day_start + i * step for i in range(len(days))]

    headers = [(col_service, "Service"), (col_total, "Service total")] + \
              [(cx, d.strftime("%b-%d")) for cx, d in zip(day_cols, days)]
    for cx, h in headers:
        ax.text(cx, 0.5, h, fontsize=8.2, fontweight="bold", va="center", color=TEXT_DARK)
        if cx > 0:
            ax.plot([cx - 0.008, cx - 0.008], [0.25, 0.75], color=BORDER_GREY, linewidth=0.8)
    ax.plot([0, 1], [1, 1], color="#879596", linewidth=1)

    for i, (srv, tot, vals) in enumerate(rows, start=1):
        yc = i + 0.5
        srv = short_name(srv)
        label = srv if len(srv) <= 30 else srv[:28] + "…"
        ax.text(col_service, yc, label, fontsize=8, va="center", color=TEXT_DARK)
        ax.text(col_total, yc, tot, fontsize=8, va="center", color=TEXT_DARK)
        for cx, v in zip(day_cols, vals):
            ax.text(cx, yc, v, fontsize=8, va="center", color=TEXT_DARK)
        if i < n:
            ax.plot([0, 1], [i + 1, i + 1], color=GRID_GREY, linewidth=0.8)

    return _png(fig), W, H


# ---------------------------------------------------------------- mock data --
MOCK_SERVICES = [
    ("Amazon Virtual Private Cloud", 16.0), ("AWS Network Firewall", 14.5), ("EC2 - Other", 11.0),
    ("Amazon Elastic Compute Cloud - Compute", 6.0), ("AmazonCloudWatch", 0.3), ("AWS CloudTrail", 0.07),
    ("Amazon Simple Storage Service", 0.04), ("AWS Cost Explorer", 0.01), ("AWS Config", 0.01),
    ("AWS Key Management Service", 0.003), ("AWS Secrets Manager", 0.002), ("Amazon Simple Notification Service", 0.001),
    ("Amazon Relational Database Service", 4.0), ("Amazon CloudFront", 1.2), ("Amazon Route 53", 0.02),
]


def mock_daily_costs(account_info, all_days):
    """
    Realistic demo costs: every account has its own size and service mix (seeded by account id)
    and every DATE gets its own numbers (seeded by account id + date), so each account and each
    week shows different data - same as real Cost Explorer data would.
    """
    acc_seed = int(account_info["account_id"])
    rnd_acc = random.Random(acc_seed)
    scale = rnd_acc.uniform(0.2, 6.0)
    services = rnd_acc.sample(MOCK_SERVICES, rnd_acc.randint(6, len(MOCK_SERVICES)))
    has_tax = rnd_acc.random() < 0.5
    daily = {}
    for d in all_days:
        rnd = random.Random(acc_seed * 100000 + d.toordinal())
        daily[d] = {}
        for srv, base in services:
            if base < 1 and rnd.random() < 0.08:
                continue   # service had no usage that day -> shown as "-"
            daily[d][srv] = round(base * scale * rnd.uniform(0.55, 1.15), 4)
        if has_tax and d.weekday() == 6:   # some accounts: a tax line every Sunday
            daily[d]["Tax"] = round(sum(daily[d].values()) * 0.18, 2)
    return daily


def generate_mock_alarm_data(accounts, start_local, end_local):
    days = date_range(start_local.date(), end_local.date())
    rnd = random.Random(start_local.date().toordinal())   # different alarms every week
    samples = [
        ("web-server-01", "i-0123456789abcdef0", "CPU 90%"),
        ("web-server-01", "i-0123456789abcdef0", "Disk 95%  [/var]"),
        ("db-server-01", "i-0fedcba9876543210", "Disk 85%  [C:]"),
        ("db-server-01", "i-0fedcba9876543210", "CPU 85%"),
        ("app-server-01", "i-0987654321fedcba0", "Memory 90%"),
    ]
    rows = []
    for acc in accounts:
        if acc["no"] % 3 == 0:
            continue
        for s_name, iid, label in rnd.sample(samples, rnd.randint(1, 3)):
            counts = {d: rnd.randint(1, 6) for d in rnd.sample(days, max(1, len(days) // 3))}
            rows.append({"account_id": acc["account_id"], "region": "ap-south-1", "server": s_name,
                         "instance_id": iid, "label": label, "counts": counts, "total": sum(counts.values())})
    return rows


def fmt_dates(counts, sep="\n"):
    """One 'September 14(2 Times)' entry per line (python-docx turns '\\n' into a line break)."""
    return sep.join(f"{d.strftime('%B %d')}({n} Time{'s' if n > 1 else ''})" for d, n in sorted(counts.items()))


# ------------------------------------------------------------- docx helpers --
_bookmark_id = [0]


def set_cell_background(cell, fill_hex):
    tcPr = cell._tc.get_or_add_tcPr()
    for old in tcPr.findall(qn("w:shd")):
        tcPr.remove(old)
    # keep the schema order of <w:tcPr> children so MS Word never reports "unreadable content"
    tcPr.insert_element_before(
        parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{fill_hex}"/>'),
        "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark",
        "w:headers", "w:cellIns", "w:cellDel", "w:cellMerge", "w:tcPrChange")


def set_table_borders(table, color_hex="000000"):
    edges = "".join(f'<w:{e} w:val="single" w:sz="4" w:space="0" w:color="{color_hex}"/>'
                    for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
    table._tbl.tblPr.insert_element_before(
        parse_xml(f'<w:tblBorders {nsdecls("w")}>{edges}</w:tblBorders>'),
        "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription", "w:tblPrChange")


def set_table_layout(table, widths_in):
    """Fixed column widths (grid + table width) so Word, LibreOffice and Google Docs all
    draw the table the same way instead of auto-squeezing columns."""
    table.autofit = False
    tblPr = table._tbl.tblPr
    for old in tblPr.findall(qn("w:tblW")):
        tblPr.remove(old)
    tblPr.insert_element_before(
        parse_xml(f'<w:tblW {nsdecls("w")} w:w="{int(round(sum(widths_in) * 1440))}" w:type="dxa"/>'),
        "w:jc", "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout", "w:tblCellMar",
        "w:tblLook", "w:tblCaption", "w:tblDescription", "w:tblPrChange")
    for grid_col, w in zip(table._tbl.tblGrid.gridCol_lst, widths_in):
        grid_col.w = Inches(w)
    for row in table.rows:
        set_row_widths(row, widths_in)


def set_row_widths(row, widths_in):
    """Call BEFORE merging cells of the row (a merge then adds the widths up)."""
    for cell, w in zip(row.cells, widths_in):
        cell.width = Inches(w)


def set_table_cell_margins(table, top=0, bottom=0, left=60, right=60):
    """Set default cell margins (twips) for the whole table so rows can be made
    very tight vertically (top/bottom 0) while keeping a little side padding.
    Used to pack all 20 account rows + Total onto one summary page."""
    tblPr = table._tbl.tblPr
    for old in tblPr.findall(qn("w:tblCellMar")):
        tblPr.remove(old)
    mar = "".join(f'<w:{side} w:w="{val}" w:type="dxa"/>'
                  for side, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)))
    tblPr.insert_element_before(
        parse_xml(f'<w:tblCellMar {nsdecls("w")}>{mar}</w:tblCellMar>'),
        "w:tblLook", "w:tblCaption", "w:tblDescription", "w:tblPrChange")


def keep_row_on_one_page(row):
    trPr = row._tr.get_or_add_trPr()
    if not trPr.findall(qn("w:cantSplit")):
        trPr.insert_element_before(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'),
                                   "w:trHeight", "w:tblHeader", "w:tblCellSpacing", "w:jc", "w:hidden",
                                   "w:ins", "w:del", "w:trPrChange")


def repeat_as_header(row):
    """Row is repeated at the top of the next page if the table ever has to continue there."""
    trPr = row._tr.get_or_add_trPr()
    if not trPr.findall(qn("w:tblHeader")):
        trPr.insert_element_before(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'),
                                   "w:tblCellSpacing", "w:jc", "w:hidden", "w:ins", "w:del", "w:trPrChange")


def add_bookmark(paragraph, name):
    _bookmark_id[0] += 1
    bid = _bookmark_id[0]
    p = paragraph._p
    p.insert(1 if p.pPr is not None else 0,    # <w:pPr> must stay the first child of <w:p>
             parse_xml(f'<w:bookmarkStart {nsdecls("w")} w:id="{bid}" w:name="{name}"/>'))
    p.append(parse_xml(f'<w:bookmarkEnd {nsdecls("w")} w:id="{bid}"/>'))


def add_internal_hyperlink(paragraph, anchor, text, font_size_pt=8, color_hex="467885", bold=False):
    run = (f'<w:r {nsdecls("w")}><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>'
           f'{"<w:b/>" if bold else ""}<w:color w:val="{color_hex}"/><w:sz w:val="{int(font_size_pt * 2)}"/>'
           f'<w:u w:val="single"/></w:rPr><w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>')
    hyperlink = parse_xml(f'<w:hyperlink {nsdecls("w")} w:anchor="{anchor}" w:history="1"/>')
    hyperlink.append(parse_xml(run))
    paragraph._p.append(hyperlink)


# --------------------------------------------------------- disc bullet lists --
# Self-contained disc (dot) bullet numbering so the account/summary pages match
# the branded reference. We register one abstractNum with two levels (both solid
# discs) and one concrete num that every bulleted paragraph references via numPr.
# This renders filled dot bullets in Word / LibreOffice / Google Docs. Indent is
# set per paragraph so the reference "ek line aage ek line piche" hierarchy holds.
# The concrete numId is cached PER DOCUMENT (stored on the doc object) rather than
# in a module global, so building more than one document in the same process keeps
# working: each new docx.Document() re-registers its own abstractNum/num and its
# bullets always render.


def _twips(inches):
    return int(round(inches * 1440))


def _ensure_disc_numbering(doc):
    """Register a disc-bullet abstractNum + num in this document's numbering part
    (once per document) and return the concrete numId. The id is cached on the
    doc object so a second document built in the same process re-registers its
    own numbering and its bullets still render."""
    cached = getattr(doc, "_disc_num_id", None)
    if cached is not None:
        return cached
    numbering = doc.part.numbering_part.element
    abstract_id = 9100            # high ids to avoid clashing with built-in styles
    num_id = 9101
    levels = "".join(
        f'<w:lvl w:ilvl="{i}">'
        f'<w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        f'<w:lvlText w:val="&#9679;"/>'          # U+25CF black circle (disc)
        f'<w:lvlJc w:val="left"/>'
        f'<w:pPr><w:ind w:left="{720 * (i + 1)}" w:hanging="360"/></w:pPr>'
        f'<w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:hint="default"/></w:rPr>'
        f'</w:lvl>'
        for i in range(3)
    )
    numbering.insert(0, parse_xml(
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{abstract_id}">'
        f'<w:multiLevelType w:val="hybridMultilevel"/>{levels}</w:abstractNum>'))
    numbering.append(parse_xml(
        f'<w:num {nsdecls("w")} w:numId="{num_id}">'
        f'<w:abstractNumId w:val="{abstract_id}"/></w:num>'))
    doc._disc_num_id = num_id
    return num_id


def _ensure_arrow_numbering(doc):
    """Register an arrow-bullet abstractNum + num in this document's numbering
    part (once per document) and return the concrete numId. Mirrors
    _ensure_disc_numbering exactly, but ilvl 0 uses the arrow glyph U+2B9A (the
    reference outer bullet, font 'Noto Sans Symbols') while ilvl 1+ stay solid
    discs, so one definition covers both the outer-arrow and inner-disc levels.
    The id is cached on the doc object so a second document built in the same
    process re-registers its own numbering and its bullets still render."""
    cached = getattr(doc, "_arrow_num_id", None)
    if cached is not None:
        return cached
    numbering = doc.part.numbering_part.element
    abstract_id = 9200            # high ids to avoid clashing with built-in styles
    num_id = 9201
    levels = ""
    for i in range(3):
        if i == 0:
            glyph = "&#11162;"    # U+2B9A arrow (outer bullet)
            fonts = ('<w:rFonts w:ascii="Noto Sans Symbols" w:hAnsi="Noto Sans Symbols" '
                     'w:cs="Noto Sans Symbols" w:hint="default"/>')
        else:
            glyph = "&#9679;"     # U+25CF black circle (disc) for inner levels
            fonts = '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:hint="default"/>'
        levels += (
            f'<w:lvl w:ilvl="{i}">'
            f'<w:start w:val="1"/><w:numFmt w:val="bullet"/>'
            f'<w:lvlText w:val="{glyph}"/>'
            f'<w:lvlJc w:val="left"/>'
            f'<w:pPr><w:ind w:left="{720 * (i + 1)}" w:hanging="360"/></w:pPr>'
            f'<w:rPr>{fonts}</w:rPr>'
            f'</w:lvl>'
        )
    numbering.insert(0, parse_xml(
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{abstract_id}">'
        f'<w:multiLevelType w:val="hybridMultilevel"/>{levels}</w:abstractNum>'))
    numbering.append(parse_xml(
        f'<w:num {nsdecls("w")} w:numId="{num_id}">'
        f'<w:abstractNumId w:val="{abstract_id}"/></w:num>'))
    doc._arrow_num_id = num_id
    return num_id


def add_bullet(doc, segments, ilvl=0, left_in=0.5, hanging_in=0.25, size=12,
               before=0, after=4, single=True, keep_with_next=False, glyph="disc"):
    """Add a bullet paragraph. `segments` is a list of (text, bold) tuples
    so a line like 'Total cost for the week: $340.00' can bold just the amount.
    `left_in`/`hanging_in` control the reference indent hierarchy. `glyph`
    selects the numbering: "disc" (default, solid dot) or "arrow" (outer arrow
    U+2B9A at ilvl 0, disc at deeper levels) to match the reference."""
    num_id = _ensure_arrow_numbering(doc) if glyph == "arrow" else _ensure_disc_numbering(doc)
    p = doc.add_paragraph()
    set_spacing(p, before, after, single, keep_with_next)
    pf = p.paragraph_format
    pf.left_indent = Inches(left_in)
    pf.first_line_indent = Inches(-hanging_in)
    # numPr must be inside pPr for the bullet glyph to render
    pPr = p._p.get_or_add_pPr()
    pPr.append(parse_xml(
        f'<w:numPr {nsdecls("w")}><w:ilvl w:val="{ilvl}"/>'
        f'<w:numId w:val="{num_id}"/></w:numPr>'))
    for text, bold in segments:
        r = p.add_run(text)
        r.font.name = "Arial"
        r.font.size = Pt(size)
        r.font.bold = bold
    return p


def set_spacing(paragraph, before=None, after=None, single=False, keep_with_next=False):
    pf = paragraph.paragraph_format
    if before is not None:
        pf.space_before = Pt(before)
    if after is not None:
        pf.space_after = Pt(after)
    if single:
        pf.line_spacing = 1.0
    if keep_with_next:
        pf.keep_with_next = True


def add_text(doc, text, size=12, bold=False, color=None, italic=False, align=None,
             before=None, after=None, single=False, keep_with_next=False):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    set_spacing(p, before, after, single, keep_with_next)
    r = p.add_run(text)
    r.font.name = "Arial"
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.italic = italic
    if color:
        r.font.color.rgb = color
    return p


def style_cell(cell, size, bold=False, align=WD_ALIGN_PARAGRAPH.LEFT, color=None, before=1, after=1):
    for p in cell.paragraphs:
        p.alignment = align
        set_spacing(p, before=before, after=after, single=True)    # compact rows (no empty gap under the text)
        for run in p.runs:
            run.font.name = "Arial"
            run.font.size = Pt(size)
            run.font.bold = bold
            if color:
                run.font.color.rgb = color


def _set_total_cell(cell, arrow, arrow_color, amount):
    """Build a Total-row money cell with an explicitly colored arrow run followed
    by the black amount run. Used instead of style_cell for the two total cells
    so the arrow keeps its own color while the amount stays black."""
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_spacing(p, before=0, after=0, single=True)
    r_arrow = p.add_run(arrow)
    r_arrow.font.name = "Arial"
    r_arrow.font.size = Pt(8.5)
    r_arrow.font.bold = True
    r_arrow.font.color.rgb = arrow_color
    r_amount = p.add_run(amount)
    r_amount.font.name = "Arial"
    r_amount.font.size = Pt(8.5)
    r_amount.font.bold = True


NAVY = RGBColor(0x1F, 0x48, 0x7C)
RED = RGBColor(0xFF, 0x00, 0x00)

# Account-page spacing (points). The page-fit estimate below uses the same numbers.
ACC_SPACE_AFTER = {"banner": 2, "name": 2, "dashes": 4, "heading": 3, "line": 2, "avg": 4,
                   "image": 3, "note": 6, "alarm_heading": 4, "end": 4}


# ------------------------------------------------- one-page-per-account fitting --
def _line_in(size_pt):
    """Height of one single-spaced Arial line in inches."""
    return size_pt * 1.15 / 72.0


def _wrapped_lines(text, width_in, size_pt, bold=False):
    """Conservative estimate of how many lines `text` needs in a box `width_in` wide."""
    avg_em = 0.56 if bold else 0.52                      # Arial average glyph width
    per_line = max(1, int(width_in * 72.0 / (size_pt * avg_em) * 0.92))
    total = 0
    for part in str(text).split("\n"):
        lines, cur = 1, 0                                # greedy word wrap
        for word in part.split():
            wl = len(word)
            if cur and cur + 1 + wl <= per_line:
                cur += 1 + wl
                continue
            if cur:
                lines += 1
            while wl > per_line:                         # a word longer than the line
                lines += 1
                wl -= per_line
            cur = wl
        total += lines
    return total


def _alarm_banner_text(cur_start, cur_end):
    return f"All EC2 Server Alarms triggered between ({cur_start:%d %B %Y} to {cur_end:%d %B %Y})"


def _alarm_server_text(a):
    return a["server"] if a["instance_id"] in ("-", None) else f"{a['server']} ({a['instance_id']})"


def _alarm_table_height_in(acc_alarms, cur_start, cur_end):
    pad = 0.035                                          # cell spacing (1pt + 1pt) + border
    inner = [w - 0.16 for w in ALARM_COL_WIDTHS_IN]      # minus default left/right cell margins
    h = _line_in(10) * _wrapped_lines(_alarm_banner_text(cur_start, cur_end), sum(inner), 10, True) + pad
    h += _line_in(10) * max(_wrapped_lines(t, w, 10, True) for t, w in zip(ALARM_HEADERS, inner)) + pad
    for a in acc_alarms:
        texts = (_alarm_server_text(a), a["region"], a["label"], fmt_dates(a["counts"]))
        h += _line_in(8.5) * max(_wrapped_lines(t, w, 8.5) for t, w in zip(texts, inner)) + pad
    return h


def account_text_height_in(item, acc_alarms, cur_start, cur_end, is_last):
    """Height of everything on an account page EXCEPT the two cost images."""
    w, sp = CONTENT_WIDTH_IN, ACC_SPACE_AFTER
    pt = 1 / 72.0
    h = _line_in(10) + sp["banner"] * pt
    h += _line_in(14) * _wrapped_lines(f"{item['name']} – {item['account_id']}", w, 14, True) + sp["name"] * pt
    h += _line_in(9.5) + sp["dashes"] * pt
    # Body is now disc bullets. Bullets add a hanging indent so the usable text
    # width shrinks (outer ~0.37in, inner ~0.94in); account for the narrower box.
    h += _line_in(12) + sp["heading"] * pt                         # 'Billing and Cost Overview' outer bullet
    h += _line_in(12) + sp["line"] * pt                            # 'Total cost ...' inner bullet
    h += _line_in(12) + sp["avg"] * pt                             # 'Average Daily Cost ...' inner bullet
    if not item.get("cur_detail"):
        h += _line_in(10) * 2 + sp["line"] * pt
    if item["tax_cost"] > 0:
        h += _line_in(12) + sp["line"] * pt                        # 'Total Tax Cost ...' inner bullet (one line)
    h += _line_in(12) * _wrapped_lines(item["remark"], w - 0.37, 12) + sp["line"] * pt   # remark bullet
    h += _line_in(12) + sp["note"] * pt                            # 'No Activity ...' outer bullet (now 12pt)
    h += _line_in(14) + sp["alarm_heading"] * pt
    h += _alarm_table_height_in(acc_alarms, cur_start, cur_end) if acc_alarms else _line_in(11) + sp["line"] * pt
    if is_last:                                          # blank line + "-- End Of Document --"
        h += 2 * (_line_in(11) + sp["end"] * pt)
    return h


def plan_cost_images(detail, text_height_in):
    """
    Choose (image display width, breakdown rows) so the WHOLE account fits on ONE page:
      1. full size (6.9 in) when it fits
      2. otherwise shrink both images, down to 5.0 in
      3. otherwise show fewer breakdown rows (top services first, never below 8)
      4. otherwise shrink down to 4.4 in
    If the alarm table alone is too long for one page, keep normal size (it continues on the next page).
    """
    room = CONTENT_HEIGHT_IN - PAGE_SAFETY_IN - text_height_in - 2 * (ACC_SPACE_AFTER["image"] + 4) / 72.0
    rows = min(MAX_TABLE_ROWS, len(detail["service_totals"]))

    def width_for(r):   # display width at which image 1 + image 2 exactly fill `room`
        return room * FIG_WIDTH_IN / (OVERVIEW_FIG_H_IN + breakdown_fig_height_in(r))

    if width_for(rows) >= IMG_MIN_WIDTH_IN:
        return min(IMG_MAX_WIDTH_IN, width_for(rows)), rows
    while rows > MIN_TABLE_ROWS and width_for(rows) < IMG_MIN_WIDTH_IN:
        rows -= 1
    if width_for(rows) >= IMG_FLOOR_WIDTH_IN:
        return min(IMG_MAX_WIDTH_IN, width_for(rows)), rows
    return IMG_MAX_WIDTH_IN, min(MAX_TABLE_ROWS, len(detail["service_totals"]))   # cannot fit anyway


# ------------------------------------------------------ branded header --
# The CONFIDENTIAL watermark is a VML text-path shape (type #_x0000_t136)
# living in the DEFAULT header, so Word/LibreOffice/Google Docs render it on
# EVERY page. python-docx has no native watermark API, so we inject the same
# markup used by the branded reference (word/header1.xml): rotation 315,
# fillcolor #c0c0c0, fill opacity 32768f, behind the page content.
_WATERMARK_XML = (
    '<w:r %s>'
    '<w:rPr><w:noProof/></w:rPr>'
    '<w:pict xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:o="urn:schemas-microsoft-com:office:office">'
    '<v:shape id="PowerPlusWaterMarkObject1" '
    'style="position:absolute;margin-left:0;margin-top:0;width:527.85pt;'
    'height:131.95pt;rotation:315;z-index:-503316481;'
    'mso-position-horizontal-relative:margin;mso-position-horizontal:center;'
    'mso-position-vertical-relative:margin;mso-position-vertical:center;" '
    'fillcolor="#c0c0c0" stroked="f" type="#_x0000_t136">'
    '<v:fill angle="0" opacity="32768f"/>'
    '<v:textpath fitshape="t" string="CONFIDENTIAL" '
    'style="font-family:&quot;Arial&quot;;font-size:1pt;"/>'
    '</v:shape></w:pict></w:r>'
) % nsdecls("w")


def _add_watermark(header):
    """Inject the CONFIDENTIAL VML watermark into the given header part."""
    para = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
    try:
        para._p.append(parse_xml(_WATERMARK_XML))
    except Exception as e:
        log(f"[WARN] Could not add watermark: {e}")


def build_branded_header(doc):
    """Build the default per-page header: AWS partner cluster (left) and the
    Operisoft logo (right), plus the diagonal CONFIDENTIAL watermark. Because it
    is the default (not first-page-only) header, it appears on every page."""
    section = doc.sections[0]
    header = section.header
    header.is_linked_to_previous = False

    # Clear any default empty paragraph then lay the two logos out in a
    # borderless two-column table so one sits left and the other right.
    left = asset_path("aws_partner_cluster.png")
    right = asset_path("operisoft_logo_header.png")

    htable = header.add_table(rows=1, cols=2, width=Inches(CONTENT_WIDTH_IN))
    htable.alignment = WD_TABLE_ALIGNMENT.CENTER
    try:
        htable.autofit = False
    except Exception:
        pass
    hcells = htable.rows[0].cells
    set_row_widths(htable.rows[0], [CONTENT_WIDTH_IN / 2.0, CONTENT_WIDTH_IN / 2.0])

    if left:
        lp = hcells[0].paragraphs[0]
        lp.alignment = WD_ALIGN_PARAGRAPH.LEFT
        try:
            lp.add_run().add_picture(left, width=Inches(0.85))
        except Exception as e:
            log(f"[WARN] Could not embed header left image: {e}")
    if right:
        rp = hcells[1].paragraphs[0]
        rp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        try:
            rp.add_run().add_picture(right, width=Inches(1.5))
        except Exception as e:
            log(f"[WARN] Could not embed header right image: {e}")

    _add_watermark(header)


# Cover date shown on page 1. This is the date the report is GENERATED (today),
# formatted DD/MM/YYYY to match the reference demo's "23/09/2026" style. It is
# computed at generation time via date.today() rather than being hardcoded or
# derived from the report data window.
def _cover_date():
    """Return today's date (report generation date) as DD/MM/YYYY."""
    return f"{date.today():%d/%m/%Y}"

# Cover title uses a BLACK serif face to match the reference (Cambria sz 28pt).
# Cambria may be unavailable on some systems; Times New Roman is a safe serif
# fallback that the Word/LibreOffice font substitution will honour.
COVER_TITLE_FONT = "Cambria"
BLACK = RGBColor(0x00, 0x00, 0x00)


def _add_cover_title(doc, text, bold):
    """Centered cover title line: black serif (Cambria) at 28pt."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    set_spacing(p, before=6, after=6, single=True)
    r = p.add_run(text)
    r.font.name = COVER_TITLE_FONT
    r.font.size = Pt(28)
    r.font.bold = bold
    r.font.color.rgb = BLACK
    return p


def build_cover_page(doc, cur_end):
    """Build the branded cover (page 1) to match branded-source.docx top to
    bottom: large Operisoft logo, "Weekly Status Report" (black serif, bold),
    "Aptech Limited" (black serif, NOT bold), the Aptech logo, the "Submitted
    By" block (Operisoft Technologies Pvt Ltd + date), and the AWS Advanced Tier
    Partner badge LAST at the bottom. The three qualifying phrases (Public
    Sector / Immersion Day / Well-Architected Partner Program) are baked into
    aws_partner_badge.png, so they are NOT repeated as text bullets here."""
    # Push the cover block down a little so it fills page 1 nicely.
    for _ in range(2):
        doc.add_paragraph()

    # NOTE on the two cover asset files: on disk the branding files
    # operisoft_logo_large.png and aptech_logo.png have SWAPPED visual content -
    # operisoft_logo_large.png actually holds the Aptech banner and
    # aptech_logo.png actually holds the Operisoft wordmark. The header asset
    # operisoft_logo_header.png is correct and is untouched. To render the cover
    # in the reference order (Operisoft logo on top, Aptech banner lower) we
    # reference each file by its ACTUAL content below rather than its name.

    # (a) Large Operisoft logo at the top (lives in aptech_logo.png on disk).
    _add_centered_image(doc, "aptech_logo.png", 3.0)

    # (b) "Weekly Status Report" - black Cambria serif, 28pt, BOLD.
    _add_cover_title(doc, "Weekly Status Report", bold=True)
    # (c) "Aptech Limited" - same black serif 28pt but NOT bold (per reference).
    _add_cover_title(doc, "Aptech Limited", bold=False)

    # (d) Aptech 'Unleash your potential' banner (lives in
    #     operisoft_logo_large.png on disk).
    _add_centered_image(doc, "operisoft_logo_large.png", 2.6)

    # (e)-(g) 'Submitted By' block (11pt, matching reference sz val=22 half-points).
    add_text(doc, "Submitted By", size=11, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
             before=6, after=2)
    add_text(doc, "Operisoft Technologies Pvt Ltd", size=11, bold=True,
             align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=2)
    cover_date = _cover_date()
    add_text(doc, cover_date, size=11, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER,
             before=6, after=6)

    # (h) AWS Advanced Tier Partner badge LAST at the bottom. The three
    # qualifying phrases are part of the image itself, so no text bullets.
    _add_centered_image(doc, "aws_partner_badge.png", 2.6)

    # Force the existing report body (master cost table) to start on page 2.
    p_break = doc.add_paragraph()
    p_break.paragraph_format.page_break_before = True
    set_spacing(p_break, before=0, after=0, single=True)


# ------------------------------------------------------------ doc builder --
def generate_docx_report(cost_data, alarm_rows, cur_start, cur_end, prev_start, prev_end, out_path, save_images_dir=None):
    doc = docx.Document()
    for section in doc.sections:
        section.page_width, section.page_height = Inches(PAGE_WIDTH_IN), Inches(PAGE_HEIGHT_IN)
        section.top_margin = section.bottom_margin = Inches(MARGIN_TOP_BOTTOM_IN)
        section.left_margin = section.right_margin = Inches(MARGIN_LEFT_RIGHT_IN)
        section.header_distance = section.footer_distance = Inches(0.3)

    # 0a. Branded per-page header (AWS partner cluster left, Operisoft logo
    #     right) plus the diagonal CONFIDENTIAL watermark on every page.
    build_branded_header(doc)

    # 0b. Branded cover page (page 1). The master cost table begins on page 2.
    build_cover_page(doc, cur_end)

    # (No page-2 title: the client reference starts the summary page directly at
    # the "Cost Summary Difference of All AWS Accounts" heading. The report name
    # and date range live only on the cover page.)

    # 2. Cost summary bullets
    tot_cur = sum(x["cur_cost"] for x in cost_data)
    tot_prev = sum(x["prev_cost"] for x in cost_data)
    tot_tax = sum(x["tax_cost"] for x in cost_data)
    diff_tot = tot_cur - tot_prev
    direction = "decreased" if diff_tot <= 0 else "increased"

    p_h1 = add_text(doc, "Cost Summary Difference of All AWS Accounts", size=14, bold=True, color=NAVY,
                    before=0, after=2, single=True)
    add_bookmark(p_h1, "Summary")

    # 3. Master cost table
    table1 = doc.add_table(rows=1, cols=7)
    table1.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(table1)
    set_table_layout(table1, MASTER_COL_WIDTHS_IN)
    set_table_cell_margins(table1, top=0, bottom=0, left=50, right=50)   # tight rows so all 20 + Total fit one page
    hdr_titles = ["No", "Account Name", "Account ID",
                  f"Last Week Cost ({prev_start:%d %b} to {prev_end:%d %b})", "Tax Cost",
                  f"Current Week Cost ({cur_start:%d %b} to {cur_end:%d %b})", "Services"]
    keep_row_on_one_page(table1.rows[0])
    for i, title in enumerate(hdr_titles):
        c = table1.rows[0].cells[i]
        c.text = title
        set_cell_background(c, "BEBEBE")
        style_cell(c, 8, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=0)

    for item in sorted(cost_data, key=lambda x: x["no"]):
        row = table1.add_row()
        set_row_widths(row, MASTER_COL_WIDTHS_IN)
        keep_row_on_one_page(row)
        cells = row.cells
        cells[0].text = str(item["no"])
        add_internal_hyperlink(cells[1].paragraphs[0], f"_acc_{item['account_id']}", item["name"])
        cells[2].text = item["account_id"]
        cells[3].text = money(item["prev_cost"])
        cells[4].text = money(item["tax_cost"]) if item["tax_cost"] > 0 else ""
        cells[5].text = money(item["cur_cost"])
        cells[6].text = item["remark"]
        for idx, c in enumerate(cells):
            style_cell(c, 7, align=WD_ALIGN_PARAGRAPH.CENTER if idx in (0, 2, 3, 4, 5) else WD_ALIGN_PARAGRAPH.LEFT,
                       before=0, after=0)
        # Meaningful cost decrease: color BOTH the Last Week Cost (index 3) and
        # Current Week Cost (index 5) cells red, matching the client reference.
        # The Tax cell (index 4) always stays black.
        if item["diff"] < -0.5:
            style_cell(cells[3], 7, align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=0, color=RED)
            style_cell(cells[5], 7, align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=0, color=RED)

    tot_row = table1.add_row()
    set_row_widths(tot_row, MASTER_COL_WIDTHS_IN)
    keep_row_on_one_page(tot_row)
    tot = tot_row.cells
    merged = tot[0].merge(tot[2])
    merged.text = "Total Cost"
    tot[4].text = money(tot_tax)
    # Both totals carry an arrow (reference): Last Week total always shows a RED
    # up arrow ED0000 (the baseline the current week is compared against); the
    # Current Week total shows a GREEN down arrow 00AF50 when the cost went down
    # (diff_tot <= 0) or a RED up arrow when it went up. The amounts stay black.
    _set_total_cell(tot[3], "\u2b06\ufe0f", RGBColor(0xED, 0x00, 0x00), money(tot_prev))
    if diff_tot <= 0:
        _set_total_cell(tot[5], "\u2b07\ufe0f", RGBColor(0x00, 0xAF, 0x50), money(tot_cur))
    else:
        _set_total_cell(tot[5], "\u2b06\ufe0f", RGBColor(0xED, 0x00, 0x00), money(tot_cur))
    for idx, c in enumerate(table1.rows[-1].cells):
        set_cell_background(c, "D9E1F2")
        if idx in (3, 5):
            # These two cells carry explicit per-run colors (arrow colored, amount
            # black); only enforce spacing/alignment so style_cell does not repaint them.
            for p in c.paragraphs:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                set_spacing(p, before=0, after=0, single=True)
        else:
            style_cell(c, 8.5, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=0)
    doc.add_paragraph()

    # 3b. Cost summary bullets, now BELOW the master table (disc bullets, bold).
    for txt in (f"The billing for the current week ({cur_start:%d %B} to {cur_end:%d %B}) has {direction} compared to the previous week.",
                f"The cost difference is ${abs(diff_tot):,.2f}."):
        add_bullet(doc, [(txt, True)], ilvl=0, left_in=0.64, hanging_in=0.25, size=12, before=0, after=6, glyph="arrow")
    doc.add_paragraph()

    # 4. Security links table
    add_text(doc, "security best practices: -", size=12, bold=True)
    table2 = doc.add_table(rows=1, cols=2)
    table2.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(table2)
    set_table_layout(table2, SECURITY_COL_WIDTHS_IN)
    for c, t in zip(table2.rows[0].cells, ("Content", "Link")):
        c.text = t
        set_cell_background(c, "FFD600")   # yellow/gold header fill (reference)
        style_cell(c, 9, bold=True)
    for label, url in SECURITY_LINKS:
        row = table2.add_row()
        set_row_widths(row, SECURITY_COL_WIDTHS_IN)
        keep_row_on_one_page(row)
        cells = row.cells
        # Content cell: plain black 8.5pt. Link cell: teal 467885 underlined run
        # (same teal the internal hyperlinks use), matching the reference.
        cells[0].text = label
        style_cell(cells[0], 8.5)
        p_link = cells[1].paragraphs[0]
        set_spacing(p_link, before=1, after=1, single=True)
        r_link = p_link.add_run(url)
        r_link.font.name = "Arial"
        r_link.font.size = Pt(8.5)
        r_link.font.color.rgb = RGBColor(0x46, 0x78, 0x85)
        r_link.font.underline = True

    # 5. One page per account
    alarms_by_acc = defaultdict(list)
    for a in alarm_rows:
        alarms_by_acc[a["account_id"]].append(a)

    sp = ACC_SPACE_AFTER
    ordered_items = sorted(cost_data, key=lambda x: x["no"])
    for pos, item in enumerate(ordered_items):
        acc_id = item["account_id"]
        is_last = pos == len(ordered_items) - 1
        acc_alarms = sorted([a for a in alarms_by_acc.get(acc_id, []) if a["total"] > 0],
                            key=lambda a: (a["region"], a["server"].lower(), a["label"]))

        p_banner = doc.add_paragraph()
        p_banner.paragraph_format.page_break_before = True      # every account starts on a new page
        p_banner.alignment = WD_ALIGN_PARAGRAPH.CENTER
        set_spacing(p_banner, before=0, after=sp["banner"], single=True)
        # Balanced dashes on BOTH sides so the "Summary" back-link sits CENTERED
        # (equal-length runs; the paragraph is center-aligned). Keep each side
        # short enough that the whole banner fits one line and does not wrap
        # (wrapping is what pushes "Summary" off-center).
        for txt, link in (("-------------------------------------------- ", False),
                          ("Summary", True),
                          (" --------------------------------------------", False)):
            if link:
                add_internal_hyperlink(p_banner, "Summary", "Summary", font_size_pt=10, color_hex="1F487C", bold=True)
            else:
                r = p_banner.add_run(txt)
                r.font.name, r.font.size, r.font.bold = "Arial", Pt(9.5), True

        # Account name heading: centered, bold, underlined, black (reference Heading1).
        p_name = add_text(doc, f"{item['name']} – {acc_id}", size=14, bold=True,
                          align=WD_ALIGN_PARAGRAPH.CENTER, before=0, after=sp["name"], single=True)
        for r in p_name.runs:
            r.font.underline = True
        add_bookmark(p_name, f"_acc_{acc_id}")
        add_text(doc, "-" * 128, size=9.5, bold=True, before=0, after=sp["dashes"], single=True)

        # Body as disc bullets with the reference indent hierarchy:
        #   'Billing and Cost Overview' = outer (top-level) bullet, bold
        #   'Total cost ...' / 'Average Daily Cost ...' = inner (deeper) bullets, amount bold
        add_bullet(doc, [("Billing and Cost Overview", True)], ilvl=0, left_in=0.37,
                   hanging_in=0.25, size=12, before=0, after=sp["heading"], glyph="arrow")
        add_bullet(doc, [("Total cost for the week: ", False), (money(item['cur_cost']), True)],
                   ilvl=1, left_in=0.94, hanging_in=0.25, size=12, before=0, after=sp["line"])
        add_bullet(doc, [("Average Daily Cost: ", False), (money(item['avg_daily']), True)],
                   ilvl=1, left_in=0.94, hanging_in=0.25, size=12, before=0, after=sp["avg"])

        detail = item.get("cur_detail")
        if detail:
            # size both images so the whole account (text + images + alarms) stays on ONE page
            img_w, table_rows = plan_cost_images(
                detail, account_text_height_in(item, acc_alarms, cur_start, cur_end, is_last))
            img1, _, _ = render_cost_overview_png(detail)
            img2, _, _ = render_cost_breakdown_png(detail, max_rows=table_rows)
            if save_images_dir:
                os.makedirs(save_images_dir, exist_ok=True)
                for tag, buf in (("1_overview", img1), ("2_breakdown", img2)):
                    with open(os.path.join(save_images_dir, f"{item['no']:02d}_{acc_id}_{tag}.png"), "wb") as f:
                        f.write(buf.getvalue())
                    buf.seek(0)
            for buf in (img1, img2):
                p_img = doc.add_paragraph()
                p_img.alignment = WD_ALIGN_PARAGRAPH.CENTER
                set_spacing(p_img, before=0, after=sp["image"], single=True)
                p_img.add_run().add_picture(buf, width=Inches(img_w))
        else:
            add_text(doc, "Cost Explorer data not available for this account (check role / ce:GetCostAndUsage permission).",
                     size=10, italic=True, before=0, after=sp["line"], single=True)

        if item["tax_cost"] > 0:
            # Inner disc bullet (same indent as the two cost lines above), label plain + value bold.
            add_bullet(doc, [("Total Tax Cost: ", False), (money(item['tax_cost']), True)],
                       ilvl=1, left_in=0.94, hanging_in=0.25, size=12, before=0, after=sp["line"])
        add_bullet(doc, [(item["remark"], False)], ilvl=0, left_in=0.37, hanging_in=0.25,
                   size=12, before=0, after=sp["line"], glyph="arrow")
        add_bullet(doc, [("No Activity performed by Operisoft in this account.", True)], ilvl=0,
                   left_in=0.37, hanging_in=0.25, size=12, before=0, after=sp["note"], glyph="arrow")

        add_text(doc, "Resource Utilization & Alarms", size=14, bold=True, before=0, after=sp["alarm_heading"],
                 single=True, keep_with_next=True)
        if acc_alarms:
            t = doc.add_table(rows=2, cols=4)
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            set_table_borders(t)
            set_table_layout(t, ALARM_COL_WIDTHS_IN)
            for r in t.rows:
                keep_row_on_one_page(r)
                repeat_as_header(r)
            banner = t.rows[0].cells[0].merge(t.rows[0].cells[3])
            banner.text = _alarm_banner_text(cur_start, cur_end)
            style_cell(banner, 10, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
            for c, h in zip(t.rows[1].cells, ALARM_HEADERS):
                c.text = h
                set_cell_background(c, "1F4E78")
                style_cell(c, 10, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, color=RGBColor(0xFF, 0xFF, 0xFF))
            for a in acc_alarms:
                row = t.add_row()
                set_row_widths(row, ALARM_COL_WIDTHS_IN)
                keep_row_on_one_page(row)
                cells = row.cells
                cells[0].text = _alarm_server_text(a)
                cells[1].text = a["region"]
                cells[2].text = a["label"]
                cells[3].text = fmt_dates(a["counts"])
                for idx, c in enumerate(cells):
                    style_cell(c, 8.5, align=WD_ALIGN_PARAGRAPH.CENTER if idx in (1, 2) else WD_ALIGN_PARAGRAPH.LEFT)
        else:
            add_text(doc, "No Resource Utilization & Alarms.", size=11, before=0, after=sp["line"], single=True)

    add_text(doc, "", size=11, before=0, after=sp["end"], single=True)
    add_text(doc, "-- End Of Document --", size=11, italic=True, align=WD_ALIGN_PARAGRAPH.CENTER,
             before=0, after=sp["end"], single=True)

    doc.save(out_path)
    return out_path


# ----------------------------------------------------------------- CLI main --
def main():
    parser = argparse.ArgumentParser(description="Generate the Aptech Weekly Status Report (.docx)")
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--account-id", help="Single Account ID or Account Name filter")
    parser.add_argument("--role-name", default=ROLE_NAME_DEFAULT, help=f"IAM role to assume (default: {ROLE_NAME_DEFAULT})")
    parser.add_argument("--start", help="Current week start date YYYY-MM-DD")
    parser.add_argument("--end", help="Current week end date YYYY-MM-DD")
    parser.add_argument("--timezone", default=DEFAULT_TZ, help=f"Default: {DEFAULT_TZ}")
    parser.add_argument("--output", help="Output .docx file name")
    parser.add_argument("--save-images", action="store_true", help="Also save the cost images as PNG files in ./report_images")
    parser.add_argument("--mock", action="store_true", help="Use mock data (no AWS credentials needed)")
    args = parser.parse_args()

    tz = get_tz(args.timezone)
    print("\n" + "=" * 66)
    print("   Aptech Limited Weekly Status Report - Word Generator")
    print("=" * 66)

    accounts = DEFAULT_ACCOUNTS
    if args.account_id:
        target = args.account_id.strip().lower()
        matched = [a for a in DEFAULT_ACCOUNTS if target in a["account_id"] or target in a["name"].lower()]
        accounts = matched or [{"no": 1, "name": f"Account-{args.account_id}", "account_id": args.account_id}]
        print(f"[INFO] Account filter matched {len(accounts)} account(s)")

    if args.start and args.end:
        try:
            cur_start = datetime.strptime(args.start, "%Y-%m-%d").date()
            cur_end = datetime.strptime(args.end, "%Y-%m-%d").date()
        except ValueError:
            sys.exit("[ERROR] Invalid date format. Use YYYY-MM-DD")
        if cur_end < cur_start:
            sys.exit("[ERROR] --end cannot be before --start")
    else:
        cur_end = datetime.now(tz).date() - timedelta(days=1)
        cur_start = cur_end - timedelta(days=6)

    span = (cur_end - cur_start).days + 1
    prev_start = cur_start - timedelta(days=span)
    prev_end = cur_start - timedelta(days=1)
    cur_days = date_range(cur_start, cur_end)
    prev_days = date_range(prev_start, prev_end)

    start_utc = datetime.combine(cur_start, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end_utc = datetime.combine(cur_end, datetime.max.time().replace(microsecond=0), tzinfo=tz).astimezone(timezone.utc)

    print(f"Current Week  : {cur_start:%d %b %Y} -> {cur_end:%d %b %Y}")
    print(f"Previous Week : {prev_start:%d %b %Y} -> {prev_end:%d %b %Y}")
    print(f"Accounts      : {len(accounts)}")
    print("-" * 66)

    t0 = time.time()
    cost_data, alarm_rows = [], []

    if args.mock:
        print("[MOCK MODE] Generating mock cost and alarm data...")
        for acc in accounts:
            daily = mock_daily_costs(acc, prev_days + cur_days)
            cost_data.append(summarise_account(acc, build_week_detail(daily, cur_days), build_week_detail(daily, prev_days)))
        alarm_rows = generate_mock_alarm_data(accounts, datetime.combine(cur_start, datetime.min.time()),
                                              datetime.combine(cur_end, datetime.min.time()))
    else:
        try:
            base_session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
            ident = base_session.client("sts", config=boto_config()).get_caller_identity()
            base_account_id = ident["Account"]
            print(f"Base AWS Account : {base_account_id}")
            print(f"Caller Identity  : {ident['Arn']}\n")
        except NoCredentialsError:
            sys.exit("[ERROR] AWS credentials not found. Run `aws configure` or use --mock.")
        except Exception as e:
            sys.exit(f"[ERROR] AWS connection failed: {e}")

        for acc in accounts:
            acc_id = acc["account_id"]
            print(f"[Account {acc['no']}] {acc['name']} ({acc_id})")
            # Always assume the readonly role (also in the base account) so permissions are identical.
            acc_session = assume_role_session(base_session, acc_id, args.role_name)
            if not acc_session and acc_id == base_account_id:
                acc_session = base_session
            if not acc_session:
                cost_data.append(failed_account(acc, "Failed (AssumeRole Error)"))
                continue

            daily = fetch_daily_service_costs(acc_session, prev_start.isoformat(), (cur_end + timedelta(days=1)).isoformat())
            if daily is None:
                cost_data.append(failed_account(acc, "Failed (Cost Explorer Error)"))
            else:
                item = summarise_account(acc, build_week_detail(daily, cur_days), build_week_detail(daily, prev_days))
                cost_data.append(item)
                print(f"   Cost: {money(item['cur_cost'])} (prev {money(item['prev_cost'])}), "
                      f"services: {_service_count(item['cur_detail'])}")

            regions = list_regions(acc_session)
            acc_rows = []
            with ThreadPoolExecutor(max_workers=6) as pool:
                for fut in as_completed([pool.submit(process_region, acc, acc_session, r, start_utc, end_utc, tz)
                                         for r in regions]):
                    try:
                        acc_rows.extend(fut.result())
                    except Exception:
                        pass
            alarm_rows.extend(acc_rows)
            print(f"   Alarms: {len(acc_rows)} monitored, {len([r for r in acc_rows if r['total'] > 0])} triggered")

    out_name = args.output or f"Aptech-Limited-Weekly-Status-Report_{cur_start:%Y%m%d}_to_{cur_end:%Y%m%d}"
    if not out_name.lower().endswith(".docx"):
        out_name += ".docx"
    out_file = generate_docx_report(cost_data, alarm_rows, cur_start, cur_end, prev_start, prev_end, out_name,
                                    save_images_dir="report_images" if args.save_images else None)

    print("-" * 66)
    print(f"Total Time : {time.time() - t0:.1f}s")
    print(f"[OK] Word Status Report Ready -> {os.path.abspath(out_file)}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n[Cancelled]")
