# Renewal reminders + login-free renewal links (0.1.76, Steward 2026-09-28).
#
# WHY: 0.1.75 stopped attaching a Paystack plan (a plan made every checkout a
# card-only subscription), so there is no automatic renewal any more. The portal
# therefore has to do what the plan used to do: warn the customer that the term
# is ending, and make paying effortless. Steward's chosen model: when the time is
# due or expired the customer repays, and the renewal date drives the reminders.
#
# SCHEDULE (one email per stage per TERM; `period` = the paid_until it was about,
# so next year's reminders are not suppressed by this year's):
#   days_left 8..30   -> "30"   about a month out
#   days_left 1..7    -> "7"
#   days_left 0       -> "0"    expires today
#   days_left -1..-21 -> "post" the workspace is paused; this is the one that
#                               actually wins customers back
#
# SENDING IS OFF unless RENEWAL_REMINDERS_ENABLED is true, so the wording can be
# reviewed against live data before any customer is emailed. `preview()` renders
# every stage without sending, and `send_for_account()` sends one deliberate
# message for a chosen account (the admin's own, for a live end-to-end test).
#
# THE LINK is a random 32-byte token bound to (account, package) with a TTL,
# stored in renewal_links. It needs no password because an expired customer may
# not remember one, and all it can do is start a payment for that same account:
# it cannot sign in, read data, change anything, or reach another account.

import secrets
import time
from html import escape as _esc

from .. import db
from ..config import settings
from . import emailer

DAY = 86400
POST_WINDOW_DAYS = 21      # how long after expiry the "workspace paused" mail still goes out
LINK_TTL_DAYS = 60         # Steward approved a 60-day link
STAGES = ("30", "7", "0", "post")
_LAST_RUN_SETTING = "renewal_reminders_last_run"


# ---------- package + formatting helpers ----------

def plan_fields(plan: str) -> tuple[str, int]:
    """(display name, amount in minor units) for a package key."""
    if plan == "b":
        return settings.plan_b_name, settings.plan_b_amount_minor
    return settings.plan_name, settings.plan_amount_minor


def format_amount(minor: int) -> str:
    return f"{settings.plan_currency} {minor // 100:,}"


def format_date(ts: int) -> str:
    return time.strftime("%d %B %Y", time.gmtime(int(ts)))


def days_left(paid_until, now: int | None = None) -> int | None:
    """Whole days until the term ends (negative once past, 0 on the last day)."""
    if not paid_until:
        return None
    now = int(now if now is not None else time.time())
    diff = int(paid_until) - now
    return -(-diff // DAY)   # ceil, so "expires today" is only the same day


def stage_for(days: int | None) -> str | None:
    """Which reminder, if any, is due for this many days left."""
    if days is None:
        return None
    if 8 <= days <= 30:
        return "30"
    if 1 <= days <= 7:
        return "7"
    if days == 0:
        return "0"
    if -POST_WINDOW_DAYS <= days <= -1:
        return "post"
    return None


# ---------- renewal links ----------

def create_link(account_id: int, plan: str, ttl_days: int = LINK_TTL_DAYS,
                now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    token = secrets.token_urlsafe(32)
    db.create_renewal_link(token, int(account_id), plan if plan in ("a", "b") else "a",
                           now, now + ttl_days * DAY)
    return token


def link_url(token: str) -> str:
    return f"{settings.portal_base_url.rstrip('/')}/r/{token}"


def resolve_link(token: str, now: int | None = None):
    """The link row when it exists and has not expired, else None."""
    if not token or len(token) > 200:
        return None
    row = db.get_renewal_link(token)
    if not row:
        return None
    now = int(now if now is not None else time.time())
    if int(row["expires_at"]) < now:
        return None
    return row


def link_for_account(account_id: int, plan: str, now: int | None = None) -> str:
    """Reuse a live link for this account+package, or mint a fresh one."""
    now = int(now if now is not None else time.time())
    existing = db.renewal_link_for_account(int(account_id), plan, now)
    if existing:
        return existing
    return create_link(account_id, plan, now=now)


# ---------- email rendering ----------

def _first_name(account) -> str:
    for key in ("first_name", "display_name"):
        val = (account[key] or "").strip() if account[key] is not None else ""
        if val:
            return val.split()[0]
    return (account["username"] or "there").strip()


def _workspace(account_id: int) -> tuple[str, str]:
    """(label, url) for the customer's workspace, falling back to the portal."""
    inst = db.get_active_instance(account_id)
    domain = (inst["domain"] or "").strip() if inst and "domain" in inst.keys() else ""
    if domain:
        return domain, f"https://{domain}"
    return "your workspace", settings.portal_base_url.rstrip("/")


def _copy(stage: str, first: str, plan_name: str, amount: str, date: str,
          workspace: str, portal: str) -> tuple[str, str]:
    """(subject, body) for a stage. No em dashes anywhere (Steward's copy rule)."""
    if stage == "30":
        subject = f"Your n8n workspace renews on {date}"
        body = (f"Hello {first},\n\n"
                f"Your {plan_name} subscription renews on {date}. To keep {workspace} "
                f"running without interruption, you can renew any time before then.\n\n"
                f"Package: {plan_name}\nAmount: {amount} for one year\n"
                f"Renewal date: {date}\n")
    elif stage == "7":
        subject = "Your n8n workspace renews in 7 days"
        body = (f"Hello {first},\n\n"
                f"Your {plan_name} subscription renews in 7 days, on {date}. "
                f"Renewing before then keeps {workspace} running without interruption.\n\n"
                f"Package: {plan_name}\nAmount: {amount} for one year\n"
                f"Renewal date: {date}\n")
    elif stage == "0":
        subject = "Your n8n workspace subscription expires today"
        body = (f"Hello {first},\n\n"
                f"Your {plan_name} subscription expires today, {date}. Renew today to "
                f"avoid any interruption to {workspace}.\n\n"
                f"Package: {plan_name}\nAmount: {amount} for one year\n"
                f"Expiry date: {date}\n")
    else:  # "post"
        subject = "Your n8n workspace has been paused"
        body = (f"Hello {first},\n\n"
                f"Your {plan_name} subscription ended on {date}, so {workspace} has been "
                f"paused. Your workflows and data are untouched, and paying now starts it "
                f"again straight away with the same login details as before.\n\n"
                f"Package: {plan_name}\nAmount: {amount} for one year\n"
                f"Ended on: {date}\n")
    return subject, body


def _html(subject: str, first: str, body_lines: list[str], link: str, portal: str,
          plan_name: str, amount: str, date_label: str, date: str) -> str:
    """Same inline-styled house style the portal's other customer emails use."""
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">{_esc(subject)}</h2>
      <p>Hello <b>{_esc(first)}</b>,</p>
      {''.join(f'<p>{line}</p>' for line in body_lines)}
      <table style="border-collapse:collapse;width:100%;margin:16px 0">
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Package</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{_esc(plan_name)}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Amount</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{_esc(amount)} for one year</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>{_esc(date_label)}</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{_esc(date)}</td></tr>
      </table>
      <p style="text-align:center;margin:24px 0">
        <a href="{_esc(link)}" style="background:#1f2937;color:#ffffff;padding:12px 22px;
           border-radius:6px;text-decoration:none;font-weight:bold">Renew now</a>
      </p>
      <p>That link opens the payment page for your account directly, so there is no need to
      sign in. Card, mobile money and bank transfer are all accepted. It stays valid for
      60 days.</p>
      <p>If you have already renewed, please ignore this email. You can also renew from your
      dashboard at <a href="{_esc(portal)}">{_esc(portal)}</a>.</p>
      <p>Thank you for hosting with SteProTECH.</p>
      <p style="color:#6b7280;font-size:12px">You are receiving this because you have a
      workspace subscription on the SteProTECH n8n portal.</p>
    </div>
    """


def render(stage: str, account, plan: str | None = None, token: str | None = None,
           now: int | None = None) -> dict:
    """The exact email for one account and stage (subject, text, html, link)."""
    now = int(now if now is not None else time.time())
    plan = plan or ((account["plan"] or "a") if account["plan"] is not None else "a")
    if plan not in ("a", "b"):
        plan = "a"
    plan_name, amount_minor = plan_fields(plan)
    amount = format_amount(amount_minor)
    paid_until = int(account["paid_until"] or now)
    date = format_date(paid_until)
    first = _first_name(account)
    workspace, _url = _workspace(int(account["id"]))
    portal = settings.portal_base_url.rstrip("/")
    token = token or create_link(int(account["id"]), plan, now=now)
    link = link_url(token)

    subject, body = _copy(stage, first, plan_name, amount, date, workspace, portal)
    body_lines = []
    for para in body.split("\n\n")[1:]:            # drop the greeting, kept separately
        body_lines.append(_esc(para).replace("\n", "<br>"))
    date_label = {"30": "Renewal date", "7": "Renewal date", "0": "Expiry date",
                  "post": "Ended on"}.get(stage, "Renewal date")
    html = _html(subject, first, body_lines, link, portal, plan_name, amount,
                 date_label, date)
    text = (body + f"\nRenew now: {link}\n"
            f"That link opens the payment page for your account directly, so there is no "
            f"need to sign in. Card, mobile money and bank transfer are all accepted. "
            f"It stays valid for 60 days.\n\n"
            f"If you have already renewed, please ignore this email. You can also renew "
            f"from your dashboard at {portal}.\n\nThank you for hosting with SteProTECH.\n")
    return {"stage": stage, "plan": plan, "subject": subject, "text": text, "html": html,
            "link": link, "to": account["email"], "days_left": days_left(paid_until, now)}


# ---------- the sweep ----------

def due_stages(now: int | None = None) -> list[dict]:
    """What the next sweep would send, without sending anything."""
    now = int(now if now is not None else time.time())
    out = []
    for acc in db.renewal_candidates(now):
        days = days_left(acc["paid_until"], now)
        stage = stage_for(days)
        if not stage:
            continue
        period = int(acc["paid_until"])
        if db.notice_sent(int(acc["id"]), stage, period):
            continue
        out.append({"account_id": int(acc["id"]), "email": acc["email"], "stage": stage,
                    "days_left": days, "paid_until": period})
    return out


def maybe_send_reminders(force: bool = False, now: int | None = None) -> dict:
    """Send the reminders that are due. Once per day unless forced.

    Requires RENEWAL_REMINDERS_ENABLED unless forced, so the wording is never
    emailed to customers before Steward approves it.
    """
    now = int(now if now is not None else time.time())
    if not (settings.renewal_reminders_enabled or force):
        return {"enabled": False, "sent": 0, "skipped": "renewal reminders are switched off"}

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    last = db.get_setting(_LAST_RUN_SETTING)
    if last == today and not force:
        return {"enabled": True, "sent": 0, "skipped": f"already ran on {today}"}

    result = {"enabled": bool(settings.renewal_reminders_enabled), "sent": 0,
              "failed": 0, "detail": []}
    for item in due_stages(now):
        acc = db.get_account(item["account_id"])
        if not acc:
            continue
        try:
            mail = render(item["stage"], acc, now=now)
            emailer.send_email(item["email"], mail["subject"], mail["html"], mail["text"])
        except Exception as e:  # noqa: BLE001 - one bad address must not stop the rest
            result["failed"] += 1
            result["detail"].append({"account_id": item["account_id"], "stage": item["stage"],
                                     "error": str(e)[:200]})
            continue
        db.record_notice(item["account_id"], item["stage"], item["paid_until"], now,
                         item["email"])
        result["sent"] += 1
        result["detail"].append({"account_id": item["account_id"], "stage": item["stage"],
                                 "email": item["email"]})
    db.set_setting(_LAST_RUN_SETTING, today)
    return result


def send_for_account(account_id: int, stage: str, now: int | None = None) -> dict:
    """Send ONE reminder for a chosen account, regardless of the switch.

    This is the deliberate live test (and the admin escape hatch); the account is
    picked by the operator, and the notice is still recorded so the normal sweep
    will not send the same stage for the same term twice.
    """
    now = int(now if now is not None else time.time())
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    acc = db.get_account(int(account_id))
    if not acc:
        raise ValueError(f"no account {account_id}")
    mail = render(stage, acc, now=now)
    emailer.send_email(mail["to"], mail["subject"], mail["html"], mail["text"])
    if acc["paid_until"]:
        db.record_notice(int(account_id), stage, int(acc["paid_until"]), now, mail["to"])
    return {"sent": 1, "to": mail["to"], "stage": stage, "subject": mail["subject"],
            "link": mail["link"]}


def preview(account_id: int | None = None, now: int | None = None) -> dict:
    """Every stage rendered for one account (no email sent, no token consumed)."""
    now = int(now if now is not None else time.time())
    acc = db.get_account(int(account_id)) if account_id else None
    sample = acc is None
    if acc is None:
        acc = _sample_account(now)
    mails = [render(stage, acc, now=now) for stage in STAGES]
    return {
        "sampled": sample,
        "account_id": int(acc["id"]),
        "to": acc["email"],
        "plan": acc["plan"],
        "paid_until": acc["paid_until"],
        "days_left": days_left(acc["paid_until"], now),
        "sending_enabled": bool(settings.renewal_reminders_enabled),
        "schedule": {"30": "30 days before", "7": "7 days before", "0": "on the expiry day",
                     "post": f"the day after expiry (up to {POST_WINDOW_DAYS} days after)"},
        "due_now": due_stages(now),
        "emails": mails,
    }


def _sample_account(now: int) -> dict:
    """A plausible account for previewing copy when no account id is given."""
    return {"id": 0, "email": "customer@example.com", "username": "customer",
            "display_name": "", "first_name": "Customer", "last_name": "",
            "plan": "a", "paid_until": now + 30 * DAY, "account_state": "active"}
