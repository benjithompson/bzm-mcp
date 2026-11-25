"""
Copyright 2025 Perforce Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import asyncio

import pytest

from config.auth import HttpAuthProvider
from config.cache import InMemoryTTLCache
from config.runtime import build_runtime
from config.session import InMemorySessionProvider, SessionError, SessionErrorCode
from config.storage import HttpSessionStorageProvider, InMemorySessionStorageProvider

STORAGE_ENV = (
    "BZM_STORAGE_API_BASE_URL",
    "BZM_MCP_STORAGE_CALLER_TOKEN",
    "BZM_MCP_TICKET_STORAGE_CALLER_TOKEN",
    "BZM_MCP_UPLOAD_PUBLIC_BASE_URL",
)


@pytest.fixture
def no_storage_service(monkeypatch):
    for name in STORAGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("BZM_MCP_CACHE_ENABLED", raising=False)


class TestHttpMemoryBackend:
    def test_memory_backend_needs_no_storage_service(self, monkeypatch, no_storage_service):
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", "memory")

        runtime = build_runtime("streamable-http")

        assert runtime.transport == "streamable-http"
        assert isinstance(runtime.auth, HttpAuthProvider)
        assert isinstance(runtime.storage, InMemorySessionStorageProvider)
        assert isinstance(runtime.sessions, InMemorySessionProvider)
        assert isinstance(runtime.cache, InMemoryTTLCache)
        assert runtime.file_access is None
        assert runtime.tickets is None
        assert runtime.user_config == {}

    def test_backend_value_is_case_insensitive(self, monkeypatch, no_storage_service):
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", " Memory ")

        runtime = build_runtime("streamable-http")

        assert isinstance(runtime.storage, InMemorySessionStorageProvider)

    def test_purged_session_releases_its_partition(self, monkeypatch, no_storage_service):
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", "memory")

        runtime = build_runtime("streamable-http")

        assert runtime.sessions._on_purge == runtime.storage.discard_session

    def test_sessions_are_isolated_per_user_and_credential(self, monkeypatch, no_storage_service):
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", "memory")
        runtime = build_runtime("streamable-http")

        async def scenario():
            session = await runtime.sessions.open("user-a", "Basic a")
            assert (await runtime.sessions.touch(session.session_id, "user-a", "Basic a")).owner_id == "user-a"
            for owner, credential in (("user-b", "Basic a"), ("user-a", "Basic other")):
                with pytest.raises(SessionError) as excinfo:
                    await runtime.sessions.touch(session.session_id, owner, credential)
                assert excinfo.value.code is SessionErrorCode.INVALID

        asyncio.run(scenario())

    def test_default_backend_still_requires_storage_service(self, monkeypatch, no_storage_service):
        monkeypatch.delenv("BZM_MCP_STORAGE_BACKEND", raising=False)

        with pytest.raises(ValueError) as excinfo:
            build_runtime("streamable-http")

        message = str(excinfo.value)
        assert "BZM_STORAGE_API_BASE_URL" in message
        assert "BZM_MCP_STORAGE_BACKEND=memory" in message

    def test_api_backend_uses_storage_service(self, monkeypatch, no_storage_service):
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", "api")
        monkeypatch.setenv("BZM_STORAGE_API_BASE_URL", "https://mcp-storage.internal")
        monkeypatch.setenv("BZM_MCP_TICKET_STORAGE_CALLER_TOKEN", "caller-secret")
        monkeypatch.setenv("BZM_MCP_UPLOAD_PUBLIC_BASE_URL", "http://127.0.0.1:8090")
        monkeypatch.setattr(HttpSessionStorageProvider, "ensure_available", lambda self: None)

        runtime = build_runtime("streamable-http")

        assert isinstance(runtime.storage, HttpSessionStorageProvider)
        assert runtime.tickets is not None

    def test_unknown_backend_is_rejected(self, monkeypatch, no_storage_service):
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", "redis")

        with pytest.raises(ValueError) as excinfo:
            build_runtime("streamable-http")

        assert "BZM_MCP_STORAGE_BACKEND" in str(excinfo.value)

    def test_stdio_ignores_backend_setting(self, monkeypatch, no_storage_service):
        monkeypatch.delenv("MCP_DOCKER", raising=False)
        monkeypatch.setenv("BZM_MCP_STORAGE_BACKEND", "redis")

        runtime = build_runtime("stdio")

        assert isinstance(runtime.storage, InMemorySessionStorageProvider)
