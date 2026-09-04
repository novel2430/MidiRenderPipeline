from pathlib import Path
import tomllib

import midi_render


def test_package_version_matches_project_metadata():
    project_root = Path(__file__).resolve().parents[1]
    with (project_root / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)

    assert midi_render.__version__ == project["project"]["version"]
