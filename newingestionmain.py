"""MCP server exposing ZenseAI ingestion tools (knowledge base, code space, tickets).

Every tool is registered through @secure_tool. If a tool takes project_name or
workspace_name, the access check runs automatically before the tool body.
A tool with no scope arguments must be marked public=True or the server
fails at import time.

Tool order (most fundamental first):
  1. Identity and access    get_user_info, list_projects, list_my_workspaces,
                            get_project_workspaces, validate_project_workspace_access
  2. Knowledge base         upload_knowledge_base_documents,
                            delete_knowledge_base_documents
  3. Code space             create_workspace_codespace, delete_workspace_codespace
  4. Tickets                get_workspace_tickets, create_workspace_ticket,
                            delete_workspace_ticket
  5. Diagnostics            whoami
"""
import json
import asyncio
import functools
import inspect
import os
import sys
import zipfile
from contextlib import ExitStack
from typing import Any, Callable

import jwt
import requests
import urllib3
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

mcp = FastMCP("ZenseAI Ingestion MCP")

# ---------------------------------------------------------------------------
# Configuration and HTTP helpers. All middleware calls go through these.
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return "https://zenseai.zensar.com/middleware"


def _default_timeout() -> int:
    return int(os.getenv("AIBUDDY_API_TIMEOUT_SECONDS", "60"))


def _verify_ssl() -> bool:
    # Default kept as before (no verification). Set AIBUDDY_VERIFY_SSL=true in production.
    return os.getenv("AIBUDDY_VERIFY_SSL", "false").strip().lower() == "true"


def _get_authorization_header() -> str:
    # FastMCP filters credentials by default and lowercases header names.
    return get_http_headers(include={"authorization"}).get("authorization", "").strip()


def _get_token() -> str:
    # The token is never accepted as a tool argument, so the model cannot
    # substitute another user's credentials.
    authorization = _get_authorization_header()
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise ValueError("A valid Authorization: Bearer <token> header is required.")
    return token.strip()


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
    files: dict[str, Any] | list[tuple[str, Any]],
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


# ---- 2. Knowledge base ----------------------------------------------------

_KB_MAX_FILE_BYTES = 50 * 1024 * 1024  # the ZenseAI upload dialog limit is 50 MB per file


@secure_tool()
def upload_knowledge_base_documents(
    project_name: str,
    workspace_name: str,
    file_paths: list[str],
    model_id: int | None = None,
) -> dict[str, Any]:
    """Upload local documents (PDF, DOC, TXT, XLS, PPT, images, etc.) to a workspace's
    knowledge base. Each file must be 50 MB or smaller. Indexing runs after the upload,
    so a successful response means the files were accepted, not yet searchable.

    model_id is the knowledge ingestion model's ID. If omitted, the
    KNOWLEDGE_INGESTION_MODEL_ID environment variable is used.
    """
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace

    workspace_id = workspace.get("workspace_id")
    if workspace_id is None:
        return _error(project_name, f"Workspace ID was not found for '{workspace_name}'.")

    paths = [p.strip() for p in file_paths if isinstance(p, str) and p.strip()]
    if not paths:
        return _error(project_name, "file_paths must contain at least one file.")

    for path in paths:
        if not os.path.isfile(path):
            return _error(project_name, f"File was not found: {path}")
        if os.path.getsize(path) > _KB_MAX_FILE_BYTES:
            return _error(project_name, f"File exceeds the 50 MB limit: {path}")

    raw_model_id = model_id if model_id is not None else os.getenv("KNOWLEDGE_INGESTION_MODEL_ID", "").strip()
    try:
        resolved_model_id = int(raw_model_id)
    except (TypeError, ValueError):
        return _error(
            project_name,
            "model_id is required. Pass it, or set KNOWLEDGE_INGESTION_MODEL_ID.",
        )

    form_data = {
        "workspace_id": str(workspace_id),
        "knowledge_ingestion_model_id": str(resolved_model_id),
    }

    try:
        with ExitStack() as stack:
            files = [
                ("files", (os.path.basename(path), stack.enter_context(open(path, "rb"))))
                for path in paths
            ]
            response = _api_multipart_request(
                "POST",
                "/ingestion/knowledgebase/upload_documents",
                data=form_data,
                files=files,
                timeout=300,
            )
        result = response.json()

    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        detail = exc.response.text[:300] if exc.response is not None else ""
        return _error(project_name, f"Knowledge base upload failed with HTTP {status_code}. {detail}".strip())

    except OSError as exc:
        return _error(project_name, f"Could not read an input file: {exc}")

    except ValueError:
        return _error(project_name, "Knowledge base upload API returned an invalid JSON response.")

    result = result if isinstance(result, dict) else {}
    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "model_id": resolved_model_id,
        "files": [os.path.basename(path) for path in paths],
        "message": result.get("message", "Knowledge base upload submitted."),
        "uploaded_count": result.get("uploaded_count"),
        "replaced_count": result.get("replaced_count"),
        "total_documents": result.get("total_documents"),
    }


@secure_tool()
def delete_knowledge_base_documents(
    project_name: str,
    workspace_name: str,
    document_names: list[str],
) -> dict[str, Any]:
    """Delete documents from a workspace's knowledge base by file name (for example
    'report.pdf'). This cannot be undone. Compare deleted_count with the number of
    names requested to confirm every document was removed.
    """
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace

    workspace_id = workspace.get("workspace_id")
    if workspace_id is None:
        return _error(project_name, f"Workspace ID was not found for '{workspace_name}'.")

    names = [n.strip() for n in document_names if isinstance(n, str) and n.strip()]
    if not names:
        return _error(project_name, "document_names must contain at least one file name.")

    try:
        response = _api_request(
            "DELETE",
            "/ingestion/knowledgebase/documents",
            json_body={"workspace_id": workspace_id, "knowledge_documents": names},
            timeout=60,
        )
        result = response.json()

    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        detail = exc.response.text[:300] if exc.response is not None else ""
        return _error(project_name, f"Knowledge base deletion failed with HTTP {status_code}. {detail}".strip())

    except ValueError:
        return _error(project_name, "Knowledge base deletion API returned an invalid JSON response.")

    result = result if isinstance(result, dict) else {}
    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "requested": names,
        "deleted_count": result.get("deleted_count"),
        "remaining_count": result.get("remaining_count"),
    }


# ---- 3. Code space --------------------------------------------------------

# Default output folder structure, the same one the ZenseAI dialog sends.
_DEFAULT_OUTPUT_FILE_STRUCTURE = [{"id": 1, "name": "Transactions_Output_Folder", "subfolders": []}]


def _flag(value: bool) -> str:
    return "true" if value else "false"


@secure_tool()
def create_workspace_codespace(
    project_name: str,
    workspace_name: str,
    codespace_name: str,
    short_description: str,
    tech_stack: str,
    version: str,
    zip_file_path: str,
    project_type: str = "",
    sonar_analysis: bool = True,
    deep_dive_analysis: bool = True,
    is_batch: bool = False,
    model_id: int | None = None,
) -> dict[str, Any]:
    """Create a code space in an accessible workspace from a local .zip of the source code.

    codespace_name: spaces are converted to underscores, as the ZenseAI dialog does.
    tech_stack: for example 'Python'.
    version: for example '3.2'.
    deep_dive_analysis: turns on LLM-based code ingestion and graph analysis. It needs a model.
    model_id: the deep dive model's ID. If omitted, CODESPACE_MODEL_ID is used, then
    KNOWLEDGE_INGESTION_MODEL_ID.

    Indexing runs after creation (about 30 minutes), so a successful response means the
    code space was accepted, not that it is ready.
    """
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace

    workspace_uuid = workspace.get("workspace_uuid")
    if not workspace_uuid:
        return _error(project_name, f"Workspace UUID was not found for '{workspace_name}'.")

    name = "_".join(codespace_name.split())
    short_description = short_description.strip()
    tech_stack = tech_stack.strip()
    version = version.strip()
    project_type = project_type.strip()
    zip_path = zip_file_path.strip()

    for field_name, value in (
        ("codespace_name", name),
        ("short_description", short_description),
        ("tech_stack", tech_stack),
        ("version", version),
    ):
        if not value:
            return _error(project_name, f"{field_name} cannot be blank.")

    if not os.path.isfile(zip_path):
        return _error(project_name, f"Zip file was not found: {zip_path}")
    if os.path.splitext(zip_path)[1].lower() != ".zip" or not zipfile.is_zipfile(zip_path):
        return _error(project_name, "Only valid .zip files are allowed.")

    raw_model_id = model_id if model_id is not None else (
        os.getenv("CODESPACE_MODEL_ID") or os.getenv("KNOWLEDGE_INGESTION_MODEL_ID") or ""
    ).strip()
    resolved_model_id: int | None = None
    if raw_model_id != "":
        try:
            resolved_model_id = int(raw_model_id)
        except (TypeError, ValueError):
            return _error(project_name, "model_id must be a number.")
    if deep_dive_analysis and resolved_model_id is None:
        return _error(
            project_name,
            "model_id is required for deep dive analysis. Pass it, or set CODESPACE_MODEL_ID.",
        )

    try:
        user_id = _extract_user_id(_get_user_record())
    except ValueError as exc:
        return _error(project_name, str(exc))

    form_data = {
        "uuid": str(workspace_uuid),
        "code_index_data": json.dumps(
            {
                "source": "local",
                "connector_id": None,
                "name": name,
                "short_description": short_description,
                "project_type": project_type,
                "tech_stack": tech_stack,
                "version": version,
            },
            separators=(",", ":"),
        ),
        "llm_based_graph": _flag(deep_dive_analysis),
        "llm_based_code_ingestion": _flag(deep_dive_analysis),
        "sonar_analysis": _flag(sonar_analysis),
        "is_batch": _flag(is_batch),
        "is_documentation": "true",
        "output_file_structure": json.dumps(_DEFAULT_OUTPUT_FILE_STRUCTURE, separators=(",", ":")),
        "created_by": str(user_id),
    }
    if resolved_model_id is not None:
        form_data["api_id"] = str(resolved_model_id)

    try:
        with open(zip_path, "rb") as zip_file:
            response = _api_multipart_request(
                "POST",
                "/ingestion/codebase/create",
                data=form_data,
                files={"file": (os.path.basename(zip_path), zip_file, "application/zip")},
                timeout=300,
            )
        result = response.json()

    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        detail = exc.response.text[:300] if exc.response is not None else ""
        return _error(project_name, f"Code space creation failed with HTTP {status_code}. {detail}".strip())

    except OSError as exc:
        return _error(project_name, f"Could not read the zip file: {exc}")

    except ValueError:
        return _error(project_name, "Code space creation API returned an invalid JSON response.")

    result = result if isinstance(result, dict) else {}
    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_uuid": workspace_uuid,
        "codespace_name": name,
        "filename": os.path.basename(zip_path),
        "message": result.get("message", "Code space creation submitted."),
        "estimated_processing_time": result.get("estimated_processing_time"),
    }


def _fetch_workspace_codespaces(workspace_uuid: str) -> list[dict[str, Any]]:
    """Code spaces (codebases) that belong to one workspace."""
    data = _api_get(
        f"/ingestion/codebase/by-workspace/{requests.utils.quote(str(workspace_uuid), safe='')}",
        timeout=60,
    )
    raw = data.get("codebases") if isinstance(data, dict) else None
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


@secure_tool()
def delete_workspace_codespace(
    project_name: str,
    workspace_name: str,
    codespace_name: str,
) -> dict[str, Any]:
    """Delete a code space from an accessible workspace by name. This cannot be undone.

    The name is looked up inside the given workspace only, so a code space in another
    workspace can never be deleted by mistake. Spaces in the name are treated as underscores.
    """
    workspace = _validate_workspace(project_name, workspace_name)
    if not workspace.get("access"):
        return workspace

    workspace_id = workspace.get("workspace_id")
    workspace_uuid = workspace.get("workspace_uuid")
    if not workspace_uuid:
        return _error(project_name, f"Workspace UUID was not found for '{workspace_name}'.")

    raw_name = codespace_name.strip()
    if not raw_name:
        return _error(project_name, "codespace_name cannot be blank.")
    wanted = {raw_name.lower(), "_".join(raw_name.split()).lower()}

    try:
        codespaces = _fetch_workspace_codespaces(workspace_uuid)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        return _error(project_name, f"Could not list code spaces (HTTP {status_code}).")
    except ValueError:
        return _error(project_name, "Code space list API returned an invalid JSON response.")

    matches = [c for c in codespaces if str(c.get("name") or "").lower() in wanted]
    if not matches:
        available = [c.get("name") for c in codespaces if c.get("name")]
        return _error(
            project_name,
            f"Code space '{codespace_name}' was not found in workspace '{workspace_name}'. "
            f"Available: {available}",
        )
    if len(matches) > 1:
        return _error(
            project_name,
            f"More than one code space is named '{codespace_name}' in this workspace "
            f"(ids: {[c.get('id') for c in matches]}). Delete it in the ZenseAI UI.",
        )

    target = matches[0]
    try:
        codespace_id = int(target["id"])
    except (KeyError, TypeError, ValueError):
        return _error(project_name, f"Code space ID was not found for '{codespace_name}'.")

    # The list is already filtered by workspace, but never delete across workspaces.
    owner_id = target.get("workspace_id")
    if owner_id is not None and workspace_id is not None and str(owner_id) != str(workspace_id):
        return _error(project_name, f"Code space '{codespace_name}' does not belong to this workspace.")

    try:
        response = _api_request("DELETE", f"/ingestion/codebase/delete/{codespace_id}", timeout=60)
        result = response.json()

    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        detail = exc.response.text[:300] if exc.response is not None else ""
        return _error(project_name, f"Code space deletion failed with HTTP {status_code}. {detail}".strip())

    except ValueError:
        return _error(project_name, "Code space deletion API returned an invalid JSON response.")

    result = result if isinstance(result, dict) else {}
    return {
        "access": True,
        "project_name": project_name,
        "workspace_name": workspace_name,
        "workspace_id": workspace_id,
        "codespace_name": target.get("name"),
        "codespace_id": result.get("codebase_id", codespace_id),
        "status": result.get("status"),
        "message": result.get("message", "Code space deleted."),
    }


# ---- 4. Tickets ------------------------------------------------------------

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


# ---- 5. Diagnostics --------------------------------------------------------

@secure_tool(public=True)
async def whoami(ctx: Context) -> dict[str, Any]:
    """Return request context and whether an Authorization header was received."""
    return {
        "server": "ZenseAI Ingestion MCP",
        "user_id": getattr(ctx, "user_id", None),
        "user_email": getattr(ctx, "user_email", None),
        "authorization_header_present": bool(_get_authorization_header()),
        "conversation_id": getattr(ctx, "conversation_id", None),
        "parent_workflow_runid": getattr(ctx, "parent_workflow_runid", None),
    }


def main() -> None:
    # Audit goes to stderr: stdout is the MCP stdio channel and must stay clean.
    print(f"Tool access audit: {audit_tool_access()}", file=sys.stderr)
    # Port differs from newmain (10001) so both servers can run on one host.
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("MCP_PORT", "10002")))


if __name__ == "__main__":
    main()
