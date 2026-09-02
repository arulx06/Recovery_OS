import os
import shutil
import socket
import tempfile
from pathlib import Path

# Must be set before app modules import settings/engine.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="recoveryos-tests-"))
_TEST_DB = _TEST_ROOT / "test.db"
os.environ["ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_fake_hermetic"
os.environ["RAZORPAY_KEY_SECRET"] = "fake_hermetic_secret"
os.environ["RAZORPAY_API_ENABLED"] = "false"
os.environ["LLM_API_KEY"] = "fake_hermetic_llm_key"
os.environ["LLM_API_ENABLED"] = "false"
os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["ANTHROPIC_API_KEY"] = "fake_hermetic_anthropic_key"
os.environ["OPENAI_API_KEY"] = "fake_hermetic_openai_key"

import pytest
from fastapi.testclient import TestClient

from sqlalchemy.orm import close_all_sessions

from app.core.database import Base, engine
from app.main import app
from app.ml import scorer, train as train_module


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=engine)
    try:
        yield
    finally:
        close_all_sessions()
        try:
            Base.metadata.drop_all(bind=engine)
        finally:
            engine.dispose()
            shutil.rmtree(_TEST_ROOT)


@pytest.fixture(autouse=True)
def _isolate_database():
    yield
    close_all_sessions()
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


@pytest.fixture(autouse=True)
def _deny_external_network(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def is_loopback(address) -> bool:
        return isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}

    def guarded_connect(sock, address):
        if is_loopback(address):
            return original_connect(sock, address)
        raise AssertionError("automated tests must not access external networks")

    def guarded_connect_ex(sock, address):
        if is_loopback(address):
            return original_connect_ex(sock, address)
        raise AssertionError("automated tests must not access external networks")

    def denied(*args, **kwargs):
        raise AssertionError("automated tests must not access external networks")

    def guarded_sendto(sock, data, address):
        if is_loopback(address):
            return original_sendto(sock, data, address)
        raise AssertionError("automated tests must not access external networks")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)


@pytest.fixture(scope="session")
def trained_model_path(tmp_path_factory):
    model_path = tmp_path_factory.mktemp("ml-model") / "model.joblib"
    train_module.train(n=6000, seed=42, save=True, save_path=model_path)
    yield model_path
    scorer.reset_cache()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
