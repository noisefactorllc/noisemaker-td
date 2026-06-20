// NM_INPUTS: inputTex=0 colorTex=1 edgeTex=2
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define colorTex sTD2DInputs[1]
#define edgeTex sTD2DInputs[2]
/*
 * Cel Shading - Blend Pass
 * Combines cel-shaded color with edge outlines
 */


uniform vec2 tileOffset;
uniform vec2 fullResolution;



uniform vec3 edgeColor;
uniform float mixAmount;

out vec4 fragColor;

void nm_main() {
    vec2 globalCoord = gl_FragCoord.xy + tileOffset;
    ivec2 texSize = textureSize(inputTex, 0);
    vec2 uv = gl_FragCoord.xy / vec2(texSize);

    vec4 origColor = texture(inputTex, uv);
    vec4 celColor = texture(colorTex, uv);
    float edgeStrength = texture(edgeTex, uv).r;

    // Apply edge color where edges are detected
    vec3 finalColor = mix(celColor.rgb, edgeColor, edgeStrength);

    // Mix with original based on mix amount
    finalColor = mix(origColor.rgb, finalColor, mixAmount);

    fragColor = vec4(finalColor, origColor.a);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
