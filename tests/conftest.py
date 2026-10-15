import re

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import GoogleConnection
from tests.fakes import FakeNet

FERNET = Fernet.generate_key().decode()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        SECRET_KEY="s" * 40,
        DATABASE_URL=f"sqlite:///{tmp_path}/app.db",
        WORKER_URL="http://127.0.0.1:8100",
        WORKER_API_KEY="worker-key-123456789012345",
        GOOGLE_CLIENT_ID="cid",
        GOOGLE_CLIENT_SECRET="csecret",
        TOKEN_ENCRYPTION_KEY=FERNET,
        VAR_DIR=tmp_path / "var",
        SCHEDULER_ENABLED=False,
    )


@pytest.fixture
def net():
    return FakeNet()


@pytest.fixture
def app(settings, net):
    return create_app(settings, transport=net.transport())


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def csrf_of(client) -> str:
    html = client.get("/login").text if "uid" not in client.cookies else client.get("/").text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


@pytest.fixture
def user(app):
    from app.auth import hash_password
    from app.models import User

    with app.state.db.session() as s:
        u = User(email="owner@example.com", password_hash=hash_password("correct horse battery"))
        s.add(u)
        s.flush()
        return u.id


@pytest.fixture
def authed(client, user):
    token = csrf_of(client)
    r = client.post("/login", json={"email": "owner@example.com", "password": "correct horse battery"},
                    headers={"X-CSRF-Token": token})
    assert r.status_code == 200, r.text
    html = client.get("/").text
    client.headers["X-CSRF-Token"] = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
    return client


@pytest.fixture
def drive_connected(app, user):
    enc = app.state.providers.cipher().encrypt("rt-secret")
    with app.state.db.session() as s:
        s.add(GoogleConnection(user_id=user, refresh_token_enc=enc, email="owner@example.com"))
