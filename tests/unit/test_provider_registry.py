import re
import tomllib
from pathlib import Path

from anycompute.providers import PROVIDERS


ROOT = Path(__file__).resolve().parents[2]


def test_registry_matches_adapter_files_extras_and_readme():
    adapters = {path.stem for path in (ROOT / "src/anycompute/providers").glob("*.py")
                if path.name != "__init__.py"}
    names = {spec.name for spec in PROVIDERS}
    assert names == adapters

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    assert set(extras) - {"all"} == names
    assert set(extras["all"]) == {dependency for name in names for dependency in extras[name]}

    readme = (ROOT / "README.md").read_text()
    assert set(re.findall(r"^\| `([a-z0-9]+)` \|", readme, re.MULTILINE)) == names
