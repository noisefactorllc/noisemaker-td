// NM_INPUTS: inputTex=0
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
/*
 * Smooth - Edge Detection Pass
 * SMAA/Blur modes: compute luma edge map (horizontal/vertical edges)
 * MSAA mode: pass through input unchanged
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;

uniform int smoothType;
uniform float threshold;

out vec4 fragColor;

const vec3 LUMA_WEIGHTS = vec3(0.299, 0.587, 0.114);

float luminance(vec3 rgb) {
    return dot(rgb, LUMA_WEIGHTS);
}

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    ivec2 coord = ivec2(gl_FragCoord.xy);

    // MSAA mode: pass through input (blend pass does its own edge detection)
    if (smoothType == 0) {
        fragColor = texelFetch(inputTex, coord, 0);
        return;
    }

    // SMAA and Blur modes: luma-based edge detection
    ivec2 maxCoord = texSize - 1;
    float L  = luminance(texelFetch(inputTex, coord, 0).rgb);
    float Ln = luminance(texelFetch(inputTex, clamp(coord + ivec2(0, -1), ivec2(0), maxCoord), 0).rgb);
    float Ls = luminance(texelFetch(inputTex, clamp(coord + ivec2(0,  1), ivec2(0), maxCoord), 0).rgb);
    float Lw = luminance(texelFetch(inputTex, clamp(coord + ivec2(-1, 0), ivec2(0), maxCoord), 0).rgb);
    float Le = luminance(texelFetch(inputTex, clamp(coord + ivec2( 1, 0), ivec2(0), maxCoord), 0).rgb);

    float edgeH = step(threshold, max(abs(L - Ln), abs(L - Ls)));
    float edgeV = step(threshold, max(abs(L - Lw), abs(L - Le)));

    fragColor = vec4(edgeH, edgeV, 0.0, 1.0);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
