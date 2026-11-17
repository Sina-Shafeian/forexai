import logging

from forexai.cli import SecretRedactingFormatter, redact_secrets
from forexai.config import load_settings


def test_logging_formatter_redacts_configured_and_telegram_url_secrets() -> None:
    token = "123456:ABC_secret"
    formatter = SecretRedactingFormatter("%(message)s", [token, "api-key"])
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "request https://api.telegram.org/bot123456:ABC_secret/send api-key",
        (),
        None,
    )
    rendered = formatter.format(record)
    assert token not in rendered
    assert "api-key" not in rendered
    assert "<redacted>" in rendered


def test_settings_repr_and_cli_error_text_do_not_expose_secrets() -> None:
    settings = load_settings().model_copy(deep=True)
    token = "123456:ABC_private_token"
    chat_id = "987654321"
    api_key = "private-api-key"
    settings.secrets.telegram_bot_token = token
    settings.secrets.telegram_chat_id = chat_id
    settings.secrets.fmp_api_key = api_key

    rendered_settings = repr(settings)
    rendered_error = redact_secrets(
        f"request bot{token}/send chat={chat_id} key={api_key}",
        [token, chat_id, api_key],
    )

    for secret in (token, chat_id, api_key):
        assert secret not in rendered_settings
        assert secret not in rendered_error
