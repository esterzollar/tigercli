import json
import os
import asyncio
from fastapi import FastAPI, HTTPException, Request, Body, Query, Depends, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from jinja2 import Environment, FileSystemLoader, select_autoescape

from tigercli.config import settings, load_auth, save_auth, KNOWN_PROVIDERS, is_provider_configured, provider_api_key_from_env
from tigercli.session.store import SessionStore
from tigercli.session.models import Session, Message, FileSnapshot, UsageEntry, new_id, now_iso
from tigercli.agent.loop import AgentLoop
from tigercli.api.middleware import add_cors_middleware, add_error_handlers
from tigercli.api.sse import sse_stream, sse_event


store_dependency = SessionStore()


def get_store() -> SessionStore:
    return store_dependency


def create_app() -> FastAPI:
    app = FastAPI(title="TigerLiteCode", version="0.2.0")

    add_cors_middleware(app)
    add_error_handlers(app)

    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    templates_dir = Path(__file__).parent.parent / "templates"
    templates = None
    if templates_dir.exists():
        jinja_env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(),
            cache_size=0,
        )
        templates = Jinja2Templates(env=jinja_env)

    # ── Web UI ─────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        store = get_store()
        sessions = await store.list_sessions(limit=50)
        auth = load_auth()
        if templates:
            return templates.TemplateResponse(request, "index.html", {
                "sessions": sessions,
                "default_model": settings.default_model,
                "default_provider": settings.default_provider,
                "providers": list(KNOWN_PROVIDERS.keys()),
                "current_path": str(Path.cwd()),
            })
        return HTMLResponse("<h1>TigerLiteCode</h1><p>Web UI requires templates.</p>")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        auth = load_auth()
        if templates:
            return templates.TemplateResponse(request, "settings.html", {
                "providers": list(KNOWN_PROVIDERS.keys()),
            })
        return HTMLResponse("<h1>Settings</h1>")

    # ── Health ──────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.2.0"}

    # ── OpenAI-compatible ──────────────────────────────────────

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {"id": "deepseek-v4-pro", "object": "model", "owned_by": "deepseek"},
                {"id": "deepseek-v4-flash", "object": "model", "owned_by": "deepseek"},
            ],
        }

    class ChatRequest(BaseModel):
        model: str = "deepseek-v4-pro"
        messages: list[dict]
        tools: list[dict] | None = None
        stream: bool = False
        thinking: bool = False
        reasoning_effort: str = "high"
        max_tokens: int | None = None

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatRequest):
        from tigercli.client import call as api_call

        last_user = None
        for m in reversed(req.messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break

        session_id = req.messages[0].get("session_id") if req.messages else None
        store = get_store()

        if session_id:
            session = await store.get_session(session_id)
        else:
            session = await store.create_session(
                project_path=str(settings.data_home.parent.parent),
            )

        loop = AgentLoop(store)
        response = await loop.run(
            session=session,
            user_message=last_user or "",
            auto_approve=True,
        )

        return {
            "id": new_id("chat"),
            "object": "chat.completion",
            "model": req.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": response},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": session.total_tokens_in,
                "completion_tokens": session.total_tokens_out,
                "total_tokens": session.total_tokens_in + session.total_tokens_out,
            },
        }

    # ── Providers API ─────────────────────────────────────────

    KNOWN_PROVIDER_IDS = list(KNOWN_PROVIDERS.keys())

    @app.get("/api/providers")
    async def list_providers():
        auth = load_auth()
        known = dict(KNOWN_PROVIDERS)
        result = []
        for pid, info in known.items():
            cfg = auth.get(pid, {})
            configured = is_provider_configured(pid, auth)
            result.append({
                "id": pid,
                "name": info["name"],
                "base_url": cfg.get("base_url", info["base_url"]),
                "configured": configured,
                "models": cfg.get("models", info.get("models", [])),
                "website": info.get("website", ""),
            })
        return result

    class ConfigureProviderRequest(BaseModel):
        provider: str
        api_key: str | None = None
        base_url: str | None = None

    @app.post("/api/providers/configure")
    async def configure_provider(req: ConfigureProviderRequest):
        if req.provider not in KNOWN_PROVIDER_IDS:
            raise HTTPException(400, f"Unknown provider: {req.provider}")
        auth = load_auth()
        if req.provider not in auth:
            auth[req.provider] = {}
        if req.api_key is not None:
            auth[req.provider]["api_key"] = req.api_key
        if req.base_url is not None:
            auth[req.provider]["base_url"] = req.base_url
        save_auth(auth)
        return {"status": "configured", "provider": req.provider}

    @app.post("/api/providers/{provider_id}/disconnect")
    async def disconnect_provider(provider_id: str):
        auth = load_auth()
        if provider_id in auth:
            del auth[provider_id]
            save_auth(auth)
        return {"status": "disconnected", "provider": provider_id}

    @app.post("/api/providers/{provider_id}/fetch-models")
    async def fetch_provider_models(provider_id: str):
        if provider_id not in KNOWN_PROVIDER_IDS:
            raise HTTPException(400, f"Unknown provider: {provider_id}")
        auth = load_auth()
        cfg = auth.get(provider_id, {})
        api_key = cfg.get("api_key") or provider_api_key_from_env(provider_id)
        base_url = cfg.get("base_url", KNOWN_PROVIDERS[provider_id]["base_url"])

        if not api_key:
            raise HTTPException(400, f"No API key configured for {provider_id}. Use /api/providers/configure first.")

        models: list[dict] = []
        try:
            import httpx
            headers = {"Authorization": f"Bearer {api_key}"}
            url = f"{base_url.rstrip('/')}/models"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        models.append({
                            "id": m.get("id", m.get("name", "unknown")),
                            "name": m.get("id", m.get("name", "unknown")),
                            "owned_by": m.get("owned_by", ""),
                        })
        except Exception:
            models = KNOWN_PROVIDERS[provider_id].get("models", [])

        if models:
            auth[provider_id]["models"] = models
            save_auth(auth)

        return {"provider": provider_id, "models": models, "count": len(models)}

    # ── Folder / Project ──────────────────────────────────────

    @app.get("/api/folder/list")
    async def list_folder(path: str = Query("~")):
        base = Path(path).expanduser().resolve()
        if not base.exists() or not base.is_dir():
            return {"path": str(base), "entries": []}
        try:
            entries = []
            for e in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if e.name.startswith("."):
                    continue
                entries.append({
                    "name": e.name,
                    "path": str(e),
                    "type": "directory" if e.is_dir() else "file",
                })
            return {"path": str(base), "entries": entries[:200]}
        except PermissionError:
            return {"path": str(base), "entries": [], "error": "Permission denied"}

    @app.post("/api/sessions/{session_id}/project-path")
    async def set_session_project(session_id: str, data: dict = Body(...)):
        store = get_store()
        new_path = data.get("project_path", "")
        await store.update_session(session_id, project_path=new_path)
        return {"status": "updated", "project_path": new_path}

    # ── Sessions API ───────────────────────────────────────────

    @app.get("/api/sessions")
    async def list_sessions(limit: int = 50):
        store = get_store()
        sessions = await store.list_sessions(limit)
        return [{
            "id": s.id, "title": s.title, "provider": s.provider,
            "model": s.model, "mode": s.mode,
            "cost_usd": s.total_cost_usd,
            "cache_hit_rate": round(s.cache_hit_rate, 3),
            "total_tokens_in": s.total_tokens_in,
            "total_tokens_out": s.total_tokens_out,
            "updated_at": s.updated_at,
        } for s in sessions]

    class CreateSessionRequest(BaseModel):
        title: str | None = None
        project_path: str | None = None
        provider: str | None = None
        model: str | None = None
        thinking: bool = False
        effort: str = "high"
        mode: str | None = None

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest):
        store = get_store()
        session = await store.create_session(
            project_path=req.project_path or str(Path.cwd()),
            title=req.title,
            provider=req.provider,
            model=req.model,
            thinking_enabled=req.thinking,
            reasoning_effort=req.effort,
            mode=req.mode,
        )
        return {"id": session.id, "title": session.title, "mode": session.mode}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        store = get_store()
        session = await store.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        messages = await store.get_messages(session_id)
        return {
            "id": session.id,
            "title": session.title,
            "project_path": session.project_path,
            "provider": session.provider,
            "model": session.model,
            "mode": session.mode,
            "total_tokens_in": session.total_tokens_in,
            "total_tokens_out": session.total_tokens_out,
            "total_cost_usd": session.total_cost_usd,
            "cache_hit_rate": session.cache_hit_rate,
            "messages": [vars(m) for m in messages],
        }

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, data: dict = Body(...)):
        store = get_store()
        allowed = {"title", "model", "provider", "mode", "thinking_enabled", "reasoning_effort", "project_path"}
        updates = {k: v for k, v in data.items() if k in allowed}
        if updates:
            await store.update_session(session_id, **updates)
        return {"status": "updated"}

    @app.delete("/api/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str):
        store = get_store()
        await store.delete_session(session_id)
        return Response(status_code=204)

    # ── Messages ───────────────────────────────────────────────

    class SendMessageRequest(BaseModel):
        content: str
        files: list[dict] | None = None

    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, req: SendMessageRequest):
        store = get_store()
        session = await store.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        files = []
        if req.files:
            for f in req.files:
                files.append((f.get("path", ""), f.get("content", "")))

        async def event_generator():
            loop = AgentLoop(store, on_event=lambda t, d: event_queue.put((t, d)))
            event_queue = asyncio.Queue()

            async def emit(event_type: str, data: dict):
                await event_queue.put((event_type, data))

            loop._on_event = emit

            async def run_loop():
                await loop.run(
                    session=session,
                    user_message=req.content,
                    files=files or None,
                )
                await event_queue.put(("__done__", {}))

            task = asyncio.create_task(run_loop())

            while True:
                event = await event_queue.get()
                if event[0] == "__done__":
                    break
                yield event

        return await sse_stream(event_generator())

    # ── Fork ────────────────────────────────────────────────────

    @app.post("/api/sessions/{session_id}/fork")
    async def fork_session(session_id: str):
        store = get_store()
        session = await store.get_session(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        new_session = await store.create_session(
            project_path=session.project_path,
            title=f"Fork: {session.title}",
            parent_id=session_id,
            provider=session.provider,
            model=session.model,
            thinking_enabled=session.thinking_enabled,
            reasoning_effort=session.reasoning_effort,
            mode=session.mode,
        )
        # Copy messages
        messages = await store.get_messages(session_id)
        for m in messages:
            m.session_id = new_session.id
            await store.add_message(m)
        return {"id": new_session.id}

    # ── Undo / Redo ────────────────────────────────────────────

    class UndoRequest(BaseModel):
        count: int = 1

    @app.post("/api/sessions/{session_id}/undo")
    async def undo_messages(session_id: str, req: UndoRequest = Body(UndoRequest())):
        store = get_store()
        reverted = await store.revert_last_messages(session_id, req.count)
        return {"reverted_count": len(reverted)}

    @app.post("/api/sessions/{session_id}/redo")
    async def redo_messages(session_id: str):
        store = get_store()
        restored = await store.unrevert_last(session_id)
        return {"restored": restored.id if restored else None}

    # ── Diff ────────────────────────────────────────────────────

    @app.get("/api/sessions/{session_id}/diff")
    async def get_diff(session_id: str, message_id: str = Query(...)):
        store = get_store()
        snaps = await store.get_snapshots_for_message(message_id)
        files = []
        for snap in snaps:
            files.append({
                "path": snap.file_path,
                "before": snap.content_before,
                "after": snap.content_after,
            })
        return {"files": files}

    # ── Permissions ────────────────────────────────────────────

    class PermissionResponse(BaseModel):
        approved: bool
        remember: bool = False

    @app.post("/api/permissions/{session_id}/{tool_call_id}")
    async def respond_permission(session_id: str, tool_call_id: str, req: PermissionResponse):
        # In SSE mode, permissions are resolved via the agent loop's permissions map
        return {"status": "ok", "approved": req.approved}

    # ── Stats ──────────────────────────────────────────────────

    @app.get("/api/stats")
    async def stats():
        store = get_store()
        return await store.get_total_stats()

    # ── File search ────────────────────────────────────────────

    @app.get("/api/files/search")
    async def file_search(q: str = Query(""), path: str = Query(".")):
        base = Path(path).expanduser().resolve()
        if not base.exists():
            return []
        results = []
        for f in base.rglob(f"*{q}*"):
            if f.is_file() and not f.name.startswith("."):
                results.append({
                    "path": str(f),
                    "name": f.name,
                    "type": "file",
                })
                if len(results) >= 20:
                    break
        return results

    # ── Plugins API ────────────────────────────────────────────

    @app.post("/api/plugins/install")
    async def install_plugin(file: UploadFile = File(...)):
        store = get_store()
        content = await file.read()
        name = Path(file.filename).stem if file.filename else "plugin"
        # Save to plugins dir
        plugins_dir = settings.plugins_dir
        plugins_dir.mkdir(parents=True, exist_ok=True)
        (plugins_dir / file.filename).write_bytes(content)
        pid = await store.add_plugin(name=name, description=f"Plugin from {file.filename}")
        return {"id": pid, "name": name}

    @app.get("/api/plugins")
    async def list_plugins():
        store = get_store()
        return await store.list_plugins()

    @app.delete("/api/plugins/{name}")
    async def remove_plugin(name: str):
        store = get_store()
        ok = await store.remove_plugin(name)
        if not ok:
            raise HTTPException(404, "Plugin not found")
        # Remove from filesystem
        plugin_file = settings.plugins_dir / f"{name}.py"
        if plugin_file.exists():
            plugin_file.unlink()
        return {"status": "removed"}

    # ── MCP API ────────────────────────────────────────────────

    class AddMcpRequest(BaseModel):
        name: str
        transport: str = "stdio"
        command: str | None = None
        args: list[str] = []
        env: dict | None = None
        url: str | None = None

    @app.post("/api/mcp/add")
    async def add_mcp_server(req: AddMcpRequest):
        store = get_store()
        mid = await store.add_mcp_server(
            name=req.name, transport=req.transport,
            command=req.command, args=req.args,
            env=req.env, url=req.url,
        )
        tools = []
        try:
            from tigercli.mcp.manager import MCPServerManager
            mgr = MCPServerManager(store)
            server = await store.list_mcp_servers()
            # Just return the server info for now
        except Exception as e:
            pass
        return {"id": mid, "tools": tools}

    @app.get("/api/mcp")
    async def list_mcp_servers():
        store = get_store()
        return await store.list_mcp_servers()

    @app.delete("/api/mcp/{server_id}")
    async def remove_mcp_server(server_id: str):
        store = get_store()
        ok = await store.remove_mcp_server(server_id)
        if not ok:
            raise HTTPException(404, "MCP server not found")
        return {"status": "removed"}

    @app.post("/api/mcp/{server_id}/refresh")
    async def refresh_mcp_server(server_id: str):
        store = get_store()
        server = None
        for s in await store.list_mcp_servers():
            if s["id"] == server_id:
                server = s
                break
        if not server:
            raise HTTPException(404, "MCP server not found")
        try:
            from tigercli.mcp.manager import MCPServerManager
            mgr = MCPServerManager(store)
            tools = []
            # Simplified: just mark as refreshed
            await store.update_mcp_tools_cache(server_id, tools)
            return {"tools": tools}
        except Exception as e:
            raise HTTPException(500, f"Failed to refresh: {e}")

    return app
