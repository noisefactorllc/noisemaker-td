// NM_INPUTS: inputTex=0 overlayTex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define overlayTex sTD2DInputs[1]
uniform float alpha;

uniform ivec2 tileOffset;
uniform ivec2 fullResolution;
uniform float renderScale;

out vec4 fragColor;

void nm_main() {
    ivec2 coord = ivec2(gl_FragCoord.xy);
    ivec2 baseSize = textureSize(inputTex, 0);
    ivec2 overlaySize = textureSize(overlayTex, 0);
    
    vec4 base = texelFetch(inputTex, clamp(coord, ivec2(0), baseSize - 1), 0);
    vec4 overlay = texelFetch(overlayTex, clamp(coord, ivec2(0), overlaySize - 1), 0);

    float a = overlay.a * alpha;
    vec3 result = base.rgb * (1.0 - a) + overlay.rgb * a;
    fragColor = vec4(result, base.a);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
