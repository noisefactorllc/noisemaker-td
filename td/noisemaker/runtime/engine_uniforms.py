"""Per-frame engine globals — port of reference/04 §10.1 `updateGlobalUniforms`.

These are the uniforms every effect shader may read (the reference declares whichever it
needs: `time`, `resolution`, `tileOffset`, `fullResolution`, `aspectRatio`, `renderScale`,
`seed` is per-effect not engine). Pure Python so it is unit-testable with stock python3; the
`uniform_binder` feeds the resulting dict onto each GLSL TOP.

`time` is normalized **0..1** (reference/04 §10), wrapping each animation loop — NOT wall
clock. For deterministic parity renders the pipeline pins it to a fixed value (e.g. 0.25).
TouchDesigner has no built-in time uniform, so we supply our own with the reference's exact
semantics rather than `absTime.seconds`.
"""


def engine_uniforms(width, height, time, *, frame=0, delta_time=0.0,
                    tile_offset=None, full_resolution=None, render_scale=1.0):
    """Build the engine-global uniform dict for one frame (reference/04 §10.1)."""
    w = float(width)
    h = float(height)
    aspect_value = w / h if h else 1.0

    if full_resolution is not None:
        fr = [float(full_resolution[0]), float(full_resolution[1])]
        full_aspect = fr[0] / fr[1] if fr[1] else aspect_value
    else:
        fr = [w, h]
        full_aspect = aspect_value

    return {
        'time': float(time),
        'deltaTime': float(delta_time),
        'frame': int(frame),
        'resolution': [w, h],
        'tileOffset': list(tile_offset) if tile_offset is not None else [0.0, 0.0],
        'fullResolution': fr,
        'aspect': full_aspect,
        'aspectRatio': full_aspect,
        'renderScale': float(render_scale) if render_scale else 1.0,
    }


# Names that are engine-supplied (so the binder can tell them apart from effect params,
# and the builder knows not to expect them in pass.uniforms).
ENGINE_UNIFORM_NAMES = frozenset({
    'time', 'deltaTime', 'frame', 'resolution', 'tileOffset',
    'fullResolution', 'aspect', 'aspectRatio', 'renderScale',
})
