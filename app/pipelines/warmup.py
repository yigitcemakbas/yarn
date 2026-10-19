"""Mark a ticker for continuous refresh the first time anyone asks for it.

The request path never waits on an upstream: it marks the company warm and
answers from what is stored. The worker refreshes warm companies first when
they have never been refreshed, so a cold ticker fills within a minute or two.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company


def ensure_warm(db: Session, ticker: str) -> bool:
    company = db.execute(select(Company).where(Company.ticker == ticker)).scalars().first()
    if company is None:
        return False
    if not company.warm:
        company.warm = True
        db.commit()
    return True
