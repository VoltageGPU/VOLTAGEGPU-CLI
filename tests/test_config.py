import os
import stat
import sys

import pytest

from volt.sdk.config import (
    Config,
    mask_api_key,
    permissions_warning,
    remove_api_key,
    save_api_key,
    validate_base_url,
)
from volt.sdk.errors import ConfigError

from .conftest import TEST_KEY

OTHER_KEY = "volt_" + "c" * 32 + "_" + "d" * 32


def test_explicit_key_wins(tmp_path):
    cfg = Config.load(api_key=TEST_KEY, env={"VOLT_API_KEY": OTHER_KEY}, config_path=tmp_path / "none.ini")
    assert cfg.api_key == TEST_KEY and cfg.source == "argument"


def test_env_then_alias_then_file(tmp_path):
    path = tmp_path / "config.ini"
    save_api_key(OTHER_KEY, path)
    assert Config.load(env={"VOLT_API_KEY": TEST_KEY}, config_path=path).source == "environment (VOLT_API_KEY)"
    assert Config.load(env={"VOLTAGEGPU_API_KEY": TEST_KEY}, config_path=path).source == "environment (VOLTAGEGPU_API_KEY)"
    from_file = Config.load(env={}, config_path=path)
    assert from_file.api_key == OTHER_KEY and from_file.source.startswith("file (")


def test_missing_key_explains_where_to_get_one(tmp_path):
    with pytest.raises(ConfigError, match="voltagegpu.com/api-keys"):
        Config.load(env={}, config_path=tmp_path / "absent.ini")


def test_malformed_key_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="malformed"):
        Config.load(env={"VOLT_API_KEY": "sk-not-a-volt-key"}, config_path=tmp_path / "x.ini")
    with pytest.raises(ConfigError):
        Config.load(env={"VOLT_API_KEY": "volt_abc\r\nX-Evil: 1"}, config_path=tmp_path / "x.ini")


@pytest.mark.parametrize("url", ["http://api.voltagegpu.com/api", "ftp://x", "https://user:pw@api.voltagegpu.com", "not a url"])
def test_unsafe_base_urls_are_refused(url):
    with pytest.raises(ConfigError):
        validate_base_url(url)


def test_https_and_local_http_are_accepted():
    assert validate_base_url("https://api.voltagegpu.com/api/") == "https://api.voltagegpu.com/api"
    assert validate_base_url("http://localhost:3000/api") == "http://localhost:3000/api"


def test_base_url_from_env_is_validated(tmp_path):
    with pytest.raises(ConfigError):
        Config.load(env={"VOLT_API_KEY": TEST_KEY, "VOLT_BASE_URL": "http://evil.example/api"}, config_path=tmp_path / "x.ini")


def test_repr_and_mask_never_show_the_key():
    cfg = Config(api_key=TEST_KEY)
    assert TEST_KEY not in repr(cfg)
    assert mask_api_key(TEST_KEY) == "volt_****" + TEST_KEY[-4:]
    assert mask_api_key("") == "(none)"


def test_save_refuses_garbage(tmp_path):
    with pytest.raises(ConfigError):
        save_api_key("hello", tmp_path / "c.ini")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_saved_file_is_private(tmp_path):
    path = save_api_key(TEST_KEY, tmp_path / "sub" / "config.ini")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert permissions_warning(path) is None
    os.chmod(path, 0o644)
    assert "chmod 600" in permissions_warning(path)


def test_remove_key(tmp_path):
    path = save_api_key(TEST_KEY, tmp_path / "config.ini")
    assert remove_api_key(path) is True
    assert remove_api_key(path) is False
    with pytest.raises(ConfigError):
        Config.load(env={}, config_path=path)


def test_inference_key_in_voltagegpu_api_key_is_not_taken_for_the_pod_api(tmp_path):
    """VOLTAGEGPU_API_KEY holds the vgpu_ inference key in the public docs: skip it, use the next source."""
    from volt.sdk.config import Config

    pod_key = "volt_" + "d" * 32
    path = tmp_path / "config.ini"
    path.write_text(f"[api]\napi_key = {pod_key}\n", encoding="utf-8")
    cfg = Config.load(env={"VOLTAGEGPU_API_KEY": "vgpu_" + "e" * 40}, config_path=path)
    assert cfg.api_key == pod_key and cfg.source.startswith("file")
