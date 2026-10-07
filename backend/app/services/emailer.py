# SMTP emailer for welcome / credentials / password-reset emails.
# Uses the portal's own SMTP config (purelymail, same as the n8n stacks).

import smtplib
import time
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from ..config import settings


class EmailError(Exception):
    pass


def _sender_parts(sender: str) -> tuple[str, str]:
    """Split 'Name <addr>' (or '<addr>', or bare 'addr') into (name, addr)."""
    name, addr = parseaddr(sender)
    if not addr and "@" in sender and sender.strip().startswith("<"):
        addr = sender.strip().strip("<>")
    if not addr:
        addr = settings.smtp_user
    return name.strip(), addr.strip()


def send_email(to: str, subject: str, html: str, text: str | None = None) -> None:
    if not settings.smtp_pass:
        raise EmailError("SMTP password not configured.")
    name, addr = _sender_parts(settings.smtp_sender)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=addr.split("@")[-1])
    msg.attach(MIMEText(text or _strip(html), "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_starttls:
                server.starttls(context=ssl.create_default_context())
            server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(settings.smtp_user, [to], msg.as_string())
    except Exception as e:
        raise EmailError(f"SMTP send failed: {e}") from e


def send_welcome_credentials(to: str, username: str, domain: str, port: int,
                             basic_auth_user: str, basic_auth_password: str) -> None:
    url = f"https://{domain}/"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Your n8n workspace is ready 🎉</h2>
      <p>Hello <b>{username}</b>, your n8n instance has been provisioned successfully.</p>
      <table style="border-collapse:collapse;width:100%;margin:16px 0">
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Workspace URL</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb"><a href="{url}">{url}</a></td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Login (email)</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{basic_auth_user}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Temporary password</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb"><code>{basic_auth_password}</code></td></tr>
      </table>
      <p><b>Your account is ready to use.</b> Sign in with the email and temporary
      password above — your n8n owner account has already been created for you.</p>
      <p>If you ever forget your password, use the <b>Forgot password</b> link on the
      sign-in page and a reset link will be emailed to you.</p>
      <p style="color:#6b7280;font-size:12px">Port {port} on environment server — internal detail.</p>
    </div>
    """
    send_email(to, "Your n8n workspace is ready", html)


def send_reset_password(to: str, username: str, new_password: str) -> None:
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Workspace password reset</h2>
      <p>Hello <b>{username}</b>, your workspace access password has been reset.</p>
      <p>New password: <code>{new_password}</code></p>
      <p>Sign in at <b>https://{username}.{settings.base_domain}/</b> with your email and
      this new password.</p>
    </div>
    """
    send_email(to, "Your n8n workspace password was reset", html)


def send_access_token(to: str, token: str) -> None:
    """Invite token for the portal: enter email + this token to register."""
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Your portal access token</h2>
      <p>Hello, an administrator has approved your request to join the SteProTECH
      n8n portal.</p>
      <p>Go to <b>https://portal.steprotech.com/</b>, enter your email address, and
      use this access token when asked:</p>
      <p style="text-align:center;font-size:24px;letter-spacing:2px;padding:12px;
                background:#f3f4f6;border-radius:4px"><code>{token}</code></p>
      <p>The token is valid for 72 hours. After you register, you will sign in with
      your email and password from then on.</p>
    </div>
    """
    send_email(to, "Your SteProTECH n8n portal access token", html)


def send_access_approved(to: str) -> None:
    """0.1.78 (Steward): approval notice. Copy approved 2026-10-01 and deliberately
    says nothing about a code and nothing about being signed in automatically, both
    of which he judged unnecessary. No em dashes (house rule)."""
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Your access is approved</h2>
      <p>Hello,</p>
      <p>An administrator has approved your request to join the SteProTECH n8n
      portal.</p>
      <p><b>What to do next</b></p>
      <ol>
        <li>Open the portal at <a href="https://portal.steprotech.com/">https://portal.steprotech.com/</a></li>
        <li>Enter this email address</li>
        <li>Create your account with your name and a password</li>
        <li>Choose a plan to set up your first workspace</li>
      </ol>
      <p>If you did not request access, you can ignore this message.</p>
    </div>
    """
    send_email(to, "Your access to the SteProTECH n8n portal is approved", html)


def send_admin_welcome_credentials(to: str, email: str, username: str,
                                   password: str) -> None:
    """Admin-created portal account: sign-in details for the portal itself."""
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Your SteProTECH n8n portal account</h2>
      <p>Hello <b>{username}</b>, an administrator has created your account on the
      SteProTECH n8n portal.</p>
      <table style="border-collapse:collapse;width:100%;margin:16px 0">
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Portal</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb"><a href="https://portal.steprotech.com/">https://portal.steprotech.com/</a></td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Email</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{email}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Temporary password</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb"><code>{password}</code></td></tr>
      </table>
      <p>Sign in with your email and this temporary password, then change it in
      your profile. Your workspace is prepared by the administrator.</p>
    </div>
    """
    send_email(to, "Your SteProTECH n8n portal account", html)


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ts)))


def _dur(row) -> str:
    if not row["started_at"] or not row["finished_at"]:
        return "-"
    secs = max(0, int(row["finished_at"]) - int(row["started_at"]))
    if secs < 60:
        return str(secs) + "s"
    return str(secs // 60) + "m " + str(secs % 60) + "s"


def send_update_report(to: str, rows) -> None:
    """The update batch report (0.1.85, Steward 2026-10-07).

    One mail per batch: what moved, from which version to which, how long it took
    and anything that did not go to plan. Also states plainly that no safety backup
    was taken, because that was Steward's decision, and a report should not imply
    protection that is not there.
    """
    rows = list(rows or [])
    done = [r for r in rows if r["status"] == "done"]
    failed = [r for r in rows if r["status"] == "failed"]
    other = [r for r in rows if r["status"] not in ("done", "failed")]
    who = (rows[0]["requested_by"] if rows else "admin")
    origin = "your dashboard" if who in ("admin", "admin-manual") else "customer requests"
    lines = []
    for r in rows:
        status = {"done": "Updated", "failed": "FAILED", "skipped": "Skipped",
                  "canceled": "Canceled"}.get(r["status"], r["status"])
        went = (r["from_version"] or "unknown") + " to " + (r["result_version"] or r["target_version"] or "?")
        detail = r["error"] or r["note"] or ""
        lines.append(
            "<tr>"
            "<td style=\"padding:6px;border:1px solid #e5e7eb\"><b>{name}</b><br>"
            "<span style=\"color:#6b7280;font-size:12px\">server {host}</span></td>"
            "<td style=\"padding:6px;border:1px solid #e5e7eb\">{went}</td>"
            "<td style=\"padding:6px;border:1px solid #e5e7eb\">{status}</td>"
            "<td style=\"padding:6px;border:1px solid #e5e7eb\">{dur}</td>"
            "<td style=\"padding:6px;border:1px solid #e5e7eb\">{detail}</td>"
            "</tr>".format(name=r["stack_name"] or ("workspace #" + str(r["instance_id"])),
                           host=r["environment_id"], went=went, status=status,
                           dur=_dur(r), detail=detail)
        )
    subject = "Workspace updates: " + str(len(done)) + " updated"
    if failed:
        subject += ", " + str(len(failed)) + " failed"
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:720px;margin:auto">
      <h2 style="color:#1f2937">Workspace update report</h2>
      <p>The update run triggered from {origin} has finished.</p>
      <p><b>{len(done)}</b> updated &middot; <b>{len(failed)}</b> failed &middot;
         {len(other)} skipped or canceled &middot; batch <b>{(rows[0]['batch_id'] if rows else '')}</b></p>
      <table style="border-collapse:collapse;width:100%;margin:16px 0;font-size:13px">
        <tr style="background:#f9fafb">
          <th style="padding:6px;border:1px solid #e5e7eb;text-align:left">Workspace</th>
          <th style="padding:6px;border:1px solid #e5e7eb;text-align:left">Version</th>
          <th style="padding:6px;border:1px solid #e5e7eb;text-align:left">Result</th>
          <th style="padding:6px;border:1px solid #e5e7eb;text-align:left">Took</th>
          <th style="padding:6px;border:1px solid #e5e7eb;text-align:left">Notes</th>
        </tr>
        {''.join(lines)}
      </table>
      <p style="color:#6b7280;font-size:12px">Updates ran one workspace per server with a
      quiet gap between them, and each one was verified before the next started. No safety
      backup was taken before these updates (by your instruction); the previous version is
      recorded above in case you need to pin a workspace back.</p>
      <p style="color:#6b7280;font-size:12px">A workspace that failed was rolled back to the
      version it was on, unless the report says the rollback also failed. Open
      <a href="https://portal.steprotech.com/#/admin/updates">Portal, Updates</a> for the
      live queue.</p>
    </div>
    """
    send_email(to, subject, html)


def send_update_failure(to: str, row) -> None:
    """Immediate alert: one workspace did not update (Steward asked for these)."""
    name = row["stack_name"] or ("workspace #" + str(row["instance_id"]))
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:auto">
      <h2 style="color:#b91c1c">An update failed</h2>
      <p>This one needs your attention.</p>
      <table style="border-collapse:collapse;width:100%;margin:16px 0">
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Workspace</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{name}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Server</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{row['environment_id']}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Was</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{row['from_version'] or 'unknown'}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Target</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{row['target_version']}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Reason</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{row['error'] or row['note']}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>When</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{_fmt_ts(row['finished_at'])}</td></tr>
      </table>
      <p style="color:#6b7280;font-size:12px">The rest of the queue keeps running one workspace
      per server. You can re-queue this one from Portal, Updates once the cause is clear.</p>
    </div>
    """
    send_email(to, "Update failed: " + name, html)


def send_workspace_updated(to: str, stack_name: str, domain: str, from_version: str,
                           to_version: str, note: str = "") -> None:
    """The customer's own confirmation (Steward said yes to this one)."""
    url = "https://" + (domain or "portal.steprotech.com")
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
      <h2 style="color:#1f2937">Your workspace has been updated</h2>
      <p>Hello,</p>
      <p>The workspace <b>{stack_name}</b> has been updated and verified, and it is
      running normally again.</p>
      <table style="border-collapse:collapse;width:100%;margin:16px 0">
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Was</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{from_version or 'older release'}</td></tr>
        <tr><td style="padding:6px;border:1px solid #e5e7eb"><b>Now</b></td>
            <td style="padding:6px;border:1px solid #e5e7eb">{to_version or 'latest release'}</td></tr>
      </table>
      <p>Your workflows, credentials and data are unchanged. Open your workspace at
      <a href="{url}">{url}</a>.</p>
      <p>If anything looks wrong, reply to this email and the SteProTECH team will
      look at it.</p>
    </div>
    """
    send_email(to, "Your n8n workspace has been updated", html)


def _strip(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html)
