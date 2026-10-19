"""yarn API: Finnhub's endpoints, served from yarn's own data."""

from fastapi import FastAPI
from sqlalchemy import func, select

from app.compat.finnhub.auth import finnhub_auth_middleware
from app.compat.finnhub.routes import router as finnhub_router
from app.config import settings
from app.db.session import SessionLocal
from app.models import Company, EarningsEvent, NewsArticle, RatingAction

app = FastAPI(
    title="yarn",
    docs_url="/docs" if settings.expose_api_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.expose_api_docs else None,
)
app.middleware("http")(finnhub_auth_middleware)
app.include_router(finnhub_router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/yarn/coverage")
def coverage():
    """What yarn holds, for operators. Not part of the Finnhub surface, so not authenticated by token."""
    with SessionLocal() as db:
        return {
            "companies": db.execute(select(func.count(Company.id))).scalar(),
            "warm_companies": db.execute(select(func.count(Company.id)).where(Company.warm.is_(True))).scalar(),
            "news_articles": db.execute(select(func.count(NewsArticle.id))).scalar(),
            "earnings_events": db.execute(select(func.count(EarningsEvent.id))).scalar(),
            "rating_actions": db.execute(select(func.count(RatingAction.id))).scalar(),
        }
