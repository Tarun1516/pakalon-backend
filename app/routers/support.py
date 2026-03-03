"""Support / contact-form router (T142 — no-auth contact endpoint)."""
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/support", tags=["support"])


class SupportRequest(BaseModel):
    """Body for the contact-form submission."""
    name: str
    email: EmailStr
    message: str
    subject: str = "Pakalon Support Request"


class SupportResponse(BaseModel):
    success: bool
    message: str


@router.post(
    "",
    response_model=SupportResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a support / contact-form message",
)
async def submit_support(body: SupportRequest) -> SupportResponse:
    """
    T142: Accept a contact-form submission from the web dashboard.

    Tries to deliver via the Resend email service.
    Falls back gracefully if Resend is not configured (no API key).
    No authentication required — anyone can submit.
    """
    from app.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    support_email = getattr(settings, "support_email", "support@pakalon.com")

    # Attempt delivery via Resend
    if settings.resend_api_key:
        try:
            import resend  # type: ignore

            resend.api_key = settings.resend_api_key
            resend.Emails.send(
                {
                    "from": "noreply@pakalon.com",
                    "to": support_email,
                    "reply_to": body.email,
                    "subject": f"[Pakalon Contact] {body.subject}",
                    "html": (
                        f"<p><strong>From:</strong> {body.name} &lt;{body.email}&gt;</p>"
                        f"<p><strong>Subject:</strong> {body.subject}</p>"
                        f"<hr/>"
                        f"<p>{body.message.replace(chr(10), '<br/>')}</p>"
                    ),
                }
            )
            logger.info("Support email sent from %s", body.email)
            return SupportResponse(
                success=True,
                message="Your message has been sent. We'll get back to you soon.",
            )
        except Exception as exc:
            logger.error("Failed to send support email: %s", exc)
            # Don't expose internal error — just log and return success-ish
            return SupportResponse(
                success=True,
                message="Your message was received. We'll get back to you soon.",
            )
    else:
        # No Resend key — log and acknowledge
        logger.info(
            "Support request from %s (%s): %s",
            body.name,
            body.email,
            body.message[:200],
        )
        return SupportResponse(
            success=True,
            message="Your message was received. We'll get back to you soon.",
        )
