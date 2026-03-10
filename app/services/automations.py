"""Automation workflows, OAuth connectors, and cron execution helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, quote

import httpx
from apscheduler.triggers.cron import CronTrigger
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.automation import Automation
from app.models.automation_connector import AutomationConnector
from app.models.automation_log import AutomationLog
from app.scheduler import scheduler

logger = logging.getLogger(__name__)

AUTOMATION_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "github-pr-slack",
        "name": "GitHub PR issues → Slack",
        "description": "Watch a GitHub repository and send issue / PR summaries to Slack.",
        "recommended_connectors": ["github", "slack"],
        "default_cron": "0 * * * *",
        "prompt_hint": "Check my repo owner/repo for open PR issues and post updates to #dev-alerts in Slack.",
    },
    {
        "key": "daily-standup-notion",
        "name": "Daily standup digest → Notion",
        "description": "Build a daily summary and push it to a Notion workspace.",
        "recommended_connectors": ["github", "notion"],
        "default_cron": "0 9 * * 1-5",
        "prompt_hint": "Every weekday morning collect yesterday's repo updates and write a standup digest to Notion.",
    },
    {
        "key": "deploy-alert-discord",
        "name": "Deploy alerts → Discord",
        "description": "Notify a Discord channel when deployment-related events are detected.",
        "recommended_connectors": ["github", "discord"],
        "default_cron": "*/30 * * * *",
        "prompt_hint": "Check release activity in my repo every 30 minutes and notify Discord when a deployment changes.",
    },
]

CONNECTOR_CATALOG: list[dict[str, Any]] = [
    {"provider": "github", "display_name": "GitHub", "category": "code", "domain": "github.com", "oauth_supported": True, "coming_soon": False},
    {"provider": "slack", "display_name": "Slack", "category": "communication", "domain": "slack.com", "oauth_supported": True, "coming_soon": False},
    {"provider": "gitlab", "display_name": "GitLab", "category": "code", "domain": "gitlab.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "discord", "display_name": "Discord", "category": "communication", "domain": "discord.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "notion", "display_name": "Notion", "category": "workspace", "domain": "notion.so", "oauth_supported": False, "coming_soon": True},
    {"provider": "linear", "display_name": "Linear", "category": "project-management", "domain": "linear.app", "oauth_supported": False, "coming_soon": True},
    {"provider": "jira", "display_name": "Jira", "category": "project-management", "domain": "atlassian.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "google-sheets", "display_name": "Google Sheets", "category": "workspace", "domain": "google.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "google-calendar", "display_name": "Google Calendar", "category": "workspace", "domain": "google.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "trello", "display_name": "Trello", "category": "project-management", "domain": "trello.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "asana", "display_name": "Asana", "category": "project-management", "domain": "asana.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "figma", "display_name": "Figma", "category": "design", "domain": "figma.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "stripe", "display_name": "Stripe", "category": "payments", "domain": "stripe.com", "oauth_supported": False, "coming_soon": True},
    {"provider": "pagerduty", "display_name": "PagerDuty", "category": "operations", "domain": "pagerduty.com", "oauth_supported": False, "coming_soon": True},
]

REPO_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
CHANNEL_RE = re.compile(r"(#[A-Za-z0-9_-]+)")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fernet() -> Fernet:
    settings = get_settings()
    seed = (settings.jwt_secret or "pakalon-automation-default-secret").encode("utf-8")
    digest = hashlib.sha256(seed).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def get_templates() -> list[dict[str, Any]]:
    return AUTOMATION_TEMPLATES


def get_connector_catalog() -> list[dict[str, Any]]:
    return CONNECTOR_CATALOG


def build_connector_logo_url(domain: str | None) -> str | None:
    if not domain:
        return None
    publishable_key = get_settings().logo_dev_publishable_key
    if not publishable_key:
        return None
    safe_domain = quote(domain.strip().lower(), safe=".")
    return (
        f"https://img.logo.dev/{safe_domain}"
        f"?token={quote(publishable_key, safe='')}"
        "&size=96&format=png&theme=dark&retina=true&fallback=404"
    )


def _normalize_schedule(schedule_cron: str | None, prompt: str) -> str:
    if schedule_cron:
        lower = schedule_cron.strip().lower()
        mapping = {
            "hourly": "0 * * * *",
            "daily": "0 9 * * *",
            "weekdays": "0 9 * * 1-5",
            "weekly": "0 9 * * 1",
        }
        return mapping.get(lower, schedule_cron.strip())

    lower_prompt = prompt.lower()
    if "week" in lower_prompt:
        return "0 9 * * 1"
    if "daily" in lower_prompt or "every day" in lower_prompt:
        return "0 9 * * *"
    if "weekday" in lower_prompt or "working day" in lower_prompt:
        return "0 9 * * 1-5"
    return "0 * * * *"


def infer_automation_config(
    prompt: str,
    schedule_cron: str | None = None,
    template_key: str | None = None,
    required_connectors_override: list[str] | None = None,
) -> dict[str, Any]:
    """Infer a first-pass automation definition from natural language."""
    lower = prompt.lower()
    connectors: set[str] = set()

    keyword_provider_map = {
        "github": "github",
        "repo": "github",
        "pull request": "github",
        "pr ": "github",
        "issue": "github",
        "slack": "slack",
        "discord": "discord",
        "notion": "notion",
        "linear": "linear",
        "jira": "jira",
        "calendar": "google-calendar",
        "sheet": "google-sheets",
        "trello": "trello",
        "asana": "asana",
        "figma": "figma",
        "stripe": "stripe",
        "pagerduty": "pagerduty",
    }
    for keyword, provider in keyword_provider_map.items():
        if keyword in lower:
            connectors.add(provider)

    if template_key:
        template = next((t for t in AUTOMATION_TEMPLATES if t["key"] == template_key), None)
        if template:
            connectors.update(template["recommended_connectors"])
    if required_connectors_override:
        connectors.update(c.strip().lower() for c in required_connectors_override if c.strip())

    repo_match = REPO_RE.search(prompt)
    channel_match = CHANNEL_RE.search(prompt)
    repo = repo_match.group(1) if repo_match else None
    channel = channel_match.group(1) if channel_match else None

    schedule = _normalize_schedule(schedule_cron, prompt)
    watches_issues = "issue" in lower
    watches_prs = "pr" in lower or "pull request" in lower
    kind = "generic"
    if {"github", "slack"}.issubset(connectors) and (watches_issues or watches_prs):
        kind = "github_repo_to_slack"

    steps = []
    if "github" in connectors:
        steps.append({"connector": "github", "operation": "scan_repo", "repo": repo, "issues": watches_issues, "prs": watches_prs})
    if channel and "slack" in connectors:
        steps.append({"connector": "slack", "operation": "post_message", "channel": channel})
    if not steps:
        steps.append({"connector": "system", "operation": "execute_prompt", "summary": prompt})

    description = prompt.strip().split("\n", maxsplit=1)[0][:280]

    return {
        "kind": kind,
        "description": description,
        "repo": repo,
        "channel": channel,
        "watches": {"issues": watches_issues, "prs": watches_prs},
        "steps": steps,
        "schedule_cron": schedule,
        "required_connectors": sorted(connectors),
    }


def _job_id(automation_id: str) -> str:
    return f"automation:{automation_id}"


def upsert_automation_job(automation: Automation) -> None:
    """Create or replace a scheduler job for an automation."""
    job_id = _job_id(automation.id)
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    if not automation.enabled or not automation.schedule_cron:
        return

    trigger = CronTrigger.from_crontab(
        automation.schedule_cron,
        timezone=automation.schedule_timezone or get_settings().scheduler_timezone,
    )
    scheduler.add_job(
        run_automation_job,
        trigger=trigger,
        id=job_id,
        args=[automation.id],
        replace_existing=True,
    )


def remove_automation_job(automation_id: str) -> None:
    try:
        scheduler.remove_job(_job_id(automation_id))
    except Exception:
        pass


async def restore_automation_jobs() -> None:
    """Restore in-memory scheduled jobs for persisted automations on startup."""
    async with AsyncSessionLocal() as session:
        try:
            rows = await session.execute(select(Automation))
        except ProgrammingError as exc:
            await session.rollback()
            error_text = str(getattr(exc, "orig", exc)).lower()
            missing_table_markers = (
                'relation "automations" does not exist',
                'no such table: automations',
                'undefinedtable',
            )
            if any(marker in error_text for marker in missing_table_markers):
                logger.warning(
                    "Automation tables are not available yet; skipping scheduler restoration until migrations are applied"
                )
                return
            raise
        for automation in rows.scalars():
            try:
                upsert_automation_job(automation)
            except Exception as exc:
                logger.warning("Failed to restore automation job %s: %s", automation.id, exc)


async def list_automations_for_user(user_id: str, session: AsyncSession) -> list[Automation]:
    rows = await session.execute(
        select(Automation).where(Automation.user_id == user_id).order_by(Automation.created_at.desc())
    )
    return list(rows.scalars())


async def list_connectors_for_user(user_id: str, session: AsyncSession) -> list[AutomationConnector]:
    rows = await session.execute(
        select(AutomationConnector).where(AutomationConnector.user_id == user_id).order_by(AutomationConnector.provider)
    )
    return list(rows.scalars())


async def build_connector_view(user_id: str, session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    connectors = await list_connectors_for_user(user_id, session)
    connector_map = {c.provider: c for c in connectors}
    available: list[dict[str, Any]] = []
    connected: list[dict[str, Any]] = []
    for provider in CONNECTOR_CATALOG:
        saved = connector_map.get(provider["provider"])
        logo_domain = provider.get("domain")
        row = {
            "provider": provider["provider"],
            "display_name": provider["display_name"],
            "category": provider["category"],
            "logo_domain": logo_domain,
            "logo_url": build_connector_logo_url(logo_domain),
            "oauth_supported": provider["oauth_supported"],
            "coming_soon": provider["coming_soon"],
            "enabled": saved.enabled if saved else False,
            "connected": saved is not None and saved.connection_status == "connected",
            "connection_status": saved.connection_status if saved else ("available" if provider["oauth_supported"] else "coming_soon"),
            "account_label": saved.account_label if saved else None,
            "scopes": saved.scopes.split(",") if saved and saved.scopes else [],
        }
        available.append(row)
        if saved:
            connected.append(row)
    return {"connected": connected, "available": available}


async def create_automation(
    *,
    user_id: str,
    name: str,
    prompt: str,
    required_connectors_override: list[str] | None,
    schedule_cron: str | None,
    schedule_timezone: str,
    template_key: str | None,
    session: AsyncSession,
) -> Automation:
    inferred = infer_automation_config(
        prompt,
        schedule_cron=schedule_cron,
        template_key=template_key,
        required_connectors_override=required_connectors_override,
    )
    automation = Automation(
        user_id=user_id,
        name=name,
        description=inferred.get("description"),
        prompt=prompt,
        template_key=template_key,
        inferred_config=inferred,
        required_connectors=inferred.get("required_connectors", []),
        schedule_cron=inferred.get("schedule_cron"),
        schedule_timezone=schedule_timezone,
        enabled=True,
        last_status="idle",
    )
    session.add(automation)
    await session.flush()
    upsert_automation_job(automation)
    return automation


async def update_automation(automation: Automation, *, enabled: bool | None, schedule_cron: str | None, schedule_timezone: str | None) -> Automation:
    if enabled is not None:
        automation.enabled = enabled
    if schedule_timezone is not None:
        automation.schedule_timezone = schedule_timezone
    if schedule_cron is not None:
        automation.schedule_cron = _normalize_schedule(schedule_cron, automation.prompt)
    automation.updated_at = _now()
    if automation.enabled:
        automation.last_status = automation.last_status or "idle"
        upsert_automation_job(automation)
    else:
        automation.last_status = "paused"
        remove_automation_job(automation.id)
    return automation


async def delete_automation(automation: Automation, session: AsyncSession) -> None:
    remove_automation_job(automation.id)
    await session.delete(automation)


def _get_provider_meta(provider: str) -> dict[str, Any]:
    meta = next((item for item in CONNECTOR_CATALOG if item["provider"] == provider), None)
    if not meta:
        raise ValueError(f"Unsupported provider: {provider}")
    return meta


async def build_oauth_url(provider: str, user_id: str, state_token: str) -> str:
    settings = get_settings()
    callback = f"{settings.backend_public_url.rstrip('/')}/automations/oauth/{provider}/callback"
    if provider == "github":
        if not settings.github_oauth_client_id or not settings.github_oauth_client_secret:
            raise ValueError("GitHub OAuth is not configured on the backend")
        params = {
            "client_id": settings.github_oauth_client_id,
            "redirect_uri": callback,
            "scope": "repo read:user user:email",
            "state": state_token,
        }
        return f"https://github.com/login/oauth/authorize?{urlencode(params)}"

    if provider == "slack":
        if not settings.slack_oauth_client_id or not settings.slack_oauth_client_secret:
            raise ValueError("Slack OAuth is not configured on the backend")
        params = {
            "client_id": settings.slack_oauth_client_id,
            "redirect_uri": callback,
            "scope": "chat:write,channels:read,groups:read,im:read,mpim:read",
            "state": state_token,
        }
        return f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"

    raise ValueError(f"OAuth is not yet implemented for provider '{provider}'")


async def exchange_oauth_code(provider: str, code: str) -> dict[str, Any]:
    settings = get_settings()
    callback = f"{settings.backend_public_url.rstrip('/')}/automations/oauth/{provider}/callback"
    async with httpx.AsyncClient(timeout=30.0) as client:
        if provider == "github":
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": callback,
                },
            )
            response.raise_for_status()
            token_data = response.json()
            access_token = token_data.get("access_token")
            if not access_token:
                raise ValueError("GitHub did not return an access token")
            user_response = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
            user_response.raise_for_status()
            user_data = user_response.json()
            return {
                "access_token": access_token,
                "refresh_token": None,
                "expires_at": None,
                "external_account_id": str(user_data.get("id")),
                "account_label": user_data.get("login") or user_data.get("name"),
                "scopes": token_data.get("scope"),
            }

        if provider == "slack":
            response = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={
                    "client_id": settings.slack_oauth_client_id,
                    "client_secret": settings.slack_oauth_client_secret,
                    "code": code,
                    "redirect_uri": callback,
                },
            )
            response.raise_for_status()
            token_data = response.json()
            if not token_data.get("ok"):
                raise ValueError(token_data.get("error", "Slack OAuth failed"))
            return {
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_at": None,
                "external_account_id": token_data.get("team", {}).get("id"),
                "account_label": token_data.get("team", {}).get("name"),
                "scopes": token_data.get("scope"),
            }

    raise ValueError(f"OAuth exchange not implemented for provider '{provider}'")


async def upsert_connector(
    *,
    user_id: str,
    provider: str,
    token_payload: dict[str, Any],
    session: AsyncSession,
) -> AutomationConnector:
    row = await session.execute(
        select(AutomationConnector).where(
            AutomationConnector.user_id == user_id,
            AutomationConnector.provider == provider,
        )
    )
    connector = row.scalar_one_or_none()
    if connector is None:
        connector = AutomationConnector(user_id=user_id, provider=provider)
        session.add(connector)

    connector.enabled = True
    connector.connection_status = "connected"
    connector.account_label = token_payload.get("account_label")
    connector.external_account_id = token_payload.get("external_account_id")
    connector.scopes = token_payload.get("scopes")
    connector.access_token_encrypted = encrypt_secret(token_payload.get("access_token"))
    connector.refresh_token_encrypted = encrypt_secret(token_payload.get("refresh_token"))
    connector.expires_at = token_payload.get("expires_at")
    connector.updated_at = _now()
    return connector


async def toggle_connector(connector: AutomationConnector, enabled: bool) -> AutomationConnector:
    connector.enabled = enabled
    connector.connection_status = "connected" if enabled else "disabled"
    connector.updated_at = _now()
    return connector


async def run_automation_job(automation_id: str, trigger_type: str = "cron") -> None:
    """Execute an automation job and write an execution log."""
    async with AsyncSessionLocal() as session:
        automation = await session.get(Automation, automation_id)
        if automation is None or not automation.enabled:
            return

        log = AutomationLog(
            automation_id=automation.id,
            user_id=automation.user_id,
            trigger_type=trigger_type,
            status="running",
            summary=f"Started automation {automation.name}",
            details={"steps": []},
        )
        session.add(log)
        await session.flush()

        try:
            details = await _execute_automation(automation, session)
            log.status = "success"
            log.summary = details.get("summary") or f"Automation {automation.name} completed successfully"
            log.details = details
            log.completed_at = _now()
            automation.last_status = "success"
            automation.last_error = None
            automation.last_run_at = log.completed_at
        except Exception as exc:
            log.status = "failed"
            log.summary = str(exc)
            log.details = {"error": str(exc)}
            log.completed_at = _now()
            automation.last_status = "failed"
            automation.last_error = str(exc)
            automation.last_run_at = log.completed_at
            logger.exception("Automation %s failed", automation.id)


async def _execute_automation(automation: Automation, session: AsyncSession) -> dict[str, Any]:
    config = automation.inferred_config or {}
    required = set(config.get("required_connectors", automation.required_connectors or []))
    rows = await session.execute(
        select(AutomationConnector).where(AutomationConnector.user_id == automation.user_id)
    )
    connectors = {row.provider: row for row in rows.scalars()}
    missing = [provider for provider in required if provider not in connectors or not connectors[provider].enabled]
    if missing:
        raise ValueError(f"Missing connected OAuth providers: {', '.join(sorted(missing))}")

    kind = config.get("kind")
    if kind == "github_repo_to_slack":
        return await _run_github_repo_to_slack(automation, connectors)

    return {
        "summary": f"Automation '{automation.name}' executed with no connector-specific action.",
        "steps": config.get("steps", []),
        "kind": kind or "generic",
    }


async def _run_github_repo_to_slack(
    automation: Automation,
    connectors: dict[str, AutomationConnector],
) -> dict[str, Any]:
    config = automation.inferred_config or {}
    repo = config.get("repo")
    channel = config.get("channel") or "#general"
    if not repo:
        raise ValueError("GitHub repository could not be inferred. Please edit the automation prompt to include owner/repo.")

    github_token = decrypt_secret(connectors["github"].access_token_encrypted)
    slack_token = decrypt_secret(connectors["slack"].access_token_encrypted)
    if not github_token or not slack_token:
        raise ValueError("Connected OAuth tokens are unavailable for GitHub or Slack")

    async with httpx.AsyncClient(timeout=30.0) as client:
        issues_response = await client.get(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"state": "open", "sort": "updated", "per_page": 10},
        )
        issues_response.raise_for_status()
        issues = issues_response.json()

        pr_count = sum(1 for item in issues if item.get("pull_request"))
        issue_count = sum(1 for item in issues if not item.get("pull_request"))
        top_items = issues[:5]
        bullet_lines = [
            f"• {item.get('title', 'Untitled')} — {item.get('html_url')}"
            for item in top_items
        ]
        message = (
            f"Pakalon automation update for *{repo}*\n"
            f"Open issues: {issue_count} | Open PRs: {pr_count}\n"
            f"{chr(10).join(bullet_lines) if bullet_lines else 'No open issues or PRs found.'}"
        )

        channel_id = await _resolve_slack_channel_id(client, slack_token, channel)
        slack_response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {slack_token}", "Content-Type": "application/json; charset=utf-8"},
            json={"channel": channel_id, "text": message},
        )
        slack_response.raise_for_status()
        slack_payload = slack_response.json()
        if not slack_payload.get("ok"):
            raise ValueError(slack_payload.get("error", "Slack chat.postMessage failed"))

    return {
        "summary": f"Posted GitHub repo summary for {repo} to Slack {channel}",
        "repo": repo,
        "channel": channel,
        "issue_count": issue_count,
        "pr_count": pr_count,
        "steps": [
            {"provider": "github", "operation": "list_issues", "count": len(issues)},
            {"provider": "slack", "operation": "post_message", "channel": channel},
        ],
    }


async def _resolve_slack_channel_id(client: httpx.AsyncClient, slack_token: str, channel_name: str) -> str:
    if not channel_name.startswith("#"):
        return channel_name
    response = await client.get(
        "https://slack.com/api/conversations.list",
        headers={"Authorization": f"Bearer {slack_token}"},
        params={"exclude_archived": "true", "limit": 1000},
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise ValueError(payload.get("error", "Slack conversations.list failed"))
    target = channel_name.lstrip("#").lower()
    for channel in payload.get("channels", []):
        if channel.get("name", "").lower() == target:
            return channel.get("id")
    raise ValueError(f"Slack channel not found: {channel_name}")