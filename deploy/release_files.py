"""Build only reviewed, Git-tracked application resources, never local files."""
from pathlib import Path
import subprocess


def release_data_files(root: Path) -> list[tuple[str, str]]:
    root = root.resolve()
    tracked = subprocess.check_output([
        "git", "-C", str(root), "ls-files", "-z", "--",
        "backend", "deploy", "frontend/src", "config.example.toml", "docs/密钥迁移.md",
    ]).decode("utf-8").split("\0")
    data = []
    for name in filter(None, tracked):
        relative = Path(name)
        source = root / relative
        if source.is_symlink() or not source.is_file() or root not in source.resolve().parents:
            raise RuntimeError(f"Unsafe release resource: {name}")
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo", ".key", ".pem"}:
            raise RuntimeError(f"Private/generated release resource: {name}")
        if relative.parts[0] == "backend" and (
            relative.name.startswith("test_") or relative.name in {"run_tests.py", "live_check.py"}
        ):
            continue
        destination = relative.parent
        if relative.parts[0] in {"backend", "deploy"}:
            destination = Path("server-payload") / destination
        data.append((str(source), destination.as_posix()))
    if not any(Path(source).name == "config.example.toml" for source, _ in data):
        raise RuntimeError("Release configuration template is missing")
    return data
