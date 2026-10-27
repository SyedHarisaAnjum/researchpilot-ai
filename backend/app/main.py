import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import auth, debug, events, report, research
from app.auth.deps import admin_user
from app.config import get_settings
from app.db.databases import engine, run_migrations
from app.logging_setup import configure_logging
from app.services.job_service import job_manager
from app.services.llm_service import (
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    get_llm_service,
)
from app.services.rate_limit import RateLimitExceeded, RedisLimiter, close_limiter, get_limiter
from app.services.search_service import (
    SearchAuthError,
    SearchError,
    SearchQuotaError,
    SearchRateLimitError,
    get_search_service,
)
from app.services.web_reader import get_web_reader

settings = get_settings()
configure_logging()
logger = logging.getLogger("app")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-requested-with"
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting %s (%s, jobs: %s)", settings.app_name, settings.environment, settings.job_backend)
    if settings.run_migrations_on_startup:
        await asyncio.to_thread(run_migrations)
    await job_manager.startup()
    if interrupted := await job_manager.recover_interrupted_jobs():
        logger.warning("Marked %d unfinished research job(s) from a previous run as failed", interrupted)
    yield
    logger.info("Shutting down %s", settings.app_name)
    await job_manager.shutdown()
    # Close only the clients that were actually created.
    for get_service in (get_llm_service, get_search_service, get_web_reader):
        if get_service.cache_info().currsize:
            await get_service().aclose()
    await close_limiter()
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    lifespan=lifespan,
    # Interactive docs are for development; production hides them unless ENABLE_DOCS=true.
    docs_url="/api/docs" if settings.docs_enabled else None,
    openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    redoc_url=None,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Requested-With", "Authorization", "Last-Event-ID"],
    )


# Every error response has the shape {"error": {"code", "message", "details"}}.
def error_response(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # CSRF: a state-changing request must carry a custom header. Another site can't add one
    # without a CORS preflight, which only the configured origins pass.
    if request.method in UNSAFE_METHODS and request.url.path.startswith("/api/") and not request.headers.get(CSRF_HEADER):
        return error_response(403, "csrf_check_failed", "Missing X-Requested-With header")
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    code = {401: "not_authenticated", 403: "forbidden"}.get(exc.status_code, f"http_{exc.status_code}")
    return error_response(exc.status_code, code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # jsonable_encoder: errors from custom validators carry exception objects in "ctx".
    return error_response(422, "validation_error", "Request validation failed", jsonable_encoder(exc.errors()))


@app.exception_handler(LLMError)
async def llm_exception_handler(request: Request, exc: LLMError):
    logger.warning("LLM error on %s %s: %s", request.method, request.url.path, exc)
    if isinstance(exc, LLMTimeoutError):
        return error_response(504, "llm_timeout", str(exc))
    if isinstance(exc, LLMRateLimitError):
        return error_response(429, "llm_rate_limited", str(exc))
    if isinstance(exc, LLMResponseError):
        return error_response(502, "llm_invalid_response", str(exc))
    return error_response(502, "llm_error", "The language model request failed")


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    response = error_response(429, exc.code, str(exc), {"retry_after": exc.retry_after})
    response.headers["Retry-After"] = str(exc.retry_after)
    return response


@app.exception_handler(SearchError)
async def search_exception_handler(request: Request, exc: SearchError):
    logger.warning("Search error on %s %s: %s", request.method, request.url.path, exc)
    if isinstance(exc, SearchRateLimitError):
        return error_response(429, "search_rate_limited", str(exc))
    if isinstance(exc, SearchQuotaError):
        return error_response(503, "search_quota_exceeded", str(exc))
    if isinstance(exc, SearchAuthError):
        return error_response(502, "search_auth_failed", str(exc))
    return error_response(502, "search_error", "The search request failed")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "An unexpected error occurred")


app.include_router(auth.router)
app.include_router(auth.admin_router)
app.include_router(research.router)
app.include_router(report.router)
app.include_router(events.router)
if not settings.is_production:
    # Single-step test endpoints, for admins, outside production only.
    app.include_router(debug.router, dependencies=[Depends(admin_user)])


@app.get("/api/health", tags=["health"])
async def health():
    """Liveness: the process is up."""
    return {"status": "ok", "message": "API is running successfully."}


@app.get("/api/ready", tags=["health"])
async def ready():
    """Readiness: the database, and the queue and Redis when used, are reachable."""
    checks = {}
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        checks["queue"] = "ok" if await job_manager.ping_queue() else "error"
        limiter = get_limiter()
        if isinstance(limiter, RedisLimiter):
            await limiter.redis.ping()
            checks["redis"] = "ok"
    except Exception as exc:
        checks["queue"] = f"error: {type(exc).__name__}"
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content={"status": "ok" if healthy else "unavailable", "checks": checks})
