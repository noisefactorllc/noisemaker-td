"""Global surfaces (o0..o7) and state surfaces — reference/04 §8 + §10.7.

The reference double-buffers every global surface (read/write) and, at end of frame, either
swaps them (display surfaces o0..o7) or persists the final bindings (state surfaces: particle
xyz/vel/rgba, trail, *state*). In TouchDesigner:

  * Display surfaces (written once per frame, presented) need NO feedback — a read of o0 maps
    straight to the TOP that wrote it this frame. This path is COMPLETE and is all Tier-1 needs.
  * State surfaces (read AND written across frames: sims, trails, reaction-diffusion) map to a
    **Feedback TOP** whose Target is the writer — cross-frame persistence. Wiring the feedback
    loop + within-frame ping-pong (reference/04 §10.2/§10.6) is Phase 5.5; structured here.

`isStateSurface` is parity-critical (getting membership wrong desyncs sims) — ported exactly,
case-sensitive substring tests on 'state'/'State'.
"""
import re

try:
    feedbackTOP        # noqa: F821
    _IN_TD = True
except NameError:
    _IN_TD = False

_STATE_NODE_RE = re.compile(r'^(xyz|vel|rgba|points_trail)_node_\d+$')


def parse_global_name(tex_id):
    """`global_o0` -> `o0`; non-global -> None (reference/04 §8 parseGlobalName)."""
    if isinstance(tex_id, str) and tex_id.startswith('global_'):
        return tex_id[len('global_'):]
    return None


def is_state_surface(name):
    """reference/04 §10.7 isStateSurface — persisted (not swapped) across frames."""
    if name in ('xyz', 'vel', 'rgba', 'trail'):
        return True
    for suf in ('_xyz', '_vel', '_rgba', '_trail'):
        if name.endswith(suf):
            return True
    if 'state' in name or 'State' in name:   # case-sensitive, both spellings
        return True
    return bool(_STATE_NODE_RE.match(name))


class SurfaceManager:
    def __init__(self, parent_comp):
        self.parent = parent_comp
        self.writers = {}        # surfaceName -> writer TOP (this frame)
        self.feedbacks = {}      # surfaceName -> Feedback TOP (state surfaces)
        self.ops = []

    def note_write(self, tex_id, top):
        name = parse_global_name(tex_id)
        if name is None:
            return
        self.writers[name] = top

    def read_top(self, tex_id):
        """Return the TOP to read `tex_id` from, or None to let the backend use its tex_top map.

        Display surfaces -> None (read straight from the writer). State surfaces -> the Feedback
        TOP (previous-frame content)."""
        name = parse_global_name(tex_id)
        if name is None:
            return None
        if is_state_surface(name):
            return self.feedbacks.get(name)   # may be None until finalize() (Phase 5.5)
        return self.writers.get(name)

    def finalize(self):
        """Create Feedback TOPs for state surfaces and wire Target = writer.

        Phase 5.5: also reconcile within-frame ping-pong. Left as a structured stub so the
        Tier-1 (display-surface) path is unaffected; sims light up when this is completed."""
        if not _IN_TD:
            return
        for name, writer in self.writers.items():
            if not is_state_surface(name) or name in self.feedbacks:
                continue
            fb = self.parent.create(feedbackTOP, 'fb_%s' % re.sub(r'[^A-Za-z0-9_]', '_', name))
            self.ops.append(fb)
            self.feedbacks[name] = fb
            try:
                fb.par.top = writer            # Target TOP = the surface writer
            except Exception:
                pass
        # TODO(Phase 5.5): rebuild reads that resolved before the feedback existed; honor the
        # 3-tier swap/persist of reference/04 §10.2/§10.6/§10.7.
