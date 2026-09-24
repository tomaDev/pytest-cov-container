from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest_plugins = ["pytester"]

# Short-form intrinsics on purpose: the preset must read past them.
SAM_TEMPLATE = """\
Globals:
  Function:
    Runtime: python3.14
    Layers:
      - !Ref SharedLayer
Resources:
  SharedLayer:
    Type: AWS::Serverless::LayerVersion
    Properties:
      ContentUri: src/shared/
  ExtraLayer:
    Type: AWS::Serverless::LayerVersion
    Properties:
      ContentUri: src/extra/
  ApiFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/api/
      Handler: run.sh
      Role: !GetAtt Role.Arn
      Environment:
        Variables:
          TABLE: !Ref Table
          URL: !Sub "https://${Api}.example.com"
  Worker:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/worker
      Handler: handler.handler
      Layers:
        - !Ref ExtraLayer
        - arn:aws:lambda:us-east-1:123456789012:layer:vendor:1
  NodeFn:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/node
      Runtime: nodejs22.x
      Handler: index.handler
  ImageFn:
    Type: AWS::Serverless::Function
    Properties:
      PackageType: Image
  S3Fn:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri:
        Bucket: artifacts
        Key: fn.zip
      Handler: app.handler
  Queue:
    Type: AWS::SQS::Queue
"""

SAM_SOURCES = {
    "src/api/app.py": "def f():\n    return 1\n",
    "src/api/tests/test_app.py": "",
    "src/shared/python/shared/util.py": "X = 1\n",
    "src/extra/python/extra.py": "Y = 2\n",
    "src/worker/handler.py": "def handler(event, context):\n    return event\n",
    "src/worker/.hidden/skip.py": "",
}


@pytest.fixture
def sam_project(tmp_path) -> Path:
    """A SAM project root: template, sources, built function dirs and a pyproject."""
    (tmp_path / "template.yaml").write_text(SAM_TEMPLATE)
    for rel, text in SAM_SOURCES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for function in ("ApiFunction", "Worker"):
        (tmp_path / ".aws-sam" / "build" / function).mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[tool.pytest-cov-container]\nframework = "aws-sam"\n')
    return tmp_path


@pytest.fixture
def mock_docker_container():
    """Return a MagicMock mimicking a docker Container object."""
    container = MagicMock()
    container.id = "abc123def456"
    container.name = "sam-local-api"
    container.image.tags = ["samcli/lambda-python:3.12"]
    container.labels = {"pytest-cov-container": "true"}
    container.status = "running"
    container.attrs = {"Config": {"Image": "samcli/lambda-python:3.12"}}
    return container


@pytest.fixture
def mock_docker_client(mock_docker_container):
    """Return a MagicMock mimicking docker.DockerClient."""
    client = MagicMock()
    client.containers.list.return_value = [mock_docker_container]
    client.containers.get.return_value = mock_docker_container
    return client
