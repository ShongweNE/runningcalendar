import logging
import os

import httpx

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(to: str, subject: str, html_body: str) -> None:
    """Send an email via Resend, or log it locally if RESEND_API_KEY isn't set.

    The local-dev fallback matters here specifically: without it, testing the
    reset flow would require a working Resend account on every dev machine.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.warning("RESEND_API_KEY not set -- email not sent, logging instead:\nTo: %s\nSubject: %s\n%s",
                        to, subject, html_body)
        return

    from_email = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.com")
    resp = httpx.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": from_email, "to": [to], "subject": subject, "html": html_body},
        timeout=10,
    )
    if resp.status_code >= 400:
        logger.error("Resend API error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
