from pathlib import Path


STRATEGY = Path("freqtrade_lab/strategies/HermesPerpStrategy.py")
LOOP = Path("src/core/loop.py")


def test_freqtrade_confirm_trade_entry_signature_matches_2026_4():
    text = STRATEGY.read_text()
    expected = (
        "def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,\n"
        "                            time_in_force: str, current_time: datetime,\n"
        "                            entry_tag: str | None, side: str, **kwargs) -> bool:"
    )
    assert expected in text


def test_freqtrade_intents_are_not_immediately_expired_and_are_atomic():
    text = STRATEGY.read_text()
    assert "EMIT_INTENTS = os.environ.get(\"HERMES_EMIT_INTENTS\", \"0\") == \"1\"" in text
    assert "if not EMIT_INTENTS:" in text
    assert "expires_at = current_time + timedelta(seconds=60)" in text
    assert "tmp_path.write_text(json.dumps(intent, indent=2))" in text
    assert "tmp_path.replace(intent_path)" in text


def test_freqtrade_snapshot_reloads_and_rejects_stale_data():
    text = STRATEGY.read_text()
    assert "_ext_snapshot_mtime" in text
    assert "snapshot_max_age_sec = 180" in text
    assert "if age.total_seconds() > self.snapshot_max_age_sec:" in text


def test_hermes_file_intent_import_quarantines_bad_json():
    text = LOOP.read_text()
    assert "invalid_dir = intent_dir / \"invalid\"" in text
    assert "f.rename(invalid_dir / f.name)" in text
    assert "Skipped duplicate intent" in text
