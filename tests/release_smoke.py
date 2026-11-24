"""Smoke-test an installed release from an isolated environment."""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        subprocess.run(
            [Path(sys.executable).with_name("anycompute"), "providers"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        from anycompute import ProviderError, run
    except Exception:
        print("package failure in anycompute providers")
        return 1

    expected = "anycompute release smoke\n"
    try:
        result = run(("printf", "%s", expected), provider="docker", ttl=60)
        if result.exit_code == 0 and result.stdout == expected and result.stderr == "":
            print("release smoke passed")
            return 0
    except ProviderError as error:
        if error.internal is False:
            print("infrastructure failure in Docker smoke job")
            return 2
    except Exception:
        pass

    print("package failure in Docker smoke job")
    return 1


if __name__ == "__main__":
    sys.exit(main())
