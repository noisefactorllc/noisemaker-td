"""dsl_compiler.py — the public Polymorphic DSL frontend entry point. Port of hlsl
Compiler/DslCompiler.cs (shaders/src/runtime/compiler.js compileGraph).

Pipeline (reference/01..04):
  1. lex(src)                  -> tokens
  2. parse(tokens, reg)        -> Program AST
  3. validate(ast, reg)        -> {plans, diagnostics, render}  (errors collected, except missing-search)
  4. expand(validated, reg)    -> passes / programs / textureSpecs / renderSurface
  5. allocate_resources(passes)-> allocations (phys_N)
  6. assemble + normalize      -> the Render Graph JSON (GRAPH-JSON-SCHEMA.md / export-graph.mjs shape)

compile_graph raises CompileError on error-severity diagnostics or expansion errors. The emitted
dict is byte-identical (modulo id/source) to tools/export-graph.mjs — the parity contract.
"""
from .lang.lexer import lex
from .lang.parser import parse
from .lang.validator import validate
from .lang.expander import expand
from .lang import diagnostics as _diag
from .graph.resources import allocate_resources


class CompileError(Exception):
    def __init__(self, message, diagnostics=None, expand_errors=None):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.expand_errors = expand_errors


_CACHED_REG = None


def compile_dsl(src, reg=None):
    """Convenience entry point: compile DSL source with a module-cached EffectRegistry (loaded
    once from td/noisemaker/effects). Used by the live runtime path (nm_renderer.set_dsl)."""
    global _CACHED_REG
    if reg is None:
        if _CACHED_REG is None:
            from .lang.effect_registry import EffectRegistry
            _CACHED_REG = EffectRegistry.load_from_directory()
        reg = _CACHED_REG
    return compile_graph(src, reg)


def compile_graph(dsl, reg):
    """Compile DSL source into a normalized Render Graph dict."""
    tokens = lex(dsl)
    ast = parse(tokens, reg)
    validated = validate(ast, reg)

    errors = [d for d in validated['diagnostics'] if d.get('severity') == _diag.SEVERITY_ERROR]
    if errors:
        raise CompileError("ERR_COMPILATION_FAILED", diagnostics=validated['diagnostics'])

    expanded = expand(validated, reg)
    if expanded['errors']:
        raise CompileError("ERR_EXPANSION_FAILED", expand_errors=expanded['errors'])

    allocations = allocate_resources(expanded['passes'])

    return {
        'id': _hash_source(dsl),
        'source': dsl,
        'renderSurface': expanded['renderSurface'],
        'passes': [_normalize_pass(p) for p in expanded['passes']],
        'allocations': allocations,
        'textures': _build_textures(expanded),
        'programs': expanded['programs'],
    }


# --- normalization (mirrors export-graph.mjs normalizePass/normalizeGraph) -----

def _normalize_pass(p):
    out = {
        'id': p['id'],
        'passType': p['passType'],
        'namespace': p['namespace'],
        'func': p['func'],
        'progName': p['progName'],
        'program': p['program'],
        'defines': p['defines'],
        'inputs': p['inputs'],
        'outputs': p['outputs'],
        'uniforms': p['uniforms'],
        'uniformSpecs': p['uniformSpecs'],
    }
    if p['drawMode'] is not None:
        out['drawMode'] = p['drawMode']
    if p['count'] is not None:
        out['count'] = p['count']
    elif p['countMode'] is not None:
        out['count'] = p['countMode']
    if p['countUniform'] is not None:
        out['countUniform'] = p['countUniform']
    if p['drawBuffers'] is not None:
        out['drawBuffers'] = p['drawBuffers']
    # Emit the RAW blend value when present (reference export-graph normalizePass:
    # `if (pass.blend !== undefined) out.blend = pass.blend`). True for additive ONE/ONE,
    # or a factor pair like ['ONE','ONE_MINUS_SRC_ALPHA'] for premultiplied-over deposit.
    # None means the pass declared no blend field -> key stays absent.
    if p['blend'] is not None:
        out['blend'] = p['blend']
    if p['repeat'] is not None:
        out['repeat'] = p['repeat']
    out['effectKey'] = p['effectKey']          # always (null when absent)
    out['nodeId'] = p['nodeId']                  # always (null when absent)
    if p['stepIndex'] is not None:
        out['stepIndex'] = p['stepIndex']
    if p['inheritsVolumeSize']:
        out['inheritsVolumeSize'] = True
    out['scopedParams'] = p['scopedParams']      # always (null when absent)
    # loopGroupId/loopIterations are an additive DSL-loops extension absent from the reference
    # normalized graph; emit only when actually in an iterated bracket (never for the corpus).
    if p['loopGroupId'] != 0:
        out['loopGroupId'] = p['loopGroupId']
        out['loopIterations'] = p['loopIterations']
    return out


def _build_textures(expanded):
    """compiler.js extractTextureSpecs: expander textureSpecs (defaulting width/height->screen,
    format->rgba16f) + pass output textures not already defined and not global_."""
    textures = {}
    for tex_id, src in expanded['textureSpecs'].items():
        textures[tex_id] = _normalize_texture(src)
    for p in expanded['passes']:
        for tex_id in p['outputs'].values():
            if tex_id is None:
                continue
            if tex_id.startswith('global_'):
                continue
            if tex_id in textures:
                continue
            textures[tex_id] = _normalize_texture({'width': 'screen', 'height': 'screen',
                                                   'format': 'rgba16f', 'is3D': False, 'depth': None})
    return textures


def _normalize_texture(src):
    is3d = src.get('is3D', False)
    out = {
        'width': src['width'] if src.get('width') is not None else 'screen',
        'height': src['height'] if src.get('height') is not None else 'screen',
        'format': src['format'] if src.get('format') is not None else 'rgba16f',
        'usage': (['storage', 'sample', 'copySrc', 'copyDst'] if is3d
                  else ['render', 'sample', 'copySrc', 'copyDst']),
    }
    if src.get('depth') is not None:
        out['depth'] = src['depth']
    if is3d:
        out['is3D'] = True
    return out


# --- hashSource (compiler.js): 32-bit ((h<<5)-h)+c rolling hash, base-36 -------

def _hash_source(source):
    h = 0
    for c in source:
        h = (((h << 5) - h) + ord(c)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000  # signed int32
    return _to_base36(h)


def _to_base36(value):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    neg = value < 0
    v = -value if neg else value
    out = []
    while v > 0:
        out.append(digits[v % 36])
        v //= 36
    if neg:
        out.append('-')
    return ''.join(reversed(out))
