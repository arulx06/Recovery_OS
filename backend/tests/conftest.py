import os

# Must be set before app modules import settings/engine.
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret"

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, engine
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    engine.dispose()

    if os.path.exists("test.db"):
        os.remove("test.db")


@pytest.fixture
def client():
    return TestClient(app)
