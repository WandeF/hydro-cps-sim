"""Create a local C-Town config copy without changing experiment semantics."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src.core.config import load_yaml

ROOT = Path(__file__).resolve().parents[2]
PATH_KEYS = {"root_path", "inp_file", "output_path", "openplc_path", "ns3_path"}


def prepare_config(source: Path, destination: Path, dependency_parent: Path,
                   output: Path, inp_file: Path | None = None) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    parent = dependency_parent.expanduser().resolve()
    output = output.expanduser().resolve()
    if destination.exists() or destination == source:
        raise ValueError(f"Refusing to overwrite config: {destination}")
    cfg = load_yaml(source)
    # C-Town models ship next to the original config; never guess an external model.
    inp = (inp_file.expanduser().resolve() if inp_file is not None
           else source.parent / Path(str(cfg['inp_file'])).name)
    for label, path in (("INP model", inp),
                        ("OpenPLC compiler", parent/'OpenPLC_v3/webserver/scripts/compile_program.sh'),
                        ("ns-3 launcher", parent/'ns-3-dev-git/ns3')):
        if not path.is_file():
            raise ValueError(f"{label} not found: {path}")
    if output == ROOT or output == Path('/') or output in source.parents or output in destination.parents:
        raise ValueError("output must be a dedicated directory, separate from source/config")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Use a new or empty output directory to preserve existing results: {output}")
    paths = dict(root_path=parent, inp_file=inp, output_path=output,
                 openplc_path=parent/'OpenPLC_v3', ns3_path=parent/'ns-3-dev-git')
    updated = source.read_text(encoding='utf-8')
    for key, value in paths.items():
        updated, count = re.subn(r'^' + key + r':[^\n]*$',
                                lambda _: f'{key}: {json.dumps(str(value), ensure_ascii=False)}',
                                updated, flags=re.M)
        if count != 1:
            raise ValueError(f"Expected exactly one top-level {key}: {source}")
    # Check semantic preservation independently of the textual substitution.
    import yaml
    parsed = yaml.safe_load(updated)
    if {k: v for k, v in cfg.items() if k not in PATH_KEYS} != {k: v for k, v in parsed.items() if k not in PATH_KEYS}:
        raise ValueError('Non-path configuration changed')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Existing runtime resolvers sometimes fall back to config.parent/output.
    # Creating the explicit directory prevents that fallback for this copy.
    output.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as stream:
        stream.write(updated)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version='Hydro-CPS-Sim V' + (ROOT/'VERSION').read_text().strip())
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--dependency-parent', type=Path, default=ROOT.parent)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--inp-file', type=Path, help='Explicit model path; default: original config sibling model')
    args = parser.parse_args()
    try:
        result = prepare_config(args.config, args.destination, args.dependency_parent, args.output, args.inp_file)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f'[ERROR] {exc}\n')
    print(f'[DONE] Local configuration: {result}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
