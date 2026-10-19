"""#988: the daily-limit link reports error=rate_limited; #980 keeps it."""
from careagents import feedback


def test_the_daily_limit_link_error_code_is_one_a_report_keeps():
    assert "rate_limited" in feedback.ERROR_CODES
