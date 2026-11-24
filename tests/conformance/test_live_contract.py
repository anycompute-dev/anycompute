from hashlib import sha256


def test_working_directory_and_literal_args(conformance):
    args = ("two words", "'single' and \"double\"", "$HOME;*|&<>()", "")
    code, report = conformance.run(("sh", "-c", 'pwd; printf "%s\\n" "$@"', "args", *args))
    assert code == report["exit_code"] == 0 and report["error"] is None
    assert report["stdout"] == "/tmp/anycompute\n" + "".join(f"{arg}\n" for arg in args)
    assert report["stderr"] == ""


def test_exit_code_and_separate_decoded_output(conformance):
    command = "printf 'output\\377\\n'; printf 'diagnostic\\n' >&2; exit 7"
    code, report = conformance.run(("sh", "-c", command))
    assert code == report["exit_code"] == 7 and report["error"] is None
    assert report["stdout"] == "output\ufffd\n"
    assert report["stderr"] == "diagnostic\n"


def test_relative_files_and_confirmed_delete(conformance, tmp_path):
    payload = b"\x00\xffbinary\n"
    source = tmp_path / "fixture.bin"
    source.write_bytes(payload)
    output = tmp_path / "output"
    code, report = conformance.run(
        ("sh", "-c", "mkdir -p nested && cp fixture.bin nested/copy.bin"),
        files=(source,), read=("nested/copy.bin", "nested", "not-present"), out=output,
    )
    assert code == report["exit_code"] == 0 and report["error"] is None
    saved = output / conformance.run_id / "nested/copy.bin"
    assert report["files"] == {"nested/copy.bin": {
        "size": len(payload), "sha256": sha256(payload).hexdigest(), "saved_path": str(saved),
    }}
    assert saved.read_bytes() == payload
    assert report["missing"] == ["nested", "not-present"]
    conformance.confirm_deleted(report["sandbox_id"])


def test_lifetime(conformance, record_property):
    code, report, expiry = conformance.lifetime()
    record_property("lifetime_seconds", expiry.elapsed_s)
    assert expiry.status in ("pass", "partial"), f"{expiry.reason} ({expiry.elapsed_s:.3f}s)"
    assert code == 123 and report["error"] is not None
    assert report["error"]["type"] == "SandboxTimeoutError"
    assert report["sandbox_id"] == expiry.sandbox_id
    if expiry.status == "partial":
        conformance.confirm_deleted(expiry.sandbox_id)


def test_kill_client(conformance, record_property):
    expiry = conformance.kill_client()
    record_property("kill_client_lifetime_seconds", expiry.elapsed_s)
    assert expiry.status in ("pass", "partial")
