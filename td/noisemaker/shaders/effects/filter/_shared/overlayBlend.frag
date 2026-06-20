// NM_INPUTS: inputTex=0 overlayTex=1
// NM_OUTPUT: fragColor
#define inputTex sTD2DInputs[0]
#define overlayTex sTD2DInputs[1]
uniform float alpha;

out vec4 fragColor;

void nm_main() {
    ivec2 coord = ivec2(gl_FragCoord.xy);
    vec4 base = texelFetch(inputTex, coord, 0);
    vec4 overlay = texelFetch(overlayTex, coord, 0);

    // Standard alpha blending: overlay.a carries worm trail opacity
    float a = overlay.a * alpha;
    vec3 result = base.rgb * (1.0 - a) + overlay.rgb * a;
    fragColor = vec4(result, base.a);
}
void main() {
    nm_main();
    fragColor = TDOutputSwizzle(fragColor);
}
