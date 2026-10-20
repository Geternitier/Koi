import logging

import pytest

from koi.kernel.llm import LLMConfig
from koi.kernel.secrets import (
    DEEPSEEK_API_KEY,
    MemoryBackend,
    MissingSecretError,
    RedactingFilter,
    Redactor,
    SecretStore,
    scrubbed_env,
)

KEY = "sk-0123456789abcdef0123"


def store(environ=None, user_env=None, backend=None) -> SecretStore:
    return SecretStore(
        backend or MemoryBackend(),
        environ=environ or {},
        user_env=(lambda name: (user_env or {}).get(name)),
    )


def test_lookup_order_env_then_user_env_then_keyring():
    backend = MemoryBackend()
    backend.set(DEEPSEEK_API_KEY, "from-keyring")
    s = store({"CHAT_DEEPSEEK": "from-env"}, {"CHAT_DEEPSEEK": "from-user"}, backend)
    assert s.lookup(DEEPSEEK_API_KEY)[1] == "env"
    s = store({}, {"CHAT_DEEPSEEK": "from-user"}, backend)
    assert s.lookup(DEEPSEEK_API_KEY)[1] == "user_env"
    s = store({}, {}, backend)
    value, source = s.lookup(DEEPSEEK_API_KEY)
    assert (value.get_secret_value(), source) == ("from-keyring", "keyring")


def test_require_explains_how_to_configure():
    with pytest.raises(MissingSecretError, match="CHAT_DEEPSEEK"):
        store().require(DEEPSEEK_API_KEY)


def test_llm_config_from_secrets_keeps_key_hidden():
    cfg = LLMConfig.from_secrets(store({"CHAT_DEEPSEEK": KEY}))
    assert cfg.api_key.get_secret_value() == KEY
    assert KEY not in repr(cfg) and KEY not in cfg.model_dump_json()


def test_masked_shows_only_tail_and_source():
    assert store({"CHAT_DEEPSEEK": KEY}).masked(DEEPSEEK_API_KEY) == "****0123（来自 env）"


def test_scrubbed_env_removes_secret_vars_and_copies():
    s = store({"CHAT_DEEPSEEK": KEY})
    environ = {"CHAT_DEEPSEEK": KEY, "chat_deepseek": KEY, "COPY": KEY, "PATH": "/bin"}
    assert scrubbed_env(s, environ) == {"PATH": "/bin"}


def test_redactor_replaces_known_values_and_generic_keys():
    backend = MemoryBackend()
    s = store({"CHAT_DEEPSEEK": KEY}, backend=backend)
    s.set("plugin.bili.cookie", "SESSDATA=abcdefgh1234")
    r = Redactor.from_store(s)
    text = f"key={KEY} cookie=SESSDATA=abcdefgh1234 other=sk-zzzzzzzzzzzzzzzzzzzz"
    assert r(text) == (
        "key=[REDACTED:llm.deepseek] cookie=[REDACTED:plugin.bili.cookie] other=[REDACTED]"
    )


def test_redacting_log_filter(caplog):
    logger = logging.getLogger("koi.test.redact")
    logger.addFilter(RedactingFilter(Redactor({DEEPSEEK_API_KEY: KEY})))
    with caplog.at_level(logging.INFO, logger="koi.test.redact"):
        logger.info("calling with %s", KEY)
    assert KEY not in caplog.text
    assert "[REDACTED:llm.deepseek]" in caplog.text
