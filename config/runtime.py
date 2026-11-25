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
from dataclasses import dataclass, field
import logging
import os
from typing import Any, Literal, Optional

from config.auth import (
    AuthPort,
    BZM_USER_CONFIG_STATE_ATTR,
    HttpAuthProvider,
    StdioAuthProvider,
)
from config.cache import CachePort, build_cache_from_env
from config.env import ENV_PREFIX, env_str
from config.file_access import FileAccessPort, build_file_access
from config.identity import BlazeMeterIdentityVerifier, CachedIdentityVerifier, IdentityPort
from config.session import HttpSessionProvider, InMemorySessionProvider, SessionPort
from config.storage import (
    DefaultSessionScopeResolver,
    HttpSessionStorageProvider,
    InMemorySessionStorageProvider,
    SessionScopeResolverPort,
    SessionStoragePort,
)
from config.service_auth import service_caller_token
from config.tickets import TicketPort, build_ticket_client
from config.token import BzmToken
from tools.utils import ConfirmMode

logger = logging.getLogger(__name__)

Transport = Literal["stdio", "streamable-http"]

# Where streamable-http keeps chat sessions, partitions and the method cache.
# "api": the remote Storage Service (BZM_STORAGE_API_BASE_URL). The default.
# "memory": this process only, for a self-hosted single-process server.
STORAGE_BACKEND_ENV = f"{ENV_PREFIX}STORAGE_BACKEND"
STORAGE_BACKENDS = ("api", "memory")


@dataclass(frozen=True)
class AppRuntime:
    """Process-level collaborators shared by tool registrations."""

    transport: Transport
    auth: AuthPort
    storage: SessionStoragePort
    file_access: Optional[FileAccessPort]
    scope_resolver: SessionScopeResolverPort
    user_config: dict[str, Any]
    tickets: Optional[TicketPort] = None
    identity: IdentityPort = field(default_factory=BlazeMeterIdentityVerifier)
    sessions: SessionPort = field(default_factory=InMemorySessionProvider)
    cache: Optional[CachePort] = None

    def resolve_user_config(self, ctx: Any) -> dict[str, Any]:
        user_config = dict(self.user_config)
        user_config.update(_read_ctx_user_config(ctx))
        token = self.auth.get_token(ctx)
        if token is not None:
            user_config["token"] = token
        return user_config

    def configure_context(self, ctx: Any) -> dict[str, Any]:
        user_config = self.resolve_user_config(ctx)
        _hydrate_ctx_user_config(ctx, user_config)
        return user_config


def _read_ctx_user_config(ctx: Any) -> dict[str, Any]:
    if ctx is None:
        return {}

    user_config: dict[str, Any] = {}
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    request_state = getattr(request, "state", None)

    for target, attr_name in (
        (ctx, "user_config"),
        (request_context, BZM_USER_CONFIG_STATE_ATTR),
        (request_state, BZM_USER_CONFIG_STATE_ATTR),
    ):
        request_config = getattr(target, attr_name, None)
        if isinstance(request_config, dict):
            user_config.update(request_config)

    return user_config


def _hydrate_ctx_user_config(ctx: Any, user_config: dict[str, Any]) -> None:
    if ctx is None:
        return

    config_copy = dict(user_config)
    request_context = getattr(ctx, "request_context", None)
    request = getattr(request_context, "request", None)
    request_state = getattr(request, "state", None)

    for target in (request_context, request_state):
        if target is not None:
            setattr(target, BZM_USER_CONFIG_STATE_ATTR, dict(config_copy))


def build_runtime(
        transport: Transport,
        startup_token: Optional[BzmToken] = None,
        startup_confirmation_mode: ConfirmMode = ConfirmMode.DELETE,
) -> AppRuntime:
    """
    Compose auth, identity, sessions, file access, storage and cache.

    - stdio: process-lifetime ``startup_token`` and in-memory session storage.
    - streamable-http: request-scoped auth and storage API-backed sessions/partitions,
      or, with BZM_MCP_STORAGE_BACKEND=memory, in-process sessions/partitions/cache.
    - cache: in-memory on stdio and on streamable-http memory; the storage API cache
      (/cache) on streamable-http api.
    - both: every tool call verifies the token against BlazeMeter (identity); a
      successful check is reused for BZM_MCP_IDENTITY_CACHE_TTL_SECONDS (cache).
    """
    if transport == "stdio":
        stdio_user_config = {
            "startup_token": startup_token,
            "token": startup_token,
            "confirmation_mode": startup_confirmation_mode.name,
        }
        stdio_storage = InMemorySessionStorageProvider()
        return AppRuntime(
            transport=transport,
            auth=StdioAuthProvider(startup_token),
            storage=stdio_storage,
            file_access=build_file_access(transport),
            scope_resolver=DefaultSessionScopeResolver(),
            user_config=stdio_user_config,
            tickets=None,
            cache=build_cache_from_env(),
            identity=CachedIdentityVerifier(BlazeMeterIdentityVerifier()),
            # A purged session releases its partition.
            sessions=InMemorySessionProvider(on_purge=stdio_storage.discard_session),
        )

    if transport == "streamable-http":
        if _http_storage_backend() == "memory":
            return _build_http_memory_runtime()
        storage_base_url = os.getenv("BZM_STORAGE_API_BASE_URL", "").strip()
        if not storage_base_url:
            raise ValueError(
                "BZM_STORAGE_API_BASE_URL is required for streamable-http transport. "
                f"To run one self-hosted process without the Storage Service, set "
                f"{STORAGE_BACKEND_ENV}=memory."
            )
        caller_token = service_caller_token()
        if not caller_token:
            raise ValueError(
                "BZM_MCP_STORAGE_CALLER_TOKEN (or BZM_MCP_TICKET_STORAGE_CALLER_TOKEN) is required "
                "for streamable-http transport."
            )
        storage: SessionStoragePort = HttpSessionStorageProvider(
            base_url=storage_base_url,
            caller_token=caller_token,
        )
        storage.ensure_available()
        return AppRuntime(
            transport=transport,
            auth=HttpAuthProvider(),
            storage=storage,
            file_access=build_file_access(transport),
            scope_resolver=DefaultSessionScopeResolver(),
            user_config={},
            tickets=build_ticket_client(transport, storage_base_url),
            identity=CachedIdentityVerifier(BlazeMeterIdentityVerifier()),
            sessions=HttpSessionProvider(base_url=storage_base_url, caller_token=caller_token),
            # Same MCP caller identity the ticket and session clients use.
            cache=build_cache_from_env(
                transport,
                storage_base_url=storage_base_url,
                caller_token=caller_token,
            ),
        )

    raise ValueError(f"Unknown transport: {transport}")


def _http_storage_backend() -> str:
    backend = env_str("STORAGE_BACKEND", "api").lower()
    if backend not in STORAGE_BACKENDS:
        raise ValueError(
            f"{STORAGE_BACKEND_ENV}={backend!r} is not supported. "
            f"Use one of: {', '.join(STORAGE_BACKENDS)}."
        )
    return backend


def _build_http_memory_runtime() -> AppRuntime:
    """
    streamable-http with Bearer auth per request, and all state in this process.

    Sessions stay scoped to the verified BlazeMeter user and the credential that
    opened them, partitions to (user, session) and cache entries to the user, as
    with the Storage Service. State is lost on restart and is not shared between
    processes: run one process (one uvicorn worker, one replica). Local files and
    upload tickets are not available, so upload_assets returns an error.
    """
    logger.warning(
        "%s=memory: sessions, dataframes, tasks and cache live in this process only. "
        "Run a single process; state is lost on restart.",
        STORAGE_BACKEND_ENV,
    )
    storage = InMemorySessionStorageProvider()
    return AppRuntime(
        transport="streamable-http",
        auth=HttpAuthProvider(),
        storage=storage,
        file_access=build_file_access("streamable-http"),
        scope_resolver=DefaultSessionScopeResolver(),
        user_config={},
        tickets=None,
        identity=CachedIdentityVerifier(BlazeMeterIdentityVerifier()),
        # A purged session releases its partition.
        sessions=InMemorySessionProvider(on_purge=storage.discard_session),
        # In-memory: entries stay keyed by user (CacheScope.USER) as on the storage API.
        cache=build_cache_from_env(),
    )
