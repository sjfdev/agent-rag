"""Runtime model overrides: validation, masking, persistence and cache keys.

运行时模型覆盖：校验、掩码、持久化与缓存键。
"""

import json

import pytest

from src import model_settings
from src.config import settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MODEL_SETTINGS_FILE", str(tmp_path / "model_settings.json"))
    monkeypatch.setattr(settings, "LLM_API_BASE", "https://env.example/v1")
    monkeypatch.setattr(settings, "LLM_API_KEY", "env-secret-key-1234")
    monkeypatch.setattr(settings, "LLM_MODEL", "env-model")
    monkeypatch.setattr(settings, "LLM_TEMPERATURE", 0.0)
    monkeypatch.setattr(settings, "LLM_MAX_TOKENS", 256)
    model_settings.reset_state()
    yield
    model_settings.reset_state()


def _payload(**overrides):
    data = {
        "api_base": "https://new.example/v1",
        "model": "new-model",
        "api_key": "sk-test-abcdefgh",
        "temperature": 0.3,
        "max_tokens": 512,
    }
    data.update(overrides)
    return data


def test_env_is_the_default():
    described = model_settings.describe()
    assert described["source"] == "env"
    assert described["model"] == "env-model"
    assert described["api_base"] == "https://env.example/v1"
    assert described["has_api_key"] is True


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"api_base": ""}, "api_base is required"),
        ({"api_base": "ftp://x"}, "http"),
        ({"api_base": "https://x y"}, "spaces"),
        ({"model": ""}, "model is required"),
        ({"temperature": 3}, "temperature"),
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": 99999}, "max_tokens"),
    ],
)
def test_validation_rejects_bad_input(overrides, message):
    with pytest.raises(ValueError, match=message):
        model_settings.save(_payload(**overrides))


def test_save_persists_and_masks_the_key(tmp_path):
    saved = model_settings.save(_payload())
    assert saved["source"] == "override"
    assert saved["model"] == "new-model"
    assert saved["api_key_hint"] == "****efgh"
    assert "sk-test-abcdefgh" not in json.dumps(saved)

    on_disk = json.loads((tmp_path / "model_settings.json").read_text())
    assert on_disk["api_key"] == "sk-test-abcdefgh"
    assert on_disk["api_base"] == "https://new.example/v1"
    assert (tmp_path / "model_settings.json").stat().st_mode & 0o777 == 0o600


def test_saving_without_a_key_keeps_the_existing_one():
    model_settings.save(_payload())
    saved = model_settings.save(_payload(api_key=None))
    assert saved["api_key_hint"] == "****efgh"
    assert model_settings.effective()["api_key"] == "sk-test-abcdefgh"


def test_effective_merges_env_and_overrides():
    model_settings.save(_payload(temperature=1.25))
    config = model_settings.effective()
    assert config["model"] == "new-model"
    assert config["temperature"] == 1.25
    assert config["max_tokens"] == 512


def test_generation_changes_only_on_writes():
    start = model_settings.generation()
    assert model_settings.generation() == start
    model_settings.save(_payload())
    assert model_settings.generation() > start
    saved = model_settings.generation()
    assert model_settings.generation() == saved
    model_settings.clear()
    assert model_settings.generation() > saved


def test_clear_falls_back_to_env(tmp_path):
    model_settings.save(_payload())
    described = model_settings.clear()
    assert described["source"] == "env"
    assert described["model"] == "env-model"
    assert not (tmp_path / "model_settings.json").exists()


def test_corrupt_file_is_ignored(tmp_path):
    path = tmp_path / "model_settings.json"
    path.write_text("{not json")
    model_settings.reset_state()
    assert model_settings.effective()["model"] == "env-model"
    assert model_settings.describe()["source"] == "env"


def test_probe_uses_the_candidate_without_saving(monkeypatch):
    import openai

    calls = {}

    class FakeCompletions:
        def create(self, **kwargs):
            calls.update(kwargs)
            return object()

    class FakeClient:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    result = model_settings.probe(_payload())
    assert result["ok"] is True
    assert calls["client"]["base_url"] == "https://new.example/v1"
    assert calls["client"]["api_key"] == "sk-test-abcdefgh"
    assert calls["model"] == "new-model"
    assert model_settings.describe()["source"] == "env"


def test_probe_reports_failures(monkeypatch):
    import openai

    class Boom:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(openai, "OpenAI", Boom)
    result = model_settings.probe(_payload())
    assert result["ok"] is False
    assert "connection refused" in result["error"]
