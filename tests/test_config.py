import pytest

from npd.config import ConfigError, parse_config

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


def test_static_app_with_no_nginx_section_is_rejected():
    with pytest.raises(ConfigError, match="static app requires.*nginx"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n',
            repo_name="x",
        )


def test_env_value_containing_newline_is_rejected():
    with pytest.raises(ConfigError, match="env.*newline"):
        parse_config(
            '[service]\nstart = "run"\n[env]\nGREET = "hello\\nworld"\n',
            repo_name="x",
        )


def test_env_value_containing_double_quote_is_rejected():
    with pytest.raises(ConfigError, match='env.*double quote'):
        parse_config(
            '[service]\nstart = "run"\n[env]\nGREET = "hello\\"world"\n',
            repo_name="x",
        )


def test_service_start_containing_newline_is_rejected():
    with pytest.raises(ConfigError, match="service.start.*newline"):
        parse_config(
            '[service]\nstart = "hello\\nworld"\n',
            repo_name="x",
        )


def test_app_name_containing_path_traversal_is_rejected():
    with pytest.raises(ConfigError, match="contains slashes"):
        parse_config(
            '[app]\nname = "../../../home/nathan/apps/pokemon"\n[service]\nstart = "run"\n',
            repo_name="x",
        )


def test_app_name_containing_forward_slash_is_rejected():
    with pytest.raises(ConfigError, match="contains slashes"):
        parse_config(
            '[app]\nname = "a/b"\n[service]\nstart = "run"\n',
            repo_name="x",
        )


def test_app_name_of_exactly_parent_dir_is_rejected():
    with pytest.raises(ConfigError, match=r"\.\."):
        parse_config(
            '[app]\nname = ".."\n[service]\nstart = "run"\n',
            repo_name="x",
        )


def test_app_name_of_exactly_dot_is_rejected():
    with pytest.raises(ConfigError, match=r"^\w"):  # Rejects '.'
        parse_config(
            '[app]\nname = "."\n[service]\nstart = "run"\n',
            repo_name="x",
        )


def test_repo_name_containing_path_traversal_is_rejected():
    with pytest.raises(ConfigError, match="contains slashes"):
        parse_config(
            '[service]\nstart = "run"\n',
            repo_name="../../../etc",
        )


def test_app_name_pokemon_is_accepted():
    c = parse_config(
        '[app]\nname = "pokemon"\n[service]\nstart = "run"\n',
        repo_name="x",
    )
    assert c.name == "pokemon"


def test_app_name_boggle_solver_is_accepted():
    c = parse_config(
        '[app]\nname = "boggle-solver"\n[service]\nstart = "run"\n',
        repo_name="x",
    )
    assert c.name == "boggle-solver"


def test_app_name_with_dot_is_accepted():
    c = parse_config(
        '[app]\nname = "my.app"\n[service]\nstart = "run"\n',
        repo_name="x",
    )
    assert c.name == "my.app"


def test_app_name_with_underscore_is_accepted():
    c = parse_config(
        '[app]\nname = "a_b"\n[service]\nstart = "run"\n',
        repo_name="x",
    )
    assert c.name == "a_b"


# --- CRITICAL 1: nginx.path and client_max_body_size are injection surfaces


def test_client_max_body_size_injection_is_rejected():
    with pytest.raises(ConfigError, match="client_max_body_size"):
        parse_config(
            '[service]\nstart = "run"\n[nginx]\npath = "/x/"\n'
            'client_max_body_size = "1m; } location /pwn { proxy_pass '
            'http://10.0.0.1;"\n',
            repo_name="x",
        )


@pytest.mark.parametrize("size", ["10m", "1M", "500k", "1g", "1G", "100"])
def test_valid_client_max_body_size_values_are_accepted(size):
    c = parse_config(
        f'[service]\nstart = "run"\n[nginx]\npath = "/x/"\n'
        f'client_max_body_size = "{size}"\n',
        repo_name="x",
    )
    assert c.nginx.client_max_body_size == size


def test_nginx_path_injection_via_brace_is_rejected():
    with pytest.raises(ConfigError, match="invalid characters"):
        parse_config(
            '[service]\nstart = "run"\n'
            '[nginx]\npath = "/x/ { proxy_pass http://10.0.0.1; } location /y"\n',
            repo_name="x",
        )


def test_nginx_path_injection_via_semicolon_is_rejected():
    with pytest.raises(ConfigError, match="invalid characters"):
        parse_config(
            '[service]\nstart = "run"\n[nginx]\npath = "/x;evil"\n',
            repo_name="x",
        )


def test_nginx_path_with_whitespace_is_rejected():
    with pytest.raises(ConfigError, match="invalid characters"):
        parse_config(
            '[service]\nstart = "run"\n[nginx]\npath = "/x y"\n',
            repo_name="x",
        )


@pytest.mark.parametrize("path", ["/pokemon/", "/blog", "/a-b_c.d/"])
def test_normal_nginx_paths_are_accepted(path):
    c = parse_config(
        f'[service]\nstart = "run"\n[nginx]\npath = "{path}"\n',
        repo_name="x",
    )
    assert c.nginx.path == path


# --- CRITICAL 2: PORT/PATH are reserved and cannot be overridden via [env]/[secrets]


def test_port_in_env_is_rejected():
    with pytest.raises(ConfigError, match="PORT"):
        parse_config(
            '[service]\nstart = "run"\n[env]\nPORT = "3000"\n',
            repo_name="x",
        )


def test_path_in_env_is_rejected():
    with pytest.raises(ConfigError, match="PATH"):
        parse_config(
            '[service]\nstart = "run"\n[env]\nPATH = "/tmp"\n',
            repo_name="x",
        )


def test_port_in_secrets_is_rejected():
    with pytest.raises(ConfigError, match="PORT"):
        parse_config(
            '[service]\nstart = "run"\n[secrets]\nPORT = "desc"\n',
            repo_name="x",
        )


# --- IMPORTANT 1: service.port must be in the unprivileged range


@pytest.mark.parametrize("port", [-5, 0, 80, 99999])
def test_out_of_range_port_pins_are_rejected(port):
    with pytest.raises(ConfigError, match="service.port"):
        parse_config(
            f'[service]\nstart = "run"\nport = {port}\n',
            repo_name="x",
        )


@pytest.mark.parametrize("port", [8080, 8151, 8152])
def test_in_range_port_pins_are_accepted(port):
    c = parse_config(
        f'[service]\nstart = "run"\nport = {port}\n',
        repo_name="x",
    )
    assert c.service.port == port


# --- MINOR 1: a non-string app name raises ConfigError, not TypeError


def test_non_string_app_name_is_rejected():
    with pytest.raises(ConfigError, match="app.name"):
        parse_config(
            '[app]\nname = 123\n[service]\nstart = "run"\n',
            repo_name="x",
        )


# --- MINOR 2: dev.start gets the same validation as service.start


def test_dev_start_with_single_quote_is_rejected():
    with pytest.raises(ConfigError, match="single quote"):
        parse_config(
            '[service]\nstart = "run"\n[dev]\nstart = "echo \'hi\'"\n',
            repo_name="x",
        )


def test_dev_start_as_non_string_is_rejected():
    with pytest.raises(ConfigError, match="dev.start"):
        parse_config(
            '[service]\nstart = "run"\n[dev]\nstart = true\n',
            repo_name="x",
        )


def test_sandbox_defaults_to_off():
    c = parse_config('[service]\nstart = "run"\n', repo_name="x")
    assert c.service.sandbox is False


def test_sandbox_must_be_a_boolean():
    # A security switch must not treat a typo like "yes" as a silent default.
    with pytest.raises(ConfigError, match="service.sandbox"):
        parse_config('[service]\nstart = "run"\nsandbox = "yes"\n', repo_name="x")


EVERY_SERVICE_KEY = """
[app]
name = "x"
type = "service"
[build]
workdir = "frontend"
steps = ["make"]
output = "dist"
[service]
workdir = "server"
start = "run"
health_path = "/"
port = 8151
sandbox = true
[nginx]
path = "/x/"
strip_prefix = false
client_max_body_size = "10m"
[dev]
start = "run --reload"
[env]
ANYTHING_AT_ALL = "1"
[secrets]
ANY_SECRET_NAME = "what it is for"
"""


def test_every_documented_key_is_accepted():
    parse_config(EVERY_SERVICE_KEY, repo_name="x")


def test_a_top_level_key_that_belongs_in_a_table_says_which_table():
    # The real mistake: name/type written above the tables instead of in [app].
    toml = 'name = "boggle"\n' + STATIC_TOML.replace('name = "boggle"\n', "", 1)
    with pytest.raises(ConfigError, match=r"'name'.*\[app\]"):
        parse_config(toml, repo_name="boggle")


def test_an_unknown_table_is_rejected():
    with pytest.raises(ConfigError, match="servce"):
        parse_config('[servce]\nstart = "run"\n', repo_name="x")


def test_an_unknown_key_inside_a_table_is_rejected():
    with pytest.raises(ConfigError, match=r"service\.healthpath"):
        parse_config('[service]\nstart = "run"\nhealthpath = "/"\n', repo_name="x")
