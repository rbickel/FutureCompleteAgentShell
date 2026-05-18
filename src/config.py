"""
Copyright (c) Microsoft Corporation. All rights reserved.
Licensed under the MIT License.
"""

class Config:
    """Agent Configuration"""

    def __init__(self, env):
        self.PORT = 3978
        self.azure_openai_api_key = env["AZURE_OPENAI_API_KEY"] # Azure OpenAI API key
        self.azure_openai_deployment_name = env["AZURE_OPENAI_DEPLOYMENT_NAME"] # Azure OpenAI model deployment name
        self.azure_openai_endpoint = env["AZURE_OPENAI_ENDPOINT"] # Azure OpenAI endpoint
        self.futurecomplete_api_base_url = env.get("FUTURECOMPLETE_API_BASE_URL") or "https://inait-saas-apim-jjyzmt7v.azure-api.net"
        self.futurecomplete_dashboard_url = env.get("FUTURECOMPLETE_DASHBOARD_URL") or "https://futurecomplete.inait.ai"
        self.futurecomplete_trial_users_url = env.get(
            "FUTURECOMPLETE_TRIAL_USERS_URL",
            "https://api.forecasting.inait.ai/users/dev/users",
        ) or "https://api.forecasting.inait.ai/users/dev/users"
        self.futurecomplete_trial_plan_id = env.get("FUTURECOMPLETE_TRIAL_PLAN_ID") or "trial"
        self.futurecomplete_job_status_path_template = env.get("FUTURECOMPLETE_JOB_STATUS_PATH_TEMPLATE") or "/v1/sessions/{session_id}/status"
        self.futurecomplete_job_result_path_template = env.get("FUTURECOMPLETE_JOB_RESULT_PATH_TEMPLATE") or "/v1/sessions/{session_id}/result"
        self.futurecomplete_job_cancel_path_template = env.get("FUTURECOMPLETE_JOB_CANCEL_PATH_TEMPLATE") or "/v1/sessions/{session_id}/cancel"
        self.futurecomplete_poll_interval_seconds = int(env.get("FUTURECOMPLETE_POLL_INTERVAL_SECONDS") or "10")
        self.futurecomplete_poll_max_attempts = int(env.get("FUTURECOMPLETE_POLL_MAX_ATTEMPTS") or "90")
