from unittest.mock import patch

import pytest

from pmus.config import Settings
from pmus.dashboard import check_password


@pytest.mark.parametrize("bad_pwd", ["", "   "])
@pytest.mark.parametrize("env_val", [None, "", "0", "false", "no"])
def test_check_password_fails_closed_when_password_missing(monkeypatch, bad_pwd, env_val):
    if env_val is None:
        monkeypatch.delenv("PMUS_DASHBOARD_ALLOW_UNAUTH_LOCAL", raising=False)
    else:
        monkeypatch.setenv("PMUS_DASHBOARD_ALLOW_UNAUTH_LOCAL", env_val)

    settings = Settings(dashboard_password=bad_pwd)
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = {}
        assert check_password(settings) is False
        mock_st.error.assert_called_once_with(
            "Dashboard password not configured. Set PMUS_DASHBOARD_PASSWORD in .env."
        )
        mock_st.form.assert_not_called()


@pytest.mark.parametrize("allow_val", ["1", "true", "TRUE", " true "])
def test_check_password_allows_unauth_local_when_explicitly_opted_in(monkeypatch, allow_val):
    monkeypatch.setenv("PMUS_DASHBOARD_ALLOW_UNAUTH_LOCAL", allow_val)
    settings = Settings(dashboard_password="")
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = {}
        assert check_password(settings) is True
        mock_st.error.assert_not_called()


def test_check_password_enforces_configured_password_even_with_local_override(monkeypatch):
    monkeypatch.setenv("PMUS_DASHBOARD_ALLOW_UNAUTH_LOCAL", "1")
    settings = Settings(dashboard_password="strong_secret")
    with patch("pmus.dashboard.st") as mock_st:
        mock_st.session_state = {}
        mock_st.text_input.return_value = ""
        mock_st.form_submit_button.return_value = False
        assert check_password(settings) is False
        mock_st.error.assert_not_called()
