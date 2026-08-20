"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.config import get_settings
from app.logging_config import configure_logging


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_level)
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="Discovers and evaluates supplement brands advertising on Meta.",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "app": settings.app_name,
        "version": __version__,
        "description": "Supplement brand discovery and qualification service",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "environment": settings.app_env,
    }
