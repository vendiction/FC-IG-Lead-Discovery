"""FastAPI app — internal API for workers, webhooks from Supabase/n8n, dashboard backend."""
from __future__ import annotations
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("api.startup")
    yield
    log.info("api.shutdown")


app = FastAPI(
    title="FC IG Lead Discovery API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    return {"service": "fc-ig-lead-discovery", "version": "0.1.0"}


# Module routes registered as we build them:
# from app.modules.m1_tagged_crawler.routes import router as m1_router
# app.include_router(m1_router, prefix="/m1")
