import os
import uuid
from typing import Any
import httpx
import jwt
import requests
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import date, datetime, timedelta

load_dotenv()

mcp = FastMCP("MCP Tool Test")


def _get_env_token(token: str | None = None) -> str | None:
    configured = token or os.getenv("AIBUDDY_JWT_TOKEN") or os.getenv("JWT_TOKEN")
    if configured is None:
        return None
    configured = configured.strip()
    return configured or None


def _mask_token(token: str | None) -> str:
    if not token:
        return "not set"
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}"


def _get_jwt_user_email(token: str) -> str:
    try:
        payload = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    except Exception:
        return ""

    for key in ("sub", "email", "user_email", "preferred_username"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _middleware_base_url() -> str:
    return os.getenv("AIBUDDY_BASE_URL", "http://localhost:8080").rstrip("/")


def _middleware_json_get(path: str, token: str, params: dict[str, str] | None = None) -> Any:
    response = requests.get(
        f"{_middleware_base_url()}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=int(os.getenv("AIBUDDY_API_TIMEOUT_SECONDS", "30")),
        verify = False,
    )
    response.raise_for_status()
    return response.json()
def _authenticated_get(
    path: str,
    params: dict[str, Any] | None = None,
    timeout: int = 60,
) -> Any:
    """Send an authenticated GET request to the ZenseAI middleware."""

    token = _get_env_token()

    if not token:
        raise ValueError(
            "AIBUDDY_JWT_TOKEN or JWT_TOKEN environment variable is not set."
        )

    response = requests.get(
        f"{_middleware_base_url()}{path}",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
        },
        verify=False,
        timeout=timeout,
    )

    response.raise_for_status()
    return response.json()


@mcp.tool()
def get_env_variable(name: str) -> str:
    """Return the value of an environment variable."""
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"Environment variable '{name}' is not set.")
    return value


@mcp.tool()
def get_user_info() -> dict[str, Any]:
    """Get the current authenticated user record from the middleware CRUD API."""
    token = _get_env_token()
    if not token:
        raise ValueError("AIBUDDY_JWT_TOKEN or JWT_TOKEN environment variable is not set.")

    user_email = _get_jwt_user_email(token)
    if not user_email:
        raise ValueError("Unable to determine the authenticated user email from the configured JWT.")

    path = os.getenv("MCP_GET_USER_PATH", "/crud/get_user")
    return _middleware_json_get(path, token, {"user_email": user_email})


@mcp.tool()
def get_all_projects() -> dict[str, Any]:
    """Get all projects for the current authenticated user."""
    return get_user_projects_and_workspaces()


# @mcp.tool()
# def get_project_by_id(project_id: str) -> dict[str, Any]:
#     """Get a project by its ID from the API."""
#     token = _get_env_token()
#     if not token:
#         raise ValueError("AIBUDDY_JWT_TOKEN or JWT_TOKEN environment variable is not set.")
#     project_id = str(project_id)
#     response = requests.get(
#         f"{_middleware_base_url()}/crud/aibuddy/project/get_project/{project_id}",
#         headers={"Authorization": f"Bearer {token}"},
#         timeout=int(os.getenv("AIBUDDY_API_TIMEOUT_SECONDS", "30")),
#     )
#     response.raise_for_status()
#     return response.json()


@mcp.tool()
async def whoami(ctx: Context) -> dict[str, Any]:
    """Return sanitized request context information."""
    return get_context_info(ctx)


@mcp.tool()
async def get_context_info(ctx: Context) -> dict[str, Any]:
    """Return sanitized request context information, including the configured JWT state."""
    configured_token = _get_env_token()
    return {
        "server": "MCP Tool Test",
        "user_id": getattr(ctx, "user_id", None),
        "user_email": getattr(ctx, "user_email", None),
        "api_token_configured": bool(configured_token),
        "api_token_preview": _mask_token(configured_token),
        "conversation_id": getattr(ctx, "conversation_id", None),
        "parent_workflow_runid": getattr(ctx, "parent_workflow_runid", None),
    }


@mcp.tool()
def get_user_projects_and_workspaces(
    token: str | None = None,
    project_name: str | None = None,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Fetch the authenticated user and their project/workspace information from the middleware CRUD APIs."""
    effective_token = _get_env_token(token)
    if not effective_token:
        raise ValueError("AIBUDDY_JWT_TOKEN or JWT_TOKEN environment variable is not set.")

    effective_user_email = user_email or _get_jwt_user_email(effective_token)
    if not effective_user_email:
        raise ValueError("The configured JWT does not contain a usable user email. Provide user_email explicitly or use a JWT with a 'sub' or 'email' claim.")

    user_payload = _middleware_json_get(
        os.getenv("MCP_GET_USER_PATH", "/crud/get_user"),
        effective_token,
        {"user_email": effective_user_email},
    )

    projects = user_payload.get("projects", []) if isinstance(user_payload, dict) else []
    project_names: list[str] = []
    for project in projects:
        project_name_value = project.get("projectName") if isinstance(project, dict) else None
        if not project_name_value:
            project_name_value = project.get("project_name") if isinstance(project, dict) else None
        if project_name_value:
            project_names.append(str(project_name_value))

    if project_name:
        project_names = [project_name]

    workspace_payload: dict[str, Any] = {
        "user": user_payload,
        "projects": projects,
        "workspaces_by_project": {},
    }
    for name in project_names:
        workspace_path = os.getenv("MCP_DETAIL_WORKSPACES_PATH", "/crud/detail_workspaces")
        workspaces = _middleware_json_get(workspace_path, effective_token, {"project_name": name})
        project_workspaces = workspaces.get("workspaces", workspaces) if isinstance(workspaces, dict) else workspaces
        if not isinstance(project_workspaces, list):
            project_workspaces = [project_workspaces] if project_workspaces is not None else []
        workspace_payload["workspaces_by_project"][name] = project_workspaces
        workspace_payload.setdefault("workspaces", []).extend(project_workspaces)

    return workspace_payload
@mcp.tool()
def list_projects():
    payload = get_user_projects_and_workspaces()
    # list all the projects for the current user
    projects = []

    for project in payload.get("projects", []):
        name = (
            project.get("projectName")
            or project.get("project_name")
        )

        if name:
            projects.append(name)

    return {"projects": projects}
@mcp.tool()
def validate_workspace_access(
    workspace_name: str,
) -> dict[str, Any]:
    """Check whether the current user has access to a workspace."""

    payload = get_user_projects_and_workspaces()

    workspaces = payload.get("workspaces", [])

    for workspace in workspaces:
        if not isinstance(workspace, dict):
            continue

        name = (
            workspace.get("workspace_name")
            or workspace.get("workspaceName")
            or workspace.get("name")
        )

        if name and name.lower() == workspace_name.lower():
            return {
                "access": True,
                "workspace_name": name,
                "workspace_uuid": (
                    workspace.get("uuid")
                    or workspace.get("workspace_uuid")
                ),
                "workspace": workspace,
            }

    return {
        "access": False,
        "message": f"No access found for workspace '{workspace_name}'",
    }
def _sum_numeric_values(data: Any) -> float:
    """Return the sum of numeric values in a dictionary."""

    if not isinstance(data, dict):
        return 0

    total = 0.0

    for value in data.values():
        if isinstance(value, (int, float)):
            total += float(value)

    return total


def _latest_metric_entry(data: Any) -> dict[str, Any] | None:
    """
    Return the final entry received from the API.

    This preserves API response order. It does not parse dates because
    the API keys do not currently include the year.
    """

    if not isinstance(data, dict) or not data:
        return None

    label, value = next(reversed(data.items()))

    return {
        "label": label,
        "value": value,
    }
def _normalize_period(period: str) -> str:
    """Normalize common period names received from Copilot."""

    normalized = period.strip().lower().replace("-", "_").replace(" ", "_")

    aliases = {
        "24_hours": "last_24_hours",
        "1_day": "last_24_hours",
        "7_days": "last_7_days",
        "14_days": "last_14_days",
        "30_days": "last_30_days",
        "90_days": "last_90_days",
    }

    return aliases.get(normalized, normalized)


def _build_daily_labels(
    period: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> set[str]:
    """Build API-style labels such as 'Oct 15' for a period."""

    normalized_period = _normalize_period(period)

    period_days = {
        "last_7_days": 7,
        "last_14_days": 14,
        "last_30_days": 30,
        "last_90_days": 90,
    }

    if normalized_period == "custom":
        if not start_date or not end_date:
            raise ValueError(
                "start_date and end_date are required "
                "when period is 'custom'."
            )

        try:
            range_start = datetime.strptime(
                start_date,
                "%Y-%m-%d",
            ).date()

            range_end = datetime.strptime(
                end_date,
                "%Y-%m-%d",
            ).date()

        except ValueError as exc:
            raise ValueError(
                "start_date and end_date must use YYYY-MM-DD format."
            ) from exc

        if range_start > range_end:
            raise ValueError(
                "start_date cannot be later than end_date."
            )

    elif normalized_period in period_days:
        range_end = date.today() - timedelta(days=1)

        range_start = range_end - timedelta(
            days=period_days[normalized_period] - 1
        )

    else:
        raise ValueError(
            f"Daily labels cannot be generated for period '{period}'."
        )

    labels: set[str] = set()
    current_date = range_start

    while current_date <= range_end:
        labels.add(
            current_date.strftime("%b %d").replace(" 0", " ")
        )
        current_date += timedelta(days=1)

    return labels


def _filter_daily_metric(
    metric_data: Any,
    period: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Filter a day-based Dashboard dictionary."""

    if not isinstance(metric_data, dict):
        return {}

    normalized_period = _normalize_period(period)

    if normalized_period == "all":
        return metric_data

    if normalized_period == "last_24_hours":
        return {}

    accepted_labels = _build_daily_labels(
        period=normalized_period,
        start_date=start_date,
        end_date=end_date,
    )

    return {
        label: value
        for label, value in metric_data.items()
        if label in accepted_labels
    }
def _filter_hourly_metric(
    metric_data: Any,
    period: str,
) -> dict[str, Any]:
    """Filter hour-based metrics for Last 24 Hours."""

    if not isinstance(metric_data, dict):
        return {}

    normalized_period = _normalize_period(period)

    if normalized_period == "all":
        return metric_data

    if normalized_period != "last_24_hours":
        return {}

    cutoff = datetime.now().replace(
        minute=0,
        second=0,
        microsecond=0,
    ) - timedelta(hours=23)

    filtered: dict[str, Any] = {}

    for label, value in metric_data.items():
        try:
            parsed = datetime.strptime(
                label,
                "%b %d %H:%M",
            )

            parsed = parsed.replace(
                year=datetime.now().year,
            )
        except ValueError:
            continue

        if cutoff <= parsed <= datetime.now():
            filtered[label] = value

    return filtered
def _build_dashboard_summary(
    dashboard_data: dict[str, Any],
    workspace_cost_data: dict[str, Any],
    period: str = "last_7_days",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Build a compact project Dashboard summary for a selected period."""

    normalized_period = _normalize_period(period)

    # Last 24 Hours uses the hourly API fields.
    if normalized_period == "last_24_hours":
        transactions = _filter_hourly_metric(
            dashboard_data.get(
                "allTransactionsByHour",
                {},
            ),
            normalized_period,
        )

        completed_transactions = _filter_hourly_metric(
            dashboard_data.get(
                "completedTransactionsByHour",
                {},
            ),
            normalized_period,
        )

        templates = _filter_hourly_metric(
            dashboard_data.get(
                "templatesCreatedByHour",
                {},
            ),
            normalized_period,
        )

        files = _filter_hourly_metric(
            dashboard_data.get(
                "filesByHour",
                {},
            ),
            normalized_period,
        )

        lines_of_code = _filter_hourly_metric(
            dashboard_data.get(
                "linesOfCodeByHour",
                {},
            ),
            normalized_period,
        )

        total_tokens = _filter_hourly_metric(
            dashboard_data.get(
                "totalTokensByHour",
                {},
            ),
            normalized_period,
        )

        api_cost = _filter_hourly_metric(
            dashboard_data.get(
                "costPerApiByHour",
                {},
            ),
            normalized_period,
        )

        workspace_tokens = _filter_hourly_metric(
            workspace_cost_data.get(
                "workspaceTotalTokensByHour",
                {},
            ),
            normalized_period,
        )

        workspace_cost = _filter_hourly_metric(
            workspace_cost_data.get(
                "workspaceTotalCostByHour",
                {},
            ),
            normalized_period,
        )

    # All other periods use the daily API fields.
    else:
        transactions = _filter_daily_metric(
            dashboard_data.get(
                "allTransactionsByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        completed_transactions = _filter_daily_metric(
            dashboard_data.get(
                "completedTransactionsByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        templates = _filter_daily_metric(
            dashboard_data.get(
                "templatesCreatedByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        files = _filter_daily_metric(
            dashboard_data.get(
                "filesByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        lines_of_code = _filter_daily_metric(
            dashboard_data.get(
                "linesOfCodeByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        total_tokens = _filter_daily_metric(
            dashboard_data.get(
                "totalTokensByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        api_cost = _filter_daily_metric(
            dashboard_data.get(
                "costPerApiByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        workspace_tokens = _filter_daily_metric(
            workspace_cost_data.get(
                "workspaceTotalTokensByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

        workspace_cost = _filter_daily_metric(
            workspace_cost_data.get(
                "workspaceTotalCostByDay",
                {},
            ),
            normalized_period,
            start_date,
            end_date,
        )

    # Calculate totals from the filtered data.
    total_transaction_count = int(
        _sum_numeric_values(transactions)
    )

    completed_transaction_count = int(
        _sum_numeric_values(completed_transactions)
    )

    failed_or_incomplete_count = max(
        total_transaction_count - completed_transaction_count,
        0,
    )

    completion_rate = (
        round(
            completed_transaction_count
            / total_transaction_count
            * 100,
            2,
        )
        if total_transaction_count
        else 0
    )

    return {
        "period": normalized_period,
        "start_date": start_date,
        "end_date": end_date,
        "templates_created": int(
            _sum_numeric_values(templates)
        ),
        "total_transactions": total_transaction_count,
        "completed_transactions": completed_transaction_count,
        "failed_or_incomplete_transactions": (
            failed_or_incomplete_count
        ),
        "completion_rate_percentage": completion_rate,
        "transaction_files": int(
            _sum_numeric_values(files)
        ),
        "lines_of_code": int(
            _sum_numeric_values(lines_of_code)
        ),
        "dashboard_total_tokens": int(
            _sum_numeric_values(total_tokens)
        ),
        "dashboard_total_api_cost_usd": round(
            _sum_numeric_values(api_cost),
            6,
        ),
        "workspace_total_tokens": int(
            _sum_numeric_values(workspace_tokens)
        ),
        "workspace_total_cost_usd": round(
            _sum_numeric_values(workspace_cost),
            6,
        ),

        # Keep these temporarily to verify which dates were included.
        "filtered_dates": {
            "transactions": list(transactions.keys()),
            "completed_transactions": list(
                completed_transactions.keys()
            ),
            "templates": list(templates.keys()),
            "files": list(files.keys()),
            "lines_of_code": list(lines_of_code.keys()),
            "workspace_tokens": list(
                workspace_tokens.keys()
            ),
            "workspace_cost": list(
                workspace_cost.keys()
            ),
        },
    }
@mcp.tool()
def get_dashboard_summary(
    project_name: str,
    metric: str = "summary",
    period: str = "last_7_days",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """
    Return Dashboard metrics for an accessible project.

    Supported metrics:
    - summary
    - transactions
    - templates
    - files
    - code
    - tokens
    - cost
    - playground
    - all
    """

    if not user_has_project_access(project_name):
        return {
            "access": False,
            "project_name": project_name,
            "message": (
                f"You do not have access to project "
                f"'{project_name}'."
            ),
        }
     # 2. Add the period-validation block here
    normalized_period = _normalize_period(period)

    supported_periods = {
        "last_24_hours",
        "last_7_days",
        "last_14_days",
        "last_30_days",
        "last_90_days",
        "custom",
        "all",
    }

    if normalized_period not in supported_periods:
        return {
            "access": True,
            "project_name": project_name,
            "error": (
                f"Unsupported period '{period}'. "
                f"Supported periods: "
                f"{', '.join(sorted(supported_periods))}"
            ),
        }

    if normalized_period == "custom":
        if not start_date or not end_date:
            return {
                "access": True,
                "project_name": project_name,
                "error": (
                    "start_date and end_date are required "
                    "for a custom period."
                ),
            }
    dashboard_data = _authenticated_get(
        "/crud/aibuddy/dashboard/data",
        params={
            "projectName": project_name,
        },
    )

    workspace_cost_data = _authenticated_get(
        "/crud/workspace_cost",
        params={
            "project_name": project_name,
        },
    )

    if not isinstance(dashboard_data, dict):
        dashboard_data = {}

    if not isinstance(workspace_cost_data, dict):
        workspace_cost_data = {}

    normalized_metric = metric.strip().lower()

    supported_metrics = {
        "summary",
        "transactions",
        "templates",
        "files",
        "code",
        "tokens",
        "cost",
        "playground",
        "all",
    }

    if normalized_metric not in supported_metrics:
        return {
            "access": True,
            "project_name": project_name,
            "error": (
                f"Unsupported metric '{metric}'. "
                f"Supported metrics: "
                f"{', '.join(sorted(supported_metrics))}"
            ),
        }

    if normalized_metric == "transactions":
        result = {
            "all_transactions_by_day": dashboard_data.get(
                "allTransactionsByDay",
                {},
            ),
            "completed_transactions_by_day": dashboard_data.get(
                "completedTransactionsByDay",
                {},
            ),
            "all_transactions_by_hour": dashboard_data.get(
                "allTransactionsByHour",
                {},
            ),
            "completed_transactions_by_hour": dashboard_data.get(
                "completedTransactionsByHour",
                {},
            ),
        }

    elif normalized_metric == "templates":
        result = {
            "templates_created_by_day": dashboard_data.get(
                "templatesCreatedByDay",
                {},
            ),
            "templates_created_by_hour": dashboard_data.get(
                "templatesCreatedByHour",
                {},
            ),
        }

    elif normalized_metric == "files":
        result = {
            "files_by_day": dashboard_data.get(
                "filesByDay",
                {},
            ),
            "files_by_hour": dashboard_data.get(
                "filesByHour",
                {},
            ),
        }

    elif normalized_metric == "code":
        result = {
            "lines_of_code_by_day": dashboard_data.get(
                "linesOfCodeByDay",
                {},
            ),
            "lines_of_code_by_hour": dashboard_data.get(
                "linesOfCodeByHour",
                {},
            ),
        }

    elif normalized_metric == "tokens":
        result = {
            "prompt_tokens_by_day": dashboard_data.get(
                "promptTokensByDay",
                {},
            ),
            "completion_tokens_by_day": dashboard_data.get(
                "completionTokensByDay",
                {},
            ),
            "total_tokens_by_day": dashboard_data.get(
                "totalTokensByDay",
                {},
            ),
            "workspace_total_tokens_by_day": workspace_cost_data.get(
                "workspaceTotalTokensByDay",
                {},
            ),
            "workspace_total_tokens_by_hour": workspace_cost_data.get(
                "workspaceTotalTokensByHour",
                {},
            ),
        }

    elif normalized_metric == "cost":
        result = {
            "api_cost_by_day": dashboard_data.get(
                "costPerApiByDay",
                {},
            ),
            "workspace_total_cost_by_day": workspace_cost_data.get(
                "workspaceTotalCostByDay",
                {},
            ),
            "workspace_total_cost_by_hour": workspace_cost_data.get(
                "workspaceTotalCostByHour",
                {},
            ),
        }

    elif normalized_metric == "playground":
        result = {
            "tokens_by_day": dashboard_data.get(
                "playgroundTokensByDay",
                {},
            ),
            "cost_by_day": dashboard_data.get(
                "playgroundCostByDay",
                {},
            ),
            "tokens_by_hour": dashboard_data.get(
                "playgroundTokensByHour",
                {},
            ),
            "cost_by_hour": dashboard_data.get(
                "playgroundCostByHour",
                {},
            ),
            "tokens_per_api_call_by_day": dashboard_data.get(
                "playgroundTokensPerApiCallByDay",
                {},
            ),
            "cost_per_api_call_by_day": dashboard_data.get(
                "playgroundCostPerApiCallByDay",
                {},
            ),
        }

    elif normalized_metric == "all":
        result = {
            "dashboard": dashboard_data,
            "workspace_cost": workspace_cost_data,
        }

    else:
        result = _build_dashboard_summary(
            dashboard_data=dashboard_data,
            workspace_cost_data=workspace_cost_data,
            period=normalized_period,
            start_date=start_date,
            end_date=end_date,
        )

    return {
        "access": True,
        "project_name": project_name,
        "metric": normalized_metric,
        "period": normalized_period,
        "start_date": start_date,
        "end_date": end_date,
        "data": result,
    }
def _extract_user_id(user_payload: Any) -> int:
    """Extract the authenticated user's numeric ID from the API response."""

    if not isinstance(user_payload, dict):
        raise ValueError("The user API returned an unexpected response.")

    candidates = [
        user_payload,
        user_payload.get("user"),
        user_payload.get("data"),
    ]

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        user_id = candidate.get("id") or candidate.get("user_id")

        if user_id is not None:
            try:
                return int(user_id)
            except (TypeError, ValueError):
                raise ValueError(
                    f"The user ID returned by the API is invalid: {user_id}"
                )

    raise ValueError("Unable to find the authenticated user ID.")
@mcp.tool()
def user_has_project_access(project_name: str) -> bool:
    payload = get_user_projects_and_workspaces()

    for project in payload.get("projects", []):
        name = (
            project.get("projectName")
            or project.get("project_name")
        )

        if (
            name
            and name.lower() == project_name.lower()
        ):
            return True

    return False
@mcp.tool()
def list_project_transactions(
    project_name: str,
    page: int = 1,
    page_size: int = 10,
    search: str = "",
    search_filter: str = "name",
    include_details: bool = False,
) -> dict[str, Any]:
    """List transactions for an accessible project."""

    if not user_has_project_access(project_name):
        return {
            "access": False,
            "project_name": project_name,
            "message": (
                f"Access denied. You do not have permission "
                f"to access project '{project_name}'."
            ),
        }

    if page < 1:
        return {
            "access": True,
            "project_name": project_name,
            "error": "page must be greater than or equal to 1.",
        }

    if page_size < 1 or page_size > 100:
        return {
            "access": True,
            "project_name": project_name,
            "error": "page_size must be between 1 and 100.",
        }

    normalized_filter = search_filter.strip().lower()

    if normalized_filter != "name":
        return {
            "access": True,
            "project_name": project_name,
            "error": "The supported search filter is 'name'.",
        }

    response_data = _authenticated_get(
        "/crud/aibuddy/get_all_transactions",
        params={
            "projectName": project_name,
            "page": page - 1,
            "limit": page_size,
            "search": search.strip(),
            "filter": normalized_filter,
        },
        timeout=60,
    )

    if not isinstance(response_data, dict):
        return {
            "access": True,
            "project_name": project_name,
            "error": "The Transactions API returned an invalid response.",
        }

    raw_transactions = response_data.get("content", [])

    if not isinstance(raw_transactions, list):
        raw_transactions = []

    transactions: list[dict[str, Any]] = []

    for transaction in raw_transactions:
        if not isinstance(transaction, dict):
            continue

        transaction_project = transaction.get("project", {})

        returned_project_name = (
            transaction_project.get("projectName")
            if isinstance(transaction_project, dict)
            else None
        )

        if (
            returned_project_name
            and returned_project_name.lower() != project_name.lower()
        ):
            continue

        if include_details:
            transactions.append(transaction)
            continue

        files = transaction.get("files", [])
        if not isinstance(files, list):
            files = []

        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        total_cost = 0.0

        for file_record in files:
            if not isinstance(file_record, dict):
                continue

            prompt_tokens += int(
                file_record.get("prompt_tokens") or 0
            )
            completion_tokens += int(
                file_record.get("completion_tokens") or 0
            )
            total_tokens += int(
                file_record.get("total_tokens") or 0
            )
            total_cost += float(
                file_record.get("costPerApi") or 0
            )

        transactions.append({
            "id": transaction.get("id"),
            "transaction_id": transaction.get("transactionId"),
            "name": transaction.get("name"),
            "description": transaction.get("description"),
            "status": transaction.get("status"),
            "created_by": transaction.get("createdBy"),
            "created_at": transaction.get("createdAt"),
            "workspace_uuid": transaction.get("workspaceUuid"),
            "files_count": transaction.get("filesCount"),
            "folders_count": transaction.get("foldersCount"),
            "lines_count": transaction.get("linesCount"),
            "model_name": transaction.get("modelName"),
            "prompt_template_name": transaction.get(
                "promptTemplateName"
            ),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_usd": round(total_cost, 6),
            },
        })

    return {
        "access": True,
        "project_name": project_name,
        "requested_page": page,
        "api_page": response_data.get("number"),
        "page_size": response_data.get("size"),
        "returned_count": len(transactions),
        "total_elements": response_data.get("totalElements"),
        "total_pages": response_data.get("totalPages"),
        "first_page": response_data.get("first"),
        "last_page": response_data.get("last"),
        "search": search,
        "transactions": transactions,
    }
@mcp.tool()
def list_project_prompt_templates(
    project_name: str,
    page: int = 1,
    page_size: int = 10,
    search: str = "",
    search_filter: str = "name",
    include_details: bool = False,
) -> dict[str, Any]:
    """List prompt templates for an accessible project."""

    if not user_has_project_access(project_name):
        return {
            "access": False,
            "project_name": project_name,
            "message": (
                f"Access denied. You do not have permission "
                f"to access project '{project_name}'."
            ),
        }

    if page < 1:
        return {
            "access": True,
            "project_name": project_name,
            "error": "page must be greater than or equal to 1.",
        }

    if page_size < 1 or page_size > 100:
        return {
            "access": True,
            "project_name": project_name,
            "error": "page_size must be between 1 and 100.",
        }

    normalized_filter = search_filter.strip().lower()

    if normalized_filter != "name":
        return {
            "access": True,
            "project_name": project_name,
            "error": "The supported search filter is 'name'.",
        }

    response_data = _authenticated_get(
        "/crud/aibuddy/get_prompt_templates",
        params={
            "projectName": project_name,
            "search": search.strip(),
            "filter": normalized_filter,
            "page": page - 1,
            "size": page_size,
        },
        timeout=60,
    )

    if not isinstance(response_data, dict):
        return {
            "access": True,
            "project_name": project_name,
            "error": (
                "The Prompt Templates API returned an invalid response."
            ),
        }

    raw_templates = response_data.get("content", [])

    if not isinstance(raw_templates, list):
        raw_templates = []

    templates: list[dict[str, Any]] = []

    for template in raw_templates:
        if not isinstance(template, dict):
            continue

        template_project = template.get("project", {})

        returned_project_name = (
            template_project.get("projectName")
            if isinstance(template_project, dict)
            else None
        )

        if (
            returned_project_name
            and returned_project_name.lower() != project_name.lower()
        ):
            continue

        if include_details:
            templates.append(template)
            continue

        raw_labels = template.get("labels", [])
        if not isinstance(raw_labels, list):
            raw_labels = []

        labels = [
            {
                "id": label.get("id"),
                "name": label.get("labelName"),
            }
            for label in raw_labels
            if isinstance(label, dict)
        ]

        templates.append({
            "id": template.get("id"),
            "name": template.get("name"),
            "description": template.get("description"),
            "status": template.get("status"),
            "created_at": template.get("createdAt"),
            "created_by": template.get("createdBy"),
            "is_deleted": template.get("isDeleted"),
            "is_agent": bool(template.get("agent")),
            "labels": labels,
        })

    return {
        "access": True,
        "project_name": project_name,
        "requested_page": page,
        "api_page": response_data.get("number"),
        "page_size": response_data.get("size"),
        "returned_count": len(templates),
        "total_elements": response_data.get("totalElements"),
        "total_pages": response_data.get("totalPages"),
        "first_page": response_data.get("first"),
        "last_page": response_data.get("last"),
        "empty": response_data.get("empty"),
        "search": search,
        "templates": templates,
    }
def get_workspace_tickets(
    project_name: str,
    workspace_name: str,
) -> dict[str, Any]:
    """Get tickets for a workspace."""

    validation = validate_project_workspace_access(
        project_name,
        workspace_name
    )

    if not validation.get("access"):
        return validation

    workspace_id = validation["workspace_id"]

    token = _get_env_token()

    response = requests.get(
        f"{_middleware_base_url()}/ingestion/tickets/get_ticket_transactions",
        params={
            "project_name": project_name,
            "workspace_id": workspace_id,
        },
        headers={
            "Authorization": f"Bearer {token}"
        },
        verify=False,
        timeout=60,
    )

    return {
        "status_code": response.status_code,
        "response": response.json(),
    }
@mcp.tool()
def get_workspace_agents(
    project_name: str,
    workspace_name: str,
    include_details: bool = False,
) -> dict[str, Any]:
    """Return agents available in a project workspace."""

    validation = validate_project_workspace_access(
        project_name=project_name,
        workspace_name=workspace_name,
    )

    if not validation.get("access"):
        return validation

    workspace_uuid = validation.get("workspace_uuid")

    if not workspace_uuid:
        return {
            "access": False,
            "message": (
                f"Workspace UUID was not found for "
                f"'{workspace_name}' in project '{project_name}'."
            ),
        }

    token = _get_env_token()

    if not token:
        raise ValueError(
            "AIBUDDY_JWT_TOKEN or JWT_TOKEN environment variable is not set."
        )

    user_payload = get_user_info()
    user_id = _extract_user_id(user_payload)

    response = requests.get(
        f"{_middleware_base_url()}/dynamic_agents",
        params={
            "projectName": project_name,
            "workspace_uuid": workspace_uuid,
            "userId": user_id,
        },
        headers={
            "Authorization": f"Bearer {token}",
        },
        verify=False,
        timeout=int(
            os.getenv("AIBUDDY_API_TIMEOUT_SECONDS", "60")
        ),
    )

    response.raise_for_status()

    response_data = response.json()

    raw_agents = (
        response_data.get("dynamic_agents", [])
        if isinstance(response_data, dict)
        else []
    )

    if not isinstance(raw_agents, list):
        raw_agents = []

    if include_details:
        agents = raw_agents
    else:
        agents = []

        for agent in raw_agents:
            if not isinstance(agent, dict):
                continue

            agents.append({
                "id": agent.get("id"),
                "agent_key": agent.get("agent_key"),
                "agent_name": agent.get("agent_name"),
                "agent_category": agent.get("agent_category"),
                "short_description": agent.get(
                    "agent_short_description"
                ),
                "input_type": agent.get("input_type"),
                "target_users": agent.get("target_users"),
                "requires_codebase_index": agent.get(
                    "requires_codebase_index"
                ),
                "requires_knowledge_index": agent.get(
                    "requires_knowledge_index"
                ),
                "requires_ticket_data": agent.get(
                    "requires_ticket_data"
                ),
                "requires_deepdive_depgraph": agent.get(
                    "requires_deepdive_depgraph"
                ),
            })

    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_id": validation.get("workspace_id"),
        "workspace_uuid": workspace_uuid,
        "user_id": user_id,
        "agent_count": len(agents),
        "agents": agents,
    }
@mcp.tool()
def get_project_workspaces(
    project_name: str,
) -> dict[str, Any]:
    """Return all workspaces for a project."""
    if not user_has_project_access(project_name):
        return {
            "access": False,
            "message": (
                f"You do not have access to project "
                f"'{project_name}'"
            )
        }

    token = _get_env_token()

    response = requests.get(
        f"{_middleware_base_url()}/crud/detail_workspaces",
        params={"project_name": project_name},
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=60,
    )

    response.raise_for_status()

    data = response.json()

    workspaces = []

    for ws in data.get("workspaces", []):
        workspaces.append({
            "workspace_id": ws.get("id"),
            "workspace_name": ws.get("name"),
            "workspace_uuid": ws.get("uuid"),
            "workspace_type": ws.get("workspace_type"),  # Public / Private
            "workspace_status": ws.get("workspace_status"),
        })
    return {
        "project_name": project_name,
        "workspaces": workspaces,
    }
@mcp.tool()
def validate_project_workspace_access(
    project_name: str,
    workspace_name: str,
) -> dict[str, Any]:
    """Validate user access to a workspace within a project."""
    project_data = get_project_workspaces(project_name)
    if project_data.get("access") is False:
        return project_data
    workspaces = project_data.get("workspaces", [])
    for workspace in workspaces:
        name =(
             workspace.get("workspace_name")
             or workspace.get("name")
       )
        if (
            name
            and name.lower() == workspace_name.lower()
        ):
            return {
                "access": True,
                "project_name": project_name,
                "workspace_id": workspace.get("workspace_id"),
                "workspace_name": name,
                "workspace_uuid": (workspace.get("uuid")
                                   or workspace.get("workspace_uuid")
                ),
                "workspace": workspace,
            }

    return {
        "access": False,
        "message": (
            f"Workspace '{workspace_name}' "
            f"not found in project '{project_name}'"
        ),
    }
@mcp.tool()
def search_zenseai(
    query: str,
    workspace_name: str,
) -> dict[str, Any]:
    """Search ZenseAI after validating workspace access."""

    access_result = validate_workspace_access(workspace_name)

    if not access_result.get("access"):
        return access_result

    workspace_uuid = access_result["workspace_uuid"]

    user_info = get_user_info()

    user_id = user_info.get("id") or user_info.get("user_id")

    params = {
        "query": query,
        "code_search": "true",
        "knowledge_search": "true",
        "ticket_search": "true",
        "internet_search": "false",
        "api_id": "246565",
        "api_type": "Azure-OpenAI",
        "user_id": str(user_id),
        "workspace_uuid": workspace_uuid,
        "project_name": "HLS AI Hub"
    }

    token = _get_env_token()

    response = requests.get(
        f"{_middleware_base_url()}/crud/combined-search",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        verify=False,
        timeout=120,
    )

    return {
        "status_code": response.status_code,
        "response": response.text
    }
@mcp.tool()
def echo(token: str) -> str:
    """Return the input token."""
    print(f"Echoing token: {token}")
    return token


@mcp.tool()
def greet(name: str) -> str:
    """Return a greeting for a person."""
    return f"Hello, {name}!"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the result."""
    return a + b


@mcp.tool()
async def run_agent(
    user_query: str,
    agent_name: str = "",
    conversation_id: str = "",
    parent_workflow_runid: str = "",
) -> dict[str, Any]:
    """Run a multi-agent workflow. Leave agent_name empty for automatic agent selection."""
    base_url = os.getenv("MAS_BASE_URL", "http://localhost:8000").rstrip("/")
    required_settings = {
        "MAS_WORKSPACE_UUID": os.getenv("MAS_WORKSPACE_UUID", ""),
        "MAS_PROJECT_NAME": os.getenv("MAS_PROJECT_NAME", ""),
        "MAS_USER_ID": os.getenv("MAS_USER_ID", ""),
        "MAS_API_ID": os.getenv("MAS_API_ID", ""),
    }
    missing_settings = [name for name, value in required_settings.items() if not value]
    if missing_settings:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": f"Configure these MCP server environment variables: {', '.join(missing_settings)}",
        }

    try:
        user_id = int(required_settings["MAS_USER_ID"])
        api_id = int(required_settings["MAS_API_ID"])
    except ValueError:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": "MAS_USER_ID and MAS_API_ID must be integers.",
        }

    payload = {
        "user_query": user_query,
        "mode": "predefined_workflow" if agent_name else "auto",
        "workspace_uuid": required_settings["MAS_WORKSPACE_UUID"],
        "project_name": required_settings["MAS_PROJECT_NAME"],
        "user_id": user_id,
        "api_id": api_id,
        "api_type": os.getenv("MAS_API_TYPE", "Azure-OpenAI"),
        "identifier": agent_name,
        "conversation_id": conversation_id or str(uuid.uuid4()),
        "parent_workflow_runid": parent_workflow_runid,
    }

    endpoint = f"{base_url}/mcp/run"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(210.0, connect=10.0)) as client:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": "The multi-agent workflow timed out. Check the MAS service logs and try again.",
        }
    except httpx.HTTPStatusError as exc:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": f"The multi-agent service returned HTTP {exc.response.status_code}.",
        }
    except httpx.RequestError as exc:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": f"Could not reach the multi-agent service at {base_url}: {exc}.",
        }
    except ValueError:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": "The multi-agent service returned an invalid JSON response.",
        }


def main() -> None:
    mcp.run(transport="stdio")
    # mcp.run(transport="http")


if __name__ == "__main__":
    main()
