"""Polymorphic DSL compiler — Python port of the noisemaker-hlsl C# `Compiler/`.

Mirrors `noisemaker-hlsl/unity/com.noisemaker.hlsl/Compiler/` file-for-file, which is
itself a 1:1 port of the upstream JS engine (its `shaders/src`). The pipeline
(reference specs 01-03) is:

    compile(src) -> tokens (lexer) -> ast (parser) -> validate -> expand -> render graph JSON

The emitted graph JSON is byte-identical (modulo `id`) to `tools/export-graph.mjs` — the
same parity contract hlsl's C# compiler is held to. Full DSL spec: the upstream engine's docs/shaders.
"""
from .dsl_compiler import compile_graph, compile_dsl, CompileError  # noqa: F401
from .lang.effect_registry import EffectRegistry  # noqa: F401

__all__ = ['compile_graph', 'compile_dsl', 'CompileError', 'EffectRegistry']
