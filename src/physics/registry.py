"""Scene-local lazy registries; configured Python sources are trusted code."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from importlib import import_module, metadata
import re
from typing import Callable
from src.physics.base import PhysicsBackend


@dataclass(frozen=True)
class Registration:
    factory: str | Callable
    dependency: str | None = None
    initialization: str = 'current'
    legacy_factory: str | None = None
    time_quantum_s: float | None = None


_BACKENDS: dict[str, Registration] = {}
ALIASES = {'epanet':'dhalsim_epynet', 'epynet':'dhalsim_epynet'}
_BUILTINS = {'dhalsim_epynet','pandapower'}
_ACTIVE = ContextVar('upsi_registry', default=None)
_MODULE = r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*'


def _source(source):
    if callable(source): return
    if not isinstance(source,str): raise ValueError('Plugin factory must be callable or module:class')
    if source.startswith('entrypoint:'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',source.split(':',1)[1]):
            raise ValueError('Invalid named plugin entry point')
    elif not re.fullmatch(_MODULE+':'+_MODULE,source):
        raise ValueError(f'Invalid trusted plugin source {source!r}; expected module:class')


class Registry:
    def __init__(self, registrations=None):
        self.backends = dict(_BACKENDS if registrations is None else registrations)

    def add(self, name, spec):
        if not isinstance(name,str) or not name or name in self.backends or name in ALIASES:
            raise ValueError(f'Duplicate or invalid backend: {name}')
        _source(spec.factory)
        if spec.dependency is not None and not re.fullmatch(_MODULE,spec.dependency):
            raise ValueError(f'Invalid plugin dependency: {spec.dependency!r}')
        self.backends[name] = spec

    def get(self, name):
        name = ALIASES.get(name,name)
        try: return self.backends[name]
        except KeyError as exc:
            raise ValueError(f'Unsupported physics backend {name!r}; declare a trusted physics.plugins entry') from exc


def register_backend(name, factory, *, dependency=None, initialization='current',
                     legacy_factory=None, time_quantum_s=None, registry=None):
    """Programmatic SDK registration, or explicit registration in one context."""
    if initialization not in {'current','dhalsim'}: raise ValueError('Unsupported initialization policy')
    target = registry if registry is not None else Registry(_BACKENDS)
    target.add(name,Registration(factory,dependency,initialization,legacy_factory,time_quantum_s))
    if registry is None:
        _BACKENDS[name] = target.backends[name]


def configure_plugins(raw):
    context = Registry()
    plugins = raw.get('physics',{}).get('plugins',{})
    if not isinstance(plugins,dict): raise ValueError('physics.plugins must be a mapping of trusted plugin names')
    for name,item in plugins.items():
        if not isinstance(item,dict) or set(item)-{'factory','dependency','entry_point'}:
            raise ValueError(f'Invalid plugin declaration: {name}')
        if ('factory' in item) == ('entry_point' in item):
            raise ValueError('Plugin needs exactly one factory or entry_point')
        if name in _BUILTINS or name in ALIASES:
            raise ValueError(f'Scene plugins cannot replace a built-in or alias: {name}')
        source = item['factory'] if 'factory' in item else 'entrypoint:'+str(item['entry_point'])
        context.add(name,Registration(source,item.get('dependency')))
    # Compatibility for the old configure_plugins(raw); create_backend(...) SDK.
    # ContextVar isolates threads/tasks; framework code passes context explicitly.
    _ACTIVE.set(context)
    return context


def registration(name, registry=None):
    context = registry or _ACTIVE.get()
    canonical = ALIASES.get(name,name)
    if registry is not None: return registry.get(canonical)
    if context is not None and canonical in context.backends: return context.get(canonical)
    return Registry().get(canonical)


def resolve_factory(source):
    _source(source)
    if callable(source): return source
    try:
        if source.startswith('entrypoint:'):
            name = source.split(':',1)[1]
            matches = list(metadata.entry_points(group='hydro_cps.physics',name=name))
            if len(matches)!=1: raise ValueError(f'Expected one installed hydro_cps.physics entry point: {name}')
            factory = matches[0].load()
        else:
            module,symbol = source.split(':',1)
            factory = import_module(module)
            for component in symbol.split('.'): factory=getattr(factory,component)
        if not callable(factory): raise TypeError('Factory is not callable')
        return factory
    except (ImportError,AttributeError,TypeError) as exc:
        raise ImportError(f'Cannot load trusted physics plugin {source!r}: {exc}') from exc


def create_backend(name, config, registry=None):
    context = registry or getattr(config,'registry',None)
    backend = resolve_factory(registration(name,context).factory)(config)
    if not isinstance(backend,PhysicsBackend): raise TypeError('Physics plugins must implement PhysicsBackend')
    return backend


register_backend('dhalsim_epynet','src.physics.adapters.epanet:EpanetBackend',
    dependency='epynet.epanet2',initialization='dhalsim',
    legacy_factory='src.physics.adapters.epanet:EpanetAdapter',time_quantum_s=1.)
register_backend('pandapower','src.physics.adapters.pandapower:PandapowerBackend',
    dependency='pandapower',legacy_factory='src.physics.adapters.pandapower:PandapowerAdapter')
