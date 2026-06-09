"""Language profile loader — maps repos to language/toolchain configs."""
import json
import os
import pathlib
from typing import Any

AGENTIC_HOME  = pathlib.Path(os.environ.get("AGENTIC_HOME", pathlib.Path.home() / ".agentic"))
PROFILES_DIR  = AGENTIC_HOME / "profiles"
DEFAULT_NAME  = "typescript"

_cache: dict[str, dict[str, Any]] = {}


def load_profile(name: str) -> dict[str, Any]:
    """Load a profile by name, falling back to the default if not found."""
    if name in _cache:
        return _cache[name]
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        path = PROFILES_DIR / f"{DEFAULT_NAME}.json"
    try:
        data = json.loads(path.read_text())
        _cache[name] = data
        return data
    except Exception:
        return _builtin_default()


def detect_profile(repo: str) -> str:
    """Scan repo root for indicator files and return the best-matching profile name.

    Detection rules (checked in order per profile, profiles sorted by priority desc):
      any_file       — at least one of these filenames exists in the repo root
      file_contains  — {filename: [strings]} — file exists AND contains any of the strings
    """
    repo_path = pathlib.Path(repo)
    if not PROFILES_DIR.exists():
        return DEFAULT_NAME

    # Load all profiles and sort by detect_priority descending (higher = checked first)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for profile_path in PROFILES_DIR.glob("*.json"):
        try:
            data = json.loads(profile_path.read_text())
            candidates.append((data.get("detect_priority", 0), data))
        except Exception:
            continue
    candidates.sort(key=lambda x: x[0], reverse=True)

    for _, data in candidates:
        detect = data.get("detect", {})

        # any_file: match if any listed filename exists
        for fname in detect.get("any_file", []):
            if (repo_path / fname).exists():
                return data["name"]

        # file_contains: match if file exists and contains any of the given strings
        for fname, needles in detect.get("file_contains", {}).items():
            fpath = repo_path / fname
            if not fpath.exists():
                continue
            try:
                content = fpath.read_text(errors="replace")
                if any(needle in content for needle in needles):
                    return data["name"]
            except Exception:
                continue

    return DEFAULT_NAME


def _builtin_default() -> dict[str, Any]:
    """Hardcoded fallback — identical to profiles/typescript.json."""
    return {
        "name": "typescript",
        "display": "TypeScript / React",
        "source_extensions": [".ts", ".tsx"],
        "exclude_dirs": [
            "node_modules", ".git", "dist", "build", ".next", ".claude",
            "coverage", "__pycache__", ".turbo", "out", ".vercel", "worktrees", "queue",
        ],
        "symbol_extraction": {
            "named_export": (
                r"^export\s+(?:(?:async|default|declare|abstract)\s+)*"
                r"(?:function\*?\s+|const\s+|let\s+|var\s+|class\s+|interface\s+|type\s+|enum\s+)(\w+)"
            ),
            "reexport": r"^export\s+\{([^}]+)\}",
        },
        "activity": {
            "build_commands": ["npm run build", "yarn build", "pnpm build", "vite build", "tsc"],
            "lint_commands": ["npm run lint", "yarn lint", "eslint", "prettier"],
            "error_file_pattern": r"([^\s(]+\.[tj]sx?)\(\d+,\d+\):\s+error",
        },
        "post_edit": {
            "dep_trigger_file": "package.json",
            "dep_install_cmd": "npm install --silent",
            "type_check_trigger_file": "tsconfig.json",
            "type_check_cmd": "tsc --noEmit --skipLibCheck",
            "type_check_local_bin": "node_modules/.bin/tsc",
        },
    }
