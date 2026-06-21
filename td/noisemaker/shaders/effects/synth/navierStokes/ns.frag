// NM_INPUTS: fbTex=0 inputTex=1
// NM_OUTPUT: fragColor
#define fbTex sTD2DInputs[0]
#define inputTex sTD2DInputs[1]
/*
 * Navier-Stokes display pass.
 * Plain bilinear blit of the intermediate smoothed canvas into the output. The smoothing kernel
 * lives in nsSmooth (between sim and display), not here — so this pass does no kernel work and
 * never operates at the compute canvas's native resolution.
 */


uniform vec2 resolution;
uniform vec2 tileOffset;
uniform vec2 fullResolution;
uniform float inputIntensity;




out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(fbTex, 0);
    ivec2 minIdx = ivec2(0);
    ivec2 maxIdx = texSize - ivec2(1);

    vec2 texelPos = (globalCoord * vec2(texSize) / fullResolution) - vec2(0.5);
    ivec2 baseI = ivec2(floor(texelPos));
    vec2 f = fract(texelPos);

    float v00 = texelFetch(fbTex, clamp(baseI,                       minIdx, maxIdx), 0).b;
    float v10 = texelFetch(fbTex, clamp(baseI + ivec2(1, 0),         minIdx, maxIdx), 0).b;
    float v01 = texelFetch(fbTex, clamp(baseI + ivec2(0, 1),         minIdx, maxIdx), 0).b;
    float v11 = texelFetch(fbTex, clamp(baseI + ivec2(1, 1),         minIdx, maxIdx), 0).b;

    float v0 = mix(v00, v10, f.x);
    float v1 = mix(v01, v11, f.x);
    float state = mix(v0, v1, f.y);

    float intensity = clamp(state, 0.0, 1.0);
    vec3 outCol = vec3(intensity);

    float blend = clamp(inputIntensity, 0.0, 100.0) * 0.01;
    if (blend > 0.0) {
        vec2 inputUv = globalCoord / fullResolution;
        // PARITY/RANGE (port guard, not in the reference GLSL): clamp to [0,1] — a no-op for the
        // reference's [0,1] o0; bounds an HDR particle-field input so the display blend can't leak
        // HDR into the output. (noisemaker-hlsl abb9578 / godot 58a1b88.)
        vec3 inputColor = clamp(texture(inputTex, inputUv).rgb, 0.0, 1.0);
        outCol = mix(outCol, inputColor, blend);
    }

    fragColor = vec4(outCol, 1.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
