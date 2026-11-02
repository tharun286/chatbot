"""MCP server exposing ZenseAI ingestion tools (knowledge base, code space, tickets).

Every tool is registered through @secure_tool. If a tool takes project_name or
workspace_name, the access check runs automatically before the tool body.
A tool with no scope arguments must be marked public=True or the server
fails at import time.

Tool order (most fundamental first):
  1. Identity and access    get_user_info, list_projects, list_my_workspaces,
                            get_project_workspaces, validate_project_workspace_access
  2. Tickets                get_workspace_tickets, create_workspace_ticket,
                            delete_workspace_ticket
  3. Diagnostics            whoami
"""
import json
import asyncio
import functools
import inspect
import os
import sys
from typing import Any, Callable

import jwt
import requests
import urllib3
from dotenv import load_dotenv
from fastmcp import Context, FastMCP

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

mcp = FastMCP("ZenseAI Ingestion MCP")

# ---------------------------------------------------------------------------
# Configuration and HTTP helpers. All middleware calls go through these.
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return os.getenv("AIBUDDY_BASE_URL", "http://localhost:10001").rstrip("/")


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


def _api_multipart_request(
    method: str,
    path: str,
    *,
    data: dict[str, Any],
    files: dict[str, Any],
    timeout: int | None = None,
) -> requests.Response:
    response = requests.request(
        method,
        f"{_base_url()}{path}",
        data=data,
        files=files,
        headers={
            "Authorization": f"Bearer {_get_token()}"
        },
        verify=_verify_ssl(),
        timeout=timeout or _default_timeout(),
    )

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
            "workspace_description": ws.get("description"),
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


# ---- 2. Tickets ------------------------------------------------------------

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
def create_workspace_ticket(
    project_name: str,
    workspace_name: str,
    ticket_name: str,
    description: str,
    input_file_path: str,
    input_columns: str,
    model_name: str = "gpt-5-nano-model",
    prompt_version: str = "default",
    rows_to_run: int = 1,
    include_previous_data: bool = False,
    is_batch: bool = False,
) -> dict[str, Any]:
    """Create a ticket in an accessible workspace from a local Excel file."""

    # workspace access is already checked by @secure_tool,
    # but we need the resolved workspace ID.
    workspace = _validate_workspace(
        project_name,
        workspace_name,
    )

    if not workspace.get("access"):
        return workspace

    workspace_id = workspace.get("workspace_id")

    if workspace_id is None:
        return _error(
            project_name,
            f"Workspace ID was not found for '{workspace_name}'.",
        )

    ticket_name = ticket_name.strip()
    description = description.strip()
    input_file_path = input_file_path.strip()
    input_columns = input_columns.strip()

    if not ticket_name:
        return _error(
            project_name,
            "ticket_name cannot be blank.",
        )

    if not description:
        return _error(
            project_name,
            "description cannot be blank.",
        )

    if not input_columns:
        return _error(
            project_name,
            "input_columns cannot be blank.",
        )

    if rows_to_run <= 0:
        return _error(
            project_name,
            "rows_to_run must be greater than 0.",
        )

    if not os.path.isfile(input_file_path):
        return _error(
            project_name,
            f"Input file was not found: {input_file_path}",
        )

    extension = os.path.splitext(input_file_path)[1].lower()

    if extension not in {".xlsx", ".xls"}:
        return _error(
            project_name,
            "The input file must be an XLSX or XLS file.",
        )

    try:
        user_id = _extract_user_id(
            _get_user_record()
        )
    except ValueError as exc:
        return _error(
            project_name,
            str(exc),
        )

    output_criteria = [
        {
            "columnName": "Incident Summary",
            "columnPrompt": (
                "Provide a detailed, contextual summary of the "
                "incident from start to finish."
            ),
            "columnDescription": (
                "Provide a detailed contextual summary of the incident."
            ),
        },
        {
            "columnName": "Incident Category",
            "columnPrompt": (
                "Determine the category of the incident."
            ),
            "columnDescription": (
                "Determine the incident category."
            ),
        },
        {
            "columnName": "Module",
            "columnPrompt": (
                "Identify the module or topic within the system "
                "or application that the incident relates to."
            ),
            "columnDescription": "Determine the topic.",
        },
        {
            "columnName": "RCA",
            "columnPrompt": (
                "Identify and document the underlying factors "
                "contributing to the issue."
            ),
            "columnDescription": (
                "Summarize the root cause of the issue."
            ),
        },
        {
            "columnName": "Resolution",
            "columnPrompt": (
                "Describe the resolution and actions taken "
                "to resolve the issue."
            ),
            "columnDescription": (
                "Describe the resolution."
            ),
        },
    ]

    form_data = {
        "name": ticket_name,
        "ticket_name": ticket_name.replace("_", " "),
        "description": description,
        "inputColumns": input_columns,
        "PreviousPromptVersions": prompt_version,
        "includePreviousData": (
            "true" if include_previous_data else "false"
        ),
        "outputCriteria": json.dumps(output_criteria),
        "projectName": project_name,
        "rowsToRun": str(rows_to_run),
        "createdBy": str(user_id),
        "apimodel": model_name,
        "is_batch": "true" if is_batch else "false",
        "dataSource": "local",
    }

    try:
        with open(input_file_path, "rb") as input_file:
            files = {
                "inputFile": (
                    os.path.basename(input_file_path),
                    input_file,
                )
            }

            response = _api_multipart_request(
                "POST",
                f"/ingestion/tickets/upload_workspace_ticket/{workspace_id}",
                data=form_data,
                files=files,
                timeout=120,
            )

        result = response.json()

    except requests.HTTPError as exc:
        status_code = (
            exc.response.status_code
            if exc.response is not None
            else None
        )

        return _error(
            project_name,
            f"Ticket creation failed with HTTP {status_code}.",
        )

    except OSError as exc:
        return _error(
            project_name,
            f"Could not read the input file: {exc}",
        )

    except ValueError:
        return _error(
            project_name,
            "Ticket creation API returned an invalid JSON response.",
        )

    created = (
        result.get("data", {})
        if isinstance(result, dict)
        else {}
    )

    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "ticket_id": created.get("ticket_id"),
        "ticket_name": ticket_name,
        "filename": os.path.basename(input_file_path),
        "message": (
            result.get("message")
            if isinstance(result, dict)
            else "Ticket upload started."
        ),
        "estimated_processing_time": (
            created.get("estimated_processing_time")
        ),
    }


@secure_tool()
def delete_workspace_ticket(
    project_name: str,
    workspace_name: str,
    ticket_id: int,
) -> dict[str, Any]:
    """Delete an existing ticket from an accessible workspace."""

    if ticket_id <= 0:
        return _error(
            project_name,
            "ticket_id must be greater than 0.",
        )

    # @secure_tool already validates workspace access,
    # but we need the resolved workspace ID for the delete API.
    workspace = _validate_workspace(
        project_name,
        workspace_name,
    )

    if not workspace.get("access"):
        return workspace

    workspace_id = workspace.get("workspace_id")

    if workspace_id is None:
        return _error(
            project_name,
            f"Workspace ID was not found for '{workspace_name}'.",
        )

    payload = {
        "ticket_id": ticket_id,
        "workspace_id": workspace_id,
    }

    try:
        response = _api_request(
            "DELETE",
            "/ingestion/tickets/delete_workspace_ticket",
            json_body=payload,
            timeout=60,
        )

        data = response.json()

    except requests.HTTPError as exc:
        status_code = (
            exc.response.status_code
            if exc.response is not None
            else None
        )

        return _error(
            project_name,
            f"Ticket deletion failed with HTTP {status_code}.",
        )

    except ValueError:
        return _error(
            project_name,
            "Ticket deletion API returned an invalid JSON response.",
        )

    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "ticket_id": ticket_id,
        "message": (
            data.get("message")
            if isinstance(data, dict)
            else "Ticket deleted successfully."
        ),
    }


# ---- 3. Diagnostics --------------------------------------------------------

@secure_tool(public=True)
async def whoami(ctx: Context) -> dict[str, Any]:
    """Return request context and whether an API token is configured. The token itself is never returned."""
    return {
        "server": "ZenseAI Ingestion MCP",
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
