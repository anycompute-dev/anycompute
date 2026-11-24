"""Keep decoded provider recordings under tests/fixtures/ free of credentials."""

import json
import re
from pathlib import Path

import pytest


_HEADERS = {"authorization", "proxyauthorization", "xapikey", "cookie", "setcookie"}
_FIELDS = _HEADERS | {"key", "token", "secret", "credentials", "jwt", "oauthclientid"}
_FIELD_SUFFIXES = (
    "apikey", "apitoken", "accesstoken", "refreshtoken", "bearertoken", "jwttoken", "idtoken", "sessiontoken",
    "tokenid", "tokensecret", "clientsecret", "privatekey", "password", "accesskeyid", "accesskey",
)
_TOKEN = re.compile(
    r"(?<![\w-])(?:a[ks]-[A-Za-z0-9]{16,}|"
    r"(?:e2b_|dtn_|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})"
)


def _normalized(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def _credential_field(name):
    field = _normalized(name)
    return field in _FIELDS or field.endswith(_FIELD_SUFFIXES) or _TOKEN.search(name) is not None


def _has_credential(value):
    if isinstance(value, tuple):
        return any(_credential_field(name) or _has_credential(field) for name, field in value)
    if isinstance(value, list):
        return any(_has_credential(item) for item in value)
    if isinstance(value, str):
        return _TOKEN.search(value) is not None or any(
            _normalized(line.partition(":")[0]) in _HEADERS for line in value.splitlines()
        )
    return False


def _find_problems(directory):
    if directory.is_symlink():
        return [("tests/fixtures", "recording directory must be local")]

    problems = []
    for recording in sorted(directory.rglob("*")):
        if not recording.is_symlink() and recording.is_dir():
            continue
        if recording.is_symlink() or not recording.is_file() or recording.suffix != ".json":
            problem = "recordings must be regular .json files"
        else:
            try:
                # Tuples preserve duplicate object keys that a dict would discard.
                decoded = json.loads(recording.read_text(encoding="utf-8"), object_pairs_hook=tuple)
                problem = "remove credential fields or tokens" if _has_credential(decoded) else None
            except (OSError, ValueError, RecursionError):
                problem = "expected decoded UTF-8 JSON"
        if problem:
            relative_path = recording.relative_to(directory).as_posix()
            problems.append((_TOKEN.sub("<redacted>", relative_path), problem))
    return problems


def test_recorded_provider_fixtures_are_keyless():
    directory = Path(__file__).resolve().parents[1] / "fixtures"
    assert _find_problems(directory) == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"headers":{"aUtHoRiZaTiOn":""},"headers":{}}',
        '{"headers":[["X-API-Key","fake"]]}',
        '{"response":"HTTP/1.1 200 OK\\nSet-Cookie: fake"}',
        '{"body":{"envdAccessToken":"fake"}}',
        '{"body":{"MODAL_TOKEN_SECRET":"fake"}}',
        *[json.dumps({"text": prefix + "x" * 24}) for prefix in ("ak-", "as-", "e2b_", "dtn_", "ghp_")],
    ],
    ids=[
        "duplicate-header", "header-pairs", "raw-cookie", "nested-token", "token-field",
        "modal-id", "modal-secret", "e2b-key", "daytona-key", "github-token",
    ],
)
def test_fixture_scan_rejects_credential_material(tmp_path, payload):
    provider = tmp_path / "provider"
    provider.mkdir()
    (provider / "response.json").write_text(payload, encoding="utf-8")
    assert _find_problems(tmp_path) == [("provider/response.json", "remove credential fields or tokens")]


def test_fixture_scan_accepts_safe_json_and_rejects_unscannable_files(tmp_path):
    (tmp_path / "safe.json").write_text('{"body":{"sandboxId":"sb-123","nextToken":"cursor"}}')
    (tmp_path / "malformed.json").write_text("{")
    (tmp_path / "recording.txt").write_text("unscannable")
    (tmp_path / "symlink.json").symlink_to(tmp_path / "safe.json")
    assert _find_problems(tmp_path) == [
        ("malformed.json", "expected decoded UTF-8 JSON"),
        ("recording.txt", "recordings must be regular .json files"),
        ("symlink.json", "recordings must be regular .json files"),
    ]
