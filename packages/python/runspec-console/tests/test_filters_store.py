from runspec_console.filters_store import load_filters, save_filters


def test_round_trip(tmp_path):
    path = tmp_path / "runspec_filters.toml"
    filters = [
        {
            "name": "id-lines",
            "pipeline": "grep -F -- {{arg}} | cut -d, -f1,4",
            "description": "Lines containing the txn id, keep cols 1 & 4",
            "param_label": "transaction id",
        },
        {"name": "errors", "pipeline": "grep -i error"},
    ]
    save_filters(path, filters)
    loaded = load_filters(path)
    assert loaded == filters


def test_round_trip_preserves_quotes_and_backslashes(tmp_path):
    # sed/awk pipelines routinely carry " and \ — they must survive the round-trip.
    path = tmp_path / "f.toml"
    filters = [
        {"name": "tabs", "pipeline": r'sed "s/\t/,/g" | grep "value"'},
    ]
    save_filters(path, filters)
    assert load_filters(path) == filters


def test_missing_file_returns_empty(tmp_path):
    assert load_filters(tmp_path / "nope.toml") == []


def test_optional_fields_omitted(tmp_path):
    path = tmp_path / "f.toml"
    save_filters(
        path, [{"name": "a", "pipeline": "cat", "description": "", "param_label": ""}]
    )
    loaded = load_filters(path)
    assert loaded == [{"name": "a", "pipeline": "cat"}]
