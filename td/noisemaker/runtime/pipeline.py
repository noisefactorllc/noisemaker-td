"""Build orchestration + deterministic time driving.

The reference runs an imperative per-frame loop (reference/04 §10). In TouchDesigner the built
network cooks itself; the pipeline's job each frame is only to (1) update the engine `time`
uniform (normalized 0..1) on every effect TOP and (2) request the output to cook. For parity we
pin `time` to a fixed value and render one frame.

Touches the TouchDesigner Python API — only runs inside a TD process.
"""
from . import uniform_binder
from .engine_uniforms import engine_uniforms
from .td_backend import TDBackend
from .surface_manager import SurfaceManager


class Pipeline:
    def __init__(self, parent_comp, shaders_root, *, width=256, height=256, time=0.25):
        self.parent = parent_comp
        self.shaders_root = shaders_root
        self.width = width
        self.height = height
        self._time = time
        self.surfaces = SurfaceManager(parent_comp)
        self.backend = TDBackend(parent_comp, shaders_root, width=width, height=height,
                                 time=time, surface_manager=self.surfaces)
        self.output = None      # the TOP presented (renderSurface)
        self._effect_tops = []

    def build(self, graph):
        # The backend binds engine uniforms (incl. `time`) at build time; no post-build re-stamp
        # (that would overwrite the per-effect uniforms). Live animation = rebuild (Phase 3.5).
        self.output = self.backend.build(graph)
        self.surfaces.finalize()
        self._effect_tops = [g for g in self.backend.ops if _is_glsl_top(g)]
        return self.output

    def set_resolution(self, width, height):
        self.width = width
        self.height = height
        # full rebuild is simplest/safest for a resolution change (rare; parity is fixed-size).

    def set_time(self, t):
        """Stamp normalized time onto every effect TOP's engine uniforms."""
        self._time = float(t)
        eu = engine_uniforms(self.width, self.height, self._time)
        for g in self._effect_tops:
            uniform_binder.bind_uniforms(g, eu)   # re-binds engine slots 0..n in stable order

    def render_to(self, filepath, *, time=None):
        """Deterministic one-shot render of the presented surface to an image file.
        Returns the save path (str) or None. Caller should ensure project.realTime=False."""
        if time is not None:
            self.set_time(time)
        if self.output is None:
            return None
        try:
            self.output.cook(force=True)
        except Exception:
            pass
        return str(self.output.save(filepath, createFolders=True))

    def teardown(self):
        self.backend.teardown()
        for o in self.surfaces.ops:
            try:
                o.destroy()
            except Exception:
                pass


def _is_glsl_top(op):
    try:
        return op.type in ('glsl', 'glslmulti') or op.OPType in ('glslTOP', 'glslmultiTOP')
    except Exception:
        return False
