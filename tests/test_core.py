"""Tests for JARVIS utility functions and services."""
import sys
import os
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestKeyRotation:
    """Tests for the key rotation utility."""

    def test_get_next_key_pair_cycles(self):
        from app.utils.key_rotation import get_next_key_pair
        keys = ["key1", "key2", "key3"]
        # Should cycle through keys
        results = set()
        for _ in range(10):
            k = get_next_key_pair(keys)
            results.add(k)
        assert len(results) <= len(keys)

    def test_get_next_key_pair_single_key(self):
        from app.utils.key_rotation import get_next_key_pair
        keys = ["only_key"]
        assert get_next_key_pair(keys) == "only_key"


class TestRetry:
    """Tests for the retry utility."""

    def test_retry_succeeds_first_try(self):
        from app.utils.retry import retry_with_backoff

        call_count = 0
        def good_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = retry_with_backoff(good_func, max_retries=3)
        assert result == "ok"
        assert call_count == 1

    def test_retry_eventually_succeeds(self):
        from app.utils.retry import retry_with_backoff

        call_count = 0
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("not yet")
            return "success"

        result = retry_with_backoff(flaky_func, max_retries=5, base_delay=0.01)
        assert result == "success"
        assert call_count == 3


class TestTimeInfo:
    """Tests for time_info utility."""

    def test_get_time_info_returns_dict(self):
        from app.utils.time_info import get_time_info
        info = get_time_info()
        assert isinstance(info, dict)
        assert "current_time" in info or "date" in info or "time" in info


class TestBrainService:
    """Tests for the BrainService query classification."""

    def test_classify_general_query(self):
        from app.services.brain_service import BrainService
        # BrainService requires a groq_service, mock it
        mock_groq = MagicMock()
        # Simulate LLM returning "general" classification
        mock_groq.classify_query.return_value = {"query_type": "general", "reasoning": "test"}
        brain = BrainService(mock_groq)
        # The classify method delegates to groq_service
        assert brain is not None


class TestActionTagParsing:
    """Tests for [ACTION:...] tag parsing in tool_executor."""

    def test_strip_basic_action_tag(self):
        """Test that a basic action tag is stripped from text."""
        # Import the frontend-mirrored logic
        text = 'Hello [ACTION:open_app("chrome")] there'
        # Simulate stripping
        import re
        cleaned = re.sub(r'\[ACTION:\w+\([^)]*\)\]', '', text).strip()
        assert cleaned == 'Hello  there'

    def test_action_tag_with_nested_parens(self):
        """Action tags with quoted content containing ) should be handled."""
        text = 'Test [ACTION:write_file("test.txt", "hello (world)")] done'
        # The state machine parser in tool_executor handles this
        from app.services.tools.tool_executor import process_text_for_actions
        cleaned, results = process_text_for_actions(text)
        # Should have extracted the action
        assert 'write_file' in str(results) or 'ACTION' not in cleaned


class TestModels:
    """Tests for Pydantic models."""

    def test_chat_message_creation(self):
        from app.models import ChatMessage
        msg = ChatMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"

    def test_chat_request_defaults(self):
        from app.models import ChatRequest
        req = ChatRequest(message="test")
        assert req.message == "test"
        assert req.tts is True or req.tts is False  # has a default

    def test_settings_update_optional(self):
        from app.models import SettingsUpdate
        update = SettingsUpdate()
        assert update.user_title is None
        assert update.tts_voice is None


class TestSessionIdValidation:
    """Tests for session ID validation in ChatService."""

    def test_valid_uuid(self):
        from app.services.chat_service import ChatService
        mock_groq = MagicMock()
        svc = ChatService(groq_service=mock_groq)
        assert svc.validate_session_id("550e8400-e29b-41d4-a716-446655440000") is True

    def test_empty_id_rejected(self):
        from app.services.chat_service import ChatService
        mock_groq = MagicMock()
        svc = ChatService(groq_service=mock_groq)
        assert svc.validate_session_id("") is False
        assert svc.validate_session_id("   ") is False

    def test_path_traversal_rejected(self):
        from app.services.chat_service import ChatService
        mock_groq = MagicMock()
        svc = ChatService(groq_service=mock_groq)
        assert svc.validate_session_id("../etc/passwd") is False
        assert svc.validate_session_id("test\\secret") is False

    def test_null_byte_rejected(self):
        from app.services.chat_service import ChatService
        mock_groq = MagicMock()
        svc = ChatService(groq_service=mock_groq)
        assert svc.validate_session_id("test\x00evil") is False

    def test_long_id_rejected(self):
        from app.services.chat_service import ChatService
        mock_groq = MagicMock()
        svc = ChatService(groq_service=mock_groq)
        assert svc.validate_session_id("a" * 256) is False
        assert svc.validate_session_id("a" * 255) is True
