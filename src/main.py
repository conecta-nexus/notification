from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.core.config import Settings
from src.core.database import create_engine, create_session_factory, ping_database
from src.core.security import load_verification_key
from src.routers.events import router
from src.routers.notifications import router as notifications_router
from src.services.event_dispatcher import EventDispatcher
from src.services.runtime_handlers import build_runtime_dispatcher


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        fields = ", ".join(
            str(item["loc"][0]) for item in error.errors(include_input=False)
        )
        raise RuntimeError(f"Invalid Notification settings: {fields}.") from None


def create_app(
    settings: Settings | None = None,
    dispatcher: EventDispatcher | None = None,
    *,
    default_handlers: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        config = settings if settings is not None else load_settings()
        application.state.settings = config
        application.state.verification_key = load_verification_key(config)
        engine = create_engine(config)
        session_factory = create_session_factory(engine)
        try:
            application.state.engine = engine
            application.state.session_factory = session_factory
            if dispatcher is not None:
                application.state.dispatcher = dispatcher
            elif default_handlers:
                application.state.dispatcher = build_runtime_dispatcher(
                    config, session_factory
                )
            else:
                application.state.dispatcher = EventDispatcher()
            yield
        finally:
            await engine.dispose()

    application = FastAPI(
        title="Conecta Nexus - Notification Service", version="0.1.0", lifespan=lifespan
    )
    application.include_router(router)
    application.include_router(notifications_router)

    @application.get("/")
    async def read_root():
        return {"service": "notification", "status": "running", "framework": "fastapi"}

    @application.get(
        "/health", responses={503: {"description": "Database unavailable"}}
    )
    async def health_check(request: Request):
        connected = await ping_database(
            request.app.state.engine, request.app.state.settings.db_health_timeout
        )
        if connected:
            return {"status": "healthy", "database": "connected"}
        return JSONResponse(
            {"status": "unhealthy", "database": "unavailable"}, status_code=503
        )

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Event bodies can contain contacts and feedback.
        return JSONResponse({"detail": "Invalid request payload."}, status_code=422)

    return application


app = create_app(default_handlers=True)


def run():
    import uvicorn

    settings = load_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, loop="asyncio")


if __name__ == "__main__":
    run()
