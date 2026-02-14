# Feature: Audit Dashboard

> Web UI for browsing the audit log with filtering, pagination, and summary stats — served on the existing health port via aiohttp + Jinja2.

## Overview

agentpass records every tool request in an append-only SQLite audit log, but there is currently no way to view this data. The audit dashboard adds a lightweight web UI at `/audit/` on the health HTTP port (default 8080), providing a filterable log table and summary statistics. No authentication — security by binding to `127.0.0.1` by default.

## Requirements

### Functional Requirements

- [ ] FR1: Serve an audit log table at `GET /audit/` showing recent entries (newest first)
- [ ] FR2: Filter by tool name, decision (allow/deny/ask), resolution (approved/denied/timed_out/executed), and date range
- [ ] FR3: Paginate results with offset/limit (default 50 per page, configurable via query params)
- [ ] FR4: Show summary stats at the top: total requests, approval rate, most-used tools, requests in last 24h
- [ ] FR5: Click a row to expand/view full details (args, execution result, timestamps)
- [ ] FR6: Provide a JSON API at `GET /audit/api/log` for programmatic access with same filters
- [ ] FR7: Provide a JSON API at `GET /audit/api/stats` for summary statistics

### Non-Functional Requirements

- [ ] NFR1: Dashboard loads in under 500ms for up to 10,000 audit entries
- [ ] NFR2: Health server binds to `127.0.0.1` by default (configurable via `gateway.health_host`)
- [ ] NFR3: No authentication required — security via network binding
- [ ] NFR4: Jinja2 templates with inline CSS/JS — no npm build step, no external CDN

## User Experience

### User Flows

1. **Browse audit log:** User opens `http://localhost:8080/audit/` in browser, sees stats summary and paginated log table
2. **Filter entries:** User selects tool name from dropdown, picks a date range, clicks "Filter" — table updates
3. **View details:** User clicks a row — expands to show full args JSON, execution result, resolution info
4. **Navigate pages:** User clicks "Next"/"Previous" to page through results
5. **Programmatic access:** Agent or script calls `/audit/api/log?tool_name=ha_get_state&limit=10` and gets JSON

### UI Layout

```
┌─────────────────────────────────────────────────┐
│  agentpass — Audit Dashboard                    │
├─────────────────────────────────────────────────┤
│  Stats Cards:                                   │
│  [Total Requests] [Last 24h] [Approval Rate]    │
│  [Top Tool: ha_get_state (42%)]                 │
├─────────────────────────────────────────────────┤
│  Filters:                                       │
│  Tool: [dropdown]  Decision: [dropdown]         │
│  Resolution: [dropdown]  From: [date] To: [date]│
│  [Apply]  [Clear]                               │
├─────────────────────────────────────────────────┤
│  Timestamp  │ Tool      │ Signature │ Decision  │
│─────────────┼───────────┼───────────┼───────────│
│  2026-02-14 │ ha_get_.. │ sensor..  │ ✅ allow  │
│  > expanded: args={...}, result={...}           │
│  2026-02-14 │ ha_call.. │ light..   │ 🔶 ask    │
│  ...                                            │
├─────────────────────────────────────────────────┤
│  Showing 1-50 of 234  [← Prev] [Next →]        │
└─────────────────────────────────────────────────┘
```

### Edge Cases

| Scenario | Expected Behavior |
| -------- | ----------------- |
| Empty audit log | Show "No audit entries yet" with zero stats |
| No entries match filters | Show "No entries match your filters" with a "Clear filters" link |
| Very long args/result JSON | Truncate in table row, show full JSON in expanded detail view |
| Invalid filter values | Ignore invalid params, use defaults |
| Database unavailable | Show error banner "Database unavailable" |

## Technical Design

### Affected Components

- `src/agentpass/db.py` — Add filtered query methods with pagination, stats queries
- `src/agentpass/server.py` — Add `setup_dashboard_routes()` to register aiohttp routes
- `src/agentpass/__main__.py` — Wire dashboard routes into health app, update binding
- `src/agentpass/config.py` — Add `health_host` config field (default `127.0.0.1`)
- `src/agentpass/dashboard/` — New package for templates and route handlers
- `src/agentpass/dashboard/__init__.py` — Route setup function
- `src/agentpass/dashboard/templates/audit.html` — Jinja2 template
- `src/agentpass/dashboard/routes.py` — aiohttp request handlers

### Data Model

No schema changes. Existing `audit_log` table has all needed columns. New query methods only.

### API Changes

**New HTTP endpoints (on health port):**

- `GET /audit/` — HTML dashboard page
  - Query params: `tool_name`, `decision`, `resolution`, `from` (ISO date), `to` (ISO date), `page` (1-based), `per_page` (default 50)

- `GET /audit/api/log` — JSON audit log
  - Same query params as above
  - Response: `{"entries": [...], "total": 234, "page": 1, "per_page": 50, "pages": 5}`

- `GET /audit/api/stats` — JSON statistics
  - Response: `{"total_requests": 234, "last_24h": 12, "approval_rate": 0.85, "top_tools": [{"name": "ha_get_state", "count": 98}], "decision_breakdown": {"allow": 150, "deny": 10, "ask": 74}}`

### Database Layer Extensions

```python
# New method signatures for db.py:

async def get_audit_log_filtered(
    self,
    tool_name: str | None = None,
    decision: str | None = None,
    resolution: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEntry], int]:
    """Return (entries, total_count) with filtering and pagination."""

async def get_audit_stats(self) -> dict[str, Any]:
    """Return summary statistics from the audit log."""
```

### Dependencies

- Existing: `aiohttp` (already used for health endpoint)
- New: `jinja2` (add to pyproject.toml, `>=3.1.0,<4.0`)
- New: `aiohttp-jinja2` (add to pyproject.toml, `>=1.6,<2.0`) — integrates Jinja2 with aiohttp

### Config Changes

Add `health_host` field to `GatewayConfig`:

```yaml
gateway:
  host: "0.0.0.0"
  port: 8443
  health_port: 8080
  health_host: "127.0.0.1"   # NEW — dashboard/health bind address
```

Default: `127.0.0.1` (secure). Docker users can set to `0.0.0.0` if needed (Docker HEALTHCHECK uses localhost internally, so default works).

## Implementation Plan

### Phase 1: Database Layer

1. [ ] Add `get_audit_log_filtered()` method to `db.py` with WHERE clause building and COUNT query
2. [ ] Add `get_audit_stats()` method to `db.py` with aggregate queries
3. [ ] Add `health_host` config field (default `127.0.0.1`) to `config.py`
4. [ ] Update health server binding in `__main__.py` to use `health_host`
5. [ ] Write tests for new DB methods

### Phase 2: API Routes

1. [ ] Create `src/agentpass/dashboard/__init__.py` with route setup
2. [ ] Create `src/agentpass/dashboard/routes.py` with handlers for `/audit/api/log` and `/audit/api/stats`
3. [ ] Wire routes into health app in `__main__.py`
4. [ ] Write tests for API endpoints

### Phase 3: Web UI

1. [ ] Add `jinja2` and `aiohttp-jinja2` to `pyproject.toml`
2. [ ] Create `src/agentpass/dashboard/templates/audit.html` with log table, filters, stats, pagination
3. [ ] Add `GET /audit/` handler that renders the template
4. [ ] Style with inline CSS (clean, minimal, responsive)
5. [ ] Add inline JS for row expansion and filter form handling
6. [ ] Write tests for HTML rendering

## Test Plan

### Unit Tests

- [ ] `test_get_audit_log_filtered` — filter by each field individually and combined
- [ ] `test_get_audit_log_filtered_pagination` — offset/limit and total count
- [ ] `test_get_audit_log_filtered_empty` — no matches returns empty list with count 0
- [ ] `test_get_audit_stats` — correct counts, approval rate, top tools
- [ ] `test_get_audit_stats_empty` — empty log returns zeroed stats
- [ ] `test_health_host_config` — parsed correctly, defaults to 127.0.0.1

### Integration Tests

- [ ] `test_audit_api_log_endpoint` — HTTP GET returns JSON with correct structure
- [ ] `test_audit_api_log_filters` — query params filter results correctly
- [ ] `test_audit_api_stats_endpoint` — returns correct stats JSON
- [ ] `test_audit_html_renders` — GET /audit/ returns 200 with HTML content-type
- [ ] `test_audit_html_empty_state` — empty log shows appropriate message

### Manual Testing

- [ ] Open dashboard in browser, verify layout and responsiveness
- [ ] Filter by tool name and verify results update
- [ ] Click through pagination
- [ ] Expand a row to see full details
- [ ] Verify stats match actual data

## Open Questions

_None — all questions resolved during discovery._

## Decision Log

| Decision | Rationale | Date |
| -------- | --------- | ---- |
| Web UI over Telegram commands | Richer interface for browsing, tables, stats | 2026-02-14 |
| No auth, localhost binding | Simplest security model, access via SSH tunnel | 2026-02-14 |
| Same port as health (8080) | Fewer ports to manage, reuse existing aiohttp server | 2026-02-14 |
| Jinja2 templates | Cleaner separation of HTML from Python than inline strings | 2026-02-14 |
| All filters (tool, decision, resolution, date) | Comprehensive filtering for production debugging | 2026-02-14 |
| Default bind 127.0.0.1 | Secure by default, Docker HEALTHCHECK still works (runs inside container) | 2026-02-14 |
