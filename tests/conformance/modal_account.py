"""Read-only checks for the workspace and image used by Modal conformance."""

import json
import os
import subprocess
import sys

from anycompute.providers.modal import _CONFORMANCE_IMAGE


def _inspect(sdk):
    try:
        workspace = sdk.Workspace.from_context().hydrate()
        if not workspace.name:
            return False, "unchecked"
    except Exception:
        return False, "unchecked"

    try:
        # The locked SDK does not support explicit Image hydration by name.
        reply = subprocess.run(
            [sys.executable, "-m", "modal", "image", "names", "list", "--json", "--prefix", _CONFORMANCE_IMAGE],
            capture_output=True, check=True, timeout=30,
        )
        images = json.loads(reply.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True, "unverified"
    if not isinstance(images, list) or any(
        not isinstance(image, dict) or not isinstance(image.get("tag"), str) for image in images
    ):
        return True, "unverified"
    found = any(image["tag"].rsplit("/", 1)[-1] == _CONFORMANCE_IMAGE for image in images)
    return True, "available" if found else "not found"


def main() -> int:
    if not (os.getenv("MODAL_TOKEN_ID") and os.getenv("MODAL_TOKEN_SECRET")):
        print("The Modal token ID and secret are required.")
        return 1

    import modal

    authenticated, image = _inspect(modal)
    print(f"Modal workspace authentication: {'verified' if authenticated else 'unverified'}")
    print(f"Modal named conformance image: {image}")
    print("Active workspace usage cap: verify in Usage & Billing")
    return 0 if authenticated else 1


if __name__ == "__main__":
    raise SystemExit(main())
