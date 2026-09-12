import pytest

from deploy.config import ConfigError, parse_config

SERVICE_TOML = """
[app]
name = "pokemon"

[build]
workdir = "pokemon-cards-app"
steps = ["npm install", "npm run build"]

[service]
workdir = "server"
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
port = 8151

[nginx]
path = "/pokemon/"
strip_prefix = false
client_max_body_size = "10m"

[secrets]
COLLECTION_PASSWORD = "password for the collection upload endpoint"
"""

STATIC_TOML = """
[app]
name = "boggle"
type = "static"

[build]
steps = ["uv run python make_static.py"]
output = "static"

[nginx]
path = "/boggle/"
"""


def test_parses_a_service():
    c = parse_config(SERVICE_TOML, repo_name="pokemon")
    assert c.name == "pokemon"
    assert c.type == "service"
    assert c.service.start.endswith("--port $PORT")
    assert c.service.workdir == "server"
    assert c.service.port == 8151
    assert c.build.steps == ("npm install", "npm run build")
    assert c.build.workdir == "pokemon-cards-app"
    assert c.nginx.path == "/pokemon/"
    assert c.nginx.strip_prefix is False
    assert c.nginx.client_max_body_size == "10m"
    assert c.secrets == {"COLLECTION_PASSWORD": "password for the collection upload endpoint"}


def test_parses_a_static_app():
    c = parse_config(STATIC_TOML, repo_name="boggle-solver")
    assert c.name == "boggle"
    assert c.type == "static"
    assert c.service is None
    assert c.build.output == "static"
    assert c.nginx.path == "/boggle/"


def test_name_defaults_to_repo_name():
    c = parse_config('[service]\nstart = "run"\n', repo_name="crochet")
    assert c.name == "crochet"


def test_strip_prefix_defaults_to_true():
    c = parse_config(
        '[service]\nstart = "run"\n[nginx]\npath = "/x/"\n', repo_name="x"
    )
    assert c.nginx.strip_prefix is True


def test_absent_sections_are_none():
    c = parse_config('[service]\nstart = "run"\n', repo_name="x")
    assert c.build is None
    assert c.nginx is None
    assert c.dev_start is None
    assert c.env == {}
    assert c.secrets == {}


def test_dev_start_overrides():
    c = parse_config(
        '[service]\nstart = "prod"\n[dev]\nstart = "dev --reload"\n', repo_name="x"
    )
    assert c.service.start == "prod"
    assert c.dev_start == "dev --reload"


def test_service_requires_start():
    with pytest.raises(ConfigError, match="service.start"):
        parse_config('[service]\nworkdir = "x"\n', repo_name="x")


def test_service_type_requires_service_section():
    with pytest.raises(ConfigError, match="service"):
        parse_config('[app]\nname = "x"\n', repo_name="x")


def test_static_rejects_service_section():
    with pytest.raises(ConfigError, match="static"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[service]\nstart = "run"\n',
            repo_name="x",
        )


def test_static_rejects_secrets():
    with pytest.raises(ConfigError, match="secrets"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[secrets]\nA = "a"\n',
            repo_name="x",
        )


def test_static_rejects_strip_prefix():
    with pytest.raises(ConfigError, match="strip_prefix"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[nginx]\npath = "/x/"\nstrip_prefix = true\n',
            repo_name="x",
        )


def test_static_requires_build_output():
    with pytest.raises(ConfigError, match="build.output"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = ["make"]\n', repo_name="x"
        )


def test_env_and_secrets_name_collision_is_an_error():
    with pytest.raises(ConfigError, match="both"):
        parse_config(
            '[service]\nstart = "run"\n[env]\nA = "1"\n[secrets]\nA = "desc"\n',
            repo_name="x",
        )


def test_nginx_path_must_be_absolute():
    with pytest.raises(ConfigError, match="must start with"):
        parse_config(
            '[service]\nstart = "run"\n[nginx]\npath = "pokemon/"\n', repo_name="x"
        )


def test_start_command_may_not_contain_a_single_quote():
    with pytest.raises(ConfigError, match="single quote"):
        parse_config("""[service]\nstart = "echo 'hi'"\n""", repo_name="x")


def test_unknown_app_type_is_rejected():
    with pytest.raises(ConfigError, match="type"):
        parse_config('[app]\ntype = "worker"\n', repo_name="x")


def test_invalid_toml_raises_config_error():
    with pytest.raises(ConfigError):
        parse_config("this is not toml {{{", repo_name="x")


def test_build_steps_as_string_is_rejected():
    with pytest.raises(ConfigError, match="build.steps"):
        parse_config(
            '[service]\nstart = "run"\n[build]\nsteps = "npm install"\n', repo_name="x"
        )


def test_service_start_as_non_string_is_rejected():
    with pytest.raises(ConfigError, match="service.start"):
        parse_config('[service]\nstart = true\n', repo_name="x")


def test_service_port_as_string_is_rejected():
    with pytest.raises(ConfigError, match="service.port"):
        parse_config(
            '[service]\nstart = "run"\nport = "abc"\n', repo_name="x"
        )


def test_service_port_as_bool_is_rejected():
    with pytest.raises(ConfigError, match="service.port"):
        parse_config('[service]\nstart = "run"\nport = true\n', repo_name="x")


def test_scalar_app_section_is_rejected():
    with pytest.raises(ConfigError, match=r"\[app\]"):
        parse_config('app = "x"\n', repo_name="x")


def test_scalar_env_section_is_rejected():
    with pytest.raises(ConfigError, match=r"\[env\]"):
        parse_config(
            'env = "x"\n[service]\nstart = "run"\n', repo_name="x"
        )


def test_static_app_with_no_build_section_is_rejected():
    with pytest.raises(ConfigError, match="build.output"):
        parse_config(
            '[app]\ntype = "static"\n[nginx]\npath = "/x/"\n', repo_name="x"
        )


def test_nginx_path_root_is_rejected():
    with pytest.raises(ConfigError, match="may not be"):
        parse_config(
            '[service]\nstart = "run"\n[nginx]\npath = "/"\n', repo_name="x"
        )


def test_static_app_without_trailing_slash_on_path_is_rejected():
    with pytest.raises(ConfigError, match='must end with "/"'):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[nginx]\npath = "/boggle"\n',
            repo_name="x",
        )


def test_static_app_with_trailing_slash_on_path_is_accepted():
    c = parse_config(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
        '[nginx]\npath = "/boggle/"\n',
        repo_name="x",
    )
    assert c.nginx.path == "/boggle/"
