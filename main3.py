"""MCP server exposing AIBuddy / ZenseAI middleware tools.

Every tool is registered through @secure_tool. If a tool takes project_name or
workspace_name, the access check runs automatically before the tool body.
A tool with no scope arguments must be marked public=True or the server
fails at import time.

Tool order (most fundamental first):
  1. Identity and access    get_user_info, list_projects, get_project_workspaces,
                            validate_project_workspace_access
  2. Project data           get_dashboard_summary, list_project_transactions,
                            list_project_prompt_templates
  3. Workspace data         get_workspace_agents, get_workspace_tickets, search_zenseai
  4. Agent execution        run_agent
  5. Diagnostics            whoami
"""

import asyncio
import functools
import inspect
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable

import httpx
import jwt
import requests
import urllib3
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

mcp = FastMCP("MCP Tool Test")


# ---------------------------------------------------------------------------
# Configuration and HTTP helpers. All middleware calls go through these.
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return os.getenv("AIBUDDY_BASE_URL", "http://localhost:8080").rstrip("/")


def _default_timeout() -> int:
    return int(os.getenv("AIBUDDY_API_TIMEOUT_SECONDS", "60"))


def _verify_ssl() -> bool:
    # Default kept as before (no verification). Set AIBUDDY_VERIFY_SSL=true in production.
    return os.getenv("AIBUDDY_VERIFY_SSL", "false").strip().lower() == "true"


def _get_token() -> str:
    # The token is never accepted as a tool argument, so the model cannot
    # substitute another user's credentials.
    token = (os.getenv("AIBUDDY_JWT_TOKEN") or os.getenv("JWT_TOKEN") or "").strip()
    if not token:
        raise ValueError("AIBUDDY_JWT_TOKEN or JWT_TOKEN environment variable is not set.")
    return token


def _api_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout: int | None = None,
    check_status: bool = True,
) -> requests.Response:
    response = requests.request(
        method,
        f"{_base_url()}{path}",
        params=params,
        json=json_body,
        headers={"Authorization": f"Bearer {_get_token()}"},
        verify=_verify_ssl(),
        timeout=timeout or _default_timeout(),
    )
    if check_status:
        response.raise_for_status()
    return response


def _api_get(path: str, params: dict[str, Any] | None = None, timeout: int | None = None) -> Any:
    return _api_request("GET", path, params=params, timeout=timeout).json()


def _error(project_name: str, message: str) -> dict[str, Any]:
    return {"access": True, "project_name": project_name, "error": message}


# ---------------------------------------------------------------------------
# Authenticated user, projects, and the access gate
# ---------------------------------------------------------------------------

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


def _get_user_record() -> dict[str, Any]:
    email = _get_jwt_user_email(_get_token())
    if not email:
        raise ValueError("Unable to determine the authenticated user email from the configured JWT.")
    data = _api_get(os.getenv("MCP_GET_USER_PATH", "/crud/get_user"), {"user_email": email}, timeout=30)
    return data if isinstance(data, dict) else {}


def _extract_user_id(user: dict[str, Any]) -> int:
    for candidate in (user, user.get("user"), user.get("data")):
        if not isinstance(candidate, dict):
            continue
        raw_id = candidate.get("id") or candidate.get("user_id")
        if raw_id is not None:
            try:
                return int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"The user ID returned by the API is invalid: {raw_id}") from exc
    raise ValueError("Unable to find the authenticated user ID.")


def _project_name_of(project: Any) -> str | None:
    if not isinstance(project, dict):
        return None
    name = project.get("projectName") or project.get("project_name")
    return str(name) if name else None


def _user_project_names(user: dict[str, Any]) -> list[str]:
    names = (_project_name_of(p) for p in (user.get("projects") or []))
    return [name for name in names if name]


def _access_denied(project_name: str) -> dict[str, Any] | None:
    """Return an error payload if the user cannot access the project, otherwise None."""
    wanted = project_name.strip().lower()
    if any(name.lower() == wanted for name in _user_project_names(_get_user_record())):
        return None
    return {
        "access": False,
        "project_name": project_name,
        "message": f"You do not have access to project '{project_name}'.",
    }


def _user_workspace_uuids(user: dict[str, Any]) -> set[str] | None:
    """Return the workspace UUIDs the user belongs to, or None if the user record has no membership data.

    When None, access falls back to project membership only. Confirm the middleware's user record
    shape: if private workspaces exist, membership data should be present.
    """
    entries = user.get("workspaces")
    if not isinstance(entries, list):
        return None
    uuids: set[str] = set()
    for workspace in entries:
        if not isinstance(workspace, dict):
            continue
        uuid_value = workspace.get("uuid") or workspace.get("workspace_uuid")
        if uuid_value:
            uuids.add(str(uuid_value))
    return uuids


# ---------------------------------------------------------------------------
# Workspaces. A workspace name is only resolved inside the project named by the caller.
# ---------------------------------------------------------------------------

def _fetch_project_workspaces(project_name: str) -> list[dict[str, Any]]:
    data = _api_get(
        os.getenv("MCP_DETAIL_WORKSPACES_PATH", "/crud/detail_workspaces"),
        {"project_name": project_name},
        timeout=60,
    )
    raw = data.get("workspaces", data) if isinstance(data, dict) else data
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    return [
        {
            "workspace_id": ws.get("id"),
            "workspace_name": ws.get("name"),
            "workspace_uuid": ws.get("uuid"),
            "workspace_type": ws.get("workspace_type"),
            "workspace_status": ws.get("workspace_status"),
        }
        for ws in raw
        if isinstance(ws, dict)
    ]


def _validate_workspace(project_name: str, workspace_name: str) -> dict[str, Any]:
    """Check project access, then find the workspace inside that project only."""
    denied = _access_denied(project_name)
    if denied:
        return denied

    allowed_uuids = _user_workspace_uuids(_get_user_record())
    wanted = workspace_name.strip().lower()
    for ws in _fetch_project_workspaces(project_name):
        if str(ws["workspace_name"] or "").lower() != wanted:
            continue
        if allowed_uuids is not None and str(ws["workspace_uuid"]) not in allowed_uuids:
            break  # Workspace exists in the project, but the user is not a member.
        return {"access": True, "project_name": project_name, **ws}

    return {
        "access": False,
        "project_name": project_name,
        "message": f"Workspace '{workspace_name}' not found in project '{project_name}'.",
    }


# ---------------------------------------------------------------------------
# Tool registration with automatic access enforcement
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: list[dict[str, Any]] = []


def secure_tool(*, public: bool = False):
    """Register an MCP tool with automatic access enforcement.

    - project_name only: the user must have access to that project.
    - project_name and workspace_name: the workspace must belong to that
      project and be accessible to the user.
    - No scope arguments: the tool must be marked public=True, otherwise
      registration fails at import time.
    - workspace_name without project_name is rejected, because workspace
      names are only unique within a project.
    """

    def decorator(func: Callable[..., Any]):
        sig = inspect.signature(func)
        params = sig.parameters
        has_project = "project_name" in params
        has_workspace = "workspace_name" in params

        if not (has_project or has_workspace) and not public:
            raise RuntimeError(
                f"Tool '{func.__name__}' has no project_name or workspace_name. "
                f"Mark it public=True if it is intentionally unscoped."
            )
        if has_workspace and not has_project:
            raise RuntimeError(
                f"Tool '{func.__name__}' takes workspace_name without project_name. "
                f"Workspace names are only unique within a project."
            )
        if public and (has_project or has_workspace):
            raise RuntimeError(
                f"Tool '{func.__name__}' takes a project/workspace argument and must not be public=True."
            )

        def check(bound_arguments: dict[str, Any]) -> dict[str, Any] | None:
            if not has_project:
                return None
            project_name = bound_arguments.get("project_name")
            if not isinstance(project_name, str) or not project_name.strip():
                return {"access": False, "message": "project_name is required."}
            if has_workspace:
                workspace_name = bound_arguments.get("workspace_name")
                if not isinstance(workspace_name, str) or not workspace_name.strip():
                    return {"access": False, "message": "workspace_name is required."}
                result = _validate_workspace(project_name, workspace_name)
                return None if result.get("access") else result
            return _access_denied(project_name)

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                denied = await asyncio.to_thread(check, bound.arguments)
                if denied:
                    return denied
                return await func(*args, **kwargs)
        else:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                denied = check(bound.arguments)
                if denied:
                    return denied
                return func(*args, **kwargs)

        _TOOL_REGISTRY.append({
            "name": func.__name__,
            "project_scoped": has_project and not has_workspace,
            "workspace_scoped": has_workspace,
            "public": public,
        })
        return mcp.tool()(wrapper)

    return decorator


def audit_tool_access() -> dict[str, list[str]]:
    """Summarize which registered tools are project-scoped, workspace-scoped, or public."""
    return {
        "project_scoped": [t["name"] for t in _TOOL_REGISTRY if t["project_scoped"]],
        "workspace_scoped": [t["name"] for t in _TOOL_REGISTRY if t["workspace_scoped"]],
        "public": [t["name"] for t in _TOOL_REGISTRY if t["public"]],
    }


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------

_SUPPORTED_PERIODS = {
    "last_24_hours", "last_7_days", "last_14_days", "last_30_days",
    "last_90_days", "custom", "all",
}

# output key -> (source, daily API key, hourly API key)
# source is "dashboard" or "workspace" (the /crud/workspace_cost response)
_SUMMARY_SERIES: dict[str, tuple[str, str, str]] = {
    "all_transactions": ("dashboard", "allTransactionsByDay", "allTransactionsByHour"),
    "completed_transactions": ("dashboard", "completedTransactionsByDay", "completedTransactionsByHour"),
    "templates_created": ("dashboard", "templatesCreatedByDay", "templatesCreatedByHour"),
    "files": ("dashboard", "filesByDay", "filesByHour"),
    "lines_of_code": ("dashboard", "linesOfCodeByDay", "linesOfCodeByHour"),
    "total_tokens": ("dashboard", "totalTokensByDay", "totalTokensByHour"),
    "api_cost": ("dashboard", "costPerApiByDay", "costPerApiByHour"),
    "workspace_tokens": ("workspace", "workspaceTotalTokensByDay", "workspaceTotalTokensByHour"),
    "workspace_cost": ("workspace", "workspaceTotalCostByDay", "workspaceTotalCostByHour"),
}

# metric -> {output key: (source, API key)} for raw metric views
_METRIC_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "transactions": {
        "all_transactions_by_day": ("dashboard", "allTransactionsByDay"),
        "completed_transactions_by_day": ("dashboard", "completedTransactionsByDay"),
        "all_transactions_by_hour": ("dashboard", "allTransactionsByHour"),
        "completed_transactions_by_hour": ("dashboard", "completedTransactionsByHour"),
    },
    "templates": {
        "templates_created_by_day": ("dashboard", "templatesCreatedByDay"),
        "templates_created_by_hour": ("dashboard", "templatesCreatedByHour"),
    },
    "files": {
        "files_by_day": ("dashboard", "filesByDay"),
        "files_by_hour": ("dashboard", "filesByHour"),
    },
    "code": {
        "lines_of_code_by_day": ("dashboard", "linesOfCodeByDay"),
        "lines_of_code_by_hour": ("dashboard", "linesOfCodeByHour"),
    },
    "tokens": {
        "prompt_tokens_by_day": ("dashboard", "promptTokensByDay"),
        "completion_tokens_by_day": ("dashboard", "completionTokensByDay"),
        "total_tokens_by_day": ("dashboard", "totalTokensByDay"),
        "workspace_total_tokens_by_day": ("workspace", "workspaceTotalTokensByDay"),
        "workspace_total_tokens_by_hour": ("workspace", "workspaceTotalTokensByHour"),
    },
    "cost": {
        "api_cost_by_day": ("dashboard", "costPerApiByDay"),
        "workspace_total_cost_by_day": ("workspace", "workspaceTotalCostByDay"),
        "workspace_total_cost_by_hour": ("workspace", "workspaceTotalCostByHour"),
    },
    "playground": {
        "tokens_by_day": ("dashboard", "playgroundTokensByDay"),
        "cost_by_day": ("dashboard", "playgroundCostByDay"),
        "tokens_by_hour": ("dashboard", "playgroundTokensByHour"),
        "cost_by_hour": ("dashboard", "playgroundCostByHour"),
        "tokens_per_api_call_by_day": ("dashboard", "playgroundTokensPerApiCallByDay"),
        "cost_per_api_call_by_day": ("dashboard", "playgroundCostPerApiCallByDay"),
    },
}

_SUPPORTED_METRICS = {"summary", "all", *_METRIC_FIELDS}


def _normalize_period(period: str) -> str:
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


def _sum_numeric_values(data: Any) -> float:
    if not isinstance(data, dict):
        return 0
    return sum(float(v) for v in data.values() if isinstance(v, (int, float)))


def _build_daily_labels(period: str, start_date: str | None = None, end_date: str | None = None) -> set[str]:
    """Build API-style day labels such as 'Oct 15' for a period."""
    period_days = {"last_7_days": 7, "last_14_days": 14, "last_30_days": 30, "last_90_days": 90}

    if period == "custom":
        if not start_date or not end_date:
            raise ValueError("start_date and end_date are required when period is 'custom'.")
        try:
            range_start = datetime.strptime(start_date, "%Y-%m-%d").date()
            range_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("start_date and end_date must use YYYY-MM-DD format.") from exc
        if range_start > range_end:
            raise ValueError("start_date cannot be later than end_date.")
    elif period in period_days:
        range_end = date.today() - timedelta(days=1)
        range_start = range_end - timedelta(days=period_days[period] - 1)
    else:
        raise ValueError(f"Daily labels cannot be generated for period '{period}'.")

    labels: set[str] = set()
    current = range_start
    while current <= range_end:
        labels.add(current.strftime("%b %d").replace(" 0", " "))
        current += timedelta(days=1)
    return labels


def _filter_daily_metric(metric_data: Any, period: str, start_date=None, end_date=None) -> dict[str, Any]:
    if not isinstance(metric_data, dict):
        return {}
    if period == "all":
        return metric_data
    if period == "last_24_hours":
        return {}
    accepted = _build_daily_labels(period, start_date, end_date)
    return {label: value for label, value in metric_data.items() if label in accepted}


def _filter_hourly_metric(metric_data: Any, period: str) -> dict[str, Any]:
    if not isinstance(metric_data, dict):
        return {}
    if period == "all":
        return metric_data
    if period != "last_24_hours":
        return {}

    now = datetime.now()
    cutoff = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)

    filtered: dict[str, Any] = {}
    for label, value in metric_data.items():
        try:
            parsed = datetime.strptime(label, "%b %d %H:%M").replace(year=now.year)
        except ValueError:
            continue
        if cutoff <= parsed <= now:
            filtered[label] = value
    return filtered


def _select_series(
    dashboard_data: dict[str, Any],
    workspace_cost_data: dict[str, Any],
    period: str,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, dict[str, Any]]:
    sources = {"dashboard": dashboard_data, "workspace": workspace_cost_data}
    selected: dict[str, dict[str, Any]] = {}
    for out_key, (source, day_key, hour_key) in _SUMMARY_SERIES.items():
        data = sources[source]
        if period == "last_24_hours":
            selected[out_key] = _filter_hourly_metric(data.get(hour_key, {}), period)
        else:
            selected[out_key] = _filter_daily_metric(data.get(day_key, {}), period, start_date, end_date)
    return selected


def _build_dashboard_summary(
    dashboard_data: dict[str, Any],
    workspace_cost_data: dict[str, Any],
    period: str,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    s = _select_series(dashboard_data, workspace_cost_data, period, start_date, end_date)

    total = int(_sum_numeric_values(s["all_transactions"]))
    completed = int(_sum_numeric_values(s["completed_transactions"]))

    return {
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "templates_created": int(_sum_numeric_values(s["templates_created"])),
        "total_transactions": total,
        "completed_transactions": completed,
        "failed_or_incomplete_transactions": max(total - completed, 0),
        "completion_rate_percentage": round(completed / total * 100, 2) if total else 0,
        "transaction_files": int(_sum_numeric_values(s["files"])),
        "lines_of_code": int(_sum_numeric_values(s["lines_of_code"])),
        "dashboard_total_tokens": int(_sum_numeric_values(s["total_tokens"])),
        "dashboard_total_api_cost_usd": round(_sum_numeric_values(s["api_cost"]), 6),
        "workspace_total_tokens": int(_sum_numeric_values(s["workspace_tokens"])),
        "workspace_total_cost_usd": round(_sum_numeric_values(s["workspace_cost"]), 6),
    }


# ---------------------------------------------------------------------------
# Paged list helper shared by transactions and prompt templates
# ---------------------------------------------------------------------------

def _summarize_transaction(transaction: dict[str, Any]) -> dict[str, Any]:
    files = transaction.get("files")
    files = files if isinstance(files, list) else []

    prompt_tokens = completion_tokens = total_tokens = 0
    total_cost = 0.0
    for file_record in files:
        if not isinstance(file_record, dict):
            continue
        prompt_tokens += int(file_record.get("prompt_tokens") or 0)
        completion_tokens += int(file_record.get("completion_tokens") or 0)
        total_tokens += int(file_record.get("total_tokens") or 0)
        total_cost += float(file_record.get("costPerApi") or 0)

    return {
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
        "prompt_template_name": transaction.get("promptTemplateName"),
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_usd": round(total_cost, 6),
        },
    }


def _summarize_template(template: dict[str, Any]) -> dict[str, Any]:
    raw_labels = template.get("labels")
    raw_labels = raw_labels if isinstance(raw_labels, list) else []
    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "description": template.get("description"),
        "status": template.get("status"),
        "created_at": template.get("createdAt"),
        "created_by": template.get("createdBy"),
        "is_deleted": template.get("isDeleted"),
        "is_agent": bool(template.get("agent")),
        "labels": [
            {"id": label.get("id"), "name": label.get("labelName")}
            for label in raw_labels
            if isinstance(label, dict)
        ],
    }


def _list_project_records(
    project_name: str,
    *,
    endpoint: str,
    size_param: str,
    result_key: str,
    summarize: Callable[[dict[str, Any]], dict[str, Any]],
    page: int,
    page_size: int,
    search: str,
    search_filter: str,
    include_details: bool,
) -> dict[str, Any]:
    if page < 1:
        return _error(project_name, "page must be greater than or equal to 1.")
    if not 1 <= page_size <= 100:
        return _error(project_name, "page_size must be between 1 and 100.")

    normalized_filter = search_filter.strip().lower()
    if normalized_filter != "name":
        return _error(project_name, "The supported search filter is 'name'.")

    data = _api_get(
        endpoint,
        {
            "projectName": project_name,
            "page": page - 1,
            size_param: page_size,
            "search": search.strip(),
            "filter": normalized_filter,
        },
        timeout=60,
    )
    if not isinstance(data, dict):
        return _error(project_name, f"The API at {endpoint} returned an invalid response.")

    raw_items = data.get("content")
    raw_items = raw_items if isinstance(raw_items, list) else []

    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        raw_project = raw.get("project")
        owner = raw_project.get("projectName") if isinstance(raw_project, dict) else None
        if owner and str(owner).lower() != project_name.lower():
            continue
        items.append(raw if include_details else summarize(raw))

    return {
        "access": True,
        "project_name": project_name,
        "requested_page": page,
        "api_page": data.get("number"),
        "page_size": data.get("size"),
        "returned_count": len(items),
        "total_elements": data.get("totalElements"),
        "total_pages": data.get("totalPages"),
        "first_page": data.get("first"),
        "last_page": data.get("last"),
        "empty": data.get("empty"),
        "search": search,
        result_key: items,
    }


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------

_AGENT_FIELDS: dict[str, str] = {
    "id": "id",
    "agent_key": "agent_key",
    "agent_name": "agent_name",
    "agent_category": "agent_category",
    "short_description": "agent_short_description",
    "input_type": "input_type",
    "target_users": "target_users",
    "requires_codebase_index": "requires_codebase_index",
    "requires_knowledge_index": "requires_knowledge_index",
    "requires_ticket_data": "requires_ticket_data",
    "requires_deepdive_depgraph": "requires_deepdive_depgraph",
}


def _summarize_agent(agent: dict[str, Any]) -> dict[str, Any]:
    return {out_key: agent.get(src_key) for out_key, src_key in _AGENT_FIELDS.items()}


# ===========================================================================
# TOOLS, in workflow order. Every tool is registered with @secure_tool.
# ===========================================================================

# ---- 1. Identity and access -----------------------------------------------

@secure_tool(public=True)
def get_user_info() -> dict[str, Any]:
    """Get the current authenticated user record."""
    return _get_user_record()


@secure_tool(public=True)
def list_projects() -> dict[str, list[str]]:
    """List the names of projects the current user can access."""
    return {"projects": _user_project_names(_get_user_record())}


@secure_tool(public=True)
def list_my_workspaces() -> dict[str, Any]:
    """List every workspace the current user can access, grouped by project.

    Only projects in the user's own project list are queried, and when the user
    record includes workspace membership, only those workspaces are returned.
    """
    user = _get_user_record()
    allowed_uuids = _user_workspace_uuids(user)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for project_name in _user_project_names(user):
        workspaces = _fetch_project_workspaces(project_name)
        if allowed_uuids is not None:
            workspaces = [ws for ws in workspaces if str(ws["workspace_uuid"]) in allowed_uuids]
        grouped[project_name] = workspaces

    return {
        "projects": grouped,
        "project_count": len(grouped),
        "workspace_count": sum(len(ws_list) for ws_list in grouped.values()),
    }


@secure_tool()
def get_project_workspaces(project_name: str) -> dict[str, Any]:
    """Return all workspaces for a project the user can access."""
    return {
        "access": True,
        "project_name": project_name,
        "workspaces": _fetch_project_workspaces(project_name),
    }


@secure_tool()
def validate_project_workspace_access(project_name: str, workspace_name: str) -> dict[str, Any]:
    """Check that the user can access a workspace inside a project and return its IDs."""
    return _validate_workspace(project_name, workspace_name)


# ---- 2. Project data (dashboard, transactions, prompt templates) ----------

@secure_tool()
def get_dashboard_summary(
    project_name: str,
    metric: str = "summary",
    period: str = "last_7_days",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """
    Return dashboard metrics for an accessible project. Read-only: the dashboard
    is aggregated data, so it has no create, update, or delete.

    metric: summary, transactions, templates, files, code, tokens, cost, playground, all
    period: last_24_hours, last_7_days, last_14_days, last_30_days, last_90_days, custom, all
    """
    normalized_period = _normalize_period(period)
    if normalized_period not in _SUPPORTED_PERIODS:
        return _error(
            project_name,
            f"Unsupported period '{period}'. Supported periods: {', '.join(sorted(_SUPPORTED_PERIODS))}",
        )
    if normalized_period == "custom" and not (start_date and end_date):
        return _error(project_name, "start_date and end_date are required for a custom period.")

    normalized_metric = metric.strip().lower()
    if normalized_metric not in _SUPPORTED_METRICS:
        return _error(
            project_name,
            f"Unsupported metric '{metric}'. Supported metrics: {', '.join(sorted(_SUPPORTED_METRICS))}",
        )

    dashboard_data = _api_get("/crud/aibuddy/dashboard/data", {"projectName": project_name}, timeout=60)
    workspace_cost_data = _api_get("/crud/workspace_cost", {"project_name": project_name}, timeout=60)
    sources = {
        "dashboard": dashboard_data if isinstance(dashboard_data, dict) else {},
        "workspace": workspace_cost_data if isinstance(workspace_cost_data, dict) else {},
    }

    if normalized_metric == "summary":
        data: Any = _build_dashboard_summary(
            sources["dashboard"], sources["workspace"], normalized_period, start_date, end_date,
        )
    elif normalized_metric == "all":
        data = {"dashboard": sources["dashboard"], "workspace_cost": sources["workspace"]}
    else:
        data = {}

        for out_key, (source, api_key) in _METRIC_FIELDS[normalized_metric].items():
            metric_data = sources[source].get(api_key, {})

            if normalized_period == "all":
                data[out_key] = metric_data

            elif normalized_period == "last_24_hours":
                data[out_key] = _filter_hourly_metric(
                    metric_data,
                    normalized_period,
                )

            else:
                data[out_key] = _filter_daily_metric(
                    metric_data,
                    normalized_period,
                    start_date,
                    end_date,
                )

    return {
        "access": True,
        "project_name": project_name,
        "metric": normalized_metric,
        "period": normalized_period,
        "start_date": start_date,
        "end_date": end_date,
        "data": data,
    }
@secure_tool()
def list_project_transactions(
    project_name: str,
    page: int = 1,
    page_size: int = 10,
    search: str = "",
    search_filter: str = "name",
    include_details: bool = False,
) -> dict[str, Any]:
    """List transactions for an accessible project. Read-only for now."""
    return _list_project_records(
        project_name,
        endpoint="/crud/aibuddy/get_all_transactions",
        size_param="limit",
        result_key="transactions",
        summarize=_summarize_transaction,
        page=page,
        page_size=page_size,
        search=search,
        search_filter=search_filter,
        include_details=include_details,
    )


@secure_tool()
def list_project_prompt_templates(
    project_name: str,
    page: int = 1,
    page_size: int = 10,
    search: str = "",
    search_filter: str = "name",
    include_details: bool = False,
) -> dict[str, Any]:
    """List prompt templates for an accessible project. Read-only for now."""
    return _list_project_records(
        project_name,
        endpoint="/crud/aibuddy/get_prompt_templates",
        size_param="size",
        result_key="templates",
        summarize=_summarize_template,
        page=page,
        page_size=page_size,
        search=search,
        search_filter=search_filter,
        include_details=include_details,
    )


# ---- 3. Workspace data (agents, tickets, search) --------------------------

@secure_tool()
def get_workspace_agents(
    project_name: str,
    workspace_name: str,
    include_details: bool = False,
) -> dict[str, Any]:
    """Return agents available in a workspace of an accessible project."""
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace
    if not workspace.get("workspace_uuid"):
        return {
            "access": False,
            "message": f"Workspace UUID was not found for '{workspace_name}' in project '{project_name}'.",
        }

    user_id = _extract_user_id(_get_user_record())
    data = _api_get(
        "/dynamic_agents",
        {"projectName": project_name, "workspace_uuid": workspace["workspace_uuid"], "userId": user_id},
        timeout=_default_timeout(),
    )

    raw = data.get("dynamic_agents", []) if isinstance(data, dict) else []
    raw = raw if isinstance(raw, list) else []
    agents = [a for a in raw if isinstance(a, dict)]
    if not include_details:
        agents = [_summarize_agent(a) for a in agents]

    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace["workspace_name"],
        "workspace_id": workspace["workspace_id"],
        "workspace_uuid": workspace["workspace_uuid"],
        "agent_count": len(agents),
        "agents": agents,
    }


@secure_tool()
def get_workspace_tickets(project_name: str, workspace_name: str) -> dict[str, Any]:
    """
    Return tickets for one workspace. This is the only tool that returns tickets.
    The ticket endpoint is filtered by the workspace ID resolved inside the project.
    """
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace

    tickets = _api_get(
        "/ingestion/tickets/get_ticket_transactions",
        {"project_name": project_name, "workspace_id": workspace["workspace_id"]},
        timeout=60,
    )
    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace["workspace_name"],
        "workspace_id": workspace["workspace_id"],
        "workspace_uuid": workspace["workspace_uuid"],
        "tickets": tickets,
    }


@secure_tool()
def search_zenseai(query: str, project_name: str, workspace_name: str) -> dict[str, Any]:
    """
    Search code and knowledge inside one workspace. Tickets are excluded here;
    use get_workspace_tickets for tickets.
    """
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace

    user_id = _extract_user_id(_get_user_record())
    response = _api_request(
        "GET",
        "/crud/combined-search",
        params={
            "query": query,
            "code_search": "true",
            "knowledge_search": "true",
            "ticket_search": "false",
            "internet_search": "false",
            "api_id": os.getenv("ZENSEAI_API_ID", "246565"),
            "api_type": os.getenv("ZENSEAI_API_TYPE", "Azure-OpenAI"),
            "user_id": str(user_id),
            "workspace_uuid": workspace["workspace_uuid"],
            "project_name": project_name,
        },
        timeout=120,
        check_status=False,
    )
    return {"status_code": response.status_code, "response": response.text}


# ---- 4. Agent execution ----------------------------------------------------

@secure_tool(public=True)
async def run_agent(
    user_query: str,
    agent_name: str = "",
    conversation_id: str = "",
    parent_workflow_runid: str = "",
) -> dict[str, Any]:
    """Run a multi-agent workflow. Leave agent_name empty for automatic agent selection."""
    base_url = os.getenv("MAS_BASE_URL", "http://localhost:8000").rstrip("/")
    settings = {
        "MAS_WORKSPACE_UUID": os.getenv("MAS_WORKSPACE_UUID", ""),
        "MAS_PROJECT_NAME": os.getenv("MAS_PROJECT_NAME", ""),
        "MAS_USER_ID": os.getenv("MAS_USER_ID", ""),
        "MAS_API_ID": os.getenv("MAS_API_ID", ""),
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        return {
            "status": "failed",
            "workflow_runid": "",
            "response": f"Configure these MCP server environment variables: {', '.join(missing)}",
        }

    try:
        user_id = int(settings["MAS_USER_ID"])
        api_id = int(settings["MAS_API_ID"])
    except ValueError:
        return {"status": "failed", "workflow_runid": "", "response": "MAS_USER_ID and MAS_API_ID must be integers."}

    # The workflow runs in the configured project, so the user must have access to it.
    # run_agent takes no project argument, so this check is done here.
    try:
        denied = await asyncio.to_thread(_access_denied, settings["MAS_PROJECT_NAME"])
    except ValueError as exc:
        return {"status": "failed", "workflow_runid": "", "response": str(exc)}
    if denied:
        return {"status": "failed", "workflow_runid": "", "response": denied["message"]}

    payload = {
        "user_query": user_query,
        "mode": "predefined_workflow" if agent_name else "auto",
        "workspace_uuid": settings["MAS_WORKSPACE_UUID"],
        "project_name": settings["MAS_PROJECT_NAME"],
        "user_id": user_id,
        "api_id": api_id,
        "api_type": os.getenv("MAS_API_TYPE", "Azure-OpenAI"),
        "identifier": agent_name,
        "conversation_id": conversation_id or str(uuid.uuid4()),
        "parent_workflow_runid": parent_workflow_runid,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(210.0, connect=10.0)) as client:
            response = await client.post(f"{base_url}/mcp/run", json=payload)
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


# ---- 5. Diagnostics --------------------------------------------------------

@secure_tool(public=True)
async def whoami(ctx: Context) -> dict[str, Any]:
    """Return request context and whether an API token is configured. The token itself is never returned."""
    return {
        "server": "MCP Tool Test",
        "user_id": getattr(ctx, "user_id", None),
        "user_email": getattr(ctx, "user_email", None),
        "api_token_configured": bool(os.getenv("AIBUDDY_JWT_TOKEN") or os.getenv("JWT_TOKEN")),
        "conversation_id": getattr(ctx, "conversation_id", None),
        "parent_workflow_runid": getattr(ctx, "parent_workflow_runid", None),
    }


def main() -> None:
    # Audit goes to stderr: stdout is the MCP stdio channel and must stay clean.
    print(f"Tool access audit: {audit_tool_access()}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
