#!/usr/bin/env python3
"""Install local symlinks/configuration only. Never contacts or changes the TV."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from tv_config import config_path, resolve_host


def link(source, target, force):
    if target.is_symlink() and target.resolve() == source.resolve():
        return
    if target.exists() or target.is_symlink():
        if not force:
            raise RuntimeError(f'{target} already exists; inspect it and use --force if intentional')
        if target.is_dir():
            raise RuntimeError(f'Refusing to replace directory {target}')
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source.resolve())
    print(f'{target} -> {source.resolve()}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tv', help='save this TV IP/hostname in the local config (not the repository)')
    parser.add_argument('--bin-dir', type=Path, default=Path.home()/'.local/bin')
    parser.add_argument('--skill-dir', type=Path, help='optional existing/new agent skill directory')
    parser.add_argument('--force', action='store_true', help='replace existing command/skill files or symlinks')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    # Check every destination before installing any link.
    pairs = [(root/name, args.bin_dir/name) for name in ('tv-access', 'tv-key', 'tv-shot')]
    if args.skill_dir:
        pairs.append((root/'SKILL.md', args.skill_dir/'SKILL.md'))
    for source, dest in pairs:
        if dest.is_dir():
            parser.error(f'Refusing to replace directory {dest}')
        if (dest.exists() or dest.is_symlink()) and dest.resolve() != source.resolve() and not args.force:
            parser.error(f'{dest} already exists; inspect it and use --force if intentional')
    if args.tv is not None:
        try:
            host = resolve_host(args.tv)
            path = config_path()
            settings = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(settings, dict):
                raise ValueError('configuration must be a JSON object')
            settings['tv_host'] = host
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
                json.dump(settings, stream, indent=2)
                stream.write('\n')
                temp = Path(stream.name)
            os.chmod(temp, 0o600)
            temp.replace(path)
            print(f'Host configuration saved to {path}')
        except (OSError, ValueError) as error:
            parser.error(str(error))
    for source, dest in pairs:
        link(source, dest, args.force)
    print('Keep this checkout in place; installed commands are symlinks. No TV access performed.')


if __name__ == '__main__':
    main()
