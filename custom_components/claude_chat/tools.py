"""Tools that Claude can call to inspect and modify Home Assistant.

Read-only:  list_entities, get_entity, list_areas, list_dashboards,
            get_dashboard, list_lovelace_resources, list_automations,
            list_services, get_script, get_helper
Staged:     propose_dashboard_update, propose_service_call,
            propose_automation_*, propose_script_*, propose_helper_*
            (write to a PendingChange the user approves in the UI)
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import timedelta
from typing import Any

import yaml as yaml_lib
from homeassistant.components.lovelace.const import ConfigNotFound
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.util import dt as dt_util, slugify

from .storage import PendingChange, SessionStore

_LOGGER = logging.getLogger(__name__)

# Input-helper domains manageable through their storage collections (the same
# mechanism the HA "Helpers" UI uses).
HELPER_DOMAINS = (
    "input_boolean",
    "input_number",
    "input_text",
    "input_select",
    "input_datetime",
    "input_button",
)


def _target_key(change: PendingChange) -> str:
    """A stable key identifying *what resource* a pending change targets.

    Pending changes with the same target supersede each other — only the
    latest one is kept. Mirrors the frontend's targetKeyFor in
    claude-chat-panel.js so backend and UI agree.
    """
    p = change.payload or {}
    kind = change.kind
    if kind == "dashboard_update":
        return f"dashboard:{p.get('url_path', '')}"
    if kind in ("automation_update", "automation_delete"):
        return f"automation:{p.get('automation_id', '')}"
    if kind == "automation_create":
        config = p.get("config") or {}
        return f"automation_create:{config.get('alias', change.id)}"
    if kind in ("script_create", "script_update", "script_delete"):
        return f"script:{p.get('script_id', '')}"
    if kind == "helper_create":
        config = p.get("config") or {}
        return f"helper_create:{p.get('domain')}:{config.get('name', change.id)}"
    if kind in ("helper_update", "helper_delete"):
        return f"helper:{p.get('entity_id', '')}"
    if kind == "service_call":
        target_json = json.dumps(p.get("target") or {}, sort_keys=True)
        return f"service:{p.get('domain')}.{p.get('service')}:{target_json}"
    return change.id


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_entities",
        "description": (
            "List Home Assistant entities. Filter by domain, area, label, and/or "
            "a keyword search across entity_id and friendly name. Use filters to "
            "keep results small — unfiltered results can be hundreds of entities."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "HA domain filter (e.g. 'sensor', 'light')"},
                "area": {"type": "string", "description": "Area name filter"},
                "label": {"type": "string", "description": "Filter by label (case-insensitive)"},
                "search": {"type": "string", "description": "Keyword to match against entity_id and friendly name (case-insensitive)"},
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    {
        "name": "get_entity",
        "description": "Get the full state and attributes for a single entity.",
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_areas",
        "description": "List Home Assistant areas with their IDs and names.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_dashboards",
        "description": (
            "List Lovelace dashboards. Each entry has a url_path, title, "
            "mode, and whether it currently has a stored config. "
            "If has_stored_config is false the dashboard is auto-generated; "
            "you can still propose an update — the first save creates it."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_dashboard",
        "description": (
            "Fetch a Lovelace dashboard config by url_path. "
            "Use summary=true first to see view titles and card counts cheaply; "
            "only fetch the full config when you need to modify it. "
            "Use 'lovelace' for the default dashboard."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "summary": {"type": "boolean", "default": False, "description": "Return only view titles and card counts instead of the full config"},
            },
        },
    },
    {
        "name": "get_dashboard_view",
        "description": (
            "Fetch a single view from a Lovelace dashboard by its path. "
            "Much cheaper than get_dashboard for large dashboards. "
            "Use get_dashboard(summary=true) first to discover view paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "view_path": {"type": "string", "description": "Path of the view or its index as a string ('0', '1', …) — use the view_path from get_dashboard(summary=true)"},
            },
            "required": ["view_path"],
        },
    },
    {
        "name": "list_lovelace_resources",
        "description": (
            "List custom Lovelace cards/modules installed on this HA. "
            "Use this to discover whether you can use cards like "
            "mushroom-card, mini-graph-card, button-card, etc. Returns "
            "url, type, and id for each resource. If empty, only stock "
            "cards are available."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_automations",
        "description": (
            "List automations defined in HA. Returns entity_id, automation_id, "
            "name, state (on/off), area, and labels. "
            "Filter with `search` (name), `area` (area name), or `label`. "
            "Call get_automation for the full config of a specific automation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Keyword to filter by automation name (case-insensitive)"},
                "area": {"type": "string", "description": "Filter by area name"},
                "label": {"type": "string", "description": "Filter by label (case-insensitive)"},
            },
        },
    },
    {
        "name": "list_automation_traces",
        "description": (
            "List recent execution traces of an automation. Each entry shows "
            "when it ran, whether it succeeded, the triggering event, and "
            "whether all conditions passed. Use get_automation_trace for "
            "the full step-by-step detail of a specific run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["automation_id"],
        },
    },
    {
        "name": "get_automation_trace",
        "description": (
            "Get the step-by-step trace of one automation execution: "
            "triggers, conditions (pass/fail), actions (timing, outputs), "
            "and any errors. Use this to debug why an automation behaved a "
            "certain way. The automation config is omitted (use get_automation "
            "if you need it)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "run_id": {"type": "string"},
            },
            "required": ["automation_id", "run_id"],
        },
    },
    {
        "name": "get_state_history",
        "description": (
            "Get recent state changes for an entity (last N hours). Returns "
            "timestamps and states. Useful to see when sensors changed, "
            "when automations triggered, when devices went offline, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_services",
        "description": (
            "List HA services — returns domain, service name, and a one-line "
            "description only. Use `domain` and/or `search` to narrow results "
            "(without filters the result can be thousands of entries). "
            "Call get_service for the full parameter details of a specific service."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Filter by service domain"},
                "search": {"type": "string", "description": "Keyword to search in domain, service name, and description"},
                "limit": {"type": "integer", "default": 100, "description": "Max services to return"},
            },
        },
    },
    {
        "name": "get_service",
        "description": (
            "Get full details for a single HA service: description, all accepted "
            "fields with types, descriptions, and examples. Use this after "
            "list_services to understand exactly what parameters a service expects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
            },
            "required": ["domain", "service"],
        },
    },
    {
        "name": "propose_dashboard_update",
        "description": (
            "Propose a new full Lovelace dashboard config. This does NOT apply "
            "immediately — it stages a pending change that the user reviews "
            "and approves in the chat UI. Always fetch the current dashboard "
            "with get_dashboard first, then send the complete modified config. "
            "Pass the original config you fetched as `current_config` so the "
            "diff can be generated without an extra storage round-trip."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "current_config": {"type": "object", "description": "The config you fetched with get_dashboard — used for diff generation, avoids a second fetch"},
                "new_config": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["new_config", "summary"],
        },
    },
    {
        "name": "propose_dashboard_view_update",
        "description": (
            "Propose updating a single view in a Lovelace dashboard. "
            "Use this instead of propose_dashboard_update when the dashboard "
            "is large — it only requires the new config for one view, not the "
            "whole dashboard (avoids output-token limits). "
            "Use get_dashboard(summary=true) to find view paths, then "
            "get_dashboard_view to fetch the current view before modifying it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url_path": {"type": "string", "default": "lovelace"},
                "view_path": {"type": "string", "description": "Path of the view (e.g. 'home') or its index as a string ('0', '1', …) for views with no explicit path — use the view_path from get_dashboard(summary=true)"},
                "new_view": {"type": "object", "description": "Complete new config for this single view"},
                "summary": {"type": "string"},
            },
            "required": ["view_path", "new_view", "summary"],
        },
    },
    {
        "name": "propose_automation_create",
        "description": (
            "Propose creating a new automation. Stages a pending change for "
            "user approval. On approve, the automation is appended to "
            "automations.yaml and automation.reload is called. Requires that "
            "the user's configuration.yaml has `automation: !include "
            "automations.yaml` (the HA default). Build `config` as a dict "
            "with keys: alias, description, trigger, condition, action, mode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "config": {
                    "type": "object",
                    "description": "Automation config dict (alias, trigger, action, …)",
                },
                "summary": {"type": "string"},
            },
            "required": ["config", "summary"],
        },
    },
    {
        "name": "propose_automation_update",
        "description": (
            "Propose updating an existing automation by its automation_id "
            "(found via list_automations). Sends the complete new config — "
            "fetch the existing one first so you don't accidentally drop "
            "fields. Stages for user approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "config": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["automation_id", "config", "summary"],
        },
    },
    {
        "name": "propose_automation_delete",
        "description": (
            "Propose deleting an automation. Stages for user approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["automation_id", "summary"],
        },
    },
    {
        "name": "get_automation",
        "description": (
            "Fetch the full config of an automation by automation_id. Use "
            "before propose_automation_update to get the current state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"automation_id": {"type": "string"}},
            "required": ["automation_id"],
        },
    },
    {
        "name": "get_script",
        "description": (
            "Fetch the full config of a script from scripts.yaml by its "
            "script_id (the part of the entity_id after 'script.'). Use "
            "before propose_script_update so you don't drop fields. "
            "Use list_entities(domain='script') to discover scripts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"script_id": {"type": "string"}},
            "required": ["script_id"],
        },
    },
    {
        "name": "propose_script_create",
        "description": (
            "Propose creating a new script. Stages a pending change for user "
            "approval. On approve, the script is added to scripts.yaml and "
            "script.reload is called. Requires `script: !include scripts.yaml` "
            "in configuration.yaml (the HA default). Pick a short snake_case "
            "script_id (becomes entity_id script.<script_id>) and build "
            "`config` as a dict with keys like: alias, description, sequence, "
            "mode, icon."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script_id": {"type": "string", "description": "snake_case id, becomes script.<script_id>"},
                "config": {"type": "object", "description": "Script config dict (alias, sequence, mode, …)"},
                "summary": {"type": "string"},
            },
            "required": ["script_id", "config", "summary"],
        },
    },
    {
        "name": "propose_script_update",
        "description": (
            "Propose replacing an existing script's config by script_id. "
            "Sends the complete new config — fetch the current one with "
            "get_script first so you don't accidentally drop fields. "
            "Stages for user approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "config": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["script_id", "config", "summary"],
        },
    },
    {
        "name": "propose_script_delete",
        "description": "Propose deleting a script. Stages for user approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "script_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["script_id", "summary"],
        },
    },
    {
        "name": "get_helper",
        "description": (
            "Fetch the stored config of a UI-managed input helper "
            "(input_boolean, input_number, input_text, input_select, "
            "input_datetime, input_button) by entity_id. Use before "
            "propose_helper_update. Errors for YAML-defined helpers — those "
            "can't be edited here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": "propose_helper_create",
        "description": (
            "Propose creating an input helper. Stages a pending change for "
            "user approval; on approve it's created via HA's storage "
            "collection (same as the Helpers UI) and takes effect "
            "immediately — no reload or restart needed. Fields per domain: "
            "input_boolean: name, icon, initial(bool). "
            "input_number: name, min, max (required), step, initial, "
            "unit_of_measurement, mode('slider'|'box'), icon. "
            "input_text: name, min, max, initial, pattern, "
            "mode('text'|'password'), unit_of_measurement, icon. "
            "input_select: name, options (required, list of strings), "
            "initial, icon. "
            "input_datetime: name, has_date(bool), has_time(bool), initial, "
            "icon. input_button: name, icon."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "enum": list(HELPER_DOMAINS)},
                "config": {"type": "object", "description": "Helper config; must include `name`"},
                "summary": {"type": "string"},
            },
            "required": ["domain", "config", "summary"],
        },
    },
    {
        "name": "propose_helper_update",
        "description": (
            "Propose replacing the config of a UI-managed input helper by "
            "entity_id. Send the COMPLETE new config (same fields as "
            "propose_helper_create) — omitted fields are removed. Fetch the "
            "current config with get_helper first. Stages for user approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "config": {"type": "object"},
                "summary": {"type": "string"},
            },
            "required": ["entity_id", "config", "summary"],
        },
    },
    {
        "name": "propose_helper_delete",
        "description": (
            "Propose deleting a UI-managed input helper by entity_id. "
            "Stages for user approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["entity_id", "summary"],
        },
    },
    {
        "name": "propose_service_call",
        "description": (
            "Propose calling a Home Assistant service (e.g. light.turn_on, "
            "automation.trigger). Stages a pending change for user approval. "
            "Use list_services to discover what's available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "service_data": {"type": "object", "description": "Service data payload"},
                "target": {
                    "type": "object",
                    "description": "Optional target with entity_id/area_id/device_id",
                },
                "summary": {"type": "string"},
            },
            "required": ["domain", "service", "summary"],
        },
    },
]


class ToolRegistry:
    """Dispatches tool calls from Claude to async Python implementations."""

    def __init__(self, hass: HomeAssistant, store: SessionStore) -> None:
        self.hass = hass
        self.store = store

    async def _add_pending_supersede(
        self, session_id: str, change: PendingChange
    ) -> None:
        """Add a pending change, dropping earlier *still-pending* ones with
        the same target. Accepted / rejected history entries are kept so
        the conversation transcript stays complete.
        """
        session = self.store.get(session_id)
        if session is not None:
            new_key = _target_key(change)
            stale = [
                c.id
                for c in session.pending_changes
                if c.status == "pending" and _target_key(c) == new_key
            ]
            for cid in stale:
                await self.store.remove_pending(session_id, cid)
        await self.store.add_pending(session_id, change)

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        session_id: str,
        tool_use_id: str | None = None,
    ) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return await handler(arguments, session_id, tool_use_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Tool %s failed", name)
            return {"error": str(err)}

    # --- read-only tools -------------------------------------------------

    async def _tool_list_entities(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        domain_filter = args.get("domain")
        area_filter = args.get("area")
        label_filter = (args.get("label") or "").lower()
        search = (args.get("search") or "").lower()
        limit = int(args.get("limit", 200))

        ent_reg = er.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        area_id_by_name = {a.name.lower(): a.id for a in area_reg.async_list_areas()}
        target_area_id = (
            area_id_by_name.get(area_filter.lower()) if area_filter else None
        )

        results = []
        for state in self.hass.states.async_all():
            if domain_filter and state.domain != domain_filter:
                continue
            if search and search not in state.entity_id.lower() and search not in state.name.lower():
                continue
            entry = ent_reg.async_get(state.entity_id)
            entity_area_id = entry.area_id if entry else None
            if target_area_id and entity_area_id != target_area_id:
                continue
            if label_filter and label_filter not in {lbl.lower() for lbl in (entry.labels if entry else set())}:
                continue
            area_name = None
            if entity_area_id:
                area = area_reg.async_get_area(entity_area_id)
                area_name = area.name if area else None
            results.append(
                {
                    "entity_id": state.entity_id,
                    "name": state.name,
                    "state": state.state,
                    "area": area_name,
                }
            )
            if len(results) >= limit:
                break
        return {"entities": results, "truncated": len(results) >= limit}

    async def _tool_get_entity(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        entity_id = args["entity_id"]
        state = self.hass.states.get(entity_id)
        if state is None:
            return {"error": f"Entity not found: {entity_id}"}
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
        }

    async def _tool_list_areas(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        area_reg = ar.async_get(self.hass)
        return {
            "areas": [
                {"id": a.id, "name": a.name} for a in area_reg.async_list_areas()
            ]
        }

    # --- dashboard tools -------------------------------------------------

    async def _tool_list_dashboards(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        dashboards = self._lovelace_dashboards()
        result = []
        for url_path, info in dashboards.items():
            stored = await self._has_stored_config(info["store"])
            result.append(
                {
                    "url_path": url_path,
                    "title": info["title"],
                    "mode": info["mode"],
                    "editable": info["mode"] == "storage",
                    "has_stored_config": stored,
                }
            )
        return {"dashboards": result}

    async def _tool_get_dashboard(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        url_path = args.get("url_path", "lovelace")
        config = await self._load_dashboard_config(url_path)
        if config is None:
            return {"error": f"Dashboard not found: {url_path}"}
        if args.get("summary"):
            return {"url_path": url_path, "summary": _dashboard_summary(config)}
        return {"url_path": url_path, "config": config}

    async def _tool_get_dashboard_view(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        url_path = args.get("url_path", "lovelace")
        view_path = args["view_path"]
        config = await self._load_dashboard_config(url_path)
        if config is None:
            return {"error": f"Dashboard not found: {url_path}"}
        idx, view = _find_view(config.get("views", []), view_path)
        if view is None:
            available = _dashboard_summary(config)
            return {"error": f"View not found: {view_path!r}", "available_views": available}
        return {"url_path": url_path, "view_path": view_path, "view": view}


    async def _tool_propose_dashboard_update(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        url_path = args.get("url_path", "lovelace")
        new_config = args["new_config"]
        summary = args["summary"]

        dashboards = self._lovelace_dashboards()
        info = dashboards.get(url_path)
        if not info:
            return {"error": f"Dashboard not found: {url_path}"}
        if info["mode"] != "storage":
            return {
                "error": (
                    f"Dashboard '{url_path}' is in {info['mode']} mode and "
                    "cannot be edited programmatically."
                )
            }

        # Use the config Claude already fetched if provided; only fall back to
        # a storage reload when it's absent (e.g. direct API calls in tests).
        current_config = args.get("current_config")
        if current_config is None:
            current_config = await self._load_dashboard_config(url_path) or {}
        diff = _format_diff(current_config, new_config)
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="dashboard_update",
            summary=summary,
            payload={"url_path": url_path, "new_config": new_config},
            diff=diff,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": summary,
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_dashboard_view_update(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        url_path = args.get("url_path", "lovelace")
        view_path = args["view_path"]
        new_view = args["new_view"]
        summary = args["summary"]

        dashboards = self._lovelace_dashboards()
        info = dashboards.get(url_path)
        if not info:
            return {"error": f"Dashboard not found: {url_path}"}
        if info["mode"] != "storage":
            return {"error": f"Dashboard '{url_path}' is in {info['mode']} mode and cannot be edited programmatically."}

        current = await self._load_dashboard_config(url_path) or {}
        views = list(current.get("views", []))
        idx, _ = _find_view(views, view_path)
        if idx is None:
            return {"error": f"View not found: {view_path!r}", "available_views": _dashboard_summary(current)}

        new_full_config = {**current, "views": views[:idx] + [new_view] + views[idx + 1:]}
        diff = _format_diff(current, new_full_config)
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="dashboard_update",
            summary=summary,
            # Store view_path + new_view so apply_pending_change can re-merge
            # against the live config at approval time, preserving any changes
            # to other views that happened between proposal and approval.
            payload={
                "url_path": url_path,
                "view_path": view_path,
                "new_view": new_view,
                # new_config is still included for the diff display in the UI.
                "new_config": new_full_config,
            },
            diff=diff,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": summary,
            "status": "awaiting_user_approval",
        }

    async def _tool_list_lovelace_resources(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        lovelace_data = self.hass.data.get("lovelace")
        if lovelace_data is None:
            return {"resources": []}
        resources = getattr(lovelace_data, "resources", None)
        if resources is None:
            return {"resources": []}
        items = list(resources.async_items()) if hasattr(resources, "async_items") else []
        return {
            "resources": [
                {"id": i.get("id"), "url": i.get("url"), "type": i.get("res_type")}
                for i in items
            ]
        }

    async def _tool_list_automations(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        search = (args.get("search") or "").lower()
        area_filter = (args.get("area") or "").lower()
        label_filter = (args.get("label") or "").lower()

        ent_reg = er.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        area_id_by_name = {a.name.lower(): a.id for a in area_reg.async_list_areas()}

        result = []
        for state in self.hass.states.async_all("automation"):
            if search and search not in state.name.lower():
                continue
            entry = ent_reg.async_get(state.entity_id)
            entity_area_id = entry.area_id if entry else None
            entity_labels = {lbl.lower() for lbl in (entry.labels if entry else set())}

            if area_filter:
                target_area_id = area_id_by_name.get(area_filter)
                if entity_area_id != target_area_id:
                    continue
            if label_filter and label_filter not in entity_labels:
                continue

            area_name = None
            if entity_area_id:
                area_obj = area_reg.async_get_area(entity_area_id)
                area_name = area_obj.name if area_obj else None

            result.append(
                {
                    "entity_id": state.entity_id,
                    "automation_id": state.attributes.get("id"),
                    "name": state.name,
                    "state": state.state,
                    "area": area_name,
                    "labels": sorted(entry.labels) if entry else [],
                }
            )
        return {"automations": result}

    async def _tool_list_automation_traces(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        automation_id = args["automation_id"]
        limit = int(args.get("limit", 10))
        try:
            from homeassistant.components.trace.const import DATA_TRACE
        except ImportError:
            return {"error": "Trace component not available in this HA version"}
        all_traces = self.hass.data.get(DATA_TRACE)
        if all_traces is None:
            return {"traces": [], "note": "No traces stored yet — has this automation run since HA started?"}
        item_traces = all_traces.get("automation", {}).get(automation_id, {})
        if not item_traces:
            return {
                "traces": [],
                "note": (
                    f"No traces found for automation_id={automation_id}. "
                    "Check the ID via list_automations and confirm the "
                    "automation has fired at least once since HA started."
                ),
            }
        sorted_traces = sorted(
            item_traces.values(),
            key=lambda t: getattr(t, "timestamp_finish", None)
            or getattr(t, "timestamp_start", ""),
            reverse=True,
        )[:limit]
        return {
            "automation_id": automation_id,
            "traces": [t.as_short_dict() for t in sorted_traces],
        }

    async def _tool_get_automation_trace(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        from homeassistant.components.trace.const import DATA_TRACE

        automation_id = args["automation_id"]
        run_id = args["run_id"]
        all_traces = self.hass.data.get(DATA_TRACE) or {}
        trace = (
            all_traces.get("automation", {}).get(automation_id, {}).get(run_id)
        )
        if trace is None:
            return {"error": f"Trace not found: {automation_id}/{run_id}"}
        # Use the extended dict but strip the full automation config and
        # blueprint_inputs — they're large and available via get_automation.
        data = trace.as_extended_dict()
        data.pop("config", None)
        data.pop("blueprint_inputs", None)
        return {"trace": data}

    async def _tool_get_state_history(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import (
                state_changes_during_period,
            )
        except ImportError:
            return {"error": "Recorder component not available"}

        entity_id = args["entity_id"]
        hours = int(args.get("hours", 24))
        end = dt_util.utcnow()
        start = end - timedelta(hours=hours)

        rec = get_instance(self.hass)
        states_by_entity = await rec.async_add_executor_job(
            state_changes_during_period,
            self.hass,
            start,
            end,
            entity_id,
            True,  # no_attributes — we only want state changes, not attribute changes
        )
        states = states_by_entity.get(entity_id, [])
        return {
            "entity_id": entity_id,
            "hours": hours,
            "count": len(states),
            "events": [
                {"timestamp": s.last_changed.isoformat(), "state": s.state}
                for s in states
            ],
        }

    async def _tool_get_automation(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        automation_id = args["automation_id"]
        path = self.hass.config.path("automations.yaml")
        automations = await self.hass.async_add_executor_job(
            _read_automations_yaml, path
        )
        for a in automations:
            if str(a.get("id")) == str(automation_id):
                return {"automation_id": automation_id, "config": a}
        return {"error": f"Automation not found: {automation_id}"}

    async def _tool_propose_automation_create(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        config = args["config"]
        if not isinstance(config, dict):
            return {"error": "config must be an object"}
        if not _yaml_file_supported(self.hass, "automations.yaml"):
            return {
                "error": (
                    "automations.yaml not in use — your configuration.yaml "
                    "needs `automation: !include automations.yaml` for "
                    "programmatic automation creation."
                )
            }
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="automation_create",
            summary=args["summary"],
            payload={"config": config, "yaml": _yaml_dump(config)},
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_automation_update(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        automation_id = args["automation_id"]
        new_config = args["config"]
        path = self.hass.config.path("automations.yaml")
        automations = await self.hass.async_add_executor_job(
            _read_automations_yaml, path
        )
        existing = next(
            (a for a in automations if str(a.get("id")) == str(automation_id)), None
        )
        if existing is None:
            return {"error": f"Automation not found: {automation_id}"}
        # Preserve id.
        new_config = {**new_config, "id": automation_id}
        diff = _format_diff(existing, new_config)
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="automation_update",
            summary=args["summary"],
            payload={
                "automation_id": automation_id,
                "config": new_config,
                "yaml": _yaml_dump(new_config),
            },
            diff=diff,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_automation_delete(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        automation_id = args["automation_id"]
        path = self.hass.config.path("automations.yaml")
        automations = await self.hass.async_add_executor_job(
            _read_automations_yaml, path
        )
        existing = next(
            (a for a in automations if str(a.get("id")) == str(automation_id)), None
        )
        if existing is None:
            return {"error": f"Automation not found: {automation_id}"}
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="automation_delete",
            summary=args["summary"],
            payload={
                "automation_id": automation_id,
                "yaml": _yaml_dump(existing),
            },
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    # --- script tools ------------------------------------------------------

    async def _tool_get_script(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        script_id = args["script_id"]
        scripts = await self._read_scripts()
        if script_id not in scripts:
            return {
                "error": f"Script not found in scripts.yaml: {script_id}",
                "available_script_ids": sorted(scripts),
            }
        return {"script_id": script_id, "config": scripts[script_id]}

    async def _tool_propose_script_create(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        script_id = args["script_id"]
        config = args["config"]
        if not isinstance(config, dict):
            return {"error": "config must be an object"}
        if script_id != slugify(script_id):
            return {"error": f"script_id must be a slug, e.g. {slugify(script_id)!r}"}
        if not _yaml_file_supported(self.hass, "scripts.yaml"):
            return {
                "error": (
                    "scripts.yaml not in use — your configuration.yaml "
                    "needs `script: !include scripts.yaml` for "
                    "programmatic script creation."
                )
            }
        scripts = await self._read_scripts()
        if script_id in scripts:
            return {
                "error": (
                    f"Script already exists: {script_id}. "
                    "Use propose_script_update instead."
                )
            }
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="script_create",
            summary=args["summary"],
            payload={
                "script_id": script_id,
                "config": config,
                "yaml": _yaml_dump({script_id: config}),
            },
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_script_update(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        script_id = args["script_id"]
        new_config = args["config"]
        scripts = await self._read_scripts()
        if script_id not in scripts:
            return {"error": f"Script not found: {script_id}"}
        diff = _format_diff(scripts[script_id], new_config)
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="script_update",
            summary=args["summary"],
            payload={
                "script_id": script_id,
                "config": new_config,
                "yaml": _yaml_dump({script_id: new_config}),
            },
            diff=diff,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_script_delete(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        script_id = args["script_id"]
        scripts = await self._read_scripts()
        if script_id not in scripts:
            return {"error": f"Script not found: {script_id}"}
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="script_delete",
            summary=args["summary"],
            payload={
                "script_id": script_id,
                "yaml": _yaml_dump({script_id: scripts[script_id]}),
            },
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _read_scripts(self) -> dict[str, Any]:
        path = self.hass.config.path("scripts.yaml")
        return await self.hass.async_add_executor_job(_read_scripts_yaml, path)

    # --- input helper tools ------------------------------------------------

    def _helper_collection(self, domain: str) -> Any | None:
        """Find a helper domain's storage collection.

        The collection instance is a local variable in the component's
        async_setup, so the only stable handle to it is the websocket
        `<domain>/create` handler it registers: unwrap the functools.wraps
        chain and take the bound method's StorageCollectionWebsocket owner.
        """
        handlers = self.hass.data.get("websocket_api") or {}
        entry = handlers.get(f"{domain}/create")
        if not entry:
            return None
        handler = entry[0]
        while hasattr(handler, "__wrapped__"):
            handler = handler.__wrapped__
        owner = getattr(handler, "__self__", None)
        return getattr(owner, "storage_collection", None)

    def _helper_item(self, entity_id: str) -> tuple[Any, dict[str, Any]] | dict[str, Any]:
        """Resolve entity_id → (collection, stored item), or an error dict."""
        domain, _, item_id = entity_id.partition(".")
        if domain not in HELPER_DOMAINS or not item_id:
            return {"error": f"Not an input helper entity_id: {entity_id}"}
        coll = self._helper_collection(domain)
        if coll is None:
            return {"error": f"The {domain} integration is not loaded"}
        item = next((i for i in coll.async_items() if i["id"] == item_id), None)
        if item is None:
            return {
                "error": (
                    f"{entity_id} is not a UI-managed helper (YAML-defined "
                    "helpers can't be edited here), or it doesn't exist."
                )
            }
        return coll, item

    async def _tool_get_helper(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        resolved = self._helper_item(args["entity_id"])
        if isinstance(resolved, dict):
            return resolved
        _, item = resolved
        return {"entity_id": args["entity_id"], "config": item}

    async def _tool_propose_helper_create(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        domain = args["domain"]
        config = args["config"]
        if domain not in HELPER_DOMAINS:
            return {"error": f"Unsupported helper domain: {domain}"}
        if not isinstance(config, dict) or not config.get("name"):
            return {"error": "config must be an object with at least a `name`"}
        if self._helper_collection(domain) is None:
            return {"error": f"The {domain} integration is not loaded"}
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="helper_create",
            summary=args["summary"],
            payload={
                "domain": domain,
                "config": config,
                "yaml": _yaml_dump({domain: config}),
            },
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_helper_update(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        entity_id = args["entity_id"]
        config = args["config"]
        if not isinstance(config, dict):
            return {"error": "config must be an object"}
        resolved = self._helper_item(entity_id)
        if isinstance(resolved, dict):
            return resolved
        _, existing = resolved
        new_item = {"id": existing["id"], **{k: v for k, v in config.items() if k != "id"}}
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="helper_update",
            summary=args["summary"],
            payload={
                "entity_id": entity_id,
                "config": config,
                "yaml": _yaml_dump(config),
            },
            diff=_format_diff(existing, new_item),
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_propose_helper_delete(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        entity_id = args["entity_id"]
        resolved = self._helper_item(entity_id)
        if isinstance(resolved, dict):
            return resolved
        _, existing = resolved
        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="helper_delete",
            summary=args["summary"],
            payload={"entity_id": entity_id, "yaml": _yaml_dump(existing)},
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    async def _tool_list_services(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        domain_filter = args.get("domain")
        search = (args.get("search") or "").lower()
        limit = int(args.get("limit", 100))
        services = self.hass.services.async_services()
        result = []
        for domain, by_name in services.items():
            if domain_filter and domain != domain_filter:
                continue
            for service_name, service in by_name.items():
                description = getattr(service, "description", "") or ""
                if search and search not in domain.lower() and search not in service_name.lower() and search not in description.lower():
                    continue
                result.append({"domain": domain, "service": service_name, "description": description})
                if len(result) >= limit:
                    return {"services": result, "count": len(result), "truncated": True}
        return {"services": result, "count": len(result), "truncated": False}

    async def _tool_get_service(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        from homeassistant.helpers.service import async_get_cached_service_description

        domain = args["domain"]
        service = args["service"]
        if not self.hass.services.has_service(domain, service):
            return {"error": f"Unknown service: {domain}.{service}"}
        desc = async_get_cached_service_description(self.hass, domain, service)
        if desc is None:
            return {"domain": domain, "service": service, "fields": {}}
        return {"domain": domain, "service": service, **desc}

    async def _tool_propose_service_call(
        self, args: dict[str, Any], session_id: str, tool_use_id: str | None = None
    ) -> dict[str, Any]:
        domain = args["domain"]
        service = args["service"]
        if not self.hass.services.has_service(domain, service):
            return {"error": f"Unknown service: {domain}.{service}"}

        change = PendingChange(
            id=uuid.uuid4().hex,
            kind="service_call",
            summary=args["summary"],
            payload={
                "domain": domain,
                "service": service,
                "service_data": args.get("service_data") or {},
                "target": args.get("target") or {},
            },
            diff=None,
            source_tool_use_id=tool_use_id,
        )
        await self._add_pending_supersede(session_id, change)
        return {
            "pending_change_id": change.id,
            "summary": args["summary"],
            "status": "awaiting_user_approval",
        }

    # --- helpers ---------------------------------------------------------

    def _lovelace_dashboards(self) -> dict[str, dict[str, Any]]:
        """Return {url_path: {title, mode, store}} for every dashboard."""
        result: dict[str, dict[str, Any]] = {}
        lovelace_data = self.hass.data.get("lovelace")
        if lovelace_data is None:
            return result

        # Default dashboard
        default = getattr(lovelace_data, "dashboards", {}).get(None)
        if default is not None:
            result["lovelace"] = {
                "title": "Overview",
                "mode": getattr(default, "mode", "storage"),
                "store": default,
            }

        # Additional dashboards
        for url_path, dash in getattr(lovelace_data, "dashboards", {}).items():
            if url_path is None:
                continue
            result[url_path] = {
                "title": getattr(dash, "config", {}).get("title", url_path)
                if hasattr(dash, "config")
                else url_path,
                "mode": getattr(dash, "mode", "storage"),
                "store": dash,
            }
        return result

    async def _load_dashboard_config(self, url_path: str) -> dict[str, Any] | None:
        """Return the current config, or an empty skeleton if auto-generated.

        A storage-mode dashboard that's never been customised raises
        ConfigNotFound on async_load. We return a sensible empty config
        so Claude can build on top of it; saving the proposed update will
        materialise the dashboard in storage.
        """
        dashboards = self._lovelace_dashboards()
        info = dashboards.get(url_path)
        if not info:
            return None
        store = info["store"]
        try:
            return await store.async_load(force=True)
        except ConfigNotFound:
            return {
                "title": info["title"],
                "views": [
                    {"title": "Home", "path": "default_view", "cards": []}
                ],
            }
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to load dashboard %s", url_path)
            return None

    async def _has_stored_config(self, store: Any) -> bool:
        try:
            await store.async_load(force=True)
            return True
        except ConfigNotFound:
            return False
        except Exception:  # noqa: BLE001
            return False

    async def apply_pending_change(
        self, change: PendingChange
    ) -> dict[str, Any]:
        """Apply a previously-staged pending change."""
        if change.kind == "dashboard_update":
            url_path = change.payload["url_path"]
            dashboards = self._lovelace_dashboards()
            info = dashboards.get(url_path)
            if not info:
                return {"error": f"Dashboard gone: {url_path}"}
            view_path = change.payload.get("view_path")
            if view_path is not None:
                # View-level proposal: re-merge against the live config so changes
                # to other views that happened after the proposal are preserved.
                live = await self._load_dashboard_config(url_path) or {}
                views = list(live.get("views", []))
                idx, _ = _find_view(views, view_path)
                if idx is None:
                    return {"error": f"View {view_path!r} no longer exists in dashboard"}
                new_config = {**live, "views": views[:idx] + [change.payload["new_view"]] + views[idx + 1:]}
            else:
                new_config = change.payload["new_config"]
            await info["store"].async_save(new_config)
            return {"ok": True, "url_path": url_path}

        if change.kind == "service_call":
            p = change.payload
            try:
                await self.hass.services.async_call(
                    p["domain"],
                    p["service"],
                    p.get("service_data") or {},
                    blocking=True,
                    target=p.get("target") or None,
                )
            except Exception as err:  # noqa: BLE001
                return {"error": str(err)}
            return {"ok": True, "service": f"{p['domain']}.{p['service']}"}

        if change.kind in ("automation_create", "automation_update", "automation_delete"):
            return await self._apply_automation_change(change)

        if change.kind in ("script_create", "script_update", "script_delete"):
            return await self._apply_script_change(change)

        if change.kind in ("helper_create", "helper_update", "helper_delete"):
            return await self._apply_helper_change(change)

        return {"error": f"Unknown change kind: {change.kind}"}

    async def _apply_script_change(self, change: PendingChange) -> dict[str, Any]:
        path = self.hass.config.path("scripts.yaml")
        script_id = change.payload["script_id"]
        try:
            scripts = await self.hass.async_add_executor_job(_read_scripts_yaml, path)
        except Exception as err:  # noqa: BLE001
            return {"error": f"Could not read scripts.yaml: {err}"}

        if change.kind == "script_delete":
            if script_id not in scripts:
                return {"error": f"Script no longer exists: {script_id}"}
            del scripts[script_id]
        else:
            if change.kind == "script_update" and script_id not in scripts:
                return {"error": f"Script no longer exists: {script_id}"}
            scripts[script_id] = change.payload["config"]

        try:
            await self.hass.async_add_executor_job(
                _write_yaml_atomic, path, scripts
            )
        except Exception as err:  # noqa: BLE001
            return {"error": f"Could not write scripts.yaml: {err}"}

        try:
            await self.hass.services.async_call("script", "reload", {}, blocking=True)
        except Exception as err:  # noqa: BLE001
            return {"error": f"Wrote file but reload failed: {err}"}

        # Same verification as automations: if the entity didn't appear the
        # include line is missing from configuration.yaml.
        if change.kind in ("script_create", "script_update"):
            if self.hass.states.get(f"script.{script_id}") is None:
                return {
                    "error": (
                        "Wrote scripts.yaml but HA didn't load the script. "
                        "Your configuration.yaml is probably missing "
                        "`script: !include scripts.yaml`. Add that line "
                        "and restart HA."
                    )
                }

        return {"ok": True, "script_id": script_id, "kind": change.kind}

    async def _apply_helper_change(self, change: PendingChange) -> dict[str, Any]:
        p = change.payload
        if change.kind == "helper_create":
            domain = p["domain"]
            coll = self._helper_collection(domain)
            if coll is None:
                return {"error": f"The {domain} integration is not loaded"}
            config = {k: v for k, v in p["config"].items() if k != "id"}
            try:
                item = await coll.async_create_item(config)
            except Exception as err:  # noqa: BLE001
                return {"error": f"Could not create helper: {err}"}
            return {"ok": True, "entity_id": f"{domain}.{item['id']}"}

        entity_id = p["entity_id"]
        resolved = self._helper_item(entity_id)
        if isinstance(resolved, dict):
            return resolved
        coll, item = resolved
        try:
            if change.kind == "helper_update":
                config = {k: v for k, v in p["config"].items() if k != "id"}
                await coll.async_update_item(item["id"], config)
            else:  # helper_delete
                await coll.async_delete_item(item["id"])
        except Exception as err:  # noqa: BLE001
            return {"error": f"Could not {change.kind.split('_')[1]} helper: {err}"}
        return {"ok": True, "entity_id": entity_id}

    async def _apply_automation_change(
        self, change: PendingChange
    ) -> dict[str, Any]:
        path = self.hass.config.path("automations.yaml")
        try:
            automations = await self.hass.async_add_executor_job(
                _read_automations_yaml, path
            )
        except Exception as err:  # noqa: BLE001
            return {"error": f"Could not read automations.yaml: {err}"}

        if change.kind == "automation_create":
            new_config = dict(change.payload["config"])
            if not new_config.get("id"):
                new_config["id"] = str(int(time.time() * 1000))
            automations.append(new_config)
            ident = new_config["id"]
        elif change.kind == "automation_update":
            target_id = str(change.payload["automation_id"])
            for i, a in enumerate(automations):
                if str(a.get("id")) == target_id:
                    automations[i] = change.payload["config"]
                    break
            else:
                return {"error": f"Automation no longer exists: {target_id}"}
            ident = target_id
        else:  # automation_delete
            target_id = str(change.payload["automation_id"])
            before = len(automations)
            automations = [a for a in automations if str(a.get("id")) != target_id]
            if len(automations) == before:
                return {"error": f"Automation no longer exists: {target_id}"}
            ident = target_id

        try:
            await self.hass.async_add_executor_job(
                _write_yaml_atomic, path, automations
            )
        except Exception as err:  # noqa: BLE001
            return {"error": f"Could not write automations.yaml: {err}"}

        try:
            await self.hass.services.async_call(
                "automation", "reload", {}, blocking=True
            )
        except Exception as err:  # noqa: BLE001
            return {"error": f"Wrote file but reload failed: {err}"}

        # Verify the automation actually loaded into HA. If not, the user's
        # configuration.yaml is missing `automation: !include automations.yaml`
        # — surface a clear, actionable error instead of pretending success.
        if change.kind in ("automation_create", "automation_update"):
            target_id = str(ident)
            loaded = any(
                str(s.attributes.get("id")) == target_id
                for s in self.hass.states.async_all("automation")
            )
            if not loaded:
                return {
                    "error": (
                        "Wrote automations.yaml but HA didn't load the "
                        "automation. Your configuration.yaml is probably "
                        "missing `automation: !include automations.yaml`. "
                        "Add that line and restart HA."
                    )
                }

        return {"ok": True, "automation_id": ident, "kind": change.kind}


def _dashboard_summary(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a compact view-by-view summary: title, path/index, and card count."""
    views = []
    for i, view in enumerate(config.get("views", [])):
        card_count = len(view.get("cards", []))
        for section in view.get("sections", []):
            card_count += len(section.get("cards", []))
        path = view.get("path")
        views.append({
            "title": view.get("title", ""),
            # Use string index when the view has no explicit path so
            # get_dashboard_view / propose_dashboard_view_update can still
            # target it (pass "0", "1", etc. as view_path).
            "view_path": path if path is not None else str(i),
            "has_explicit_path": path is not None,
            "card_count": card_count,
        })
    return views


def _find_view(views: list[dict[str, Any]], view_path: str) -> tuple[int, dict[str, Any]] | tuple[None, None]:
    """Return (index, view) for view_path, which is either a path string or a
    string index ('0', '1', …) for views that have no explicit path set."""
    # Try by explicit path first.
    for i, v in enumerate(views):
        if v.get("path") == view_path:
            return i, v
    # Fall back to numeric index.
    if view_path.isdigit():
        idx = int(view_path)
        if 0 <= idx < len(views):
            return idx, views[idx]
    return None, None


def _format_diff(old: Any, new: Any) -> str:
    old_text = json.dumps(old, indent=2, sort_keys=True, default=str).splitlines(
        keepends=True
    )
    new_text = json.dumps(new, indent=2, sort_keys=True, default=str).splitlines(
        keepends=True
    )
    return "".join(
        difflib.unified_diff(old_text, new_text, fromfile="current", tofile="proposed")
    )


def _yaml_dump(data: Any) -> str:
    return yaml_lib.safe_dump(
        data, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def _read_automations_yaml(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml_lib.safe_load(f)
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("automations.yaml is not a list")
    return data


def _read_scripts_yaml(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml_lib.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("scripts.yaml is not a mapping")
    return data


def _write_yaml_atomic(path: str, data: Any) -> None:
    """Atomic YAML write (tmp file + os.replace)."""
    parent = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        dir=parent, prefix=os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml_lib.safe_dump(
                data,
                f,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            )
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _yaml_file_supported(hass: HomeAssistant, filename: str) -> bool:
    """Cheap heuristic: the file exists or its parent is writable."""
    path = hass.config.path(filename)
    if os.path.exists(path):
        return True
    parent = os.path.dirname(path)
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def automations_include_configured(hass: HomeAssistant) -> bool:
    """Heuristic: does configuration.yaml include automations.yaml?

    Checks for `automation: !include automations.yaml` (or the `_list` /
    `_named` variants). Misses package-based and split-config setups, but
    covers the default HA install — which is what this banner is for.
    """
    config_path = hass.config.path("configuration.yaml")
    if not os.path.exists(config_path):
        return False
    try:
        with open(config_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("automation:") and "!include" in stripped and "automations.yaml" in stripped:
            return True
    return False
