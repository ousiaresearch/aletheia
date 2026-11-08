import subprocess

import pytest

from aletheia import adapters


def test_failed_audio_conversion_removes_input_and_output_temporary_files(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, '_scratch_dir', lambda: str(tmp_path))
    monkeypatch.setattr(adapters.shutil, 'which', lambda _: '/synthetic/ffmpeg')
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(adapters.subprocess, 'run', fail)
    transcriber = adapters.WhisperTranscriber(tmp_path / 'model')
    with pytest.raises(subprocess.CalledProcessError):
        transcriber._to_wav(b'synthetic audio')
    assert list(tmp_path.iterdir()) == []


def test_output_allocation_failure_removes_input_temporary_file(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, '_scratch_dir', lambda: str(tmp_path))
    monkeypatch.setattr(adapters.shutil, 'which', lambda _: '/synthetic/ffmpeg')
    original = adapters.tempfile.mkstemp
    calls = []
    def allocate(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError('output allocation failed')
        return original(*args, **kwargs)
    monkeypatch.setattr(adapters.tempfile, 'mkstemp', allocate)
    with pytest.raises(OSError, match='output allocation failed'):
        adapters.WhisperTranscriber(tmp_path / 'model')._to_wav(b'synthetic audio')
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_audio_conversion_timeout_removes_both_temporary_files(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, '_scratch_dir', lambda: str(tmp_path))
    monkeypatch.setattr(adapters.shutil, 'which', lambda _: '/synthetic/ffmpeg')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 600)
    monkeypatch.setattr(adapters.subprocess, 'run', timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        adapters.WhisperTranscriber(tmp_path / 'model')._to_wav(b'synthetic audio')
    assert list(tmp_path.iterdir()) == []
