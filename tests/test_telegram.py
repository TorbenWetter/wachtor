"""Tests for agentpass.messenger.telegram — TelegramAdapter."""

import asyncio
import contextlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentpass.config import TelegramConfig
from agentpass.messenger.base import (
    ApprovalChoice,
    ApprovalRequest,
    ApprovalResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def telegram_config():
    return TelegramConfig(token="test-token", chat_id=12345, allowed_users=[111, 222])


@pytest.fixture
def mock_app():
    """Create a mock PTB Application with a mock Bot."""
    app = AsyncMock()
    app.bot = AsyncMock()
    app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    app.bot.edit_message_text = AsyncMock()
    app.add_handler = MagicMock()
    return app


@pytest.fixture
def adapter(mock_app, telegram_config, tmp_path):
    """Build a TelegramAdapter with a fully mocked PTB Application."""
    with (
        patch("agentpass.messenger.telegram.Application") as mock_app_cls,
        patch("agentpass.messenger.telegram.PicklePersistence"),
    ):
        mock_builder = MagicMock()
        mock_app_cls.builder.return_value = mock_builder
        mock_builder.token.return_value = mock_builder
        mock_builder.persistence.return_value = mock_builder
        mock_builder.arbitrary_callback_data.return_value = mock_builder
        mock_builder.build.return_value = mock_app

        from agentpass.messenger.telegram import TelegramAdapter

        adp = TelegramAdapter(telegram_config, persistence_path=str(tmp_path / "cb.pickle"))

    return adp


@pytest.fixture
def approval_request():
    return ApprovalRequest(
        request_id="req-1",
        tool_name="ha_call_service",
        args={"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"},
        signature="ha_call_service(light.turn_on, light.kitchen)",
    )


@pytest.fixture
def choices():
    return [
        ApprovalChoice(label="Allow", action="allow"),
        ApprovalChoice(label="Deny", action="deny"),
    ]


# ---------------------------------------------------------------------------
# Test: send_approval
# ---------------------------------------------------------------------------


class TestSendApproval:
    async def test_calls_bot_send_message_with_correct_text_and_keyboard(
        self, adapter, mock_app, approval_request, choices
    ):
        """FR5-AC1: sends formatted message with tool signature and inline buttons."""
        await adapter.send_approval(approval_request, choices)

        mock_app.bot.send_message.assert_awaited_once()
        call_kwargs = mock_app.bot.send_message.call_args.kwargs
        assert call_kwargs["chat_id"] == 12345
        assert "ha_call_service" in call_kwargs["text"]
        assert "ha_call_service(light.turn_on, light.kitchen)" in call_kwargs["text"]

        # Verify inline keyboard markup
        markup = call_kwargs["reply_markup"]
        # InlineKeyboardMarkup is constructed in the adapter; we check its structure
        assert markup is not None

    async def test_returns_message_id_as_string(self, adapter, mock_app, approval_request, choices):
        """send_approval returns message_id as string."""
        msg_id = await adapter.send_approval(approval_request, choices)
        assert msg_id == "42"
        assert isinstance(msg_id, str)


# ---------------------------------------------------------------------------
# Test: update_approval
# ---------------------------------------------------------------------------


class TestUpdateApproval:
    async def test_calls_edit_message_text_with_correct_params(self, adapter, mock_app):
        """update_approval edits the Telegram message."""
        await adapter.update_approval("42", "Approved", "Approved by @alice at 14:30")

        mock_app.bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_app.bot.edit_message_text.call_args.kwargs
        assert call_kwargs["chat_id"] == 12345
        assert call_kwargs["message_id"] == 42
        assert "Approved" in call_kwargs["text"]
        assert "Approved by @alice at 14:30" in call_kwargs["text"]

    async def test_logs_warning_on_failure(self, adapter, mock_app, caplog):
        """update_approval logs warning on exception, never raises."""
        mock_app.bot.edit_message_text.side_effect = Exception("Telegram API error")

        with caplog.at_level(logging.WARNING):
            await adapter.update_approval("42", "Approved", "detail")

        assert "Failed to edit Telegram message" in caplog.text


# ---------------------------------------------------------------------------
# Test: on_approval_callback
# ---------------------------------------------------------------------------


class TestOnApprovalCallback:
    async def test_stores_callback(self, adapter):
        """on_approval_callback stores the callback for later invocation."""

        async def my_callback(result: ApprovalResult) -> None:
            pass

        await adapter.on_approval_callback(my_callback)
        assert adapter._callback is my_callback


# ---------------------------------------------------------------------------
# Test: _handle_callback (user presses inline button)
# ---------------------------------------------------------------------------


class TestHandleCallback:
    def _make_update(self, user_id: int, username: str | None, request_id: str, action: str):
        """Create a mock Update with a callback_query for testing."""
        update = MagicMock()
        query = AsyncMock()
        query.from_user = MagicMock()
        query.from_user.id = user_id
        query.from_user.username = username
        query.data = {"request_id": request_id, "action": action}
        query.message = MagicMock()
        query.message.message_id = 99
        query.message.text = (
            "\U0001f6a8 ha_call_service\nha_call_service(light.turn_on, light.kitchen)"
        )
        query.answer = AsyncMock()
        update.callback_query = query
        return update

    async def test_allowed_user_triggers_callback(self, adapter):
        """FR5-AC2: allowed user triggers the registered callback with correct ApprovalResult."""
        results = []

        async def cb(result: ApprovalResult) -> None:
            results.append(result)

        await adapter.on_approval_callback(cb)

        update = self._make_update(111, "alice", "req-1", "allow")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        assert len(results) == 1
        assert results[0].request_id == "req-1"
        assert results[0].action == "allow"
        assert results[0].user_id == "111"
        assert isinstance(results[0].timestamp, float)

    async def test_non_allowed_user_silently_ignored(self, adapter):
        """FR5-AC2: non-allowed user is silently ignored, callback NOT triggered."""
        results = []

        async def cb(result: ApprovalResult) -> None:
            results.append(result)

        await adapter.on_approval_callback(cb)

        update = self._make_update(999, "hacker", "req-1", "allow")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        assert len(results) == 0
        update.callback_query.answer.assert_not_awaited()

    async def test_cancels_timeout_task(self, adapter):
        """_handle_callback cancels the timeout task for the resolved request_id."""
        await adapter.on_approval_callback(AsyncMock())

        mock_task = MagicMock()
        mock_task.cancel = MagicMock()
        adapter._pending["req-1"] = mock_task

        update = self._make_update(111, "alice", "req-1", "allow")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        mock_task.cancel.assert_called_once()
        assert "req-1" not in adapter._pending

    async def test_edits_message_after_allow(self, adapter, mock_app):
        """FR5-AC5: edits message to compact 'Approved' with tool name."""
        await adapter.on_approval_callback(AsyncMock())

        update = self._make_update(111, "alice", "req-2", "allow")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        mock_app.bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_app.bot.edit_message_text.call_args.kwargs
        assert "Approved" in call_kwargs["text"]
        assert "ha_call_service" in call_kwargs["text"]

    async def test_edits_message_after_deny(self, adapter, mock_app):
        """FR5-AC5: edits message to compact 'Denied' with tool name."""
        await adapter.on_approval_callback(AsyncMock())

        update = self._make_update(222, "bob", "req-3", "deny")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        mock_app.bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_app.bot.edit_message_text.call_args.kwargs
        assert "Denied" in call_kwargs["text"]
        assert "ha_call_service" in call_kwargs["text"]

    async def test_resolved_message_includes_tool_name(self, adapter, mock_app):
        """Resolved message includes the tool name from the original message."""
        await adapter.on_approval_callback(AsyncMock())

        update = self._make_update(111, None, "req-4", "allow")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        call_kwargs = mock_app.bot.edit_message_text.call_args.kwargs
        assert "ha_call_service" in call_kwargs["text"]

    async def test_answers_callback_query(self, adapter):
        """Handler calls query.answer() for allowed users."""
        await adapter.on_approval_callback(AsyncMock())

        update = self._make_update(111, "alice", "req-5", "allow")
        context = MagicMock()

        await adapter._handle_callback(update, context)

        update.callback_query.answer.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test: _handle_invalid_callback (stale button)
# ---------------------------------------------------------------------------


class TestHandleInvalidCallback:
    async def test_answers_with_expired_message(self, adapter):
        """FR5-AC4: InvalidCallbackData answers with 'expired' message."""
        update = MagicMock()
        query = AsyncMock()
        query.answer = AsyncMock()
        update.callback_query = query

        await adapter._handle_invalid_callback(update, MagicMock())

        query.answer.assert_awaited_once_with("This button has expired")


# ---------------------------------------------------------------------------
# Test: schedule_timeout and _timeout_handler
# ---------------------------------------------------------------------------


class TestTimeout:
    async def test_timeout_fires_and_calls_callback_with_deny(self, adapter, mock_app):
        """FR6-AC1/AC2: timeout resolves as deny with user_id='timeout'."""
        results = []

        async def cb(result: ApprovalResult) -> None:
            results.append(result)

        await adapter.on_approval_callback(cb)

        # Schedule a very short timeout
        adapter.schedule_timeout("req-t1", 0, "50")

        # Wait for it to fire
        await asyncio.sleep(0.05)

        assert len(results) == 1
        assert results[0].request_id == "req-t1"
        assert results[0].action == "deny"
        assert results[0].user_id == "timeout"

    async def test_timeout_edits_message_to_expired(self, adapter, mock_app):
        """FR6-AC3: timeout does best-effort edit of message to 'Expired'."""
        await adapter.on_approval_callback(AsyncMock())

        adapter.schedule_timeout("req-t2", 0, "51")
        await asyncio.sleep(0.05)

        mock_app.bot.edit_message_text.assert_awaited_once()
        call_kwargs = mock_app.bot.edit_message_text.call_args.kwargs
        assert "Expired" in call_kwargs["text"]
        assert call_kwargs["message_id"] == 51

    async def test_timeout_message_edit_failure_does_not_raise(self, adapter, mock_app, caplog):
        """FR6-AC3: failure to edit message is logged as warning, never blocks."""
        mock_app.bot.edit_message_text.side_effect = Exception("network error")

        results = []

        async def cb(result: ApprovalResult) -> None:
            results.append(result)

        await adapter.on_approval_callback(cb)

        with caplog.at_level(logging.WARNING):
            adapter.schedule_timeout("req-t3", 0, "52")
            await asyncio.sleep(0.05)

        # Callback should still have been called despite edit failure
        assert len(results) == 1
        assert results[0].action == "deny"

    async def test_schedule_timeout_creates_tracked_task(self, adapter):
        """schedule_timeout creates a task tracked in _pending."""
        adapter.schedule_timeout("req-t4", 10, "53")

        assert "req-t4" in adapter._pending
        task = adapter._pending["req-t4"]
        assert isinstance(task, asyncio.Task)

        # Cleanup
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Test: Race-safe resolution (asyncio.Lock)
# ---------------------------------------------------------------------------


class TestRaceSafeResolution:
    async def test_callback_after_timeout_is_noop(self, adapter, mock_app):
        """FR6-AC4: if timeout fires first, subsequent callback is no-op."""
        results = []

        async def cb(result: ApprovalResult) -> None:
            results.append(result)

        await adapter.on_approval_callback(cb)

        # Schedule a very short timeout
        adapter.schedule_timeout("req-race1", 0, "60")
        await asyncio.sleep(0.05)

        # Now simulate a user pressing the button after timeout
        update = MagicMock()
        query = AsyncMock()
        query.from_user = MagicMock()
        query.from_user.id = 111
        query.from_user.username = "alice"
        query.data = {"request_id": "req-race1", "action": "allow"}
        query.message = MagicMock()
        query.message.message_id = 60
        query.answer = AsyncMock()
        update.callback_query = query

        await adapter._handle_callback(update, MagicMock())

        # Only the timeout result should be present
        assert len(results) == 1
        assert results[0].user_id == "timeout"

        # The query should get "Already resolved"
        query.answer.assert_awaited_once_with("Already resolved")

    async def test_timeout_after_callback_is_noop(self, adapter, mock_app):
        """FR6-AC4: if callback fires first, subsequent timeout is no-op."""
        results = []

        async def cb(result: ApprovalResult) -> None:
            results.append(result)

        await adapter.on_approval_callback(cb)

        # Schedule a longer timeout
        adapter.schedule_timeout("req-race2", 5, "61")

        # Simulate user pressing the button before timeout
        update = MagicMock()
        query = AsyncMock()
        query.from_user = MagicMock()
        query.from_user.id = 111
        query.from_user.username = "alice"
        query.data = {"request_id": "req-race2", "action": "allow"}
        query.message = MagicMock()
        query.message.message_id = 61
        query.answer = AsyncMock()
        update.callback_query = query

        await adapter._handle_callback(update, MagicMock())

        # Now fast-forward: cancel the timeout task manually and verify only 1 result
        assert len(results) == 1
        assert results[0].action == "allow"
        assert results[0].user_id == "111"

        # The timeout task should have been cancelled
        assert "req-race2" not in adapter._pending


# ---------------------------------------------------------------------------
# Test: stop()
# ---------------------------------------------------------------------------


class TestStop:
    async def test_cancels_all_pending_timeout_tasks(self, adapter):
        """stop() cancels all pending timeout tasks and clears _pending."""
        task1 = MagicMock()
        task1.cancel = MagicMock()
        task2 = MagicMock()
        task2.cancel = MagicMock()

        adapter._pending["req-s1"] = task1
        adapter._pending["req-s2"] = task2

        await adapter.stop()

        task1.cancel.assert_called_once()
        task2.cancel.assert_called_once()
        assert len(adapter._pending) == 0


# ---------------------------------------------------------------------------
# Test: application property
# ---------------------------------------------------------------------------


class TestApplicationProperty:
    def test_application_returns_ptb_app(self, adapter, mock_app):
        """The application property returns the PTB Application instance."""
        assert adapter.application is mock_app


# ---------------------------------------------------------------------------
# Test: Major 4 — handler registration order + guard
# ---------------------------------------------------------------------------


class TestHandlerRegistrationOrder:
    def test_invalid_callback_handler_registered_before_valid(
        self, mock_app, telegram_config, tmp_path
    ):
        """Major 4: InvalidCallbackData handler registered before valid."""
        from telegram.ext import InvalidCallbackData

        with (
            patch("agentpass.messenger.telegram.Application") as mock_app_cls,
            patch("agentpass.messenger.telegram.PicklePersistence"),
        ):
            mock_builder = MagicMock()
            mock_app_cls.builder.return_value = mock_builder
            mock_builder.token.return_value = mock_builder
            mock_builder.persistence.return_value = mock_builder
            mock_builder.arbitrary_callback_data.return_value = mock_builder
            mock_builder.build.return_value = mock_app

            from agentpass.messenger.telegram import TelegramAdapter

            TelegramAdapter(
                telegram_config,
                persistence_path=str(tmp_path / "cb.pickle"),
            )

        # Check add_handler was called at least twice
        calls = mock_app.add_handler.call_args_list
        assert len(calls) >= 2

        # Find the InvalidCallbackData handler call
        invalid_idx = None
        valid_idx = None
        for i, call in enumerate(calls):
            handler = call[0][0]
            has_pattern = hasattr(handler, "pattern")
            if has_pattern and handler.pattern is InvalidCallbackData:
                invalid_idx = i
            elif not has_pattern or handler.pattern is None:
                valid_idx = i
            else:
                valid_idx = i

        assert invalid_idx is not None, "InvalidCallbackData not registered"
        assert valid_idx is not None, "Valid callback handler not registered"
        assert invalid_idx < valid_idx, "InvalidCallbackData handler must be registered first"


class TestCallbackDataGuard:
    async def test_non_dict_data_does_not_crash(self, adapter):
        """Major 4: _handle_callback guards against non-dict data (e.g. InvalidCallbackData)."""
        await adapter.on_approval_callback(AsyncMock())

        update = MagicMock()
        query = AsyncMock()
        query.from_user = MagicMock()
        query.from_user.id = 111
        query.from_user.username = "alice"
        query.data = "not_a_dict"  # Simulate non-dict callback data
        query.message = MagicMock()
        query.message.message_id = 99
        query.answer = AsyncMock()
        update.callback_query = query

        # Should not raise
        await adapter._handle_callback(update, MagicMock())

        # Should answer with "Invalid callback data"
        query.answer.assert_awaited_once_with("Invalid callback data")
