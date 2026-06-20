// NM_INPUTS: xyzTex=0 velTex=1
// NM_OUTPUT: fragColor
#define xyzTex sTD2DInputs[0]
#define velTex sTD2DInputs[1]
// Standard uniforms
uniform float time;
uniform vec2 resolution;

// Input textures (post-agent state)



out vec4 fragColor;

void nm_main() {
    ivec2 coord = ivec2(gl_FragCoord.xy);
    vec4 pos = texelFetch(xyzTex, coord, 0);
    vec4 vel = texelFetch(velTex, coord, 0);

    // Dead agent — zero z
    if (pos.w < 0.5) {
        fragColor = vec4(0.0);
        return;
    }

    float cRe = vel.x;
    float cIm = vel.y;
    int stepI = int(vel.z);

    // Recompute z from scratch to current step
    vec2 z = vec2(0.0);
    for (int i = 0; i < 2048; i++) {
        if (i >= stepI) break;
        float zr = z.x * z.x - z.y * z.y + cRe;
        float zi = 2.0 * z.x * z.y + cIm;
        z = vec2(zr, zi);
    }

    fragColor = vec4(z.x, z.y, 0.0, 0.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
