"""
FastAPI Application Entry Point
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer

from app.api.router import api_router

from app.core.config import settings
from app.core.errors import (
    AppError,
    ValidationError,
    app_exception_handler,
    http_exception_handler,
    validation_exception_handler,
    general_exception_handler,
)
from app.core.logging import setup_logging, RequestIDMiddleware, RequestLoggingMiddleware
from app.infra.db import close_db_connection
from app.infra.redis import init_redis_pool, close_redis_pool
from app.infra.qdrant import init_qdrant_client, close_qdrant_client, ensure_collections_exist
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    setup_logging()
    await init_redis_pool()
    await init_qdrant_client()
    
    # Initialize Qdrant collections background text
    # In production, this might be better as a migration step
    try:
        await ensure_collections_exist()
    except Exception as e:
        # Don't fail startup if qdrant is down, but log it
        print(f"Warning: Failed to initialize Qdrant collections: {e}")
        
    yield
    
    # Shutdown
    await close_redis_pool()
    await close_qdrant_client()
    await close_db_connection()


tags_metadata = [
    {
        "name": "debug",
        "description": "Debug tools for seeding data and testing.",
    },
    {
        "name": "auth",
        "description": "Standard Authentication operations (Login, Register).",
    },
    {
        "name": "meetings",
        "description": "Create and manage translation meetings.",
    },
    {
        "name": "websocket",
        "description": "Real-time communication using WebSocket.",
    },
    {
        "name": "health",
        "description": "System health check.",
    },
]


class RawASGIMiddleware:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            print(f"\n[RAW-WS-INCOMING] Path: {scope.get('path')} | Origin: {dict(scope.get('headers', [])).get(b'origin', b'none').decode()}")
        return await self.app(scope, receive, send)

def create_app() -> FastAPI:
    app = FastAPI(
        title="URITOMO Backend",
        description="""
URITOMO API provides real-time translation with cultural context explanations.

## Features
* **Real-time Translation**: Low-latency WebSocket streaming.
* **Cultural Context**: RAG-powered explanations for nuanced expressions.
* **Meeting Management**: Complete lifecycle for multilingual sessions.
* **Organizations**: Shared glossaries and cultural knowledge.
""",
        version="0.1.0",
        openapi_tags=tags_metadata,
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        swagger_ui_parameters={
            "persistAuthorization": True,
            "displayRequestDuration": True
        },
        lifespan=lifespan,
    )

    # --- Debugging Middlewares (Lowest Level) ---
    app.add_middleware(RawASGIMiddleware)

    # Middleware
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)
    
    # CORS Configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*", 
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Connection Test Ping
    @app.get("/ws-test-ping")
    @app.get(f"{settings.api_prefix}/ws-test-ping")
    async def ws_test_ping():
        return {
            "status": "ok", 
            "message": "MAGIC_WS_PONG_2026", 
            "hint": "If you see this, code IS syncing!"
        }

    # Debug WebSocket Test (Unique path to avoid conflict)
    @app.websocket("/debug-ws-test")
    async def websocket_debug_test(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"message": "If you see this, WebSocket works!"})
        await websocket.close()

    # Root Route
    @app.get("/debug/ping", tags=["debug"])
    async def debug_ping(request: Request):
        return {
            "status": "ok",
            "message": "Backend version 0.1.1 (WS-Fix-Applied)",
            "headers": dict(request.headers),
            "cors_origins": settings.cors_origins
        }

    @app.get("/", tags=["health"], include_in_schema=False)
    async def root():
        return {
            "message": "Welcome to URITOMO Backend API",
            "docs": "/docs",
            "status": "operational"
        }

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard/", include_in_schema=False)
    async def dashboard_redirect(request: Request):
        host = request.headers.get("host", "localhost:8000")
        hostname = host.split(":")[0]
        target = f"http://{hostname}:8501/dashboard"
        return RedirectResponse(url=target, status_code=307)

    # Routes

    # We include dependencies=[Depends(HTTPBearer())] if we want to FORCE it everywhere globally.
    # But usually, it's better to apply it to the main api_router.
    # Routes
    # WebSocket Router (Bypass api_prefix, mounted directly, HIGH PRIORITY)
    from app.meeting.ws.ws_base import router as meeting_ws_router
    app.include_router(meeting_ws_router)

    # Routes
    app.include_router(api_router, prefix=settings.api_prefix)

    # Exception Handlers
    app.add_exception_handler(AppError, app_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)

    return app


app = create_app()
