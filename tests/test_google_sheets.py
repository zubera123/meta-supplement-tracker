"""Mocked tests for Google Sheets candidate output."""

import json
import re
from datetime import date
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.config import Settings
from app.jobs import brand_scan
from app.models import SheetCandidate, SheetRowState
from app.services import ProviderConfigurationError, ProviderError
from app.services.google_sheets import (
    SHEET_HEADERS,
    GoogleSheetsApiClient,
    GoogleSheetsProvider,
    parse_service_account_json,
)


SPREADSHEET_ID = "sheet-id"
TAB = "Candidates"


def credential_json() -> str:
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "example-project",
            "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-live-key\n-----END PRIVATE KEY-----\n",
            "client_email": "tracker@example-project.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


def candidate(
    *,
    advertiser_id: int = 1,
    first_seen: date = date(2026, 8, 1),
    brand: str = "Example Supplements",
    region: str = "UK",
    instagram: str | None = "example_supplements",
    followers: int = 25_000,
    active_ads: int = 3,
) -> SheetCandidate:
    return SheetCandidate(
        advertiser_id=advertiser_id,
        first_seen=first_seen,
        brand=brand,
        region=region,
        instagram_username=instagram,
        followers=followers,
        active_ads=active_ads,
    )


def row_state(
    *,
    advertiser_id: int = 1,
    row_number: int = 2,
    brand: str = "Example Supplements",
    instagram: str | None = "@example_supplements",
) -> SheetRowState:
    return SheetRowState(
        advertiser_id=advertiser_id,
        spreadsheet_id=SPREADSHEET_ID,
        sheet_tab=TAB,
        row_number=row_number,
        last_exported_first_seen=date(2026, 8, 1),
        last_exported_brand=brand,
        last_exported_region="UK",
        last_exported_instagram=instagram,
    )


class FakeSheetsApi:
    def __init__(
        self,
        *,
        tab_exists: bool = True,
        rows: list[list[object]] | None = None,
        row_count: int = 1000,
    ) -> None:
        self.tab_exists = tab_exists
        self.rows = rows if rows is not None else []
        self.row_count = row_count
        self.structure_updates: list[dict[str, Any]] = []
        self.value_updates: list[tuple[str, list[list[object]]]] = []
        self.batch_value_updates: list[list[dict[str, Any]]] = []

    def get_spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        assert spreadsheet_id == SPREADSHEET_ID
        sheets = []
        if self.tab_exists:
            sheets.append(
                {
                    "properties": {
                        "sheetId": 7,
                        "title": TAB,
                        "gridProperties": {"rowCount": self.row_count},
                    }
                }
            )
        return {"spreadsheetId": spreadsheet_id, "sheets": sheets}

    def batch_update_spreadsheet(
        self, spreadsheet_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        assert spreadsheet_id == SPREADSHEET_ID
        self.structure_updates.append(body)
        request = body["requests"][0]
        if "addSheet" in request:
            self.tab_exists = True
            self.row_count = 1000
            return {
                "replies": [
                    {
                        "addSheet": {
                            "properties": {
                                "sheetId": 7,
                                "title": TAB,
                                "gridProperties": {"rowCount": 1000},
                            }
                        }
                    }
                ]
            }
        append = request["appendDimension"]
        self.row_count += append["length"]
        return {"replies": [{}]}

    def get_values(self, spreadsheet_id: str, range_name: str) -> dict[str, Any]:
        assert spreadsheet_id == SPREADSHEET_ID
        if range_name.endswith("!A1:J1"):
            return {"values": self.rows[:1]}
        return {"values": [list(row) for row in self.rows]}

    def update_values(
        self, spreadsheet_id: str, range_name: str, values: list[list[object]]
    ) -> dict[str, Any]:
        assert spreadsheet_id == SPREADSHEET_ID
        self.value_updates.append((range_name, values))
        if range_name.endswith("!A1:J1"):
            if self.rows:
                self.rows[0] = list(values[0])
            else:
                self.rows.append(list(values[0]))
        return {"updatedRows": len(values)}

    def batch_update_values(
        self, spreadsheet_id: str, data: list[dict[str, Any]]
    ) -> dict[str, Any]:
        assert spreadsheet_id == SPREADSHEET_ID
        self.batch_value_updates.append(data)
        for update in data:
            match = re.search(r"!A(\d+):([A-Z])(\d+)$", update["range"])
            assert match is not None
            row_number = int(match.group(1))
            width = {"F": 6, "H": 8, "J": 10}[match.group(2)]
            while len(self.rows) < row_number:
                self.rows.append([])
            existing = (self.rows[row_number - 1] + [""] * 10)[:10]
            incoming = list(update["values"][0])
            existing[:width] = incoming
            self.rows[row_number - 1] = existing
        return {"totalUpdatedRows": len(data)}


def provider(api: FakeSheetsApi) -> GoogleSheetsProvider:
    return GoogleSheetsProvider(
        spreadsheet_id=SPREADSHEET_ID,
        sheet_tab=TAB,
        api=api,
    )


def test_service_account_json_parsing() -> None:
    parsed = parse_service_account_json(credential_json())

    assert parsed["type"] == "service_account"
    assert parsed["client_email"] == "tracker@example-project.iam.gserviceaccount.com"


@pytest.mark.parametrize("value", [None, "", "not-json", "[]", '{"type":"user"}'])
def test_malformed_credential_json_is_rejected(value: str | None) -> None:
    with pytest.raises(ProviderConfigurationError):
        parse_service_account_json(value)


def test_inaccessible_spreadsheet_fails_clearly() -> None:
    response = httplib2.Response({"status": "403"})

    class Request:
        def execute(self, *, num_retries: int) -> dict[str, Any]:
            raise HttpError(response, b'{"error":{"message":"forbidden"}}')

    class Service:
        def spreadsheets(self) -> "Service":
            return self

        def get(self, **_: object) -> Request:
            return Request()

    api = GoogleSheetsApiClient(Service(), retry_attempts=3, sleep=lambda _: None)

    with pytest.raises(ProviderError, match="Share the target spreadsheet"):
        api.get_spreadsheet(SPREADSHEET_ID)


def test_missing_tab_is_created() -> None:
    api = FakeSheetsApi(tab_exists=False)

    provider(api).ensure_ready()

    assert api.tab_exists is True
    assert api.structure_updates[0]["requests"][0]["addSheet"]["properties"] == {
        "title": TAB
    }


def test_empty_sheet_gets_exact_header_once() -> None:
    api = FakeSheetsApi(rows=[])
    sheets = provider(api)

    sheets.ensure_ready()
    sheets.ensure_ready()

    assert api.rows == [list(SHEET_HEADERS)]
    assert len(api.value_updates) == 1


def test_correct_existing_headers_are_not_duplicated() -> None:
    api = FakeSheetsApi(rows=[list(SHEET_HEADERS)])

    provider(api).ensure_ready()

    assert api.value_updates == []
    assert api.rows == [list(SHEET_HEADERS)]


def test_new_candidate_appends_with_blank_spend_and_reviews() -> None:
    api = FakeSheetsApi(rows=[list(SHEET_HEADERS)])
    sheets = provider(api)

    result = sheets.sync_candidates([candidate()], {})

    assert result.appended == 1
    assert result.updated == 0
    assert api.rows[1] == [
        "2026-08-01",
        "Example Supplements",
        "UK",
        "@example_supplements",
        25_000,
        3,
        "",
        "",
        "",
        "",
    ]


def test_existing_advertiser_updates_without_duplicate() -> None:
    api = FakeSheetsApi(
        rows=[
            list(SHEET_HEADERS),
            ["2026-08-01", "Example Supplements", "UK", "@example_supplements", 20_000, 2],
        ]
    )
    sheets = provider(api)

    result = sheets.sync_candidates(
        [candidate(followers=30_000, active_ads=5)], {1: row_state()}
    )

    assert result.updated == 1
    assert result.appended == 0
    assert len(api.rows) == 2
    assert api.rows[1][4:6] == [30_000, 5]


def test_repeated_candidate_input_is_deduplicated() -> None:
    api = FakeSheetsApi(rows=[list(SHEET_HEADERS)])
    sheets = provider(api)

    result = sheets.sync_candidates([candidate(), candidate()], {})

    assert result.appended == 1
    assert result.excluded == 1
    assert len(api.rows) == 2


def test_first_seen_is_preserved_from_candidate_database_value() -> None:
    api = FakeSheetsApi(
        rows=[
            list(SHEET_HEADERS),
            ["2026-08-01", "Example Supplements", "UK", "@example_supplements", 20_000, 2],
        ]
    )

    provider(api).sync_candidates(
        [candidate(first_seen=date(2026, 8, 1), followers=22_000)],
        {1: row_state()},
    )

    assert api.rows[1][0] == "2026-08-01"


def test_existing_spend_and_review_values_are_preserved() -> None:
    future_values = ["£8,000", "model-v1", 450, "Trustpilot"]
    api = FakeSheetsApi(
        rows=[
            list(SHEET_HEADERS),
            [
                "2026-08-01",
                "Example Supplements",
                "UK",
                "@example_supplements",
                20_000,
                2,
                *future_values,
            ],
        ]
    )

    provider(api).sync_candidates(
        [candidate(followers=24_000, active_ads=4)], {1: row_state()}
    )

    assert api.rows[1][6:10] == future_values
    written_range = api.batch_value_updates[0][0]["range"]
    assert written_range.endswith("!A2:F2")


def test_spend_columns_update_while_review_columns_are_preserved() -> None:
    api = FakeSheetsApi(
        rows=[
            list(SHEET_HEADERS),
            [
                "2026-08-01", "Example Supplements", "UK", "@example_supplements",
                20_000, 2, "$1k–$2k/mo", "Old model", 450, "Trustpilot",
            ],
        ]
    )
    updated = candidate().model_copy(
        update={
            "spend_estimate": "$8k–$14k/mo",
            "spend_source": "Activity model - very rough",
        }
    )

    provider(api).sync_candidates([updated], {1: row_state()})

    assert api.rows[1][6:10] == [
        "$8k–$14k/mo", "Activity model - very rough", 450, "Trustpilot"
    ]
    assert api.batch_value_updates[0][0]["range"].endswith("!A2:H2")


def test_unknown_username_does_not_blank_existing_sheet_value() -> None:
    api = FakeSheetsApi(
        rows=[
            list(SHEET_HEADERS),
            ["2026-08-01", "Example Supplements", "UK", "@known_handle", 20_000, 2],
        ]
    )
    state = row_state(instagram="@known_handle")

    provider(api).sync_candidates(
        [candidate(instagram=None, followers=24_000)], {1: state}
    )

    assert api.rows[1][3] == "@known_handle"


def test_transient_google_error_is_retried() -> None:
    response = httplib2.Response({"status": "503"})
    calls = 0

    class Request:
        def execute(self, *, num_retries: int) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HttpError(response, b'{"error":{"message":"unavailable"}}')
            return {"spreadsheetId": SPREADSHEET_ID, "sheets": []}

    class Service:
        def spreadsheets(self) -> "Service":
            return self

        def get(self, **_: object) -> Request:
            return Request()

    sleeps: list[float] = []
    api = GoogleSheetsApiClient(
        Service(),
        retry_attempts=2,
        retry_min_wait_seconds=0.5,
        retry_max_wait_seconds=1,
        sleep=sleeps.append,
    )

    assert api.get_spreadsheet(SPREADSHEET_ID)["spreadsheetId"] == SPREADSHEET_ID
    assert calls == 2
    assert sleeps == [0.5]


def test_check_sheets_only_verifies_sheet_access(monkeypatch, capsys) -> None:
    calls: list[bool] = []

    class CheckProvider:
        def ensure_ready(self, *, verify_write_access: bool = False) -> None:
            calls.append(verify_write_access)

    monkeypatch.setattr(
        brand_scan, "_build_sheets_provider", lambda settings: CheckProvider()
    )
    settings = Settings(_env_file=None, google_sheet_tab=TAB)

    assert brand_scan._check_sheets(settings) == 0
    assert calls == [True]
    assert json.loads(capsys.readouterr().out) == {
        "spreadsheet": "reachable",
        "tab": TAB,
        "headers": "ready",
        "write_access": "verified",
    }
