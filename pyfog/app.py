from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from pyfog import api, web
from pyfog.config import PACKAGE_DIR, Settings
from pyfog.database import make_engine
from pyfog.middleware import RequestLimitsMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    application = FastAPI(
        title="PyFog",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        debug=settings.debug,
    )
    application.state.settings = settings
    application.state.engine = make_engine(settings.database_url)
    application.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    application.include_router(web.router)
    application.include_router(api.router)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="pyfog_session",
        max_age=settings.session_seconds,
        same_site="lax",
        https_only=settings.production,
    )
    application.add_middleware(RequestLimitsMiddleware, max_bytes=settings.max_body_bytes)
    if settings.production:
        application.add_middleware(HTTPSRedirectMiddleware)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    if settings.trusted_proxy_ips:
        application.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.trusted_proxy_ips)

    async def http_error(request: Request, exc: Exception) -> Response:
        if not isinstance(exc, HTTPException):
            raise exc
        if exc.status_code == 303:
            return RedirectResponse((exc.headers or {}).get("Location", "/login"), 303)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)
        return web.render(request, "error.html", status=exc.status_code, error=str(exc.detail))

    application.add_exception_handler(HTTPException, http_error)
    return application


app = create_app()
