import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SAMPLE_DATASET = REPO_ROOT / "dataset_GKYZ_2016_AAPL_MSFT_trimmed.csv"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def sample_dataset_path() -> Path:
    assert SAMPLE_DATASET.exists(), f"Missing sample dataset: {SAMPLE_DATASET}"
    return SAMPLE_DATASET


@pytest.fixture()
def agent_module(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT_NAME", "test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("FUTURECOMPLETE_API_BASE_URL", "https://inait-saas-apim-jjyzmt7v.azure-api.net")
    monkeypatch.setenv("FUTURECOMPLETE_DASHBOARD_URL", "https://futurecomplete.inait.ai")
    monkeypatch.setenv("FUTURECOMPLETE_TRIAL_USERS_URL", "https://api.forecasting.inait.ai/users/dev/users")
    monkeypatch.setenv("FUTURECOMPLETE_TRIAL_PLAN_ID", "trial")
    monkeypatch.setenv("FUTURECOMPLETE_JOB_STATUS_PATH_TEMPLATE", "/v1/sessions/{session_id}/status")
    monkeypatch.setenv("FUTURECOMPLETE_JOB_RESULT_PATH_TEMPLATE", "/v1/sessions/{session_id}/result")
    monkeypatch.setenv("FUTURECOMPLETE_JOB_CANCEL_PATH_TEMPLATE", "/v1/sessions/{session_id}/cancel")

    sys.path.insert(0, str(SRC_DIR))
    module = importlib.import_module("agent")
    module = importlib.reload(module)

    module._session_users.clear()
    module._session_attachments.clear()
    module._dataset_schemas_by_session.clear()
    module._jobs_by_session.clear()
    module._subscriptions_by_user.clear()
    module._session_subscription_keys.clear()

    yield module

    for task in list(module._polling_tasks):
        task.cancel()
    module._polling_tasks.clear()
    if str(SRC_DIR) in sys.path:
        sys.path.remove(str(SRC_DIR))


@pytest.fixture()
def fake_context(agent_module):
    session_id = "test-conversation:session"
    identity = {
        "aad_object_id": "user-123",
        "email": "user@example.com",
        "user_name": "Test User",
        "user_id": "user@example.com",
    }
    agent_module._session_users[session_id] = identity
    return SimpleNamespace(session=SimpleNamespace(session_id=session_id), metadata={"user_identity": identity})


@pytest.fixture()
def active_full_subscription(agent_module, fake_context):
    subscription = {
        "cache_key": "user-123",
        "user_email": "user@example.com",
        "plan_id": "paid",
        "subscription_key": "paid-subscription-key",
        "source": "test",
    }
    agent_module._subscriptions_by_user["user-123"] = subscription
    agent_module._session_subscription_keys[fake_context.session.session_id] = "user-123"
    return subscription


@pytest.fixture()
def active_trial_subscription(agent_module, fake_context):
    subscription = {
        "cache_key": "user-123",
        "user_email": "user@example.com",
        "plan_id": "trial",
        "subscription_key": "trial-subscription-key",
        "source": "test",
    }
    agent_module._subscriptions_by_user["user-123"] = subscription
    agent_module._session_subscription_keys[fake_context.session.session_id] = "user-123"
    return subscription
