import json


def test_inspect_sample_dataset_returns_metadata_only(agent_module, sample_dataset_path):
    metadata = agent_module._inspect_dataset_file(sample_dataset_path, sample_dataset_path.name)

    assert metadata["source"] == sample_dataset_path.name
    assert metadata["file_type"] == "csv"
    assert metadata["row_count"] > 0
    assert metadata["sample_preview_rows"] <= agent_module.MAX_SAMPLE_ROWS
    assert "AAPL" in metadata["columns"]
    assert "MSFT" in metadata["columns"]
    assert metadata["dtypes"]["AAPL"].startswith("float")

    serialized = json.dumps(metadata)
    assert "2015-08-24" not in serialized
    assert len(metadata["sample_preview"]) <= agent_module.MAX_SAMPLE_ROWS


def test_load_dataset_data_uses_openapi_data_object(agent_module, sample_dataset_path):
    data = agent_module._load_dataset_data(sample_dataset_path)

    assert isinstance(data, dict)
    assert "AAPL" in data
    assert "MSFT" in data
    assert "Unnamed: 0" in data
    assert data["Unnamed: 0"]["0"] == "2015-08-03"
    assert isinstance(data["AAPL"]["0"], float)


def test_forecast_payload_matches_public_openapi_schema(agent_module, sample_dataset_path):
    data = agent_module._load_dataset_data(sample_dataset_path)
    arguments = agent_module._operation_arguments(
        "forecast",
        target_columns="AAPL,MSFT",
        horizon=5,
        prediction_intervals="0.8,0.95",
        feature_columns="AAPL__hl,MSFT__hl",
        run_explain=True,
    )
    payload = agent_module._public_operation_payload("forecast", data, arguments, background=True)
    summary = agent_module._summarize_public_payload(payload, sample_dataset_path.name)

    assert set(payload) == {"data", "config", "background"}
    assert payload["config"]["operation"] == "forecast"
    assert payload["config"]["operation_arguments"]["operation_type"] == "forecast"
    assert payload["config"]["operation_arguments"]["forecasting_horizon"] == 5
    assert payload["config"]["operation_arguments"]["targets"] == ["AAPL", "MSFT"]
    assert summary["data_columns"] == list(data.keys())
    assert "AAPL" in summary["data_columns"]
    assert "2015-08-03" not in json.dumps(summary)


def test_backtest_payload_matches_public_openapi_schema(agent_module, sample_dataset_path):
    data = agent_module._load_dataset_data(sample_dataset_path)
    arguments = agent_module._operation_arguments(
        "backtest",
        target_columns="AAPL",
        horizon=4,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=False,
        prediction_stride=4,
        backtest_size=20,
    )
    payload = agent_module._public_operation_payload("backtest", data, arguments, background=True)

    assert payload["config"]["operation"] == "backtest"
    assert payload["config"]["operation_arguments"]["operation_type"] == "backtest"
    assert payload["config"]["operation_arguments"]["prediction_stride"] == 4
    assert payload["config"]["operation_arguments"]["backtest_size"] == 20
    assert "start_date" not in payload["config"]["operation_arguments"]


def test_benchmark_payload_nests_backtest_config(agent_module, sample_dataset_path):
    data = agent_module._load_dataset_data(sample_dataset_path)
    backtest_config = agent_module._operation_arguments(
        "backtest",
        target_columns="AAPL",
        horizon=2,
        prediction_intervals="0.8,0.95",
        feature_columns="MSFT",
        run_explain=False,
        prediction_stride=2,
        backtest_size=10,
    )
    payload = agent_module._public_operation_payload(
        "benchmark",
        data,
        {"operation_type": "benchmark", "backtest_config": backtest_config},
        background=True,
    )

    assert payload["config"]["operation"] == "benchmark"
    assert payload["config"]["operation_arguments"]["operation_type"] == "benchmark"
    assert payload["config"]["operation_arguments"]["backtest_config"]["operation_type"] == "backtest"
    assert payload["background"] is True


def test_response_summaries_do_not_expose_inline_result_data(agent_module):
    response = {
        "status": "completed",
        "response": {
            "operation_type": "backtest",
            "session_id": "session-123",
            "resource_id": "response:session-123",
            "dataset_resource_id": "data:session-123",
            "data": {"large_result": [1, 2, 3]},
        },
    }

    summary = agent_module._summarize_api_response(response)

    assert summary["response"]["session_id"] == "session-123"
    assert summary["response"]["data_keys"] == ["large_result"]
    assert [1, 2, 3] not in summary["response"].values()


def test_session_id_can_be_extracted_from_nested_response_or_location(agent_module):
    assert agent_module._response_session_id({"response": {"session_id": "nested-123"}}) == "nested-123"
    assert (
        agent_module._response_session_id(
            {"location": "https://inait-saas-apim-jjyzmt7v.azure-api.net/v1/sessions/location-123/status"}
        )
        == "location-123"
    )
