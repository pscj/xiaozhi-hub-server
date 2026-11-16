"""[xiaozhi-hub] SherpaXASR 热词解码参数：保留热词的前提下，beam 候选路径数 max_active_paths
默认由 sherpa-onnx 的 4 收窄到 2 给弱 CPU 提速，配置可覆盖；无热词时不带任何解码参数。"""
import sys
import types

# sherpa_onnx 是可选重依赖，本测试只测参数拼装，import provider 前先塞桩（同 conftest 里 opuslib_next 的做法）
sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))

from core.providers.asr.sherpa_onnx_local import ASRProvider


def make_provider(tmp_path):
    provider = ASRProvider.__new__(ASRProvider)
    provider.model_dir = str(tmp_path / "model")
    provider.output_dir = str(tmp_path / "out")
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    return provider


def test_hotwords_default_max_active_paths_is_2(tmp_path):
    result = make_provider(tmp_path)._transducer_hotwords("小程同学,北京天气", {})
    assert result["decoding_method"] == "modified_beam_search"
    assert result["max_active_paths"] == 2
    assert result["hotwords_score"] == 1.5


def test_hotwords_max_active_paths_config_override(tmp_path):
    provider = make_provider(tmp_path)
    result = provider._transducer_hotwords("小程同学", {"max_active_paths": 4})
    assert result["max_active_paths"] == 4
    # 配置里写成字符串也要转成 int
    result = provider._transducer_hotwords("小程同学", {"max_active_paths": "3"})
    assert result["max_active_paths"] == 3


def test_no_hotwords_returns_empty(tmp_path):
    provider = make_provider(tmp_path)
    assert provider._transducer_hotwords("", {}) == {}
    assert provider._transducer_hotwords("  ", {"max_active_paths": 3}) == {}
