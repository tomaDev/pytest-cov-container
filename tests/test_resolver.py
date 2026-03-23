from pytest_cov_container.resolver import SamBuildResolver


class TestSamBuildResolver:
    def test_resolves_relative_path(self, tmp_path):
        build_dir = tmp_path / ".aws-sam" / "build" / "ApiFunction"
        build_dir.mkdir(parents=True)
        resolver = SamBuildResolver()
        result = resolver.resolve_target_dir(".aws-sam/build/ApiFunction", tmp_path)
        assert result == build_dir

    def test_returns_path_even_if_missing(self, tmp_path):
        resolver = SamBuildResolver()
        result = resolver.resolve_target_dir(".aws-sam/build/ApiFunction", tmp_path)
        assert result == tmp_path / ".aws-sam" / "build" / "ApiFunction"
        assert not result.exists()

    def test_resolves_absolute_path(self, tmp_path):
        build_dir = tmp_path / "custom" / "build"
        build_dir.mkdir(parents=True)
        resolver = SamBuildResolver()
        result = resolver.resolve_target_dir(str(build_dir), tmp_path)
        assert result == build_dir
