import sys


def test_offline_e2e_evaluations_pass(repo_root, sample_dataset_path):
    sys.path.insert(0, str(repo_root))
    try:
        from evals.futurecomplete_e2e_eval import run_offline_evals

        results = run_offline_evals(sample_dataset_path)
    finally:
        if str(repo_root) in sys.path:
            sys.path.remove(str(repo_root))

    assert results
    assert all(result.passed for result in results), [result for result in results if not result.passed]
    assert {result.name for result in results} == {
        "trial_backtest_flow",
        "full_license_forecast_and_benchmark_flow",
        "cancel_running_job_flow",
    }
