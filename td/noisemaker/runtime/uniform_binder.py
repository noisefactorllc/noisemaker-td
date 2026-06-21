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


def declared_uniform_names(frag_text):
    """Scalar/vector uniform names declared in a .frag (samplers excluded) — so we bind only
    what the shader uses and keep the Vectors slot count minimal."""
    import re
    return set(re.findall(
        r'\buniform\s+(?:float|int|uint|bool|vec[234]|ivec[234]|uvec[234]|bvec[234]|mat[234])\s+'
        r'([A-Za-z_]\w*)', frag_text))


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


def _matrix_order(value):
    """3 for a 9-element mat3 flat list, 4 for a 16-element mat4, else 0 (not a matrix).

    The Vectors page only carries 1–4 floats, so a `uniform mat3`/`mat4` (e.g. renderCubemap3d's
    `cubeBasis`) must take the Matrices page instead. Matrix uniform VALUES are the only ones with
    9 or 16 components (vecs/colors are ≤4, scalars are 1), so length alone classifies them — which
    also lets set_time() re-bind without re-parsing the shader."""
    if isinstance(value, (list, tuple)) and not isinstance(value, str):
        n = len(value)
        if n == 9:
            return 3
        if n == 16:
            return 4
    return 0


def bind_uniforms(glsl_top, values):
    """Bind {uniformName: value} onto `glsl_top`'s Vectors page.

    The GLSL TOP's `vec` parameter is the SLOT COUNT — only `vec0` exists until it's set, so we
    set `g.par.vec = N` first to materialize N slots, then fill each. Returns the slot count.
    Uniforms bind by name, not slot position. Pass only the uniforms the shader declares.
    """
    items = []
    matrices = []                                  # (name, order, flat) for the Matrices page
    for name, value in values.items():
        order = _matrix_order(value)
        if order:
            matrices.append((name, order, [float(x) for x in value]))
            continue
        comps = _as_components(value)
        if comps is not None:
            items.append((name, comps))
    try:
        setattr(glsl_top.par, 'vec', len(items))   # materialize the slots — THE key step
    except Exception as exc:
        _warn('set vec count=%d: %s' % (len(items), exc))
    for slot, (name, comps) in enumerate(items):
        try:
            setattr(glsl_top.par, 'vec%dname' % slot, name)
            for i, comp in enumerate(comps):
                setattr(glsl_top.par, 'vec%d%s' % (slot, VEC_COMPONENTS[i]), comp)
        except Exception as exc:  # noqa: BLE001 — surface, don't abort the whole build
            _warn('uniform %r slot %d: %s' % (name, slot, exc))
    _bind_matrices(glsl_top, matrices)
    return len(items)


def _bind_matrices(glsl_top, matrices):
    """Bind mat3/mat4 uniforms onto the GLSL TOP's **Matrices** page.

    A matrix uniform's value comes from a Table DAT (`matrix{i}value`), not float params. The
    reference feeds these flat arrays COLUMN-major (WebGL `uniformMatrix*fv` is transpose-false),
    and TD reads table row r as GLSL matrix row r (probed: td/matrix_probe.py), so we transpose on
    the way in — table[r][c] = flat[order*c + r] — making GLSL column k == the reference's column k.
    Idempotent: the per-slot Table DAT is reused (and refilled) across set_time re-binds."""
    if not matrices:
        return
    try:
        setattr(glsl_top.par, 'matrix', len(matrices))   # materialize matrix slots
    except Exception as exc:
        _warn('set matrix count=%d: %s' % (len(matrices), exc))
        return
    parent = glsl_top.parent()
    for slot, (name, order, flat) in enumerate(matrices):
        try:
            dat = _matrix_table(glsl_top, parent, slot, order, flat)
            setattr(glsl_top.par, 'matrix%dname' % slot, name)
            setattr(glsl_top.par, 'matrix%dvalue' % slot, dat)
        except Exception as exc:  # noqa: BLE001
            _warn('matrix %r slot %d: %s' % (name, slot, exc))


def _matrix_table(glsl_top, parent, slot, order, flat):
    """Reuse-or-create the `<top>_mtxN` Table DAT and fill it with the transposed matrix."""
    import td
    dat_name = '%s_mtx%d' % (glsl_top.name, slot)
    dat = parent.op(dat_name) or parent.create(td.tableDAT, dat_name)
    dat.clear()
    for r in range(order):
        dat.appendRow([repr(flat[order * c + r]) for c in range(order)])
    return dat


def _warn(msg):
    try:
        import td  # noqa: F401 — TD injects this; debug() is global in-process
        debug('[uniform_binder] ' + msg)  # noqa: F821
    except Exception:
        pass
