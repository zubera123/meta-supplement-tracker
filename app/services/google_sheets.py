"""Google Sheets output backed by the documented Sheets API v4."""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from google.auth import exceptions as google_auth_exceptions
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.models import SheetCandidate, SheetRowState
from app.services import ProviderConfigurationError, ProviderError


logger = logging.getLogger(__name__)

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEET_HEADERS: tuple[str, ...] = (
    "First seen",
    "Brand",
    "Region",
    "Instagram",
    "Followers",
    "Active ads",
    "Spend est.",
    "Spend source",
    "Reviews",
    "Review source",
)
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_REQUIRED_CREDENTIAL_FIELDS = frozenset(
    {"type", "project_id", "private_key", "client_email", "token_uri"}
)


@dataclass(frozen=True)
class SheetInfo:
    sheet_id: int
    row_count: int


@dataclass(frozen=True)
class SheetSyncResult:
    appended: int
    updated: int
    excluded: int
    row_states: tuple[SheetRowState, ...]


class SheetsApi(Protocol):
    def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]: ...

    def batch_update_spreadsheet(
        self, spreadsheet_id: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...

    def get_values(self, spreadsheet_id: str, range_name: str) -> dict[str, Any]: ...

    def update_values(
        self, spreadsheet_id: str, range_name: str, values: list[list[object]]
    ) -> dict[str, Any]: ...

    def batch_update_values(
        self, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


def parse_service_account_json(value: str | None) -> dict[str, Any]:
    """Parse a complete service-account JSON value without logging it."""

    if value is None or not value.strip():
        raise ProviderConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is required for Google Sheets"
        )
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProviderConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is malformed JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON must contain a JSON object"
        )
    missing = sorted(_REQUIRED_CREDENTIAL_FIELDS.difference(payload))
    if missing:
        raise ProviderConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is missing required service-account fields: "
            + ", ".join(missing)
        )
    if payload.get("type") != "service_account":
        raise ProviderConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON must be a service-account credential"
        )
    return payload


def create_sheets_api(
    service_account_json: str | None,
    *,
    retry_attempts: int = 3,
    retry_min_wait_seconds: float = 1.0,
    retry_max_wait_seconds: float = 10.0,
) -> "GoogleSheetsApiClient":
    info = parse_service_account_json(service_account_json)
    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[SHEETS_SCOPE]
        )
    except (ValueError, google_auth_exceptions.GoogleAuthError) as exc:
        raise ProviderConfigurationError(
            "GOOGLE_SERVICE_ACCOUNT_JSON contains invalid service-account credentials"
        ) from exc
    service = build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )
    return GoogleSheetsApiClient(
        service,
        retry_attempts=retry_attempts,
        retry_min_wait_seconds=retry_min_wait_seconds,
        retry_max_wait_seconds=retry_max_wait_seconds,
    )


class GoogleSheetsApiClient:
    """Small API adapter with bounded retries for documented transient errors."""

    def __init__(
        self,
        service: Any,
        *,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1.0,
        retry_max_wait_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._retry_attempts = retry_attempts
        self._retry_min_wait_seconds = retry_min_wait_seconds
        self._retry_max_wait_seconds = retry_max_wait_seconds
        self._sleep = sleep

    def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        return self._execute(
            lambda: self._service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="spreadsheetId,sheets.properties(sheetId,title,gridProperties(rowCount))",
            ),
            "read spreadsheet",
        )

    def batch_update_spreadsheet(
        self, spreadsheet_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return self._execute(
            lambda: self._service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body=body
            ),
            "update spreadsheet structure",
        )

    def get_values(self, spreadsheet_id: str, range_name: str) -> dict[str, Any]:
        return self._execute(
            lambda: self._service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueRenderOption="UNFORMATTED_VALUE",
            ),
            "read sheet values",
        )

    def update_values(
        self, spreadsheet_id: str, range_name: str, values: list[list[object]]
    ) -> dict[str, Any]:
        return self._execute(
            lambda: self._service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption="RAW",
                body={"values": values},
            ),
            "write sheet values",
        )

    def batch_update_values(
        self, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._execute(
            lambda: self._service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ),
            "batch write sheet values",
        )

    def _execute(self, request_factory: Callable[[], Any], operation: str) -> dict[str, Any]:
        for attempt in range(1, self._retry_attempts + 1):
            try:
                result = request_factory().execute(num_retries=0)
                if not isinstance(result, dict):
                    raise ProviderError(
                        f"Google Sheets API {operation} returned an invalid response"
                    )
                return result
            except HttpError as exc:
                status = int(getattr(exc.resp, "status", 0))
                if status in _TRANSIENT_HTTP_STATUSES and attempt < self._retry_attempts:
                    wait = min(
                        self._retry_min_wait_seconds * (2 ** (attempt - 1)),
                        self._retry_max_wait_seconds,
                    )
                    logger.warning(
                        "Transient Google Sheets API error; retrying",
                        extra={"operation": operation, "status": status, "attempt": attempt},
                    )
                    self._sleep(wait)
                    continue
                raise _google_http_error(operation, status, attempt) from exc
            except google_auth_exceptions.GoogleAuthError as exc:
                raise ProviderConfigurationError(
                    "Google service-account authentication failed; rotate or replace the "
                    "Railway GOOGLE_SERVICE_ACCOUNT_JSON value"
                ) from exc
        raise RuntimeError("Google Sheets retry loop ended without a result")


class GoogleSheetsProvider:
    """Maintain exactly one visible candidate row per PostgreSQL advertiser."""

    def __init__(
        self,
        *,
        spreadsheet_id: str | None,
        sheet_tab: str,
        service_account_json: str | None = None,
        api: SheetsApi | None = None,
        retry_attempts: int = 3,
        retry_min_wait_seconds: float = 1.0,
        retry_max_wait_seconds: float = 10.0,
    ) -> None:
        if spreadsheet_id is None or not spreadsheet_id.strip():
            raise ProviderConfigurationError(
                "GOOGLE_SHEET_ID is required when Google Sheets is enabled"
            )
        if not sheet_tab.strip():
            raise ProviderConfigurationError("GOOGLE_SHEET_TAB cannot be empty")
        self._spreadsheet_id = spreadsheet_id.strip()
        self._sheet_tab = sheet_tab.strip()
        self._api = api or create_sheets_api(
            service_account_json,
            retry_attempts=retry_attempts,
            retry_min_wait_seconds=retry_min_wait_seconds,
            retry_max_wait_seconds=retry_max_wait_seconds,
        )
        self._sheet_info: SheetInfo | None = None

    def ensure_ready(self, *, verify_write_access: bool = False) -> SheetInfo:
        spreadsheet = self._api.get_spreadsheet(self._spreadsheet_id)
        sheet_info = _find_sheet(spreadsheet, self._sheet_tab)
        if sheet_info is None:
            response = self._api.batch_update_spreadsheet(
                self._spreadsheet_id,
                {"requests": [{"addSheet": {"properties": {"title": self._sheet_tab}}}]},
            )
            sheet_info = _created_sheet_info(response)

        header_range = f"{_a1_tab(self._sheet_tab)}!A1:J1"
        response = self._api.get_values(self._spreadsheet_id, header_range)
        values = response.get("values")
        first_row = values[0] if isinstance(values, list) and values else []
        normalized_header = _normalize_row(first_row)
        if not any(str(value).strip() for value in normalized_header):
            self._api.update_values(
                self._spreadsheet_id, header_range, [list(SHEET_HEADERS)]
            )
        elif tuple(str(value) for value in normalized_header) != SHEET_HEADERS:
            raise ProviderError(
                "Google Sheet header mismatch. The Candidates tab must contain exactly: "
                + ", ".join(SHEET_HEADERS)
            )
        elif verify_write_access:
            # An idempotent header update proves Editor access without adding rows.
            self._api.update_values(
                self._spreadsheet_id, header_range, [list(SHEET_HEADERS)]
            )

        self._sheet_info = sheet_info
        return sheet_info

    def sync_candidates(
        self,
        candidates: Sequence[SheetCandidate],
        row_states: Mapping[int, SheetRowState],
    ) -> SheetSyncResult:
        sheet_info = self._sheet_info or self.ensure_ready()
        unique_candidates = {item.advertiser_id: item for item in candidates}
        excluded = len(candidates) - len(unique_candidates)
        if not unique_candidates:
            return SheetSyncResult(0, 0, excluded, ())

        all_range = f"{_a1_tab(self._sheet_tab)}!A:J"
        response = self._api.get_values(self._spreadsheet_id, all_range)
        raw_rows = response.get("values")
        rows = (
            [_normalize_row(row) for row in raw_rows]
            if isinstance(raw_rows, list)
            else []
        )
        if not rows or tuple(str(value) for value in rows[0]) != SHEET_HEADERS:
            raise ProviderError("Google Sheet headers changed during candidate synchronization")

        updates: list[dict[str, Any]] = []
        new_states: list[SheetRowState] = []
        assigned_rows: dict[int, int] = {}
        next_row = max(2, len(rows) + 1)
        appended = 0
        updated = 0

        for candidate in unique_candidates.values():
            state = row_states.get(candidate.advertiser_id)
            row_number = _resolve_existing_row(
                rows,
                candidate,
                state,
                spreadsheet_id=self._spreadsheet_id,
                sheet_tab=self._sheet_tab,
            )
            if row_number is not None:
                owner = assigned_rows.get(row_number)
                if owner is not None and owner != candidate.advertiser_id:
                    raise ProviderError(
                        "Two PostgreSQL advertisers resolve to the same visible Sheet row"
                    )
                assigned_rows[row_number] = candidate.advertiser_id
                existing = rows[row_number - 1]
                visible = _visible_values(candidate, existing)
                updates.append(
                    {
                        "range": f"{_a1_tab(self._sheet_tab)}!A{row_number}:F{row_number}",
                        "values": [visible[:6]],
                    }
                )
                rows[row_number - 1] = [*visible[:6], *existing[6:10]]
                updated += 1
            else:
                row_number = next_row
                next_row += 1
                assigned_rows[row_number] = candidate.advertiser_id
                visible = _visible_values(candidate, None)
                updates.append(
                    {
                        "range": f"{_a1_tab(self._sheet_tab)}!A{row_number}:J{row_number}",
                        "values": [visible],
                    }
                )
                rows.append(visible)
                appended += 1

            new_states.append(
                SheetRowState(
                    advertiser_id=candidate.advertiser_id,
                    spreadsheet_id=self._spreadsheet_id,
                    sheet_tab=self._sheet_tab,
                    row_number=row_number,
                    last_exported_first_seen=candidate.first_seen,
                    last_exported_brand=candidate.brand,
                    last_exported_region=str(visible[2]) or None,
                    last_exported_instagram=str(visible[3]) or None,
                )
            )

        required_rows = next_row - 1
        if required_rows > sheet_info.row_count:
            self._api.batch_update_spreadsheet(
                self._spreadsheet_id,
                {
                    "requests": [
                        {
                            "appendDimension": {
                                "sheetId": sheet_info.sheet_id,
                                "dimension": "ROWS",
                                "length": required_rows - sheet_info.row_count,
                            }
                        }
                    ]
                },
            )
            self._sheet_info = SheetInfo(sheet_info.sheet_id, required_rows)

        self._api.batch_update_values(self._spreadsheet_id, updates)
        return SheetSyncResult(
            appended=appended,
            updated=updated,
            excluded=excluded,
            row_states=tuple(new_states),
        )


def _find_sheet(spreadsheet: dict[str, Any], title: str) -> SheetInfo | None:
    sheets = spreadsheet.get("sheets")
    if not isinstance(sheets, list):
        raise ProviderError("Google Sheets API returned no sheet metadata")
    for sheet in sheets:
        properties = sheet.get("properties") if isinstance(sheet, dict) else None
        if not isinstance(properties, dict) or properties.get("title") != title:
            continue
        sheet_id = properties.get("sheetId")
        grid = properties.get("gridProperties")
        row_count = grid.get("rowCount") if isinstance(grid, dict) else None
        if not isinstance(sheet_id, int) or not isinstance(row_count, int):
            raise ProviderError("Google Sheets API returned malformed tab properties")
        return SheetInfo(sheet_id=sheet_id, row_count=row_count)
    return None


def _created_sheet_info(response: dict[str, Any]) -> SheetInfo:
    replies = response.get("replies")
    reply = replies[0] if isinstance(replies, list) and replies else None
    add_sheet = reply.get("addSheet") if isinstance(reply, dict) else None
    properties = add_sheet.get("properties") if isinstance(add_sheet, dict) else None
    sheet_id = properties.get("sheetId") if isinstance(properties, dict) else None
    grid = properties.get("gridProperties") if isinstance(properties, dict) else None
    row_count = grid.get("rowCount") if isinstance(grid, dict) else 1000
    if not isinstance(sheet_id, int) or not isinstance(row_count, int):
        raise ProviderError("Google Sheets API did not return the created tab properties")
    return SheetInfo(sheet_id=sheet_id, row_count=row_count)


def _resolve_existing_row(
    rows: list[list[object]],
    candidate: SheetCandidate,
    state: SheetRowState | None,
    *,
    spreadsheet_id: str,
    sheet_tab: str,
) -> int | None:
    if state is not None and (
        state.spreadsheet_id == spreadsheet_id and state.sheet_tab == sheet_tab
    ):
        if 2 <= state.row_number <= len(rows) and _matches_state(
            rows[state.row_number - 1], state
        ):
            return state.row_number
        matches = [
            number
            for number, row in enumerate(rows[1:], start=2)
            if _matches_state(row, state)
        ]
        if len(matches) == 1:
            return matches[0]
        raise ProviderError(
            "A stored Google Sheet row mapping could not be reconciled safely; "
            "restore the advertiser row or repair its PostgreSQL mapping"
        )

    matches = [
        number
        for number, row in enumerate(rows[1:], start=2)
        if _matches_candidate(row, candidate)
    ]
    if len(matches) > 1:
        raise ProviderError(
            "The Candidates tab already contains duplicate rows for a qualifying advertiser"
        )
    return matches[0] if matches else None


def _matches_state(row: list[object], state: SheetRowState) -> bool:
    return (
        str(row[0]) == state.last_exported_first_seen.isoformat()
        and str(row[1]) == state.last_exported_brand
        and str(row[3]) == (state.last_exported_instagram or "")
    )


def _matches_candidate(row: list[object], candidate: SheetCandidate) -> bool:
    return (
        str(row[0]) == candidate.first_seen.isoformat()
        and str(row[1]) == candidate.brand
        and str(row[3]) == _instagram(candidate.instagram_username)
    )


def _visible_values(
    candidate: SheetCandidate, existing: list[object] | None
) -> list[object]:
    existing = existing or [""] * len(SHEET_HEADERS)
    region = candidate.region or str(existing[2])
    instagram = _instagram(candidate.instagram_username) or str(existing[3])
    return [
        candidate.first_seen.isoformat(),
        candidate.brand,
        region,
        instagram,
        candidate.followers,
        candidate.active_ads,
        "",
        "",
        "",
        "",
    ]


def _instagram(username: str | None) -> str:
    if username is None or not username.strip():
        return ""
    return "@" + username.strip().lstrip("@")


def _normalize_row(row: object) -> list[object]:
    values = list(row) if isinstance(row, list) else []
    return (values + [""] * len(SHEET_HEADERS))[: len(SHEET_HEADERS)]


def _a1_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _google_http_error(operation: str, status: int, attempt: int) -> ProviderError:
    if status == 401:
        return ProviderConfigurationError(
            "Google service-account authentication was rejected; replace "
            "GOOGLE_SERVICE_ACCOUNT_JSON with a valid current JSON key"
        )
    if status == 403:
        return ProviderError(
            "Google Sheets access denied. Share the target spreadsheet with the "
            "service-account client_email as Editor and verify the Sheets API is enabled"
        )
    if status == 404:
        return ProviderError(
            "Google spreadsheet not found. Verify GOOGLE_SHEET_ID and share that "
            "spreadsheet with the service-account client_email as Editor"
        )
    if status in _TRANSIENT_HTTP_STATUSES:
        return ProviderError(
            f"Google Sheets API {operation} failed after {attempt} attempts (HTTP {status})"
        )
    return ProviderError(f"Google Sheets API {operation} failed with HTTP {status}")
