"""Exercise model download failures without contacting external services."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


spec = importlib.util.spec_from_file_location(
    'download_models', Path(__file__).resolve().parents[1] / 'download_models.py')
downloader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(downloader)


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_SERVER_DIR', str(tmp_path))
    monkeypatch.delenv('HF_ENDPOINT', raising=False)
    monkeypatch.setattr(downloader, 'FILES', {'model.onnx': 4})
    folder = tmp_path / 'models' / downloader.NAME
    folder.mkdir(parents=True)
    return folder / 'model.onnx'


def test_http_failure_falls_back_with_fresh_file(target, monkeypatch):
    partial = target.with_suffix('.onnx.part')
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            partial.write_bytes(b'old')
            return subprocess.CompletedProcess(args, 22)
        assert not partial.exists()
        partial.write_bytes(b'valid')
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(downloader.subprocess, 'run', run)
    downloader.main()
    assert calls[0][-1].startswith(downloader.DEFAULT_ENDPOINT + '/')
    assert calls[1][-1].startswith(downloader.MIRROR_ENDPOINT + '/')
    assert target.read_bytes() == b'valid'
    assert not partial.exists()


def test_range_failure_restarts_same_source(target, monkeypatch):
    partial = target.with_suffix('.onnx.part')
    partial.write_bytes(b'old')
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            assert '-C' in args
            return subprocess.CompletedProcess(args, 33)
        assert '-C' not in args
        assert not partial.exists()
        partial.write_bytes(b'valid')
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(downloader.subprocess, 'run', run)
    downloader.main()
    assert calls[0][-1] == calls[1][-1]
    assert target.read_bytes() == b'valid'


@pytest.mark.parametrize('endpoint,attempts', [('', 2), ('https://private.example/', 1)])
def test_all_sources_fail_without_installing_partial(target, monkeypatch, endpoint, attempts):
    monkeypatch.setenv('HF_ENDPOINT', endpoint)
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 22)

    monkeypatch.setattr(downloader.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='set HF_ENDPOINT'):
        downloader.main()
    assert len(calls) == attempts
    assert not target.exists()


def test_incomplete_file_is_not_installed(target, monkeypatch):
    def run(args):
        Path(args[args.index('-o') + 1]).write_bytes(b'x')
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(downloader.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='Cannot download'):
        downloader.main()
    assert not target.exists()


def test_existing_complete_model_is_reused(target, monkeypatch):
    target.write_bytes(b'valid')

    def run(args):
        pytest.fail('Existing complete file should not be downloaded')

    monkeypatch.setattr(downloader.subprocess, 'run', run)
    downloader.main()
    assert target.read_bytes() == b'valid'
