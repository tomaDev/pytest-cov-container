import pytest

from pytest_cov_container import config
from pytest_cov_container.frameworks import SourceMapping


def _load(root, toml: str = "") -> config.PluginConfig:
    (root / "pyproject.toml").write_text('[tool.pytest-cov-container]\nframework = "aws-sam"\n' + toml)
    result = config.load_config(root / "pyproject.toml")
    assert result is not None
    return result


def _targets(result: config.PluginConfig) -> dict:
    return {target.name: target for target in result.targets}


class TestLoadConfig:
    def test_returns_none_when_file_missing(self, tmp_path):
        assert config.load_config(tmp_path / "nonexistent.toml") is None

    def test_returns_none_when_no_plugin_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
        assert config.load_config(tmp_path / "pyproject.toml") is None

    def test_framework_is_required(self, sam_project):
        (sam_project / "pyproject.toml").write_text('[tool.pytest-cov-container]\nlabel = "x"\n')
        with pytest.raises(ValueError, match=r"framework is required \(supported: aws-sam\)"):
            config.load_config(sam_project / "pyproject.toml")

    def test_unknown_framework_is_rejected(self, sam_project):
        (sam_project / "pyproject.toml").write_text('[tool.pytest-cov-container]\nframework = "compose"\n')
        with pytest.raises(ValueError, match="'compose' is not supported"):
            config.load_config(sam_project / "pyproject.toml")

    def test_disabled_config(self, sam_project):
        assert _load(sam_project, "enabled = false\n").enabled is False

    def test_scalar_keys(self, sam_project):
        result = _load(
            sam_project,
            'worker_env = "MARK"\nrequired = true\nbranch = false\nsink_host = "10.0.0.1"\nsink_bind = "0.0.0.0"\n'
            'docker_network = "sam-net"\n',
        )
        assert (result.worker_env, result.required, result.branch) == ("MARK", True, False)
        assert (result.sink_host, result.sink_bind, result.docker_network) == ("10.0.0.1", "0.0.0.0", "sam-net")

    def test_sink_address_and_branch_are_detected_by_default(self, sam_project):
        result = _load(sam_project)
        assert (result.sink_host, result.sink_bind, result.docker_network, result.branch) == (None, None, None, None)

    def test_non_bool_is_rejected(self, sam_project):
        with pytest.raises(ValueError, match="required must be true or false"):
            _load(sam_project, 'required = "yes"\n')


class TestAwsSamFramework:
    def test_measures_every_zip_python_function_by_default(self, sam_project):
        result = _load(sam_project)
        assert [target.name for target in result.targets] == ["ApiFunction", "Worker"]

    def test_function_targets(self, sam_project):
        targets = _targets(_load(sam_project))
        api, worker = targets["ApiFunction"], targets["Worker"]
        assert api.build_dir == ".aws-sam/build/ApiFunction"
        assert api.container_root == "/var/task"
        # Code at /var/task, the Globals layer at /opt.
        assert api.mappings == (SourceMapping("src/api", "/var/task"), SourceMapping("src/shared", "/opt"))
        # SAM appends a function's own layers to the Globals ones; ARNs are skipped.
        assert worker.mappings == (
            SourceMapping("src/worker", "/var/task"),
            SourceMapping("src/shared", "/opt"),
            SourceMapping("src/extra", "/opt"),
        )

    @pytest.mark.parametrize(("build_method", "layer_root"), [("python3.14", "/opt/python"), ("makefile", "/opt")])
    def test_an_unbuilt_layer_is_placed_by_its_build_method(self, sam_project, build_method, layer_root):
        # No build output to read: `BuildMethod: python3.x` puts the ContentUri's
        # files under python/; any other build copies the tree (which holds python/).
        template = sam_project / "template.yaml"
        template.write_text(
            template.read_text().replace(
                "      ContentUri: src/extra/\n",
                f"      ContentUri: src/extra/\n    Metadata:\n      BuildMethod: {build_method}\n",
            )
        )
        worker = _targets(_load(sam_project))["Worker"]
        assert worker.mappings[2] == SourceMapping("src/extra", layer_root)

    def test_build_dirs_come_from_the_built_template(self, sam_project):
        (sam_project / ".aws-sam/build/template.yaml").write_text(
            "Resources:\n"
            "  ApiFunction:\n    Type: AWS::Serverless::Function\n    Properties:\n      CodeUri: ApiFunction\n"
            "  Worker:\n    Type: AWS::Serverless::Function\n    Properties:\n      CodeUri: Worker-Shared\n"
        )
        targets = _targets(_load(sam_project))
        assert targets["Worker-Shared"].build_dir == ".aws-sam/build/Worker-Shared"
        assert targets["Worker-Shared"].functions == ("Worker",)

    def test_functions_sharing_a_build_dir_become_one_target(self, sam_project):
        # SAM builds functions with the same code once; their layers are merged.
        (sam_project / ".aws-sam/build/template.yaml").write_text(
            "Resources:\n"
            "  ApiFunction:\n    Properties:\n      CodeUri: Both\n"
            "  Worker:\n    Properties:\n      CodeUri: Both\n"
        )
        (target,) = _load(sam_project).targets
        assert (target.name, target.functions) == ("Both", ("ApiFunction", "Worker"))
        assert target.mappings == (
            SourceMapping("src/api", "/var/task"),
            SourceMapping("src/shared", "/opt"),
            SourceMapping("src/worker", "/var/task"),
            SourceMapping("src/extra", "/opt"),
        )

    def test_selects_the_label_and_this_checkouts_build_root(self, sam_project):
        result = _load(sam_project)
        assert result.framework == "aws-sam"
        assert result.label == "sam.cli.container.type=lambda"
        assert result.mount_prefix == ".aws-sam/build"

    def test_functions_selects_a_subset(self, sam_project):
        assert [target.name for target in _load(sam_project, 'functions = ["Worker"]\n').targets] == ["Worker"]

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("NodeFn", "runtime 'nodejs22.x' is not Python"),
            ("ImageFn", "is an image function"),
            ("S3Fn", "has no local CodeUri"),
            ("Queue", "is not an AWS::Serverless::Function"),
            ("Missing", "is not an AWS::Serverless::Function"),
        ],
    )
    def test_ineligible_functions_are_rejected(self, sam_project, name, reason):
        with pytest.raises(ValueError, match=f"functions: '{name}' {reason}"):
            _load(sam_project, f'functions = ["{name}"]\n')

    def test_build_dir_moves_targets_and_ownership(self, sam_project):
        result = _load(sam_project, 'build_dir = "out/sam"\n')
        assert _targets(result)["Worker"].build_dir == "out/sam/Worker"
        assert result.mount_prefix == "out/sam"

    def test_paths_are_relative_to_the_template(self, sam_project):
        infra = sam_project / "infra"
        infra.mkdir()
        (sam_project / "template.yaml").rename(infra / "template.yaml")
        text = (infra / "template.yaml").read_text().replace("src/", "../src/")
        (infra / "template.yaml").write_text(text)
        targets = _targets(_load(sam_project, 'template = "infra/template.yaml"\n'))
        assert targets["ApiFunction"].mappings[0] == SourceMapping("src/api", "/var/task")

    def test_missing_template_is_reported(self, sam_project):
        (sam_project / "template.yaml").unlink()
        with pytest.raises(FileNotFoundError, match="SAM template"):
            _load(sam_project)

    def test_template_without_python_functions(self, sam_project):
        (sam_project / "template.yaml").write_text("Resources:\n  Queue:\n    Type: AWS::SQS::Queue\n")
        with pytest.raises(ValueError, match="no zip Python function"):
            _load(sam_project)

    def test_explicit_keys_win(self, sam_project):
        result = _load(sam_project, 'label = "app=x"\nmount_prefix = "build"\n')
        assert (result.label, result.mount_prefix) == ("app=x", "build")

    def test_empty_strings_switch_the_filters_off(self, sam_project):
        result = _load(sam_project, 'label = ""\nmount_prefix = ""\n')
        assert not result.label
        assert not result.mount_prefix


def _build_layer(root, files: dict[str, str], sources: dict[str, str] | None = None) -> None:
    """``sam build`` output for SharedLayer (``files`` under its build dir), optionally replacing its sources."""
    if sources is not None:
        shared = root / "src/shared"
        for path in sorted(shared.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        for rel, text in sources.items():
            (shared / rel).parent.mkdir(parents=True, exist_ok=True)
            (shared / rel).write_text(text)
    build_root = root / ".aws-sam/build"
    (build_root / "template.yaml").write_text(
        "Resources:\n  SharedLayer:\n    Type: AWS::Serverless::LayerVersion\n    Properties:\n      ContentUri: SharedLayer\n"
    )
    for rel, text in files.items():
        (build_root / "SharedLayer" / rel).parent.mkdir(parents=True, exist_ok=True)
        (build_root / "SharedLayer" / rel).write_text(text)


class TestLayerLayoutFromTheBuild:
    """Where a layer's files land is read off ``sam build``'s output, whatever built it."""

    def _shared(self, root) -> tuple[SourceMapping, ...]:
        api = _targets(_load(root))["ApiFunction"]
        return api.mappings[1:]

    def test_a_tree_copied_as_is_sits_at_opt(self, sam_project):
        _build_layer(sam_project, {"python/shared/util.py": "X = 1\n"})
        assert self._shared(sam_project) == (SourceMapping("src/shared", "/opt"),)

    def test_files_built_into_python_sit_at_opt_python(self, sam_project):
        _build_layer(sam_project, {"python/shared/util.py": "X = 1\n"}, sources={"shared/util.py": "X = 1\n"})
        assert self._shared(sam_project) == (SourceMapping("src/shared", "/opt/python"),)

    def test_a_subdirectory_built_into_python_maps_on_its_own(self, sam_project):
        # e.g. a Makefile that copies src/ into $(ARTIFACTS_DIR)/python/
        _build_layer(
            sam_project,
            {"python/pkg/x.py": "X = 1\n", "python/pkg/__init__.py": ""},
            sources={"src/pkg/x.py": "X = 1\n", "src/pkg/__init__.py": "", "Makefile": ""},
        )
        assert self._shared(sam_project) == (SourceMapping("src/shared/src", "/opt/python"),)

    def test_a_dependency_with_the_same_name_is_not_a_match(self, sam_project):
        _build_layer(
            sam_project,
            {"python/vendor/shared/util.py": "VENDORED = 1\n", "python/shared/util.py": "X = 1\n"},
            sources={"shared/util.py": "X = 1\n"},
        )
        assert self._shared(sam_project) == (SourceMapping("src/shared", "/opt/python"),)

    def test_no_source_file_in_the_build_warns_and_falls_back(self, sam_project):
        _build_layer(sam_project, {"python/other.py": "Y = 2\n"})
        with pytest.warns(UserWarning, match="SharedLayer: none of its source files"):
            assert self._shared(sam_project) == (SourceMapping("src/shared", "/opt"),)


def _build_function(root, function: str, files: dict[str, str]) -> None:
    """``sam build`` output for ``function`` (``files`` under its build dir)."""
    build_root = root / ".aws-sam/build"
    (build_root / "template.yaml").write_text(
        f"Resources:\n  {function}:\n    Type: AWS::Serverless::Function\n    Properties:\n      CodeUri: {function}\n"
    )
    for rel, text in files.items():
        (build_root / function / rel).parent.mkdir(parents=True, exist_ok=True)
        (build_root / function / rel).write_text(text)


def _write(root, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


# A package under a module root (uv_build ``module-root = "python"``), with
# the files a package checkout holds but never ships.
_PACKAGE = {
    "src/common/pyproject.toml": "[project]\nname = 'common'\n",
    "src/common/python/common/__init__.py": "",
    "src/common/python/common/net.py": "def get():\n    return 1\n",
    "src/common/python/common/sub/deep.py": "Z = 3\n",
    "src/common/tests/test_net.py": "",
    "src/common/.venv/lib/site.py": "",
}


class TestUvPathSources:
    """A local package the function depends on is measured where ``sam build`` put it."""

    def _uses(self, root, sources: str) -> None:
        (root / "src/api/pyproject.toml").write_text(f"[project]\nname = 'api'\n\n[tool.uv.sources]\n{sources}")

    def _packages(self, root) -> tuple[SourceMapping, ...]:
        api = _targets(_load(root))["ApiFunction"]
        # The code at /var/task and the Globals layer come first.
        assert api.mappings[:2] == (SourceMapping("src/api", "/var/task"), SourceMapping("src/shared", "/opt"))
        return api.mappings[2:]

    def test_a_path_source_maps_each_top_level_package(self, sam_project):
        _write(sam_project, _PACKAGE)
        self._uses(sam_project, 'common = { path = "../common", editable = false }\n')
        _build_function(
            sam_project,
            "ApiFunction",
            {
                "app.py": "def f():\n    return 1\n",
                "common/__init__.py": "",
                "common/net.py": "def get():\n    return 1\n",
                "common/sub/deep.py": "Z = 3\n",
                # A dependency's empty __init__.py must not claim the package's.
                "vendor/__init__.py": "",
            },
        )
        assert self._packages(sam_project) == (SourceMapping("src/common/python/common", "/var/task/common"),)

    def test_a_flat_layout_package(self, sam_project):
        _write(sam_project, {"src/lib/pyproject.toml": "", "src/lib/tool/run.py": "R = 1\n"})
        self._uses(sam_project, 'tool = { path = "../lib" }\n')
        _build_function(sam_project, "ApiFunction", {"tool/run.py": "R = 1\n"})
        assert self._packages(sam_project) == (SourceMapping("src/lib/tool", "/var/task/tool"),)

    def test_a_source_list_with_markers(self, sam_project):
        _write(sam_project, _PACKAGE)
        self._uses(sam_project, 'common = [{ path = "../common", marker = "sys_platform == \'linux\'" }]\n')
        _build_function(sam_project, "ApiFunction", {"common/net.py": "def get():\n    return 1\n"})
        assert self._packages(sam_project) == (SourceMapping("src/common/python/common", "/var/task/common"),)

    def test_other_sources_are_not_packages_here(self, sam_project):
        _write(sam_project, {"src/wheels/pkg-1.0-py3-none-any.whl": ""})
        self._uses(
            sam_project,
            'a = { git = "https://example.com/a.git" }\nb = { index = "internal" }\nc = { workspace = true }\n'
            'd = { url = "https://example.com/d.whl" }\ne = { path = "../wheels/pkg-1.0-py3-none-any.whl" }\n'
            'f = { path = "../missing" }\n',
        )
        _build_function(sam_project, "ApiFunction", {"app.py": "def f():\n    return 1\n"})
        assert self._packages(sam_project) == ()

    def test_a_package_changed_since_the_build_is_not_matched(self, sam_project):
        # e.g. an editable install: the build holds a link, not the files.
        _write(sam_project, _PACKAGE)
        self._uses(sam_project, 'common = { path = "../common" }\n')
        _build_function(sam_project, "ApiFunction", {"common/net.py": "def get():\n    return 2\n"})
        with pytest.warns(UserWarning, match=r"ApiFunction: none of \.\./common's source files"):
            assert self._packages(sam_project) == ()

    def test_before_a_build_packages_are_skipped_quietly(self, sam_project):
        _write(sam_project, _PACKAGE)
        self._uses(sam_project, 'common = { path = "../common" }\n')
        assert self._packages(sam_project) == ()

    def test_a_function_without_a_pyproject_has_no_packages(self, sam_project):
        _build_function(sam_project, "ApiFunction", {"app.py": "def f():\n    return 1\n"})
        assert self._packages(sam_project) == ()
