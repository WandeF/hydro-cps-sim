"""Small explicit engineering-unit catalog (no implicit per-unit base guesses)."""
from __future__ import annotations
import math

# dimension, scale to canonical, offset to canonical
UNITS = {
 '1':('ratio',1.,0.), '%':('ratio',.01,0.),
 'W':('active_power',1.,0.), 'kW':('active_power',1000.,0.), 'MW':('active_power',1e6,0.),
 'var':('reactive_power',1.,0.), 'kvar':('reactive_power',1000.,0.), 'Mvar':('reactive_power',1e6,0.),
 'V':('voltage',1.,0.), 'kV':('voltage',1000.,0.),
 'A':('current',1.,0.), 'kA':('current',1000.,0.),
 'm':('length',1.,0.), 'ft':('length',.3048,0.),
 's':('time',1.,0.), 'ms':('time',.001,0.),
 'Pa':('pressure',1.,0.), 'kPa':('pressure',1000.,0.), 'bar':('pressure',1e5,0.),
 'K':('temperature',1.,0.), 'degC':('temperature',1.,273.15),
 'degree':('angle',math.pi/180,0.), 'rad':('angle',1.,0.),
 'm3/s':('flow',1.,0.), 'CMS':('flow',1.,0.),
 'LPS':('flow',.001,0.), 'L/s':('flow',.001,0.),
 'LPM':('flow',.001/60,0.), 'L/min':('flow',.001/60,0.),
 'CMH':('flow',1/3600,0.), 'CMD':('flow',1/86400,0.), 'MLD':('flow',1000/86400,0.),
}


def validate_transform(native, logical, scale, offset, *, context=None, calibration=None):
    context=context or {}
    if not isinstance(context,dict) or set(context)-{'density_kg_m3','gravity_m_s2'}:
        raise ValueError('Invalid unit_context')
    if calibration is not None:
        if native!=logical or not isinstance(calibration,str) or not calibration.strip():
            raise ValueError('Calibration requires identical units and an explicit reason')
        return 'calibration: '+calibration
    if native==logical:
        expected=(1.,0.)  # Opaque/pu/domain units permit identity without SI assumptions.
    else:
        def definition(unit):
            if unit=='mH2O':
                if set(context)!={'density_kg_m3','gravity_m_s2'}:
                    raise ValueError('Water-column pressure conversion requires explicit density and gravity')
                for value in context.values():
                    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
                        raise ValueError('Invalid water-column conversion context')
                return ('pressure',context['density_kg_m3']*context['gravity_m_s2'],0.)
            if unit not in UNITS: raise ValueError(f'Unknown/context-dependent unit conversion: {unit}')
            return UNITS[unit]
        a,b=definition(native),definition(logical)
        if a[0]!=b[0]: raise ValueError(f'Incompatible dimensions: {native} → {logical}')
        expected=(a[1]/b[1],(a[2]-b[2])/b[1])
    if not math.isclose(scale,expected[0],rel_tol=1e-12,abs_tol=1e-15) or not math.isclose(offset,expected[1],rel_tol=1e-12,abs_tol=1e-12):
        raise ValueError(f'Incorrect unit transform {native} → {logical}; expected scale/offset {expected}')
    return 'identity' if native==logical else 'catalog_conversion'
