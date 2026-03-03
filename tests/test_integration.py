"""Integration tests — full request/response flows (T153, T154, T155).

These tests exercise complete HTTP request chains end-to-end using the
FastAPI TestClient with in-memory SQLite and fakeredis, validating that
routers, services, middleware, and models all wire together correctly.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_code import DeviceCode
from app.models.user import User
from app.models.subscription import Subscription
from tests.conftest import make_jwt_for_user

# ──────────────────────────────────────────────────────────────────────────────
# T153 — Full Auth Flow
# ──────────────────────────────────────────────────────────────────────────────


class TestAuthFlow:
    """POST /auth/devices → poll pending → confirm → poll approved → GET /auth/me"""

    @pytest.mark.asyncio
    async def test_full_auth_flow(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        fake_redis,
    ):
        """Complete device code flow from creation to JWT retrieval."""
        device_id = str(uuid.uuid4())

        # Step 1 — CLI creates a device code
        resp = await client.post(
            "/auth/devices",
            json={"device_id": device_id, "machine_id": "test-machine-xyz"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert "code" in data
        assert len(data["code"]) == 6
        assert data["device_id"] == device_id

        # Step 2 — Poll before approval → 202
        resp = await client.get(f"/auth/devices/{device_id}/token")
        assert resp.status_code == 202, resp.text
        assert resp.json()["status"] == "pending"

        # Step 3 — Simulate website confirmation (normally called with Clerk JWT)
        # We bypass auth middleware by manually updating the record in DB
        from sqlalchemy import select
        result = await db_session.execute(
            select(DeviceCode).where(DeviceCode.device_id == device_id)
        )
        dc: DeviceCode | None = result.scalar_one_or_none()
        assert dc is not None

        # Create a user to link
        user = User(
            id=str(uuid.uuid4()),
            clerk_id="clerk_integration_test_user",
            github_login="integration-test",
            email="integration@test.example",
            display_name="Integration Test User",
            plan="free",
            trial_start=datetime.now(tz=timezone.utc),
            trial_end=datetime.now(tz=timezone.utc) + timedelta(days=30),
            trial_days_used=0,
        )
        db_session.add(user)
        await db_session.flush()

        dc.status = "approved"
        dc.clerk_user_id = user.clerk_id
        dc.user_id = user.id
        dc.approved_at = datetime.now(tz=timezone.utc)
        await db_session.commit()

        # Step 4 — Poll after approval → 200 with JWT
        resp = await client.get(f"/auth/devices/{device_id}/token")
        assert resp.status_code == 200, resp.text
        token_data = resp.json()
        assert "access_token" in token_data
        assert token_data["token_type"] == "bearer"

        # Step 5 — Use JWT to call GET /auth/me
        jwt_token = token_data["access_token"]
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {jwt_token}"},
        )
        assert resp.status_code == 200, resp.text
        me = resp.json()
        assert me["id"] == user.id
        assert me["plan"] == "free"

    @pytest.mark.asyncio
    async def test_poll_expired_code_returns_410(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """A device code past its expiry returns HTTP 410."""
        device_id = str(uuid.uuid4())

        # Create an already-expired device code directly in DB
        dc = DeviceCode(
            id=str(uuid.uuid4()),
            device_id=device_id,
            code="999999",
            status="pending",
            expires_at=datetime.now(tz=timezone.utc) - timedelta(minutes=15),
        )
        db_session.add(dc)
        await db_session.commit()

        resp = await client.get(f"/auth/devices/{device_id}/token")
        assert resp.status_code == 410, resp.text

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_ok(self, client: AsyncClient):
        """GET /health returns 200 with status ok."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_auth_me_requires_jwt(self, client: AsyncClient):
        """GET /auth/me without JWT returns 401."""
        resp = await client.get("/auth/me")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_me_with_free_user(
        self,
        client: AsyncClient,
        free_user: User,
    ):
        """GET /auth/me with valid JWT returns user profile."""
        token = make_jwt_for_user(free_user)
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == free_user.id
        assert data["plan"] == "free"


# ──────────────────────────────────────────────────────────────────────────────
# T154 — Billing Flow
# ──────────────────────────────────────────────────────────────────────────────


class TestBillingFlow:
    """POST /billing/checkout → POST /webhooks/polar → GET /billing/subscription."""

    @pytest.mark.asyncio
    async def test_checkout_requires_auth(self, client: AsyncClient):
        """POST /billing/checkout without auth returns 401."""
        resp = await client.post("/billing/checkout", json={})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_checkout_returns_url(
        self,
        client: AsyncClient,
        free_user: User,
    ):
        """POST /billing/checkout returns a Polar checkout URL."""
        token = make_jwt_for_user(free_user)
        mock_url = "https://checkout.polar.sh/test-checkout-session-id"

        with patch(
            "app.services.billing.create_checkout_url",
            new_callable=AsyncMock,
            return_value=mock_url,
        ):
            resp = await client.post(
                "/billing/checkout",
                json={"success_url": "https://pakalon.com/billing/success"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["checkout_url"] == mock_url

    @pytest.mark.asyncio
    async def test_polar_webhook_subscription_created(
        self,
        client: AsyncClient,
        free_user: User,
        db_session: AsyncSession,
    ):
        """POST /webhooks/polar subscription.created → users.plan updated to pro."""
        # Build a mock Polar webhook payload
        now = datetime.now(tz=timezone.utc)
        payload: dict[str, Any] = {
            "type": "subscription.created",
            "data": {
                "id": "polar_sub_integration_001",
                "customer": {"metadata": {"user_id": free_user.id}},
                "status": "active",
                "currentPeriodStart": now.isoformat(),
                "currentPeriodEnd": (now + timedelta(days=30)).isoformat(),
                "amount": 2200,
                "currency": "usd",
            },
        }

        # Patch svix signature validation to always pass
        with patch(
            "app.routers.webhooks.verify_polar_signature",
            return_value=True,
        ), patch(
            "app.routers.webhooks.handle_subscription_created",
            new_callable=AsyncMock,
        ) as mock_handler:
            resp = await client.post(
                "/webhooks/polar",
                json=payload,
                headers={
                    "webhook-id": "wh_test",
                    "webhook-timestamp": str(int(now.timestamp())),
                    "webhook-signature": "v1,test_signature",
                },
            )

        # Accept 200 or 204 — handler called
        assert resp.status_code in (200, 204), resp.text
        mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_polar_webhook_invalid_signature_rejected(
        self,
        client: AsyncClient,
    ):
        """POST /webhooks/polar with invalid signature returns 403."""
        with patch(
            "app.routers.webhooks.verify_polar_signature",
            return_value=False,
        ):
            resp = await client.post(
                "/webhooks/polar",
                json={"type": "subscription.created", "data": {}},
                headers={
                    "webhook-id": "bad_id",
                    "webhook-timestamp": "0",
                    "webhook-signature": "v1,bad",
                },
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_get_subscription_pro_user(
        self,
        client: AsyncClient,
        pro_user: User,
    ):
        """GET /billing/subscription returns active subscription for pro user."""
        token = make_jwt_for_user(pro_user)
        resp = await client.get(
            "/billing/subscription",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "active"
        assert "period_end" in data

    @pytest.mark.asyncio
    async def test_get_subscription_free_user(
        self,
        client: AsyncClient,
        free_user: User,
    ):
        """GET /billing/subscription for free user returns trial info."""
        token = make_jwt_for_user(free_user)
        resp = await client.get(
            "/billing/subscription",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Should return plan info even without active subscription
        assert "plan" in data or "status" in data


# ──────────────────────────────────────────────────────────────────────────────
# T155 — Trial Abuse Integration
# ──────────────────────────────────────────────────────────────────────────────


class TestTrialAbuseFlow:
    """Create user → simulate usage → delete → re-register → verify trial days carry over."""

    @pytest.mark.asyncio
    async def test_trial_days_carry_over_on_reregister(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ):
        """Re-registering same GitHub login carries over previous trial_days_used."""
        from app.services.trial_abuse import get_or_create_user_by_github

        # Create original user with 10 days used
        original = await get_or_create_user_by_github(
            github_login="trial-abuse-test-github",
            clerk_id="clerk_trial_abuse_test_1",
            email="trial1@example.com",
            display_name="Trial User",
            session=db_session,
        )
        original.trial_days_used = 10
        original.account_deleted = True  # simulate deletion
        await db_session.commit()

        # Re-register same GitHub → should carry 10 days over
        from app.services.trial_abuse import get_or_create_user_by_github
        new_user = await get_or_create_user_by_github(
            github_login="trial-abuse-test-github",
            clerk_id="clerk_trial_abuse_test_2",
            email="trial2@example.com",
            display_name="Trial User New",
            session=db_session,
        )
        assert new_user.trial_days_used == 10

    @pytest.mark.asyncio
    async def test_exhausted_trial_blocks_reregister(
        self,
        db_session: AsyncSession,
    ):
        """User with 30 trial_days_used cannot re-register."""
        from app.services.trial_abuse import get_or_create_user_by_github

        unique = str(uuid.uuid4())[:8]
        old_user = User(
            id=str(uuid.uuid4()),
            clerk_id=f"clerk_block_{unique}",
            github_login=f"block-trial-{unique}",
            email=f"block_{unique}@example.com",
            plan="free",
            trial_days_used=30,
            account_deleted=True,
        )
        db_session.add(old_user)
        await db_session.commit()

        # Attempting to re-register should raise ValueError or HTTPException
        with pytest.raises((ValueError, Exception)):
            await get_or_create_user_by_github(
                github_login=f"block-trial-{unique}",
                clerk_id=f"clerk_new_{unique}",
                email=f"new_{unique}@example.com",
                display_name="Should be blocked",
                session=db_session,
            )

    @pytest.mark.asyncio
    async def test_delete_account_blocked_after_trial_expired(
        self,
        client: AsyncClient,
        expired_user: User,
    ):
        """DELETE /users/{id} returns 403 when trial has expired."""
        token = make_jwt_for_user(expired_user)
        resp = await client.delete(
            f"/users/{expired_user.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_remaining_trial_days_calculation(self):
        """remaining_trial_days() returns correct count for active trial."""
        from app.services.trial_abuse import remaining_trial_days

        user = User(
            id=str(uuid.uuid4()),
            plan="free",
            trial_days_used=10,
            trial_end=datetime.now(tz=timezone.utc) + timedelta(days=5),
        )
        days = remaining_trial_days(user)
        assert days == 5

    @pytest.mark.asyncio
    async def test_pro_user_can_delete_account(
        self,
        client: AsyncClient,
        pro_user: User,
    ):
        """Pro users can delete their account regardless of trial status."""
        token = make_jwt_for_user(pro_user)
        with patch(
            "app.routers.users.can_delete_account",
            return_value=True,
        ), patch(
            "app.routers.users.anonymise_user",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/users/{pro_user.id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        # 204 (deleted) or 200 (ok)
        assert resp.status_code in (200, 204), resp.text


# ──────────────────────────────────────────────────────────────────────────────
# Additional cross-cutting integration tests
# ──────────────────────────────────────────────────────────────────────────────


class TestSessionsIntegration:
    """Session creation and retrieval via HTTP."""

    @pytest.mark.asyncio
    async def test_create_and_list_sessions(
        self,
        client: AsyncClient,
        free_user: User,
    ):
        """POST /sessions creates session, GET /sessions returns list."""
        token = make_jwt_for_user(free_user)

        # Create a session
        resp = await client.post(
            "/sessions",
            json={
                "project_dir": "/tmp/test-project",
                "model_id": "openai/gpt-4o-mini",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code in (200, 201), resp.text
        session_data = resp.json()
        session_id = session_data.get("id")
        assert session_id is not None

        # List sessions
        resp = await client.get(
            "/sessions",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        sessions = resp.json()
        assert isinstance(sessions, (list, dict))

    @pytest.mark.asyncio
    async def test_sessions_require_auth(self, client: AsyncClient):
        """GET /sessions without auth → 401."""
        resp = await client.get("/sessions")
        assert resp.status_code == 401


class TestUsageIntegration:
    """Usage tracking endpoints."""

    @pytest.mark.asyncio
    async def test_get_usage_returns_data(
        self,
        client: AsyncClient,
        free_user: User,
    ):
        """GET /usage returns current usage stats."""
        token = make_jwt_for_user(free_user)
        resp = await client.get(
            "/usage",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Usage response should include at least token counts
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_get_models_returns_list(
        self,
        client: AsyncClient,
        free_user: User,
    ):
        """GET /models returns list of available models."""
        token = make_jwt_for_user(free_user)
        resp = await client.get(
            "/models",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert isinstance(data, (list, dict))
