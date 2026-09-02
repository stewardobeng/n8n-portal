# SMTP emailer for welcome / credentials / password-reset emails.
# Uses the portal's own SMTP config (purelymail, same as the n8n stacks).

import smtplib
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


def _strip(html: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", html)
