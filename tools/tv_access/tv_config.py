"""Host selection only. This file never stores a credential or discovers TVs."""
import json
import os
from pathlib import Path


def config_path():
    override = os.environ.get('TV_ACCESS_CONFIG')
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base / 'tv-access' / 'config.json'


def resolve_host(explicit=None):
    """Precedence: --tv, TV_HOST, local config. No developer's LAN default."""
    host = explicit if explicit is not None else os.environ.get('TV_HOST')
    if host is None:
        path = config_path()
        try:
            settings = json.loads(path.read_text())
        except FileNotFoundError:
            settings = {}
        except (OSError, ValueError) as error:
            raise ValueError(f'Cannot load TV configuration at {path}: {error}') from error
        if not isinstance(settings, dict):
            raise ValueError('TV configuration must be a JSON object')
        host = settings.get('tv_host')
    if not isinstance(host, str) or not host or any(c.isspace() for c in host) or any(c in host for c in '\x00/\\'):
        raise ValueError('Specify a TV IP/hostname with --tv, TV_HOST, or config.json tv_host')
    return host
