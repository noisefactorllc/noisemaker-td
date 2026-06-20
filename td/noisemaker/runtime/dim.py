"""Dimension resolution — port of reference/04 §9 `resolveDimension`, exact.

A texture dimension (`width`/`height`/`depth`) in the Render Graph JSON is a `Dim`:
a number, the string `"screen"`/`"auto"`, a percent string (`"6.25%"`), or an object
`{param,…}` | `{screenDivide,…}` | `{scale,…}`. The rounding rules are parity-critical
(floor for param/percent/scale, round for screenDivide, always `max(1, …)`).

Python's `int(math.floor(x))` matches JS `Math.floor`; `round_half` below matches JS
`Math.round` (round-half-UP, NOT Python's banker's rounding).
"""
import math


def _js_round(x):
    # JS Math.round: round half toward +Infinity (NOT Python round-half-even).
    return int(math.floor(x + 0.5))


def _nullish(value, fallback):
    # JS `??` — only None substitutes (0 / "" / False are kept). Python None == JS null/undefined.
    return fallback if value is None else value


def resolve_dimension(spec, screen_size, uniforms=None):
    """Resolve a Dim spec to a concrete int pixel size. `screen_size` is the axis
    size (width for width, height for height). `uniforms` supplies `{param}` lookups."""
    uniforms = uniforms or {}

    # 1. number -> max(1, floor)
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return max(1, int(math.floor(spec)))

    # 2/3. strings
    if isinstance(spec, str):
        if spec in ('screen', 'auto'):
            return int(screen_size)
        if spec.endswith('%'):
            return max(1, int(math.floor(screen_size * float(spec[:-1]) / 100.0)))
        # unknown string -> fall through to screen
        return int(screen_size)

    # 4. object
    if isinstance(spec, dict):
        if spec.get('param') is not None:
            param = spec['param']
            has_transform = ('power' in spec and spec['power'] is not None) or \
                            ('multiply' in spec and spec['multiply'] is not None)
            param_default = _nullish(spec.get('paramDefault'), 64)
            value = _nullish(uniforms.get(param), param_default)
            if spec.get('multiply') is not None:
                value = value * spec['multiply']
            if spec.get('power') is not None:
                value = math.pow(value, spec['power'])
            if has_transform and uniforms.get(param) is None and spec.get('default') is not None:
                value = spec['default']
            return max(1, int(math.floor(value)))

        if spec.get('screenDivide') is not None:
            divisor = _nullish(_nullish(uniforms.get(spec['screenDivide']), spec.get('default')), 1)
            return max(1, _js_round(screen_size / divisor))  # NOTE: round, not floor

        if spec.get('scale') is not None:
            computed = int(math.floor(screen_size * spec['scale']))
            clamp = spec.get('clamp')
            if clamp:
                if clamp.get('min') is not None:
                    computed = max(computed, int(clamp['min']))
                if clamp.get('max') is not None:
                    computed = min(computed, int(clamp['max']))
            return max(1, computed)

    # 5. fallback
    return int(screen_size)


def is_dynamic_dimension(spec):
    """reference/04 §9 `isDynamicDimension`: number -> fixed (False); string/object -> dynamic."""
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return False
    return True
