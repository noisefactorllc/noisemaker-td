"""deposit_shaders.py — TouchDesigner GLSL MAT shaders for the agent-scatter (deposit) passes.

The reference `render/pointsRender/glsl/deposit.{vert,frag}` and `pointsBillboardRender/...` run as
a bufferless `gl.drawArrays(POINTS|TRIANGLES, 0, N|N*6)` keyed off `gl_VertexID`. TD has no
bufferless draw: the deposit is a Geo COMP (Grid SOP -> Convert SOP "point sprites") + this GLSL MAT
+ a Render TOP (see docs/TD-PLATFORM-NOTES.md "GPU point scatter"). So the ONLY change vs the
reference is the agent-index source: `gl_VertexID` -> recovered from the Grid point POSITION
(`TDPos()`), validated bit-for-placement by td/points_probe.py. Math, culling, view transform, and
the fragment shading are mirrored verbatim from the reference.

These are MAT vertex/pixel shaders (TDPos / TDPointCoord / TDOutputSwizzle, direct gl_Position NDC),
not fullscreen TOP frags, so they live here rather than in the auto-transpiled shaders/ tree.
"""

# Shared vertex prologue: recover the agent texel (x,y) and linear id from the grid point position,
# fetch state, density-cull, and compute the 2D/3D clip position EXACTLY as the reference does.
# (`stateSize` is the agent grid width; `vid = y*ss + x` reproduces the reference gl_VertexID so the
# golden-ratio density random matches.)
_VERT_PROLOGUE = """uniform sampler2D xyzTex;
uniform sampler2D rgbaTex;
uniform float density;
uniform float viewMode;     // float (TD Vectors page is float-only); cast to int in-shader
uniform float rotateX;
uniform float rotateY;
uniform float rotateZ;
uniform float viewScale;
uniform float posX;
uniform float posY;

int nm_agentTexel(out int vid, out vec4 pos, out vec4 col) {
    int ss = textureSize(xyzTex, 0).x;   // agent grid width (reference reads stateSize from the tex)
    vec3 gp = TDPos();
    int x = int(floor((gp.x * 0.5 + 0.5) * float(ss - 1) + 0.5));
    int y = int(floor((gp.y * 0.5 + 0.5) * float(ss - 1) + 0.5));
    x = clamp(x, 0, ss - 1);
    y = clamp(y, 0, ss - 1);
    vid = y * ss + x;
    pos = texelFetch(xyzTex, ivec2(x, y), 0);
    col = texelFetch(rgbaTex, ivec2(x, y), 0);
    return ss;
}

// PARITY (large-stateSize precision): the reference density cull is fract(particleID*GR), GR=the
// golden ratio. At ~1M agents (stateSize 1024) the raw product particleID*GR reaches ~6.5e5, where
// float32's representable step (~0.06) is coarse enough that fract() quantizes into ~16 buckets ->
// Metal over-deposits ~8x vs the reference's ANGLE -> an HDR over-bright trail that blows out the
// downstream navierStokes (white-out / -- on TD's Metal float16 -- Inf->NaN). A hi/lo split keeps
// the products small so fract is exact for IDs into the millions. (noisemaker-hlsl abb9578,
// noisemaker-godot 58a1b88.)
float nm_particleRandom(int vid) {
    float pidf = float(vid);
    float pidHi = floor(pidf / 4096.0);
    float pidLo = pidf - pidHi * 4096.0;
    return fract(pidHi * fract(4096.0 * 0.618033988749895) + pidLo * 0.618033988749895);
}

vec2 nm_clipPos(vec4 pos) {
    if (int(viewMode + 0.5) == 0) {
        return pos.xy * 2.0 - 1.0;
    }
    vec3 p = pos.xyz;
    bool is2D = abs(p.z) < 1.0 && p.x >= 0.0 && p.x <= 1.0 && p.y >= 0.0 && p.y <= 1.0;
    if (is2D) { p.xy = p.xy - 0.5; p.z = 0.0; }
    float cx = cos(rotateX), sx = sin(rotateX);
    p = vec3(p.x, p.y * cx - p.z * sx, p.y * sx + p.z * cx);
    float cy = cos(rotateY), sy = sin(rotateY);
    p = vec3(p.x * cy + p.z * sy, p.y, -p.x * sy + p.z * cy);
    float cz = cos(rotateZ), sz = sin(rotateZ);
    p = vec3(p.x * cz - p.y * sz, p.x * sz + p.y * cz, p.z);
    p.x += posX; p.y += posY;
    return is2D ? p.xy * 3.5 * viewScale : p.xy / 40.0 * viewScale;
}
"""

# ---- pointsRender: one GL_POINT (gl_PointSize 1) per live, un-culled agent ----------------------
POINTS_VERT = _VERT_PROLOGUE + """
out vec4 vColor;
void main() {
    int vid; vec4 pos; vec4 col;
    nm_agentTexel(vid, pos, col);
    float cullThreshold = density / 100.0;
    float particleRandom = nm_particleRandom(vid);
    if (particleRandom > cullThreshold || pos.w < 0.5) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0); gl_PointSize = 0.0; vColor = vec4(0.0); return;
    }
    gl_PointSize = 1.0;
    gl_Position = vec4(nm_clipPos(pos), 0.0, 1.0);
    vColor = vec4(col.rgb, col.a);
}
"""

POINTS_FRAG = """in vec4 vColor;
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = TDOutputSwizzle(vColor);
}
"""

# ---- pointsBillboardRender: a sized point sprite per agent; SDF/sprite shading in the frag --------
# Per-particle SIZE variation mirrors the reference (seeded hash). Per-particle ROTATION is dropped:
# point sprites are screen-aligned (TD can't rotate them per-vertex). The flagship uses rotationVar:0
# so this is exact for it; rotationVar>0 would need real quad geometry (documented limitation).
BILLBOARD_VERT = _VERT_PROLOGUE + """
uniform float pointSize;
uniform float sizeVariation;
uniform float seed;
out vec4 vColor;
uint nm_hash_uint(uint s) {
    uint state = s * 747796405u + 2891336453u;
    uint word = ((state >> ((state >> 28u) + 4u)) ^ state) * 277803737u;
    return (word >> 22u) ^ word;
}
float nm_hash(float n) { return float(nm_hash_uint(floatBitsToUint(n + seed))) / 4294967295.0; }
void main() {
    int vid; vec4 pos; vec4 col;
    nm_agentTexel(vid, pos, col);
    float cullThreshold = density / 100.0;
    float particleRandom = nm_particleRandom(vid);
    if (particleRandom > cullThreshold || pos.w < 0.5) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0); gl_PointSize = 0.0; vColor = vec4(0.0); return;
    }
    float sizeNoise = nm_hash(float(vid));
    float sizeMultiplier = 1.0 - (sizeVariation / 100.0) * (sizeNoise - 0.5);
    float finalSize = pointSize * sizeMultiplier;
    gl_PointSize = max(finalSize, 0.0);
    gl_Position = vec4(nm_clipPos(pos), 0.0, 1.0);
    vColor = vec4(col.rgb, col.a);
}
"""

# Fragment mirrors render/pointsBillboardRender/glsl/deposit.frag, with vSpriteUV -> TDPointCoord()
# (point-sprite auto texcoord, (0,0) bottom-left .. (1,1) top-right — same convention as the
# reference quad's offset*0.5+0.5).
BILLBOARD_FRAG = """uniform sampler2D spriteTex;
uniform float shapeMode;     // float (TD Vectors page is float-only); cast to int in-shader
uniform float depositOpacity;
in vec4 vColor;
layout(location = 0) out vec4 fragColor;
void main() {
    int sm = int(shapeMode + 0.5);
    float opacity = depositOpacity / 100.0;
    vec2 uv = TDPointCoord();
    if (sm == 0) {
        vec4 spriteColor = texture(spriteTex, uv);
        fragColor = TDOutputSwizzle(vec4(spriteColor.rgb * vColor.rgb, spriteColor.a * vColor.a) * opacity);
        return;
    }
    vec2 p = uv - 0.5;
    float sdf;
    float alpha;
    if (sm == 1) {
        sdf = length(p) - 0.45;
    } else if (sm == 2) {
        sdf = abs(length(p) - 0.35) - 0.08;
    } else if (sm == 3) {
        sdf = max(abs(p.x), abs(p.y)) - 0.4;
    } else if (sm == 4) {
        sdf = abs(p.x) + abs(p.y) - 0.45;
    } else if (sm == 5) {
        float r = 0.25;
        float k = 1.732050808;
        vec2 t = vec2(abs(p.x) - r, p.y - 0.04 + r / k);
        if (t.x + k * t.y > 0.0) t = vec2(t.x - k * t.y, -k * t.x - t.y) / 2.0;
        t.x -= clamp(t.x, -2.0 * r, 0.0);
        sdf = -length(t) * sign(t.y);
    } else if (sm == 6) {
        float r = 0.35;
        float rf = 0.4;
        vec2 k1 = vec2(0.809016994375, -0.587785252292);
        vec2 k2 = vec2(-k1.x, k1.y);
        vec2 s = vec2(abs(p.x), p.y);
        s -= 2.0 * max(dot(k1, s), 0.0) * k1;
        s -= 2.0 * max(dot(k2, s), 0.0) * k2;
        s.x = abs(s.x);
        s.y -= r;
        vec2 ba = rf * vec2(-k1.y, k1.x) - vec2(0.0, 1.0);
        float h = clamp(dot(s, ba) / dot(ba, ba), 0.0, r);
        sdf = length(s - ba * h) * sign(s.y * ba.x - s.x * ba.y);
    } else {
        alpha = exp(-dot(p, p) * 8.0);
        fragColor = TDOutputSwizzle(vec4(vColor.rgb * alpha, alpha * vColor.a) * opacity);
        return;
    }
    alpha = 1.0 - smoothstep(-0.02, 0.02, sdf);
    fragColor = TDOutputSwizzle(vec4(vColor.rgb * alpha, alpha * vColor.a) * opacity);
}
"""


# ---- filter3d/flow3d deposit: agents carry a 3D VOLUME position, scattered into a 2D atlas --------
# Distinct from the 2D pointsRender deposit above: flow3d's agents live in a volume (state1.xyz in
# VOXEL units [0,volSize)), and the trail is that volume stored as a 2D atlas (width=volSize,
# height=volSize², row = y + floor(z)·volSize). The cull is the reference's index threshold
# (maxAgents = maxDim·density·0.2), not the golden-ratio fract used by pointsRender. Mirrors
# filter3d/flow3d/glsl/deposit.vert verbatim; only gl_VertexID -> the grid texel (xyzTex == stateTex1,
# rgbaTex == stateTex2) changes, exactly as for the 2D deposit. See docs/TD-PLATFORM-NOTES.md.
FLOW3D_VERT = """uniform sampler2D xyzTex;   // stateTex1: xyz = 3D voxel position
uniform sampler2D rgbaTex;  // stateTex2: rgb = agent color
uniform float density;
uniform int volumeSize;
out vec4 vColor;
void main() {
    ivec2 sz = textureSize(xyzTex, 0);
    int texW = sz.x, texH = sz.y;
    vec3 gp = TDPos();
    int x = clamp(int(floor((gp.x * 0.5 + 0.5) * float(texW - 1) + 0.5)), 0, texW - 1);
    int y = clamp(int(floor((gp.y * 0.5 + 0.5) * float(texH - 1) + 0.5)), 0, texH - 1);
    int agentIndex = y * texW + x;                 // == reference gl_VertexID (texW == stateSize)
    int maxAgents = int(float(max(texW, texH)) * density * 0.2);
    if (agentIndex >= maxAgents) {
        gl_Position = vec4(2.0, 2.0, 0.0, 1.0); gl_PointSize = 0.0; vColor = vec4(0.0); return;
    }
    vec4 state1 = texelFetch(xyzTex, ivec2(x, y), 0);
    vec4 state2 = texelFetch(rgbaTex, ivec2(x, y), 0);
    float volF = float(volumeSize);
    float atlasX = state1.x;                        // voxel x in [0,volSize)
    float atlasY = state1.y + floor(state1.z) * volF;   // atlas row = y + z·volSize
    vec2 ndc = vec2((atlasX / volF) * 2.0 - 1.0, (atlasY / (volF * volF)) * 2.0 - 1.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    gl_PointSize = 1.0;
    vColor = vec4(state2.rgb, 1.0);
}
"""


def shaders_for(draw_mode):
    """(vertex_src, fragment_src) for a deposit pass: 'points', 'billboards', or 'points3d'.

    'points3d' is the filter3d/flow3d volume-atlas deposit (agents in voxel space); it reuses the
    plain points fragment (straight additive color write)."""
    if draw_mode == 'billboards':
        return BILLBOARD_VERT, BILLBOARD_FRAG
    if draw_mode == 'points3d':
        return FLOW3D_VERT, POINTS_FRAG
    return POINTS_VERT, POINTS_FRAG
