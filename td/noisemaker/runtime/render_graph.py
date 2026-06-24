# render_graph.py — the in-memory model of the normalized Render Graph JSON.
#
# Pure Python (no TouchDesigner API) so it can be unit-tested with stock python3.
# Shape contract: ../../../docs/GRAPH-JSON-SCHEMA.md (mirrors reference/03 + 04).
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TextureSpec:
    width: Any = "screen"          # Dim: number | "screen"/"auto" | "6.25%" | {param|screenDivide|scale}
    height: Any = "screen"
    depth: Any = None
    is3D: bool = False
    fmt: str = "rgba16f"           # rgba16f | rgba32f | rgba8
    usage: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "TextureSpec":
        return TextureSpec(
            width=d.get("width", "screen"),
            height=d.get("height", "screen"),
            depth=d.get("depth"),
            is3D=bool(d.get("is3D", False)),
            fmt=d.get("format", "rgba16f"),
            usage=list(d.get("usage", [])),
        )


@dataclass
class Pass:
    id: str
    pass_type: str                 # "effect" | "blit"
    namespace: Optional[str]
    func: Optional[str]            # effect function -> selects the .frag program dir
    prog_name: Optional[str]       # program basename -> the specific <prog>.frag
    program: Optional[str]         # graph program id (e.g. "node_0_noise")
    defines: dict[str, Any] = field(default_factory=dict)   # compile-time #define overrides
    inputs: dict[str, str] = field(default_factory=dict)    # samplerName -> texId | "none"
    outputs: dict[str, str] = field(default_factory=dict)   # attachment -> texId (MRT: color,color1,…)
    uniforms: dict[str, Any] = field(default_factory=dict)  # name -> literal value
    uniform_specs: dict[str, Any] = field(default_factory=dict)
    # execution modifiers
    draw_mode: Optional[str] = None         # "points" -> scatter (Phase 5.5)
    count: Any = None
    count_uniform: Optional[str] = None
    draw_buffers: int = 1                    # MRT attachment count
    # Raw blend spec: True (additive Blend One One), a factor pair like
    # ['ONE','ONE_MINUS_SRC_ALPHA'] (premultiplied OVER), or None (no blend declared).
    blend: Any = None
    repeat: Any = None                       # int | uniform-name
    clear: Any = None
    # Per-pass runIf/skipIf gating (reference Pipeline.shouldSkipPass). NOT present in the
    # serialized graph (the reference expander drops it); the TD backend loads it from the
    # effect JSON and attaches it before building. {runIf|skipIf: [{uniform, equals}]}.
    conditions: Optional[dict] = None
    # metadata
    effect_key: Optional[str] = None
    node_id: Optional[str] = None
    step_index: int = 0

    @property
    def is_blit(self) -> bool:
        return self.pass_type == "blit"

    @property
    def is_effect(self) -> bool:
        return self.pass_type == "effect"

    @property
    def is_points(self) -> bool:
        return self.draw_mode == "points"

    @property
    def is_scatter(self) -> bool:
        """A geometry scatter draw (agents -> trail): points (1px) or billboards (sized sprites)."""
        return self.draw_mode in ("points", "billboards")

    @property
    def is_mrt(self) -> bool:
        return (self.draw_buffers or 0) > 1 or len(self.outputs) > 1

    @property
    def blend_factors(self):
        """Resolve the raw blend spec to a (src, dst) factor-name pair, or None for no blend.
        `True` -> additive ONE/ONE; a 2-element list -> that explicit pair (case-insensitive)."""
        b = self.blend
        if b is True:
            return ("ONE", "ONE")
        if isinstance(b, (list, tuple)) and len(b) == 2:
            return (str(b[0]).upper(), str(b[1]).upper())
        return None

    @staticmethod
    def from_dict(d: dict) -> "Pass":
        return Pass(
            id=d["id"],
            pass_type=d.get("passType", "effect"),
            namespace=d.get("namespace"),
            func=d.get("func"),
            prog_name=d.get("progName"),
            program=d.get("program"),
            defines=dict(d.get("defines", {})),
            inputs=dict(d.get("inputs", {})),
            outputs=dict(d.get("outputs", {})),
            uniforms=dict(d.get("uniforms", {})),
            uniform_specs=dict(d.get("uniformSpecs", {})),
            draw_mode=d.get("drawMode"),
            count=d.get("count"),
            count_uniform=d.get("countUniform"),
            draw_buffers=int(d.get("drawBuffers", 1) or 1),
            blend=d.get("blend"),
            repeat=d.get("repeat"),
            clear=d.get("clear"),
            conditions=d.get("conditions"),
            effect_key=d.get("effectKey"),
            node_id=d.get("nodeId"),
            step_index=int(d.get("stepIndex", 0) or 0),
        )


@dataclass
class RenderGraph:
    id: str
    source: str
    render_surface: Optional[str]
    passes: list[Pass]
    allocations: dict[str, str]                 # virtual texId -> phys_N
    textures: dict[str, TextureSpec]            # texId -> spec
    programs: dict[str, Any]                    # program id -> {uniformLayout, defines}

    @staticmethod
    def from_dict(d: dict) -> "RenderGraph":
        return RenderGraph(
            id=d.get("id", ""),
            source=d.get("source", ""),
            render_surface=d.get("renderSurface"),
            passes=[Pass.from_dict(p) for p in d.get("passes", [])],
            allocations=dict(d.get("allocations", {})),
            textures={k: TextureSpec.from_dict(v) for k, v in d.get("textures", {}).items()},
            programs=dict(d.get("programs", {})),
        )

    def spec_for(self, tex_id: str) -> Optional[TextureSpec]:
        """Resolve a texId to its TextureSpec, following the allocations indirection."""
        if tex_id in self.textures:
            return self.textures[tex_id]
        phys = self.allocations.get(tex_id)
        if phys and phys in self.textures:
            return self.textures[phys]
        return None
