"""expander.py — Logical Graph (plans) -> Render Graph (passes). 1:1 port of hlsl
Compiler/Lang/Expander.cs (shaders/src/runtime/expander.js, reference/03), emitting the
normalized schema (passType/namespace/func/progName/defines/...).

SCOPE: builtin _read/_write/_read3d/_write3d, 2D + 3D effect passes, programs, texture specs
(is3D), two-pass arg/uniform processing (volumeSize inheritance), inputs/outputs mapping
(2D/3D/geo/agent lanes), palette expansion, scoped params, last-pass-to-surface fusion,
inline-write dedupe, final blit, render-surface resolution. Subchain markers + DSL loops.
Compute/MRT pass fields (entryPoint/workgroups/storage*) raise UnsupportedDsl — staged, as in hlsl.

PARITY-CRITICAL (reference/03):
  - compile-time defines: globals SORTED by name; suffix is sorted entries `__K_V`; value
    stringified like JS String(v) (§4.5).
  - colorMode first-pass / non-surface second-pass arg ordering (§4.8).
  - inputs resolution order (§5.1); outputs incl. last-pass fusion (§5.3).
  - 0 vs missing distinction; insertion order preserved (§9 hazards 4/5).

Pass values are native: uniforms are number/bool/string/list/{type:'Oscillator',...}; surfaces
are {kind,name}/{kind:'temp',index}. The C# UniformValue/ArgValue/JsonValue collapse to these.
"""
import math
import re

from . import enums
from . import palette_expansion
from .validator import UnsupportedDsl
from ..graph import dim as dimmod

_SURFACE_REF_PATTERN = re.compile(r"^(?:o|vol|geo|xyz|vel|rgba)[0-7]$")

_PARTICLE_TEXES = frozenset([
    'global_xyz', 'global_vel', 'global_rgba', 'global_points_trail', 'global_life_data',
])
_COLORMODE_SURFACE_KINDS = frozenset([
    'temp', 'output', 'source', 'feedback', 'xyz', 'vel', 'rgba',
])


def expand(validated, reg):
    return _Expander(validated['plans'], validated.get('render'), reg)._run()


# --- native value predicates / helpers ----------------------------------

def _is_surface(v):
    return isinstance(v, dict) and 'kind' in v


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _str_of(obj, key):
    if not isinstance(obj, dict):
        return None
    v = obj.get(key)
    return v if isinstance(v, str) else None


def _num_or(obj, key, fallback):
    v = obj.get(key) if isinstance(obj, dict) else None
    return v if (isinstance(v, (int, float)) and not isinstance(v, bool)) else fallback


def _num_default(def_):
    d = def_.get('default') if isinstance(def_, dict) else None
    if isinstance(d, bool):
        return 1 if d else 0
    if isinstance(d, (int, float)):
        return d
    return None


def _js_number_string(v):
    """JS String(number): integer-valued doubles print without '.0'."""
    if isinstance(v, float) and v.is_integer() and not math.isinf(v):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    return repr(v)


def _arg_to_uniform(arg):
    """Native arg value -> uniform value (reference/03 ArgToUniform). Mirrors the reference
    expander's `arg.value ?? arg`: only the COLORMODE surface kinds (temp/output/source/feedback/
    xyz/vel/rgba — texture inputs handled via pass.inputs) are dropped; vol/geo (and mesh/pipeline/
    state) surface-refs ARE uniforms and pass through as the {kind,name} object (e.g. a 3D
    generator's `source`/`geoSource` globals → `{kind:'vol',name:'vol0'}`). Both call sites already
    pre-skip the colormode kinds, so the guard here is belt-and-suspenders. Oscillator/object dicts
    and number/bool/string/array pass through unchanged."""
    if arg is None:
        return None
    if _is_surface(arg) and _is_colormode_surface_kind(arg['kind']):
        return None
    return arg


def _resolve_default_uniform(def_, dflt):
    """A global's default -> uniform value; member-string defaults resolve to their enum int."""
    type_ = _str_of(def_, 'type')
    if isinstance(dflt, bool):
        return dflt
    if isinstance(dflt, (int, float)):
        return dflt
    if isinstance(dflt, list):
        if all(isinstance(e, (int, float)) and not isinstance(e, bool) for e in dflt):
            return list(dflt)
        return dflt  # preserved as object
    if isinstance(dflt, str):
        if type_ == 'member':
            resolved = _resolve_enum_static(dflt.split('.'))
            if resolved is not None:
                return resolved
        return dflt
    return None


def _resolve_enum_static(path):
    """resolveEnum over the std/project enum tree only (no symbols), reference/03 §1."""
    if not path:
        return None
    node = enums.try_get_head(path[0])
    if node is None:
        return None
    for i in range(1, len(path)):
        if node is None or enums.is_leaf(node):
            return None
        node = node.get(path[i])
        if node is None:
            return None
    return node['value'] if (node is not None and enums.is_leaf(node)) else None


def _is_particle_tex(name):
    return name in _PARTICLE_TEXES


def _is_colormode_surface_kind(kind):
    return kind in _COLORMODE_SURFACE_KINDS


def _resolve_global_surface_ref(name):
    if name == 'none':
        return 'none'
    if name.startswith('global_'):
        return name
    if _SURFACE_REF_PATTERN.match(name):
        return 'global_' + name
    return name


def _parse_texture_spec(s):
    spec = {
        'width': dimmod.parse_dim(s.get('width')),
        'height': dimmod.parse_dim(s.get('height')),
        'is3D': s.get('is3D') is True,
        'format': _str_of(s, 'format'),
        'depth': None,
    }
    depth = s.get('depth')
    if depth is not None:
        spec['depth'] = dimmod.parse_dim(depth)
    if spec['width'] is None:
        spec['width'] = 'screen'
    if spec['height'] is None:
        spec['height'] = 'screen'
    return spec


class _Expander:
    def __init__(self, plans, render, reg):
        self._plans = plans
        self._render = render
        self._reg = reg
        self._passes = []
        self._errors = []
        self._programs = {}        # id -> {uniformLayout, defines}
        self._texture_specs = {}   # id -> spec dict
        self._render_surface = None
        self._texture_map = {}
        self._last_written_surface = None
        self._blit_registered = False
        self._loop_group_counter = 0
        self._active_loop_group_id = 0
        self._active_loop_iterations = 0
        self._current_particle_pipeline_id = None
        self._current_input_xyz = None
        self._current_input_vel = None
        self._current_input_rgba = None
        self._current_input_3d = None
        self._current_input_geo = None

    def _run(self):
        for plan_index, plan in enumerate(self._plans):
            self._expand_plan(plan, plan_index)
        if self._render is not None:
            self._render_surface = self._render
        elif self._last_written_surface is not None:
            self._render_surface = self._last_written_surface
        else:
            self._errors.append("No render surface specified and no write() found - add render(oN) or write(oN)")
            self._render_surface = None
        return {
            'passes': self._passes,
            'errors': self._errors,
            'programs': self._programs,
            'textureSpecs': self._texture_specs,
            'renderSurface': self._render_surface,
        }

    def _expand_plan(self, plan, plan_index):
        current_input = None
        last_inline_write_target = None
        pipe = {}  # pipelineUniforms: name -> value
        chain_scope_id = "chain_" + str(plan_index)
        self._current_particle_pipeline_id = None
        self._current_input_xyz = self._current_input_vel = self._current_input_rgba = None
        self._current_input_3d = self._current_input_geo = None

        chain = plan['chain']
        for step_pos, step in enumerate(chain):
            op = step['op']
            builtin = step.get('builtin', False)
            args = step['args']
            temp = step['temp']

            if builtin and op == "_read":
                tex = args.get('tex')
                if _is_surface(tex) and tex['kind'] == 'output':
                    current_input = "global_" + tex['name']
                node_id_r = "node_" + str(temp)
                if current_input is not None:
                    self._texture_map[node_id_r + "_out"] = current_input
                continue

            if builtin and op == "_read3d":
                tex3d = args.get('tex3d')
                geo = args.get('geo')
                if _is_surface(tex3d):
                    self._current_input_3d = ("global_" + tex3d['name']) if tex3d['kind'] == 'vol' else tex3d['name']
                if _is_surface(geo):
                    self._current_input_geo = ("global_" + geo['name']) if geo['kind'] == 'geo' else geo['name']
                node_id3 = "node_" + str(temp)
                if self._current_input_3d is not None:
                    self._texture_map[node_id3 + "_out3d"] = self._current_input_3d
                if self._current_input_geo is not None:
                    self._texture_map[node_id3 + "_outGeo"] = self._current_input_geo
                continue

            if builtin and op == "_write3d":
                tex3d = args.get('tex3d')
                geo = args.get('geo')
                node_id_w3 = "node_" + str(temp)
                if _is_surface(tex3d) and tex3d['name'] != "none" and self._current_input_3d is not None:
                    target_vol = "global_" + tex3d['name']
                    if self._current_input_3d != target_vol:
                        self._passes.append(self._new_blit(node_id_w3 + "_write3d_vol_blit",
                                                           self._current_input_3d, target_vol, node_id_w3, temp))
                        self._ensure_blit_program()
                if _is_surface(geo) and geo['name'] != "none" and self._current_input_geo is not None:
                    target_geo = "global_" + geo['name']
                    if self._current_input_geo != target_geo:
                        self._passes.append(self._new_blit(node_id_w3 + "_write3d_geo_blit",
                                                           self._current_input_geo, target_geo, node_id_w3, temp))
                if current_input is not None:
                    self._texture_map[node_id_w3 + "_out"] = current_input
                if self._current_input_3d is not None:
                    self._texture_map[node_id_w3 + "_out3d"] = self._current_input_3d
                if self._current_input_geo is not None:
                    self._texture_map[node_id_w3 + "_outGeo"] = self._current_input_geo
                continue

            if builtin and op == "_write":
                tex = args.get('tex')
                if _is_surface(tex) and current_input is not None:
                    if tex['name'] != "none":
                        target = "global_" + tex['name']
                        if current_input != target:
                            node_id_w = "node_" + str(temp)
                            self._passes.append(self._new_blit(node_id_w + "_write_blit",
                                                               current_input, target, node_id_w, temp))
                            self._ensure_blit_program()
                            self._last_written_surface = tex['name']
                            last_inline_write_target = {'kind': tex['kind'], 'name': tex['name']}
                    self._texture_map["node_" + str(temp) + "_out"] = current_input
                continue

            if builtin and op == "_subchain_begin":
                if current_input is not None:
                    self._texture_map["node_" + str(temp) + "_out"] = current_input
                iters = args.get('iterations')
                if _is_number(iters) and iters > 1:
                    self._loop_group_counter += 1
                    self._active_loop_group_id = self._loop_group_counter
                    self._active_loop_iterations = int(math.floor(iters))
                continue

            if builtin and op == "_subchain_end":
                if current_input is not None:
                    self._texture_map["node_" + str(temp) + "_out"] = current_input
                iters = args.get('iterations')
                if _is_number(iters) and iters > 1:
                    self._active_loop_group_id = 0
                    self._active_loop_iterations = 0
                continue

            last_inline_write_target = None

            if args.get('_skip') is True:
                node_id_s = "node_" + str(temp)
                if current_input is not None:
                    self._texture_map[node_id_s + "_out"] = current_input
                continue

            effect_name = op
            effect_def = self._reg.get_effect(effect_name)
            if effect_def is None:
                self._errors.append("Effect '" + effect_name + "' not found")
                continue

            node_id = "node_" + str(temp)
            scoped_param_map = {}

            textures = effect_def.textures
            if isinstance(textures, dict) and 'global_xyz' in textures:
                self._current_particle_pipeline_id = node_id
                self._current_input_xyz = self._current_input_vel = self._current_input_rgba = None

            defines, raw_define_values, define_suffix = self._build_defines(effect_def, step)
            self._collect_programs(effect_def, node_id, define_suffix, defines)
            self._collect_textures(effect_def, node_id, chain_scope_id, scoped_param_map)
            self._collect_textures3d(effect_def, node_id, chain_scope_id)

            if step.get('from') is not None:
                key = "node_" + str(step['from']) + "_out"
                current_input = self._texture_map.get(key)

            self._apply_global_defaults(effect_def, step, pipe)

            colormode_controlled = set()
            self._args_first_pass(effect_def, step, pipe, colormode_controlled)
            self._args_second_pass(effect_def, step, pipe, colormode_controlled)

            self._expand_passes(effect_def, step, node_id, define_suffix, defines, plan, pipe,
                                scoped_param_map, step_pos, current_input, chain_scope_id)

            # cursor update (reference/03 §4.10, 2D)
            current_input = self._texture_map.get(node_id + "_out")
            if effect_def.output_tex is not None and current_input is None:
                internal_tex = effect_def.output_tex
                if internal_tex == "inputTex":
                    if step.get('from') is not None:
                        prev = "node_" + str(step['from']) + "_out"
                        if prev in self._texture_map:
                            self._texture_map[node_id + "_out"] = self._texture_map[prev]
                            current_input = self._texture_map[prev]
                else:
                    vtid = (self._scope_chain_tex(internal_tex, chain_scope_id)
                            if internal_tex.startswith("global_") else node_id + "_" + internal_tex)
                    self._texture_map[node_id + "_out"] = vtid
                    current_input = vtid
            if (node_id + "_out3d") in self._texture_map:
                self._current_input_3d = self._texture_map[node_id + "_out3d"]
            if (node_id + "_outXyz") in self._texture_map:
                self._current_input_xyz = self._texture_map[node_id + "_outXyz"]
            if (node_id + "_outVel") in self._texture_map:
                self._current_input_vel = self._texture_map[node_id + "_outVel"]
            if (node_id + "_outRgba") in self._texture_map:
                self._current_input_rgba = self._texture_map[node_id + "_outRgba"]
            self._current_input_xyz = self._apply_agent_passthrough(effect_def.output_xyz, node_id + "_outXyz", node_id, chain_scope_id, "inputXyz", self._current_input_xyz)
            self._current_input_vel = self._apply_agent_passthrough(effect_def.output_vel, node_id + "_outVel", node_id, chain_scope_id, "inputVel", self._current_input_vel)
            self._current_input_rgba = self._apply_agent_passthrough(effect_def.output_rgba, node_id + "_outRgba", node_id, chain_scope_id, "inputRgba", self._current_input_rgba)
            if effect_def.output_tex3d is not None and (node_id + "_out3d") not in self._texture_map:
                internal_tex = effect_def.output_tex3d
                if internal_tex == "inputTex3d":
                    if self._current_input_3d is not None:
                        self._texture_map[node_id + "_out3d"] = self._current_input_3d
                else:
                    vtid = (self._scope_chain_tex(internal_tex, chain_scope_id)
                            if internal_tex.startswith("global_") else node_id + "_" + internal_tex)
                    self._texture_map[node_id + "_out3d"] = vtid
                    self._current_input_3d = vtid
            if effect_def.output_geo is not None:
                geo_tex = effect_def.output_geo
                if geo_tex == "inputGeo":
                    if self._current_input_geo is not None:
                        self._texture_map[node_id + "_outGeo"] = self._current_input_geo
                else:
                    vgid = node_id + "_" + geo_tex
                    self._texture_map[node_id + "_outGeo"] = vgid
                    self._current_input_geo = vgid

        # final chain output (reference/03 §4.11)
        if plan.get('write') is not None and current_input is not None:
            out_name = plan['write']['name']
            self._last_written_surface = out_name
            already_written = (last_inline_write_target is not None
                               and last_inline_write_target['kind'] == "output"
                               and last_inline_write_target['name'] == out_name)
            if already_written:
                return
            target = "global_" + out_name
            if current_input != target:
                self._passes.append(self._new_blit("final_blit_" + out_name, current_input, target, None, None))
                self._ensure_blit_program()

    # --- defines (reference/03 §4.5) ------------------------------------

    def _build_defines(self, effect_def, step):
        defines = {}          # defineName -> int
        raw_values = {}
        pairs = []
        bool_defines = set()
        globals_ = effect_def.globals
        if isinstance(globals_, dict):
            for global_name in sorted(globals_.keys()):
                def_ = globals_[global_name]
                define_name = _str_of(def_, 'define')
                if define_name is None:
                    continue
                if _str_of(def_, 'type') == 'boolean':
                    bool_defines.add(define_name)
                value = _num_default(def_)
                if global_name in step['args']:
                    av = step['args'][global_name]
                    if isinstance(av, bool):
                        value = 1 if av else 0
                    elif _is_number(av):
                        value = av
                if value is not None:
                    pairs.append((define_name, value))
                    raw_values[define_name] = value
        # Suffix order follows the sorted-GLOBAL-KEY iteration above (reference/03 §4.5) — do NOT
        # re-sort by define name. The camelCase global key and its SNAKE_CASE define can sort
        # differently (refractMode<type but REFRACT_MODE>NOISE_TYPE); the reference keys off the
        # global. (hlsl re-sorts by define name — a latent bug invisible to its 12-program corpus.)
        suffix = []
        for k, v in pairs:
            defines[k] = int(v)
            sv = ("true" if v != 0.0 else "false") if k in bool_defines else _js_number_string(float(v))
            suffix.append("__" + k + "_" + sv)
        return defines, raw_values, "".join(suffix)

    # --- programs (reference/03 §4.6) -----------------------------------

    def _collect_programs(self, effect_def, node_id, define_suffix, defines):
        shaders = effect_def.shaders
        if not isinstance(shaders, dict):
            return
        for prog_name in shaders:
            unique = node_id + "_" + prog_name + define_suffix
            if unique in self._programs:
                continue
            layout = None
            if isinstance(effect_def.uniform_layouts, dict):
                layout = effect_def.uniform_layouts.get(prog_name)
            if layout is None:
                layout = effect_def.uniform_layout
            ul = {}
            if isinstance(layout, dict):
                for lk, lv in layout.items():
                    if isinstance(lv, dict):
                        ul[lk] = {'slot': int(_num_or(lv, 'slot', 0)), 'components': _str_of(lv, 'components')}
            self._programs[unique] = {'uniformLayout': ul, 'defines': dict(defines)}

    # --- texture specs (reference/03 §6) --------------------------------

    def _collect_textures(self, effect_def, node_id, chain_scope_id, scoped_param_map):
        textures = effect_def.textures
        if not isinstance(textures, dict):
            return
        for tex_name, spec_json in textures.items():
            is_particle = _is_particle_tex(tex_name)
            particle_scoped = is_particle and self._current_particle_pipeline_id is not None
            if particle_scoped:
                virtual_tex_id = tex_name + "_" + self._current_particle_pipeline_id
            elif tex_name.startswith("global_"):
                virtual_tex_id = tex_name + "_" + chain_scope_id
            else:
                virtual_tex_id = node_id + "_" + tex_name
            spec = _parse_texture_spec(spec_json)
            has_param_ref = dimmod.dim_references_param(spec['width']) or dimmod.dim_references_param(spec['height'])
            should_scope = (particle_scoped
                            or (not particle_scoped and tex_name.startswith("global_"))
                            or (self._current_particle_pipeline_id is not None and not tex_name.startswith("global_"))
                            or has_param_ref)
            scope_suffix = self._current_particle_pipeline_id if particle_scoped else chain_scope_id
            if should_scope:
                spec['width'] = dimmod.scope_dim(spec['width'], scope_suffix, scoped_param_map)
                spec['height'] = dimmod.scope_dim(spec['height'], scope_suffix, scoped_param_map)
            self._texture_specs[virtual_tex_id] = spec

    def _collect_textures3d(self, effect_def, node_id, chain_scope_id):
        textures3d = effect_def.textures3d
        if not isinstance(textures3d, dict):
            return
        for tex_name, spec_json in textures3d.items():
            virtual_tex_id = (self._scope_chain_tex(tex_name, chain_scope_id)
                              if tex_name.startswith("global_") else node_id + "_" + tex_name)
            spec = _parse_texture_spec(spec_json)
            spec['is3D'] = True
            self._texture_specs[virtual_tex_id] = spec

    # --- globals defaults + colorMode (reference/03 §4.7) ---------------

    def _apply_global_defaults(self, effect_def, step, pipe):
        globals_ = effect_def.globals
        if not isinstance(globals_, dict):
            return
        for global_name, def_ in globals_.items():
            uniform = _str_of(def_, 'uniform')
            dflt = def_.get('default')
            if uniform is not None and dflt is not None:
                if uniform not in pipe:
                    val = _resolve_default_uniform(def_, dflt)
                    if val is not None:
                        pipe[uniform] = val
            type_ = _str_of(def_, 'type')
            colormode_uniform = _str_of(def_, 'colorModeUniform')
            if type_ == "surface" and colormode_uniform is not None:
                if global_name not in step['args']:
                    is_none = isinstance(dflt, str) and dflt == "none"
                    pipe[colormode_uniform] = 0 if is_none else 1

    # --- args two passes (reference/03 §4.8) ----------------------------

    def _args_first_pass(self, effect_def, step, pipe, colormode_controlled):
        for arg_name, arg in step['args'].items():
            if _is_surface(arg) and _is_colormode_surface_kind(arg['kind']):
                colormode_uniform = self._global_colormode_uniform(effect_def, arg_name)
                if colormode_uniform is not None:
                    is_none = arg['name'] == "none"
                    pipe[colormode_uniform] = 0 if is_none else 1
                    colormode_controlled.add(colormode_uniform)

    def _args_second_pass(self, effect_def, step, pipe, colormode_controlled):
        for arg_name, arg in step['args'].items():
            if _is_surface(arg) and _is_colormode_surface_kind(arg['kind']):
                continue
            if arg_name == "_skip":
                continue
            uniform_name = self._global_uniform_name(effect_def, arg_name) or arg_name
            if uniform_name in colormode_controlled:
                continue
            if uniform_name == "volumeSize" and self._current_input_3d is not None and "volumeSize" in pipe:
                continue
            v = _arg_to_uniform(arg)
            if v is not None:
                pipe[uniform_name] = v

    # --- per-pass expansion (reference/03 §4.9) -------------------------

    def _expand_passes(self, effect_def, step, node_id, define_suffix, defines, plan, pipe,
                       scoped_param_map, step_pos, current_input, chain_scope_id):
        pass_defs = effect_def.passes
        if not isinstance(pass_defs, list):
            return

        define_keys = set()
        globals_ = effect_def.globals
        if isinstance(globals_, dict):
            for gk, gv in globals_.items():
                if _str_of(gv, 'define') is None:
                    continue
                define_keys.add(gk)
                un = _str_of(gv, 'uniform')
                if un is not None:
                    define_keys.add(un)

        for i, pass_def in enumerate(pass_defs):
            program_name = node_id + "_" + _str_of(pass_def, 'program') + define_suffix
            p = {
                'id': node_id + "_pass_" + str(i),
                'passType': 'effect',
                'program': program_name,
                'progName': _str_of(pass_def, 'program'),
                'drawMode': _str_of(pass_def, 'drawMode'),
                'countUniform': _str_of(pass_def, 'countUniform'),
                'effectKey': step['op'],
                'func': effect_def.func or step['op'],
                'namespace': effect_def.namespace,
                'nodeId': node_id,
                'stepIndex': step['temp'],
                'loopGroupId': self._active_loop_group_id,
                'loopIterations': self._active_loop_iterations if self._active_loop_group_id != 0 else 0,
                'inheritsVolumeSize': False,
                'drawBuffers': None,
                'count': None,
                'countMode': None,
                'blend': None,
                'repeat': None,
                'defines': dict(defines),
                'uniforms': {},
                'uniformSpecs': {},
                'inputs': {},
                'outputs': {},
                'scopedParams': None,
            }
            if self._current_input_3d is not None and "volumeSize" in pipe:
                p['inheritsVolumeSize'] = True
            db = pass_def.get('drawBuffers')
            if _is_number(db):
                p['drawBuffers'] = int(db)
            count_val = pass_def.get('count')
            if _is_number(count_val):
                p['count'] = int(count_val)
            elif isinstance(count_val, str):
                p['countMode'] = count_val
            # Preserve the RAW blend value (reference expander.js: `blend: passDef.blend`).
            # `True` (additive ONE/ONE), an explicit factor pair like ['ONE','ONE_MINUS_SRC_ALPHA']
            # (pointsBillboardRender deposit_alpha), or None when the pass declares no blend field.
            # Coercing the array to a bool here dropped it from the graph and broke graph parity.
            p['blend'] = pass_def.get('blend')
            repeat = pass_def.get('repeat')
            if _is_number(repeat):
                p['repeat'] = int(repeat)
            elif isinstance(repeat, str):
                p['repeat'] = repeat

            if (_str_of(pass_def, 'entryPoint') is not None or 'workgroups' in pass_def
                    or 'storageBuffers' in pass_def or 'storageTextures' in pass_def):
                raise UnsupportedDsl("compute/MRT pass fields (entryPoint/workgroups/storage*) are not "
                                     "implemented in the first-cut Expander (reference/03 §2.1).")

            # pass.uniforms = pipeline minus this effect's define-globals
            for uk, uv in pipe.items():
                if uk not in define_keys:
                    p['uniforms'][uk] = uv

            # defaults fill (step 5)
            if isinstance(globals_, dict):
                for _gk, def_ in globals_.items():
                    uniform = _str_of(def_, 'uniform')
                    dflt = def_.get('default')
                    if (uniform is not None and dflt is not None
                            and uniform not in p['uniforms'] and uniform not in define_keys):
                        val = _resolve_default_uniform(def_, dflt)
                        if val is not None:
                            p['uniforms'][uniform] = val
                            pipe[uniform] = val

            # uniformSpecs (step 6)
            if isinstance(globals_, dict):
                for gk, def_ in globals_.items():
                    uniform = _str_of(def_, 'uniform') or gk
                    type_ = _str_of(def_, 'type')
                    has_choices = isinstance(def_.get('choices'), dict)
                    if (type_ == "float" or type_ == "int") and not has_choices:
                        p['uniformSpecs'][uniform] = {'min': _num_or(def_, 'min', 0), 'max': _num_or(def_, 'max', 100)}

            # args -> uniforms (step 7)
            for arg_name, arg in step['args'].items():
                if _is_surface(arg) and _is_colormode_surface_kind(arg['kind']):
                    continue
                if arg_name == "_skip":
                    continue
                uniform_name = self._global_uniform_name(effect_def, arg_name) or arg_name
                if self._is_colormode_controlled(effect_def, uniform_name):
                    continue
                if arg_name in define_keys or uniform_name in define_keys:
                    continue
                if uniform_name == "volumeSize" and self._current_input_3d is not None and "volumeSize" in pipe:
                    continue
                v = _arg_to_uniform(arg)
                if v is not None:
                    p['uniforms'][uniform_name] = v
                    pipe[uniform_name] = v

            # pass-level uniform wiring (step 8)
            pass_uniforms = pass_def.get('uniforms')
            if isinstance(pass_uniforms, dict):
                for uniform_name, ref_val in pass_uniforms.items():
                    global_ref = ref_val if isinstance(ref_val, str) else None
                    if uniform_name in pipe:
                        p['uniforms'][uniform_name] = pipe[uniform_name]
                    elif global_ref is not None and global_ref in pipe:
                        p['uniforms'][uniform_name] = pipe[global_ref]
                    elif global_ref is not None and isinstance(globals_, dict):
                        gdef = globals_.get(global_ref)
                        if gdef is not None:
                            gd = gdef.get('default')
                            if gd is not None:
                                val = _resolve_default_uniform(gdef, gd)
                                if val is not None:
                                    p['uniforms'][uniform_name] = val

            # palette expansion (step 9)
            self._expand_palettes(effect_def, p, pipe)

            # inputs / outputs
            self._map_inputs(effect_def, pass_def, step, node_id, plan, current_input, p, chain_scope_id)
            self._map_outputs(pass_def, step, node_id, plan, i, len(pass_defs), step_pos, p, chain_scope_id)

            # scoped-param propagation (step 12)
            for sp_key, sp_val in scoped_param_map.items():
                if sp_key in p['uniforms']:
                    p['uniforms'][sp_val] = p['uniforms'][sp_key]
                    pipe[sp_val] = p['uniforms'][sp_key]
            if scoped_param_map:
                p['scopedParams'] = dict(scoped_param_map)

            self._passes.append(p)

    # --- inputs / outputs (reference/03 §5) -----------------------------

    def _map_inputs(self, effect_def, pass_def, step, node_id, plan, current_input, p, chain_scope_id):
        inputs = pass_def.get('inputs')
        if not isinstance(inputs, dict):
            return
        cur = current_input
        for uniform_name, tex_val in inputs.items():
            tex_ref = tex_val if isinstance(tex_val, str) else None
            if tex_ref is None:
                continue
            is_pipeline_input = tex_ref == "inputTex" or (tex_ref.startswith("o") and _int_prefix_ok(tex_ref))
            if is_pipeline_input:
                p['inputs'][uniform_name] = cur if cur is not None else tex_ref
            elif tex_ref == "inputXyz":
                p['inputs'][uniform_name] = self._current_input_xyz if self._current_input_xyz is not None else tex_ref
            elif tex_ref == "inputVel":
                p['inputs'][uniform_name] = self._current_input_vel if self._current_input_vel is not None else tex_ref
            elif tex_ref == "inputRgba":
                p['inputs'][uniform_name] = self._current_input_rgba if self._current_input_rgba is not None else tex_ref
            elif tex_ref == "inputTex3d":
                p['inputs'][uniform_name] = self._current_input_3d if self._current_input_3d is not None else tex_ref
            elif tex_ref == "inputGeo":
                p['inputs'][uniform_name] = self._current_input_geo if self._current_input_geo is not None else tex_ref
            elif tex_ref == "noise":
                p['inputs'][uniform_name] = "global_noise"
            elif tex_ref == "midiNoteGrid":
                p['inputs'][uniform_name] = "midiNoteGrid"
            elif tex_ref == "feedback" or tex_ref == "selfTex":
                if plan.get('write') is not None:
                    out_name = plan['write']['name']
                    prefix = "feedback" if plan['write']['kind'] == "feedback" else "global"
                    p['inputs'][uniform_name] = prefix + "_" + out_name
                else:
                    p['inputs'][uniform_name] = cur if cur is not None else "global_inputTex"
            elif effect_def.external_texture is not None and tex_ref == effect_def.external_texture:
                p['inputs'][uniform_name] = tex_ref + "_step_" + str(step['temp'])
            elif tex_ref in step['args']:
                arg = step['args'][tex_ref]
                if arg is None:
                    continue
                if _is_surface(arg):
                    if arg['kind'] == "temp":
                        key = "node_" + str(arg['index']) + "_out"
                        p['inputs'][uniform_name] = self._texture_map.get(key)
                    else:
                        p['inputs'][uniform_name] = "none" if arg['name'] == "none" else "global_" + arg['name']
                elif isinstance(arg, str):
                    p['inputs'][uniform_name] = _resolve_global_surface_ref(arg)
            elif isinstance(effect_def.globals, dict) and _global_has_default(effect_def, tex_ref) is not None:
                default_val = _global_has_default(effect_def, tex_ref)
                if default_val == "none":
                    p['inputs'][uniform_name] = "none"
                elif default_val == "inputTex" or default_val == "inputColor":
                    p['inputs'][uniform_name] = cur if cur is not None else default_val
                elif _SURFACE_REF_PATTERN.match(default_val):
                    p['inputs'][uniform_name] = "global_" + default_val
                elif default_val.startswith("global_"):
                    p['inputs'][uniform_name] = self._scope_chain_tex(default_val, chain_scope_id)
                else:
                    p['inputs'][uniform_name] = default_val
            elif tex_ref.startswith("global_"):
                p['inputs'][uniform_name] = self._scope_chain_tex(tex_ref, chain_scope_id)
            elif tex_ref == "outputTex":
                p['inputs'][uniform_name] = node_id + "_out"
            else:
                p['inputs'][uniform_name] = node_id + "_" + tex_ref

    def _map_outputs(self, pass_def, step, node_id, plan, i, pass_count, step_pos, p, chain_scope_id):
        outputs = pass_def.get('outputs')
        if not isinstance(outputs, dict):
            return
        for attachment, tex_val in outputs.items():
            tex_ref = tex_val if isinstance(tex_val, str) else None
            if tex_ref is None:
                continue
            if tex_ref == "outputTex":
                is_last_step = step_pos == len(plan['chain']) - 1
                is_last_pass = i == pass_count - 1
                if is_last_step and is_last_pass and plan.get('write') is not None:
                    out_name = plan['write']['name']
                    prefix = "feedback" if plan['write']['kind'] == "feedback" else "global"
                    virtual_tex = prefix + "_" + out_name
                    self._last_written_surface = out_name
                else:
                    virtual_tex = node_id + "_out"
                self._texture_map[virtual_tex] = virtual_tex
                self._texture_map[node_id + "_out"] = virtual_tex
            elif tex_ref == "outputXyz":
                virtual_tex = node_id + "_outXyz"
                self._texture_map[node_id + "_outXyz"] = virtual_tex
            elif tex_ref == "outputVel":
                virtual_tex = node_id + "_outVel"
                self._texture_map[node_id + "_outVel"] = virtual_tex
            elif tex_ref == "outputRgba":
                virtual_tex = node_id + "_outRgba"
                self._texture_map[node_id + "_outRgba"] = virtual_tex
            elif tex_ref == "inputXyz":
                virtual_tex = self._current_input_xyz if self._current_input_xyz is not None else (node_id + "_inputXyz")
            elif tex_ref == "inputVel":
                virtual_tex = self._current_input_vel if self._current_input_vel is not None else (node_id + "_inputVel")
            elif tex_ref == "inputRgba":
                virtual_tex = self._current_input_rgba if self._current_input_rgba is not None else (node_id + "_inputRgba")
            elif tex_ref == "outputTex3d":
                virtual_tex = node_id + "_out3d"
                self._texture_map[node_id + "_out3d"] = virtual_tex
            elif tex_ref == "inputTex3d":
                virtual_tex = self._current_input_3d if self._current_input_3d is not None else (node_id + "_inputTex3d")
            elif tex_ref == "inputGeo":
                virtual_tex = self._current_input_geo if self._current_input_geo is not None else (node_id + "_inputGeo")
            elif tex_ref.startswith("global_"):
                virtual_tex = self._scope_chain_tex(tex_ref, chain_scope_id)
            elif tex_ref.startswith("feedback_"):
                virtual_tex = tex_ref
            else:
                virtual_tex = node_id + "_" + tex_ref
            p['outputs'][attachment] = virtual_tex

    # --- palette expansion (reference/03 §4.9 step 9 / §7) --------------

    def _expand_palettes(self, effect_def, p, pipe):
        globals_ = effect_def.globals
        if not isinstance(globals_, dict):
            return
        for gk, def_ in globals_.items():
            if _str_of(def_, 'type') != "palette":
                continue
            uniform_name = _str_of(def_, 'uniform') or gk
            if uniform_name not in p['uniforms']:
                continue
            uv = p['uniforms'][uniform_name]
            if not _is_number(uv):
                continue
            vecs = palette_expansion.expand_vectors(uv)
            if vecs is None:
                continue
            for vk, vv in vecs:
                if vk in p['uniforms']:
                    p['uniforms'][vk] = list(vv)
                    pipe[vk] = list(vv)
            mode = palette_expansion.expand_mode(uv)
            if mode is not None and "paletteMode" in p['uniforms']:
                p['uniforms']["paletteMode"] = mode
                pipe["paletteMode"] = mode

    # --- helpers --------------------------------------------------------

    def _new_blit(self, id_, src, dst, node_id, step_index):
        return {
            'id': id_,
            'passType': 'blit',
            'program': 'blit',
            'progName': 'blit',
            'func': 'blit',
            'namespace': None,
            'nodeId': node_id,
            'effectKey': None,
            'stepIndex': step_index,
            'loopGroupId': self._active_loop_group_id,
            'loopIterations': self._active_loop_iterations if self._active_loop_group_id != 0 else 0,
            'inheritsVolumeSize': False,
            'drawMode': None,
            'countUniform': None,
            'drawBuffers': None,
            'count': None,
            'countMode': None,
            'blend': None,
            'repeat': None,
            'defines': {},
            'uniforms': {},
            'uniformSpecs': {},
            'inputs': {'src': src},
            'outputs': {'color': dst},
            'scopedParams': None,
        }

    def _ensure_blit_program(self):
        if self._blit_registered:
            return
        if 'blit' not in self._programs:
            self._programs['blit'] = {'uniformLayout': {}, 'defines': {}}
        self._blit_registered = True

    @staticmethod
    def _global_uniform_name(effect_def, arg_name):
        g = effect_def.globals.get(arg_name) if isinstance(effect_def.globals, dict) else None
        return _str_of(g, 'uniform') if g is not None else None

    @staticmethod
    def _global_colormode_uniform(effect_def, arg_name):
        g = effect_def.globals.get(arg_name) if isinstance(effect_def.globals, dict) else None
        return _str_of(g, 'colorModeUniform') if g is not None else None

    @staticmethod
    def _is_colormode_controlled(effect_def, uniform_name):
        if not isinstance(effect_def.globals, dict):
            return False
        for gv in effect_def.globals.values():
            if _str_of(gv, 'colorModeUniform') == uniform_name:
                return True
        return False

    def _scope_particle_tex(self, name):
        if self._current_particle_pipeline_id is not None and _is_particle_tex(name):
            return name + "_" + self._current_particle_pipeline_id
        return name

    def _scope_chain_tex(self, tex_name, chain_scope_id):
        if _is_particle_tex(tex_name):
            return self._scope_particle_tex(tex_name)
        if tex_name.startswith("global_"):
            return tex_name + "_" + chain_scope_id
        return tex_name

    def _apply_agent_passthrough(self, decl, out_key, node_id, chain_scope_id, reuse_keyword, cursor):
        if decl is None or out_key in self._texture_map:
            return cursor
        if decl == reuse_keyword:
            vtid = cursor
        elif decl.startswith("global_"):
            vtid = self._scope_chain_tex(decl, chain_scope_id)
        else:
            vtid = node_id + "_" + decl
        if vtid is None:
            return cursor
        self._texture_map[out_key] = vtid
        return vtid


def _int_prefix_ok(tex_ref):
    if len(tex_ref) < 2:
        return False
    c = tex_ref[1]
    return '0' <= c <= '9'


def _global_has_default(effect_def, name):
    """Returns the string default of a global, or None (reference GlobalHasDefault)."""
    if not isinstance(effect_def.globals, dict):
        return None
    g = effect_def.globals.get(name)
    if g is None:
        return None
    d = g.get('default')
    if d is None:
        return None
    return d if isinstance(d, str) else None
