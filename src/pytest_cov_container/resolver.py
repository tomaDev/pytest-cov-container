from pathlib import Path


class SamBuildResolver:
    def resolve_target_dir(self, build_dir: str, project_root: Path) -> Path:
        path = Path(build_dir)
        if path.is_absolute():
            return path
        return project_root / path
