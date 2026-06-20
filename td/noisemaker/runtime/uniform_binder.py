"""Feed a {name: value} uniform dict onto a GLSL TOP's **Vectors** page.

TouchDesigner GLSL TOP custom uniforms (docs.derivative.ca/Write_a_GLSL_TOP): declare a
uniform of the same name/size as a Vectors-page entry; TD binds it. The Python par names are
`vec{i}name` (the uniform name) and `vec{i}valuex/y/z/w` (the components). We assign one slot
per uniform, in a stable order, packing 1–4 components by the value's arity.

This module touches the TouchDesigner Python API and only runs inside a TD process.

Two things to CONFIRM at bring-up (Task 2.3) — localized to this module if they need changing:
  * Vector-slot count: an effect like `noise` has ~13 uniforms. If the GLSL TOP exposes fewer
    Vectors slots than needed, switch this feed to an Arrays-page "Uniform Array" sourced from
    a Constant CHOP (one channel per scalar) — the call sites don't change.
  * int / bool uniforms: the reference declares `uniform int`/`uniform bool` (seed, octaves,
    ridges, …). The Vectors page sends floats. If TD won't bind a float slot to an int/bool
    uniform, the fix is a transpiler refinement emitting those decls as `float` with in-shader
    casts (PORTING-GUIDE "uniform typing"). We pass bool→1.0/0.0 and int→float here.
"""

VEC_COMPONENTS = ('valuex', 'valuey', 'valuez', 'valuew')


def _as_components(value):
    """Normalize a uniform value to a list of 1–4 floats."""
    if isinstance(value, bool):
        return [1.0 if value else 0.0]
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, (list, tuple)):
        comps = [float(x) for x in value][:4]
        return comps or [0.0]
    # Unknown (e.g. an unresolved oscillator object) — skip rather than crash.
    return None


def bind_uniforms(glsl_top, values, *, start_slot=0):
    """Assign each (name, value) in `values` to a Vectors-page slot on `glsl_top`.

    `values` is an ordered mapping {uniformName: value}. Returns the next free slot index.
    Order matters only for determinism; uniforms bind by name, not by slot position.
    """
    slot = start_slot
    for name, value in values.items():
        comps = _as_components(value)
        if comps is None:
            continue
        try:
            setattr(glsl_top.par, 'vec%dname' % slot, name)
            for i, comp in enumerate(comps):
                setattr(glsl_top.par, 'vec%d%s' % (slot, VEC_COMPONENTS[i]), comp)
        except Exception as exc:  # noqa: BLE001 — surface, don't abort the whole build
            _warn('uniform %r slot %d: %s' % (name, slot, exc))
        slot += 1
    return slot


def _warn(msg):
    try:
        import td  # noqa: F401 — TD injects this; debug() is global in-process
        debug('[uniform_binder] ' + msg)  # noqa: F821
    except Exception:
        pass
